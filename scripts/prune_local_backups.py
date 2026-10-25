"""Keep the latest verified backup and the latest from the preceding backup day.

Only superseded local encrypted copies are removed. The remote history is never
rewritten. A filtered mirror rebuild bounds local Git history storage as well.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime

from verify_backup import verify_archive

NAME = re.compile(r"state-(\d{8})-(\d{4})\.tar\.gz\.enc\Z")
RESERVE = 2_684_354_560


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, input=input_text
    ).strip()


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Not a regular backup file: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def retained_names(names: list[str]) -> set[str]:
    regular = sorted(name for name in names if NAME.fullmatch(name))
    days = sorted({name[6:14] for name in regular}, reverse=True)
    if len(days) < 2:
        raise ValueError("Two distinct backup days are required before pruning")
    return {max(name for name in regular if name[6:14] == day) for day in days[:2]}


def verified_record(archive: Path) -> bool:
    record = Path(str(archive) + ".verification.json")
    if not record.exists():
        return False
    if record.is_symlink():
        raise ValueError("Symlink verification record")
    data = json.loads(record.read_text())
    if not (
        data.get("status") == "PASS"
        and data.get("archive_name") == archive.name
        and data.get("archive_sha256") == digest(archive)
        and data.get("archive_size_bytes") == archive.stat().st_size
        and all(
            data.get("verification_checks", {}).get(key) == "PASS"
            for key in ("encrypted_round_trip", "archive_read", "sqlite_snapshots")
        )
    ):
        raise ValueError(f"Invalid verification record: {archive.name}")
    return True


def remote_head(repo: Path, remote: str, branch: str) -> str:
    rows = git(repo, "ls-remote", remote, f"refs/heads/{branch}").splitlines()
    if len(rows) != 1:
        raise ValueError("Remote backup branch is unavailable")
    head = rows[0].split()[0]
    if git(repo, "rev-parse", "HEAD") != head:
        raise ValueError("Local mirror does not match the remote branch")
    return head


def remote_digest(repo: Path, head: str, name: str) -> str:
    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "blob", f"{head}:{name}"],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    checksum = hashlib.file_digest(process.stdout, "sha256").hexdigest()
    process.stdout.close()
    if process.wait() != 0:
        raise ValueError(f"Backup is not stored in remote commit: {name}")
    return checksum


def compact_mirror(repo: Path, backups: Path, keep: set[str], remote: str, branch: str) -> None:
    """Replace only the fully pushed local Git object store with a sparse clone."""
    head = remote_head(repo, remote, branch)
    if git(repo, "status", "--porcelain"):
        raise ValueError("Refusing to compact a dirty backup mirror")
    if git(repo, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines() != [
        f"refs/heads/{branch}"
    ]:
        raise ValueError("Mirror contains additional local branches")
    url = git(repo, "remote", "get-url", remote)
    selected = sorted(keep | {name + ".verification.json" for name in keep})
    evidence_rows = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "ls-tree",
            "-r",
            "-l",
            "-z",
            head,
            "--",
            "carry-dated-evidence",
        ]
    ).split(b"\0")
    evidence_blobs: dict[str, tuple[str, int]] = {}
    for row in evidence_rows:
        if not row:
            continue
        metadata, raw_name = row.split(b"\t", 1)
        _, kind, blob, size = metadata.decode("ascii").split(" ", 3)
        name = raw_name.decode("utf-8")
        if kind == "blob" and name.endswith(".enc"):
            evidence_blobs[name] = (blob, int(size))
    selected_sizes = {name: int(git(repo, "cat-file", "-s", f"{head}:{name}")) for name in selected}
    byte_budget = sum(selected_sizes.values()) + sum(size for _, size in evidence_blobs.values())
    checked_names = sorted(set(selected) | set(evidence_blobs))
    # A filtered clone must stay small; retain both mirrors until validated.
    if shutil.disk_usage(repo).free < RESERVE + 3 * byte_budget + 268_435_456:
        raise ValueError("Insufficient disk reserve for mirror compaction")
    # systemd bind-mounts each writable path separately. Keep atomic renames
    # within the mirror mount, even when both paths share the physical device.
    stage = Path(tempfile.mkdtemp(prefix=".mirror-stage-", dir=repo))
    clone = stage / "repo"
    retired = stage / "retired.git"
    committed = False
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth=1",
                "--single-branch",
                "--branch",
                branch,
                "--no-checkout",
                "--origin",
                remote,
                url,
                str(clone),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        git(
            clone,
            "sparse-checkout",
            "set",
            "--no-cone",
            "--stdin",
            input_text="\n".join("/" + name for name in selected) + "\n/carry-dated-evidence/\n",
        )
        git(clone, "read-tree", "-mu", "HEAD")
        if git(clone, "rev-parse", "HEAD") != head or remote_head(repo, remote, branch) != head:
            raise ValueError("Remote changed during mirror compaction")
        expected_blobs = {name: git(repo, "rev-parse", f"{head}:{name}") for name in selected}
        expected_blobs.update({name: blob for name, (blob, _) in evidence_blobs.items()})
        for name in checked_names:
            if git(clone, "hash-object", "--", str(clone / name)) != expected_blobs[name]:
                raise ValueError(f"Compacted mirror differs: {name}")
        # Keep sparse-checkout paths materialized in this mirror before swapping
        # indexes; the clone and mirror share this bind mount, so hard links are
        # safe here and avoid a second copy of the retained ciphertext.
        for name in checked_names:
            target = repo / name
            source_path = clone / name
            if target.is_symlink():
                raise ValueError(f"Refusing symlink in backup mirror: {name}")
            if target.exists():
                if git(repo, "hash-object", "--", str(target)) != expected_blobs[name]:
                    raise ValueError(f"Local mirror differs: {name}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_path, target)
            except OSError:
                staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                try:
                    shutil.copyfile(source_path, staged)
                    os.replace(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
        object_bytes = sum(p.stat().st_size for p in (clone / ".git").rglob("*") if p.is_file())
        if object_bytes > 2 * byte_budget + 268_435_456:
            raise ValueError("Server did not provide a bounded filtered mirror")
        # Preserve skip-worktree entries while allowing older backup writers to add new names.
        git(clone, "config", "--worktree", "core.sparseCheckout", "false")
        if git(clone, "status", "--porcelain"):
            raise ValueError("New mirror is dirty")
        for key in ("user.name", "user.email", "core.sshCommand"):
            result = subprocess.run(
                ["git", "-C", str(repo), "config", "--get", key], text=True, capture_output=True
            )
            if result.returncode == 0:
                git(clone, "config", key, result.stdout.strip())
        # The service can rename within its writable mirror, but not its parent.
        os.rename(repo / ".git", retired)
        try:
            os.rename(clone / ".git", repo / ".git")
            if git(repo, "rev-parse", "HEAD") != head or git(
                repo, "status", "--porcelain", "--", ".", f":(exclude){stage.name}"
            ):
                raise ValueError("Replacement mirror failed validation")
        except Exception:
            if (repo / ".git").exists():
                os.rename(repo / ".git", clone / ".git")
            os.rename(retired, repo / ".git")
            raise
        committed = True
        print(json.dumps({"mirror_compacted": head}), flush=True)
    finally:
        # A retired object store is removed only after the replacement passed.
        if retired.exists() and not committed:
            raise RuntimeError(f"Preserving recovery directory: {stage}")
        shutil.rmtree(stage)


def prune(root: Path, *, apply: bool, compact: bool, lock_fd: int | None = None) -> dict:
    backups, repo = root / "backups", root / "backups-repo"
    if backups.is_symlink() or repo.is_symlink():
        raise ValueError("Symlink backup roots are refused")
    if lock_fd is None:
        lock = (backups / ".backup.lock").open("a")
        lock_fd = lock.fileno()
    else:
        if (os.fstat(lock_fd).st_dev, os.fstat(lock_fd).st_ino) != (
            (backups / ".backup.lock").stat().st_dev,
            (backups / ".backup.lock").stat().st_ino,
        ):
            raise ValueError("Inherited lock does not identify the backup lock")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    remote = os.environ.get("BACKUP_OFFHOST_REMOTE", "origin")
    branch = os.environ.get("BACKUP_OFFHOST_BRANCH", "backups")
    head = remote_head(repo, remote, branch)
    if git(repo, "status", "--porcelain"):
        raise ValueError("Backup mirror must be clean before retention")
    archives = {p.name: p for p in backups.iterdir() if NAME.fullmatch(p.name)}
    keep = retained_names(list(archives))
    for name in sorted(keep):
        if not verified_record(archives[name]):
            raise ValueError(f"Retained backup lacks verification: {name}")
        if digest(archives[name]) != remote_digest(repo, head, name):
            raise ValueError(f"Retained backup not verified off-host: {name}")
    candidates = sorted(set(archives) - keep)
    result = {
        "retained": sorted(keep),
        "candidates": candidates,
        "removed": [],
        "remote_commit": head,
    }
    if not apply:
        return result
    receipts = backups / ".retention"
    receipts.mkdir(exist_ok=True)
    for name in candidates:
        archive = archives[name]
        checksum = digest(archive)
        if remote_digest(repo, head, name) != checksum:
            raise ValueError(f"Remote archive differs: {name}")
        if not verified_record(archive):
            proof = verify_archive(archive)
            if proof["integrity"] != "ok":
                raise ValueError(f"Unverified archive: {name}")
        mirror = repo / name
        if mirror.exists() and digest(mirror) != checksum:
            raise ValueError(f"Local mirror differs: {name}")
        sidecar = Path(str(archive) + ".verification.json")
        paths = [name] + ([sidecar.name] if sidecar.exists() else [])
        for item in paths:
            source = backups / item
            if remote_digest(repo, head, item) != digest(source):
                raise ValueError(f"Off-host file differs: {item}")
            target = repo / item
            if target.exists() and digest(target) != digest(source):
                raise ValueError(f"Mirror file differs: {item}")
        if remote_head(repo, remote, branch) != head:
            raise ValueError("Remote changed before deletion")
        receipt = {
            "archive": name,
            "sha256": checksum,
            "remote_commit": head,
            "verified_at": datetime.now(UTC).isoformat(),
            "action": "remove_local_only",
        }
        receipt_path = receipts / (name + ".json")
        with receipt_path.open("w") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        for item in paths:
            git(repo, "update-index", "--skip-worktree", "--", item)
            (repo / item).unlink(missing_ok=True)
            (backups / item).unlink()
        result["removed"].append(name)
        print(json.dumps({"removed_local": name, "retained": sorted(keep)}), flush=True)
    if compact:
        compact_mirror(repo, backups, keep, remote, branch)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--compact-mirror", action="store_true")
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            prune(args.root, apply=args.apply, compact=args.compact_mirror, lock_fd=args.lock_fd),
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
