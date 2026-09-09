# BTCQuant Dashboard V4.1 — Visual QA

Date: 2026-09-09 (UTC)  
Scope: dashboard/reporting read-only only.

## Baseline

- `origin/main`: `540e3d2c0d42f103d11c1e462b14f6d3e90da5d9`
- tree: `c7176ee0427328e4d1b0e7c069102d1a13d80672`
- branch: `fix/dashboard-v4-visual-quality`
- production, database, schema and trading services: untouched.

## Defects reproduced and corrected

| ID | Severity | Finding | Correction |
| --- | --- | --- | --- |
| VIS-001 | P1 | Risk Radar reserved 552 px despite content at top and gauges at bottom. | Content-led five-measure instrument; static tracks and natural height. |
| VIS-002 | P1 | A stale Trend could display a last observed `LONG` as current decision. | Decision hero is now fail-closed `UNKNOWN` unless Trend is alive and FRESH. |
| VIS-003 | P2 | Rail trust grid was unreadable in its fixed desktop width. | Compact single-column truth nodes desktop; two columns only on mobile. |
| VIS-004 | P2 | Pulse used an oversized panel for four operational values. | Two-column health matrix desktop, compact stack mobile. |
| VIS-005 | P2 | Carry/Exposure consumed excessive height for sparse data. | Compact, content-led metrics and exposure strip. |
| VIS-006 | P2 | Event stream was visually raw and lost its column rhythm. | Stable time/engine/event grid desktop; two-line mobile composition. |
| VIS-007 | P2 | Flat positions repeated `N/A` without useful meaning. | Quiet explicit FLAT copy; UNKNOWN remains visible and fail-closed. |
| VIS-008 | P2 | Trend grid/readouts had a table-like density. | Unified 1 px surface grouping, normalized padding and numeric typography. |
| VIS-009 | P2 | Performance secondary metrics were visually nested and uneven. | Compact metric strip and content-driven breakdown/yearly modules. |
| VIS-010 | P2 | Market chart and labels had disproportionate vertical rhythm. | Chart uses bounded responsive height and compact readouts. |
| VIS-011 | P2 | Long event content and narrow rail markers needed containment proof. | Browser stress checks confirm both remain inside their containers. |

## Browser evidence

- Baseline: `/home/ubuntu/btcquant-dashboard-v4-evidence-20260909/V4_1_BASELINE/`
- Final: `/home/ubuntu/btcquant-dashboard-v4-evidence-20260909/V4_1_FINAL/`
- Matrix: 42 captures — Monitor, Performance, Risk; 1920, 1440, 1280, 1024, 768, 430, 390 px; dark and light.
- Before/after: `MONITOR_BEFORE_AFTER.png`, `PERFORMANCE_BEFORE_AFTER.png`, `RISK_BEFORE_AFTER.png`, `MONITOR_MOBILE_BEFORE_AFTER.png`.
- Layout matrix: 42/42 cases had no document or card horizontal overflow.
- State stress: FLAT, UNKNOWN/STALE, long incident, large unbroken event text and position-rail marker bounds passed.
- Zoom: 90 %, 100 %, 110 %, 125 % and 150 % at a 1440 px physical viewport passed for all views.

## Accessibility and validation

- axe-core WCAG 2A/2AA: 0 violations across 3 views × dark/light.
- Keyboard: ArrowRight moved the focused view tab from Monitor to Performance and preserved focus.
- Font stack verified in Chromium: `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`; numeric values use tabular figures where relevant.
- Dashboard asset tests: 9 passed under Python 3.11 and 3.12.
- Full Python 3.12 suite completed with an empty `lastfailed` cache.
- Ruff check: pass; Ruff format check: pass; JS syntax: pass; `git diff --check`: pass.
- mypy `src`: pass (109 files); pip-audit: no known vulnerabilities.

## Limits

This campaign does not deploy. Screenshots use a deterministic loopback fixture and no exchange, credential, wallet, financial database write, strategy, runtime or service was changed.

## Final assessment

The dominant visual defects are resolved: Risk is no longer oversized, secondary panels follow their content, FLAT/UNKNOWN semantics are clear, typography is more deliberate, and desktop/mobile containment is evidenced. No P1 or P2 visual defect remains reproducible in the tested matrix.

