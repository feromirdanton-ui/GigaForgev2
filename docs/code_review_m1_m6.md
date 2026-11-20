# GigaForge V2 — Code Review (layers M1–M6)

> Historical review of an earlier snapshot. Some findings were fixed or superseded
> since then; consult [`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) for the
> current review and modernization plan.

**Reviewer:** Code Reviewer agent
**Scope:** `gigaforge/client` (M1), `gigaforge/l0`, `gigaforge/firewall`, `gigaforge/core` (M3), `gigaforge/cli` + `gigaforge/ui` (M4/M5), DevOps/CI surface (M6)
**Commit reviewed:** `main` (HEAD at time of review)
**Method:** full read of the layer sources listed in the task + targeted greps for the usual vulnerability classes (`subprocess`/`eval`/`pickle`, SQL string interpolation, TLS verification, weak hashing/RNG, `assert` in library code, schema migrations).

---

## Резюме для команды (коротко)

Кодовая база **сильная**: слоистость выдержана, docstring'и объясняют *почему*, а не *что*, есть настоящая защита от path-traversal (`firewall/diff.py::_safe_path`), есть контракт exit-кодов CLI, есть read-only доступ к БД для UI, есть честные комментарии про уже пойманные баги. Это выше среднего по качеству.

Но **V2 пока не ship-ready**, по четырём причинами: (1) M6 (devops) практически отсутствует — нет CI, Docker, LICENSE, `py.typed`; (2) у локальной веб-панели есть мутирующий эндпоинт `/api/chat`, который может запускать реальный pipeline, **без аутентификации и без проверки Origin**; (3) `WorktreeManager._slug` может склеить два разных имени агента в один каталог и `rmtree` чужой рабочий worktree (потеря работы); (4) кэш (`core/cache.py`) мигрирует схему вслепую — без `schema_version`, в отличие от `l0/db.py`.

Ниже — приоритизированный список: 🔴 блокер, 🟡 должно быть исправлено, 💭 нит.

---

## 1. Executive summary

| # | Severity | Area | Finding |
|---|----------|------|---------|
| 1 | 🔴 | `ui/webui.py`, `cli/app.py` | Unauthenticated mutating `POST /api/chat` that can drive the real `TaskPipeline`; no auth token, no `Origin`/CSRF check |
| 2 | 🔴 | `core/agents.py` | `_slug()` collision → `shutil.rmtree()` of another agent's live worktree (silent work loss) |
| 3 | 🔴 | M6 / Devops | No CI, no containerisation, no dependency pinning, no LICENSE, no `py.typed`, no lint/type/security gate |
| 4 | 🟡 | `core/cache.py` | Cache schema migrated with unconditional `executescript`; no `schema_version`/append-only migrations (unlike `l0/db.py`) |
| 5 | 🟡 | `client/transport.py` | `trust_env=True` by default + `tls_verify` toggle with no warning → silent MITM surface |
| 6 | 🟡 | `client/auth.py` | Error payloads attach the raw OAuth response body → tokens/secrets can leak into errors, logs and the JSONL trace |
| 7 | 🟡 | `firewall/validator.py`, `firewall/diff.py` | `assert` used as a runtime guard in library code (stripped under `python -O`) |
| 8 | 🟡 | `core/cost.py` | Money computed through `float` intermediates despite an int-micros storage type |
| 9 | 🟡 | `ui/store.py`, `ui/tui.py` | 1 Hz dashboard re-reads the whole JSONL trace on every repaint (O(file) per frame) |
| 10 | 🟡 | Tests / contracts | No JSONL-trace schema, no property-based tests for the diff engine, no metrics↔UI contract test |

**What is genuinely good (keep it):** `diff.py::_safe_path` semantics; atomic multi-file apply with rollback (`diff.py::_rollback`); `max_retries=0` on the urllib3 adapter so retry policy lives in one place (`client/retry.py`); one shared WAL SQLite connection with an RLock (`core/db.py`); `mode=ro` UI connections (`ui/store.py:179`); documented CLI exit-code contract (`cli/app.py`); `stored_variants()` in `ui/store.py` — an explicit defence against the "dashboard silently shows zero" bug class; late import of `TaskPipeline` in `ui/chat.py:220` to keep the UI import-light.

---

## 2. Per-layer findings

### M1 — `gigaforge/client`

**🟡 `client/transport.py:119–129` — proxy env + TLS toggle, silently.**
`self._session.trust_env = getattr(config, "trust_env", True)` means `HTTP_PROXY`/`HTTPS_PROXY`/`REQUESTS_CA_BUNDLE` are honoured by default, and `verify=bool(getattr(config, "tls_verify", True))` (`:139`) lets a single env var turn verification off. The default is correct, but a disabled verifier is invisible in the trace.
**Why:** an operator setting `GF_CLIENT__tls_verify=false` "to make it work" gets a permanently unauthenticated transport, and any proxy env var in a CI image can redirect OAuth calls.
**Fix:** (a) emit a `WARNING`-level structured event (and a `client.tls.verify_disabled` metric) whenever `tls_verify` is false; (b) refuse `tls_verify=false` when `GF_ENV=prod`; (c) make proxy usage explicit (`proxies=` from config) instead of `trust_env`, or at minimum log the resolved proxy.

**🟡 `client/auth.py:184–190` — OAuth response body attached to the error.**
`raise AuthError("OAuth response did not contain access_token", payload=data)` puts the raw provider response into an exception whose `detail`/`payload` is rendered into the trace and CLI output.
**Why:** provider error responses routinely echo request fields; any future debug logging of `payload` can print `client_secret` or a partial token. Credentials should have exactly one path out of the process: into the `Authorization` header.
**Fix:** add a redaction helper (`_redact(obj)` masking `access_token`, `refresh_token`, `client_secret`, `authorization`, `*_key` by key name, recursively) and run every error payload through it before attaching. Add a unit test asserting a payload containing `access_token` renders as `***`.

**👍 `client/transport.py:123–127` — `max_retries=0` on the adapter.**
Correct call: retry semantics are owned by `client/retry.py`, so urllib3 must not add a second, invisible retry layer. Worth a comment in CI-enforced form (a test asserting the adapter's `max_retries == 0`) so a future "just bump retries" change can't silently reintroduce double-retrying of non-idempotent POSTs.

**💭 `client/transport.py:111` — lazy `import requests`.**
Good for keeping the core import-light, but it makes the failure mode a runtime `NetworkError` instead of an install-time error. Since `requests` is in the declared dependencies, consider importing eagerly in the transport module and keeping the *rest* of the package stdlib-only — an error the user can see at import beats one they see on their first prompt.

### M2 — `gigaforge/l0`

**💭 `l0/db.py:232–260` — the migration pattern here is the template the rest of the codebase should copy.**
`SCHEMA_VERSION` + append-only `MIGRATIONS` + `migrate(conn, target=...)` is exactly right. The cache layer does not do this (see below) — reuse this module's pattern rather than inventing a second one.

**🟡 `l0/indexer.py`, `l0/lint.py` — subprocess calls need an explicit runtime bound.**
The call sites use argv lists (no `shell=True` anywhere in the package — verified by grep), which is the correct, injection-free form. What is *not* evident from the reviewed code is a `timeout=` on each child process invocation.
**Why:** `ruff` or a tree-sitter build step hanging on a pathological file turns a code-agent run into an unbounded hang with no timeout, no trace event and no exit code — the worst possible failure for a CI-driven tool.
**Fix:** require `timeout=` on every `subprocess.run(..., timeout=N, check=False)`, capture `TimeoutExpired` into a structured diagnostic, and fail the node (not the run) with a `l0.<tool>.timeout` event. Add a test with a stub that sleeps past the timeout.

### M3 — `gigaforge/firewall`

**👍 `firewall/diff.py:1024–1037` — `_safe_path` is correct.**
The two-stage check is the right one: `is_path_allowed()` rejects `..`, absolute and home-relative paths and applies allow/deny globs, then `(self.root / path).resolve()` + `relative_to(self.root)` catches the residual cases (symlinks, unicode tricks, mounted parents). **No change required.** Two notes:
- It is also *stricter* than intended: a legitimate symlinked directory inside the repo (a common `node_modules`-style link) resolves outside the root and is refused. That is a safe default — document it, don't "fix" it.
- **💭 TOCTOU:** the check happens in `_prepare()` and the write later in `apply()`. A concurrent writer (another sub-agent sharing the worktree) could swap a path component for a symlink between the two. Low severity on a single machine; if agents ever share a worktree, resolve-and-open atomically (`os.open(..., O_NOFOLLOW)` on the final component) or re-validate immediately before `write_text`.

**💭 `firewall/diff.py:750–763` — one fragile branch in the deny-glob matcher.**
`fnmatch.fnmatchcase(normalised, pattern.lstrip("*"))` strips *all* leading asterisks, so for `**/*.pem` the branch tests against `/*.pem`, which can never match a path that has already been `lstrip("/")`-ed — a dead comparison that also *looks* like coverage. The following basename branch already covers the real intent.
**Fix:** drop the `.lstrip("*")` branch and cover the two real cases explicitly (glob against the full relative path, and glob against the basename), with a table-driven test over `*.pem`, `**/*.pem`, `secrets/*`, `**/secrets/**`.

**🟡 `firewall/validator.py:322, 474, 517, 554` and `firewall/diff.py:452` — `assert` as a runtime guard.**
`assert attempt.repair is not None`, `assert self.store is not None`, `assert match is not None` are invariants *documented* as guaranteed by earlier control flow.
**Why:** under `python -O` (or `-OO`) every one of them disappears; the failure then surfaces as `AttributeError: 'NoneType'` far from the cause, in the one module whose entire job is telling the agent precisely what went wrong.
**Fix:** convert to explicit `if x is None: raise <SpecificError>(..., detail={...})`, or use `typing.cast` with a comment where it is truly unreachable. Add `-O` to one CI test job so this class cannot come back.

**💭 `firewall/diff.py` (1107 lines) — highest-risk module, lowest test depth.**
Fenced/prose-wrapped diff normalisation, fuzzy and character-level matching, hunk offsets and counters all live in one file. Grep-based evidence shows the test suite has no property-based fuzzing of `parse`/`locate`/`apply_hunks`.
**Fix:** add `hypothesis` strategies that generate (a) valid diffs from mutated files and assert round-trip apply+revert yields the original bytes, and (b) malformed diffs and assert `parse` never raises anything but `DiffParseError`. This is the single highest-value test investment in the repo.

### M3 — `gigaforge/core`

**🟡 `core/agents.py:133–160, 226–229` — worktree slug collisions destroy work.**
`_slug()` maps any non-alphanumeric run to `-`, lowercases, and falls back to the literal `"agent"` when the result is empty. `create()` then does `path = os.path.join(self.root, safe)` and, if that path exists, `shutil.rmtree(path, ignore_errors=True)` before `git worktree add`.
**Why this is a real bug, not a theoretical one:** `create("Task 1")` and `create("task-1")` both yield `safe == "task-1"`; the second call deletes the first agent's checked-out worktree while it is mid-edit, and `self._created[safe]` silently forgets the first handle. Names that are entirely non-alphanumeric (`"!!!"`, `"###"`) all collapse to `"agent"`. The result is lost work plus a branch left behind by the `-D` cleanup path (`:197–199`).
**Fix:**
1. Disambiguate: if `safe in self._created` or `os.path.exists(path)`, append a short deterministic suffix (`f"{safe}-{len(self._created)}"`) rather than deleting.
2. Never `rmtree` a path that is registered in `self._created`; raise `WorktreeError` with a hint, or remove it through the proper `remove()` path first.
3. Normalise to ASCII (`unicodedata.normalize("NFKD")` + strip combining marks) and validate the final branch name against `git check-ref-format --branch` semantics; keep the `or "agent"` fallback but make it unique.
4. Add tests for the three collision shapes above.

**🟡 `core/cache.py:165–171` — schema migrated blindly.**
`_migrate()` calls `self.conn.executescript(CACHE_SCHEMA)` on every construction, and `CACHE_SCHEMA` uses `CREATE TABLE IF NOT EXISTS`.
**Why:** `IF NOT EXISTS` adds nothing to an existing table, so a V2 that adds a column or a `cache_entries.bytes`/index change silently runs against a V1 database. Failures then look like corrupt data, not a version skew — and `ui/cache_ops.py::purge` queries `SUM(bytes)` and would raise `no such column` at delete time.
**Fix:** copy `l0/db.py`'s pattern — a `cache_meta(key, value)` table holding `schema_version`, an append-only `MIGRATIONS: list[str]` list, and `migrate(conn, target=SCHEMA_VERSION)` that applies only the missing steps inside one transaction. Add a test that opens a v1-shaped DB and asserts the upgrade lands on `SCHEMA_VERSION`.

**🟡 `core/db.py:36–50, 100–140` — "WAL, best-effort" plus a single global write lock.**
`configure_connection()` swallows `sqlite3.Error` for every pragma, while the module docstring promises "readers never block the single writer (WAL)". If `journal_mode=WAL` is rejected (network/synced filesystem, read-only file), the code proceeds with the *rollback journal* and the documented concurrency guarantee is false. Separately, all access funnels through one `RLock`, which serialises every reader and writer across the process — with `core/parallel.py` running nodes that is the hot path.
**Fix:**
1. Verify the pragma result instead of trusting the call: `conn.execute("PRAGMA journal_mode=WAL").fetchone()` returns the *effective* mode — log a one-time `WARNING` (and a metric) when it is not `wal`.
2. Keep one writer connection; add a small pool of `mode=ro` reader connections (the UI already does exactly this in `ui/store.py:179`) so read traffic never queues behind the write lock.
3. Use `executemany` for bulk metric/checkpoint inserts (already available at `core/db.py:105` but the emit paths should be checked for per-row `execute` loops — N round-trips per node).

**🟡 `core/cost.py:36–45, 95–99` — money through `float`.**
The storage type is deliberately integer micros, but `micros = prompt_tokens * prompt_per_million / 1e6 + ...` is computed in binary floating point and only rounded at the end; prices themselves arrive from config as `float` (`_as_float`).
**Why:** the docstring's stated goal ("a long run cannot drift through float rounding") is only true of the *storage*, not of the *computation*. With contract-priced models the price itself is the imprecise quantity, and billing reconciliation against an invoice is exactly where a half-cent discrepancy gets escalated.
**Fix:** `Decimal(str(value))` for prices and token counts, quantise once at the micros boundary (`Decimal.quantize(Decimal("1"))`, `ROUND_HALF_UP`), keep the int column. Consider adding `"prompt_per_million": {"type": "string"}` to the config contract so a price is never parsed as a binary float anywhere.

**🟡 `core/config.py` — env parsing is hand-rolled and unvalidated.**
Config is read from `GF_<SECTION>__<KEY>` names with manual coercion; nothing validates types, ranges or unknown keys, and `pydantic` is absent from the codebase entirely.
**Why:** a typo (`GF_CORE__max_retires=3`) is silently ignored and the run proceeds with the default — a class of bug that is invisible in production and expensive to find in an agent harness where the config *is* the safety envelope.
**Fix:** adopt `pydantic-settings` (see §4). Typed fields, `model_config = SettingsConfigDict(env_prefix="GF_", env_nested_delimiter="__", extra="forbid")`, range validators, and a `--print-config` CLI flag that dumps the resolved config with secrets redacted. `extra="forbid"` alone eliminates the typo class.

**💭 `core/pipeline.py:542–596` — the fix-loop budget hand-off is correct but fragile.**
The M2 fix (only `_node_fix` may spend the repair budget; settle falls back to the proposal diff; `ctx.extras["fix_result"]` prevents a second competing loop) is right and well commented at `:577–580`. The fragility is that the guarantee depends on *insertion into a mutable `extras` dict shared by reference* across nodes — under `core/parallel.py` that is an unsynchronised race waiting for the first person who reorders the DAG.
**Fix:** make the hand-off explicit instead of implicit: a typed per-run `RunContext` field (`ctx.fix_result`) or a frozen dataclass passed as a node input, so the type-checker — not a comment — enforces single-ownership. If `extras` must stay a dict, freeze it after construction (`types.MappingProxyType`) and pass per-node extras by copy.

### M4/M5 — `cli` + `ui`

**🔴 `ui/webui.py:573–587` (with `cli/app.py` `web --serve`) — unauthenticated, mutating, pipeline-driving HTTP endpoint.**
`do_POST` accepts `POST /api/chat` and dispatches to `build_chat_handler(engine)` → `ChatEngine` → `PipelineBackend` → the real `TaskPipeline` (`ui/chat.py:187–224, 657`). There is no API token, no session cookie, no `Origin`/`Referer`/`Sec-Fetch-Site` check, and no CSRF token — the only gate is `if self.path.rstrip("/") != "/api/chat"`.
**Why:** the default bind is loopback (`cli/app.py`: `--host` default `127.0.0.1`), which is good, but (a) any web page the developer visits while the panel is running can issue a cross-origin `POST` to `127.0.0.1:8787` (simple-request form), and (b) a `--host 0.0.0.0` on a shared machine or a container publish turns this into an unauthenticated remote "run arbitrary task with repo-write access" endpoint. Because the handler drives an agent that writes to a worktree, the blast radius is code execution, not just information disclosure.
**Fix (in order of importance):**
1. Generate a random per-process token at startup (`secrets.token_urlsafe(32)`), print it once in the banner, and require it on every mutating request (`X-GigaForge-Token` header or `?token=`), compared with `hmac.compare_digest`.
2. Reject any request whose `Origin` header is present and not the panel's own origin, and require `Sec-Fetch-Site: same-origin` where available.
3. Refuse `--host` other than a loopback address unless a token is explicitly configured; print a loud warning otherwise.
4. Add `--read-only` mode (default when no token is set) that disables `/api/chat` entirely — the dashboard's read path stays available, `ui/store.py` connections are already `mode=ro`.
5. Add a security note to `docs/UI_V2.md` stating the panel is a local operator tool, not a multi-user service.

**👍 `ui/webui.py:35, 494–496` — bootstrap payload escaping is done correctly.**
`json.dumps(...).replace("</", "<\\/")` inside a `<script type="application/json">` block that the page reads via `JSON.parse(textContent)` is the right construction: no executable assignment context, and `</` cannot terminate the element early. Keep it.
**🟡 But the posture is unverified downstream of the bootstrap.** The panel renders model-derived, attacker-influenced strings — the task goal, model summaries, diff bodies, error messages, branch names. The reviewed code shows the escaping for the embedded snapshot only; there is no evidence of a systematic escaping strategy for the JS that builds the DOM from it.
**Fix:** require an explicit assertion in review and in CI that **every** insertion of a snapshot-derived string into the document uses `textContent`/`createTextNode`, never `innerHTML`/`insertAdjacentHTML`; add a CSP meta tag (`default-src 'none'; script-src 'self' 'unsafe-inline'`) as defence in depth; and add one test that plants `"<img src=x onerror=alert(1)>"` in a goal, a summary and a diff line and asserts the rendered HTML contains no live element.

**🟡 `ui/store.py:191–200` + `ui/tui.py` watch loop — O(file) JSONL read per frame.**
`read_jsonl()` re-opens and parses the entire trace file every call, and `cli/app.py` `ui --watch --interval 1.0` repaints at 1 Hz. A run with a large trace therefore costs a full parse per second, growing with the run — the dashboard gets slower exactly as the thing it monitors gets more interesting.
**Fix:** give `UiStore` a cached `(path → (inode, size, offset, events))` entry and read only the new tail bytes; drop the cache on inode/size shrink (rotation). Add a benchmark test asserting tail cost is O(new lines), not O(file).

**💭 `cli/app.py:_chat_engine` — a silent `except Exception: return None` disables the chat.**
When `default_engine` fails (no credentials, missing dep), the web command degrades to a panel whose `/api/chat` simply 404s — indistinguishable from "this build has no chat".
**Fix:** capture the exception message and surface it in the page/CLI banner ("chat disabled: <reason>"), and add an `/api/health` payload that the panel renders.

**👍 `cli/app.py` — exit-code contract.**
`0` success, `1` run rejected, `2` no data, `3` usage/config error, documented in the module docstring and used consistently via `R.EXIT_*`. This is what makes the tool scriptable, and `--json` bypassing `rich` so output is pipe-safe is the correct detail most CLIs get wrong.

### M6 — DevOps / CI surface

**🔴 The layer is effectively absent.** `ls .github` → *No such file or directory*. No `Dockerfile`, no `docker-compose*`, no `Makefile`, no `*.yml`/`*.yaml` anywhere, no `requirements*.txt`, no `LICENSE`, no `py.typed`.
**Why this is a blocker for a "V2":** none of the quality that exists in the code is *enforced*. Nothing stops a change that breaks the type surface, disables TLS verification, or drops the repair budget; there is no reproducible environment for the "V2 acceptance" step; and a published package without `py.typed` gives downstream users no types at all.
**Fix:** see §4 — minimal concrete file set.

**🟡 Secrets handling is correct in principle, unenforced in practice.**
`GF_CLIENT__AUTH_KEY` (base64 `client_id:client_secret`) via environment is the right design and the CLI's error message says so ("secrets come from the environment only"). But with no CI, no `bandit`/`pip-audit`, and no `.gitignore`-verified pre-commit hook, there is no mechanism that would catch a committed `.env` or a hard-coded key.
**Fix:** `pre-commit` with `detect-secrets`/`gitleaks`, `bandit`, `ruff`; a CI job running `pip-audit`; and a documented rule that a missing secret is a startup failure (`exit 3`) and never a fallback to a default.

**🟡 Command allowlist is a config field, not a policy.**
`firewall.apply_allow_paths` / `apply_deny_globs` are the right hooks for a write-path allowlist. There is no equivalent *command* allowlist for anything the harness shells out to, and no checked-in default policy file.
**Fix:** ship a versioned `gigaforge.policy.toml` (default-deny) covering: allowed write globs, denied globs, permitted executables + argv shapes, per-command timeouts, and network egress policy; load it via config with `GF_POLICY=/path` override; fail closed if it is absent in `GF_ENV=prod`.

**🟡 No dev/prod distinction.**
Nothing in the reviewed code branches on environment. `tls_verify=false`, non-loopback binds, cache writes and the repair budget should all have different defaults in dev vs prod.
**Fix:** introduce `GF_ENV=dev|prod` (default `dev`), with prod enforcing: TLS verification on, no non-loopback web bind without a token, `extra="forbid"` config, and a startup self-check that logs the effective policy.

---

## 3. Prioritized top-10 action items

| Priority | Action | Files | Effort |
|---|---|---|---|
| P0 | Token + Origin/CSRF gate on `/api/chat`; default read-only mode; warn on non-loopback bind | `ui/webui.py:559–590`, `cli/app.py` (`web`) | S |
| P0 | Unique, collision-proof worktree slugs; never `rmtree` a registered worktree | `core/agents.py:133–160,226–229` | S |
| P0 | CI pipeline: `ruff`, `mypy --strict`, `pytest --cov`, `bandit`, `pip-audit`, `-O` job, matrix 3.10–3.12 | new `.github/workflows/ci.yml` | M |
| P0 | Multi-stage `Dockerfile` (non-root, no secrets, read-only rootfs) + `docker-compose.dev.yml` with a cache volume | new `Dockerfile`, `docker-compose.dev.yml`, `.dockerignore` | M |
| P1 | `schema_version` + append-only migrations for the cache; migration test from a v1 DB | `core/cache.py:165–171`, reuse `l0/db.py` | M |
| P1 | Redact secrets in every error payload / log / trace event | `client/auth.py:184–190`, `core/errors.py` | S |
| P1 | Replace `assert` guards with explicit raises | `firewall/validator.py:322,474,517,554`, `firewall/diff.py:452` | S |
| P1 | `pydantic-settings` config with `extra="forbid"`, range validators, `--print-config` (redacted) | `core/config.py`, `client/config.py`, `firewall/config.py` | M |
| P1 | `hypothesis` fuzzing + round-trip apply/revert tests for the diff engine | `firewall/diff.py`, `tests/` | M |
| P2 | `Decimal` money end-to-end; WAL as a checked post-condition; reader-connection pool; JSONL tail-read in the UI | `core/cost.py`, `core/db.py`, `ui/store.py` | M |

---

## 4. Architecture & framework recommendations

These are proposals, not findings — each one is justified by a specific pain point above.

**Configuration & contracts → pydantic v2 / `pydantic-settings`.**
Removes the typo-silent-failure class (P1 item), gives validated nested config for free, and lets `contracts.py` express the node input/output and JSONL-trace event shapes as models with `model_json_schema()` exported for documentation. Publish the trace schema as JSON Schema and validate it in tests — that converts "the UI silently reads zero" from a runtime surprise into a contract test.

**Tracing/observability → OpenTelemetry.**
Every node already writes a JSONL event and a metric point; those are spans and attributes in disguise. An OTel `TracerProvider` with an OTLP exporter (plus the existing SQLite sink as a local backend) gives flame graphs per run, model-call latency distributions and cache-hit attribution without hand-rolled aggregation. Pair with `structlog` for JSON logs and a `trace_id` on every event.

**HTTP/transport → keep `requests` for sync now; move to `httpx` if/when `parallel.py` goes async.**
`httpx` gives a single client with `verify=`, `timeout=httpx.Timeout(...)`, HTTP/2, and a first-class async client that would let the DAG's independent nodes run as `asyncio` tasks instead of thread-pool work — removing the thread-per-node ceiling and the GIL contention around the shared SQLite lock. Do **not** migrate for its own sake: the current sync transport with a sized pool, `max_retries=0` and retry policy in `client/retry.py` is well built. Migrate when concurrency, not cleanliness, demands it.

**Resilience → `tenacity` for retry policy.**
`breaker.py` + `fixloop.py` are sound; `retry.py` reimplements backoff+jitter. `tenacity` would standardise the policy (retry predicates per HTTP status/exception, jitter strategies, `stop_after_delay`) and remove bespoke code that must be reviewed for correctness. Keep the circuit breaker hand-rolled — it encodes domain knowledge tenacity does not have.

**Durable execution → not yet.**
`dag.py` + checkpoints + resume is an appropriate hand-rolled journal at this scale. Revisit (Temporal, or SQLite-backed durable task queues) only when runs must survive process restarts *and* fan out across machines. Document that threshold so the decision is deliberate.

**Web UI → for the "most modern UI" goal, split the panel from the engine.**
The self-contained-HTML panel is a genuinely clever local tool and should stay for `gigaforge web --out`. But a V2 UI with live runs, diff review and multi-session chat wants: a **Vite + TypeScript** frontend (React or Svelte) against a **FastAPI/uvicorn** service, which brings OpenAPI docs, SSE/WebSocket streaming for run events, `TrustedHostMiddleware`, dependency-injected auth, and a proper static-asset pipeline — versus `ThreadingHTTPServer` where every security concern above must be hand-written. This also naturally serves the embeddable-panel use case via a built static bundle.

**Money → `Decimal` or integer micros everywhere, never `float`** (§M3, `core/cost.py`).

**Typing & lint → `ruff` + `mypy --strict` + `pre-commit`.**
The code is already fully annotated with `from __future__ import annotations`; adding `py.typed` and running strict type checking in CI locks in a real asset the project has already paid for.

**Security tooling → `bandit`, `pip-audit`, `semgrep`.**
Semgrep rules targeted at exactly this codebase's risk shapes: `subprocess.*` without `timeout=`, `verify=False`, `except Exception: pass` around security paths, `assert` in non-test code.

**DevOps minimum viable set (for the DevOps Automator turn):**
```
.github/workflows/ci.yml        ruff + mypy --strict + pytest --cov (floor) + bandit + pip-audit, matrix 3.10/3.11/3.12, one `python -O` job
.github/workflows/release.yml   build + publish on tag, PyPI trusted publishing (OIDC) — no long-lived tokens
.github/dependabot.yml          weekly, grouped
Dockerfile                      multi-stage, python:3.11-slim, non-root USER, no secrets, HEALTHCHECK
docker-compose.dev.yml          dev env, cache volume, GF_ENV=dev
.dockerignore  Makefile  LICENSE  py.typed  .pre-commit-config.yaml  gigaforge.policy.toml
```

---

## 5. Risks explicitly NOT fixed in this review

- **XSS in the panel's DOM-building JS** — the escaping of the embedded snapshot is verified correct; the downstream insertion strategy is *not* verified from source. It must be proven by the test described in §M4/M5, not assumed.
- **Subprocess timeouts** in the L0 tool wrappers — flagged as a required audit; I did not confirm a `timeout=` on every call site, and I deliberately did not claim a bug where the evidence is a missing detail.
- **`core/parallel.py` shared-state races** — the `extras`-by-reference hand-off is flagged; the full concurrency surface of every node was out of this review's budget.
- **Type-checker baseline** — `mypy --strict` has never been run against this tree; expect an initial backlog before the gate can be enforced as blocking.

---

## Verification checklist for the implementing agent

1. `ruff check .` and `mypy --strict gigaforge` clean.
2. `pytest -q` green, plus `python -O -m pytest -q` green (proves the `assert` refactor).
3. New tests: worktree slug collisions (3 shapes), OAuth payload redaction, cache migration from a v1 DB, malformed-diff fuzzing, `/api/chat` auth rejection (no token / wrong Origin), HTML escaping of a planted `<img onerror>` in goal/summary/diff.
4. `docker build .` succeeds and the image runs as non-root with no secret in any layer (`docker history`).
5. CI red on a deliberately broken `tls_verify=false` + `GF_ENV=prod` combination.

*Reviewed with the repository's own conventions in mind; findings reference the commit on `main` at review time. Suggestions are proposals — every one is traceable to a specific line, a concrete failure scenario, and a test that would prove the fix.*
