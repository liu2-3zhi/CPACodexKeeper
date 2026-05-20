# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

- Install dependencies: `python -m pip install -r requirements.txt`
- Run all tests: `python -m unittest discover -s tests`
- Run a targeted pytest file: `pytest tests/test_maintainer.py -q`
- Run a single pytest test: `pytest tests/test_maintainer.py::MaintainerTests::test_name -q`
- Run once: `python main.py --once`
- Run once in dry-run mode: `python main.py --once --dry-run`
- Run daemon mode: `python main.py`
- Build Docker image: `docker build -t cpacodexkeeper .`
- Start with Compose: `docker compose up -d --build`
- Stop Compose: `docker compose down`
- If `just` is installed, the same common flows are available via `just install`, `just test`, `just run-once`, `just dry-run`, `just daemon`, `just docker-build`, `just docker-up`, `just docker-down`

## High-level architecture

- `src/cli.py` is the entrypoint. It parses `--once`, default daemon mode, `-monitor`, `--dry-run`, and `--force-refresh` (only valid with `--once`).
- `src/settings.py` is the `.env`-backed configuration layer. It validates required CPA settings plus runtime tuning such as quota thresholds, scan pacing, retries, archive limits, and recheck timing.
- `src/maintainer.py` is the orchestration core. It owns the main full scan, quota decisions, enable/disable/delete/refresh behavior, state persistence, stats, and logging.
- `src/cpa_client.py` wraps CPA management API operations such as listing auth files, downloading details, toggling disabled state, querying usage logs, deleting auth files, and uploading refreshed token payloads.
- `src/openai_client.py` wraps usage checks and refresh requests, and normalizes usage payloads into the quota structures used by the maintainer.

## Runtime model and behavior

There are three distinct operational paths that share state but have different roles:

1. Full scan: scans all `type=codex` accounts and applies the main maintenance rules.
2. Log-driven inspection: when `CPA_USAGE_QUERY_INTERVAL > 0`, inspects only recently-used accounts from CPA usage logs and only performs quota-based disable decisions.
3. Tracked timer-based recheck: restores and executes follow-up rechecks from `disabled_accounts.json`.

Important current behavior:

- Main full scan is sequential per account, not thread-pooled.
- The pacing for the main full scan is controlled by `CPA_FULL_SCAN_MIN_INTERVAL_SECONDS` and `CPA_FULL_SCAN_MAX_INTERVAL_SECONDS`.
- Full scan must still inspect accounts even if they are already disabled or already present in `disabled_accounts.json`.
- `disabled_accounts.json` stores keeper-managed future recheck plans; it does not decide whether the main full scan may inspect or re-enable an account.
- During full scan, disabled accounts with recovered quota should be re-enabled even if they were not previously tracked.
- During full scan, disabled accounts that are still over threshold should be backfilled into tracked recheck state if missing.

## Testing and change guidance

- Most behavior changes in this repo should be driven from tests first, especially in `tests/test_maintainer.py` and `tests/test_settings.py`.
- For quota-policy changes, verify both business behavior and state persistence side effects (`disabled_accounts.json`, delete-blocked tracking, enable verification, scheduling).
- Keep README documentation in sync in both `README.md` and `README.en.md` when runtime behavior changes.
- Prefer minimal, behavior-focused edits in `src/maintainer.py`; many regressions come from changing full-scan logic, tracked recheck logic, and log-driven inspection rules together.

## Branch workflow note

- `new` is the branch that keeps AI traces.
- `main` is the de-AI version derived from `new`.
- Do not invert that relationship when preparing or describing branch-related work.
