from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


_GIT_ENV_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_worktree_available(root: Path) -> bool:
    git_env = os.environ.copy()
    for variable in _GIT_ENV_OVERRIDES:
        git_env.pop(variable, None)
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
        env=git_env,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def require_git_worktree(root: Path) -> None:
    if not git_worktree_available(root):
        pytest.skip("Git metadata unavailable in immutable release validation")
