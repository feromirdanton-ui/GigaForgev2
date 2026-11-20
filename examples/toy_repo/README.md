# toy_repo — GigaForge end-to-end test target

A minimal real Python repo for the M4c pipeline e2e test.

## Task

Fix `validate_email` in `math_utils.py` so it accepts standard email
addresses like `"x@y.z"`. After the fix, all tests under `tests/` must pass.

## Layout

- `math_utils.py` — the module under edit. Contains:
  - `add(a, b)` — already correct.
  - `validate_email(email)` — **BUGGY** (rejects short TLDs like `.z`).
  - `register_user(email)` — caller of `validate_email`.
- `tests/test_math_utils.py` — one passing test for `add`, one FAILING test
  for `validate_email` (expects `"x@y.z"` accepted). This is the failure the
  fix-loop must resolve.

## Why

Gives the e2e test a deterministic, observable failure that the Coder +
fix-loop can repair. The toy repo is a real git repo (committed at test
setup time), so the pipeline's worktree + git-commit + cache-write paths
all exercise real git operations.

## How the e2e test uses it

The test copies this directory to a temp dir, runs `git init && git add -A
&& git commit`, then runs the pipeline against `target_function=
validate_email`, `target_file=math_utils.py`. The FakeGigaClient returns a
canned diff that fixes the regex. The test asserts the post-pipeline
worktree's tests are green and the cache now has an entry.
