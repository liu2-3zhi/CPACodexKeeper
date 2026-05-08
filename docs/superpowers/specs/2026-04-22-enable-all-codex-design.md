# Enable-all-codex helper script design

## Summary

Add a minimal standalone Python script at the project root that connects to one CPA instance, lists all auth-files, filters to `type == "codex"`, and directly enables those accounts by setting `disabled=false`.

The script should prefer built-in configuration values defined inside the file. If any required built-in value is missing, it should prompt for the missing value at startup.

The script should execute immediately without a confirmation step, and it should emit detailed logs for startup, account discovery, per-account actions, and final summary.

## Goals

- Provide a minimal standalone utility for enabling all codex accounts in one CPA instance
- Reuse the existing CPA API client instead of reimplementing HTTP logic
- Prefer built-in configuration values, with startup prompts only as fallback
- Only process auth-files where `type == "codex"`
- Log every important step and outcome in a human-readable way
- Keep the change isolated from the existing keeper CLI and runtime paths
- Cover the helper behavior with focused unit tests

## Non-goals

- Integrating this helper into `src/cli.py`
- Changing any behavior in the main keeper workflow
- Adding refresh, quota checking, deletion, or recheck logic
- Adding concurrency or batching
- Supporting partial enable filters such as email patterns or name prefixes
- Writing logs to files or introducing a new logging subsystem

## Recommended approach

Create a new root-level script named `enable_all_codex.py`.

The script should import and reuse `CPAClient` from `src/cpa_client.py`. This keeps CPA request behavior, retries, timeout handling, and proxy wiring aligned with the rest of the project while avoiding changes to the main keeper code.

The helper should remain function-based and small. It should not introduce a new settings abstraction, argparse interface, or CLI mode.

## Configuration model

The script should define built-in configuration constants near the top of the file:

- `DEFAULT_CPA_ENDPOINT`
- `DEFAULT_CPA_TOKEN`
- `DEFAULT_CPA_PROXY`
- optional timeout / retry constants only if needed for direct `CPAClient` construction

Resolution rules:

1. If a built-in value is present and non-empty after trimming, use it.
2. If a required built-in value is missing, prompt the user at startup.
3. `CPA_ENDPOINT` and `CPA_TOKEN` are required.
4. `CPA_PROXY` is optional.

This means built-in values always win over prompted input. Prompting is only a fallback for missing required values.

The helper should not read `.env` because the requested behavior is specifically “built-in first, otherwise input,” and adding `.env` would blur the priority model unnecessarily.

## Runtime flow

The script should follow this linear flow:

1. Resolve configuration from built-in values and fallback prompts.
2. Log startup information:
   - target CPA endpoint
   - whether proxy is configured
   - token in masked form only
   - configuration source for each field (`built-in` or `prompt`)
3. Create `CPAClient`.
4. Request all auth-files from CPA.
5. If the request fails or returns no usable list, log the failure and exit with status code `1`.
6. Filter auth-files to entries where `type == "codex"`.
7. Log total auth-file count and filtered codex count.
8. Process codex accounts one by one.
9. For each codex account:
   - read `name`
   - read `email` if present, otherwise log `unknown`
   - read current `disabled` state
   - if already enabled (`disabled is False`), log that it is already enabled and skip the PATCH call
   - otherwise call `set_disabled(name, False)`
   - log success or failure
10. Emit a final summary and exit.

## Per-account behavior

Each codex account should be treated independently.

Expected behavior:

- Missing `name`: log a warning, count as skipped failure, continue to next account
- `disabled is False`: log “already enabled”, count as skipped, continue
- `disabled is True`: attempt `set_disabled(name, False)`
- PATCH success: log success, count as enabled
- PATCH failure: log error, count as failure, continue

The script should continue after per-account failures so one broken account does not block the whole batch.

## Logging

The script should use simple stdout logging with timestamps.

### Startup logs

Log:

- script start banner
- target endpoint
- proxy configured or not
- masked token
- source of endpoint/token/proxy (`built-in` or `prompt`)

`CPA_TOKEN` must never be printed in full. It should be masked, for example by preserving a small prefix and suffix and replacing the middle section with `***`.

### List retrieval logs

Log:

- starting auth-file fetch
- fetch success with total count
- filtered codex count

### Per-account logs

For each processed codex account, log:

- index and total
- account name
- account email or `unknown`
- original disabled state
- action taken (`skip already enabled` or `set disabled=false`)
- result (`success` or `failure`)

### Final summary logs

Log a summary containing:

- total auth-files returned
- total codex accounts
- already enabled count
- attempted enable count
- successful enable count
- failed enable count
- skipped invalid-entry count

If failures occurred, also list the failed account names in a short final section.

## Error handling

Keep error handling minimal and explicit.

- Missing required config after prompt: exit `1`
- Auth-file list fetch failure: exit `1`
- Empty auth-file list: log and exit `0`
- Zero codex accounts: log and exit `0`
- Per-account enable failure: continue processing, but exit `1` at the end if any failures occurred

This keeps the script easy to reason about while still making the exit code useful for automation.

## Script structure

Keep the file small and function-based.

Recommended functions:

- `mask_secret(value: str) -> str`
- `prompt_if_missing(label: str, current: str | None, *, secret: bool = False) -> tuple[str | None, str]`
- `resolve_config() -> tuple[str, str, str | None, dict[str, str]]`
- `log(level: str, message: str) -> None`
- `fetch_codex_accounts(client: CPAClient) -> tuple[list[dict], int]`
- `enable_accounts(client: CPAClient, accounts: list[dict]) -> int`
- `main() -> int`

The return value from `main()` should be passed into `SystemExit(main())`.

## Test plan

Add focused unit tests for the helper script. The tests should mock `CPAClient` rather than performing real network calls.

Cover at least:

1. built-in values are used without prompting
2. missing built-in values trigger prompt fallback
3. only `type == "codex"` accounts are processed
4. already-enabled codex accounts do not call `set_disabled`
5. disabled codex accounts call `set_disabled(name, False)`
6. one failed enable does not stop later accounts from processing
7. final exit code is `0` when all actions succeed or are already enabled
8. final exit code is `1` when any enable action fails
9. token logging uses masking, not plaintext

The tests can stay isolated to the helper file by patching input/output and the imported client.

## Acceptance criteria

- Running `python enable_all_codex.py` works without touching the main keeper CLI
- The script prefers built-in config values over startup input
- The script only processes auth-files where `type == "codex"`
- The script directly enables eligible accounts by calling `disabled=false`
- Already-enabled codex accounts are logged and skipped without a PATCH call
- Detailed logs are printed for startup, list fetch, per-account actions, and final summary
- Token values are masked in logs
- The script exits non-zero when any enable action fails or the auth-file list cannot be fetched
- The implementation remains minimal and isolated from existing keeper runtime behavior
