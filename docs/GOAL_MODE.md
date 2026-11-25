# Long-lived goal execution

GigaForge exposes two distinct execution contracts:

- `gigaforge run "<task>"` authors and validates one bounded pipeline run.
- `gigaforge goal "<objective>"` keeps pursuing an objective across multiple
  implementation and verification cycles.

## Goal cycle

1. Goal mode creates a private project copy at
   `.gigaforge/workspaces/<goal-id>/project`. The normal pipeline plans, edits,
   runs its diff gate and reviews the change inside that copy. `.env` secrets,
   Git metadata, runtime state and common dependency caches are not copied;
   `.env.example` remains available as a template.
2. Before the first model call, Goal provisions and checks its parser, test, and
   browser runtimes. An independent workspace verifier then checks changed file
   types: Python compilation and an existing pytest suite, JavaScript syntax,
   HTML structure, and a headless browser smoke run for HTML apps. Explicit
   word and character counts in the request are also checked against the
   produced artifact before it can be accepted.
3. A code failure is summarized and included in the next cycle's prompt so the
   model repairs the current result. Passing verification sends only reported
   changed files through the destination writer. The writer checks that the
   destination files have not changed since the copy was created, rejects paths
   outside the project and replaces files atomically. A conflict pauses
   publication and keeps the isolated workspace available for resume.
4. Missing verification tools block completion instead of being reported as a
   successful run. The app provisions Playwright and Chromium automatically at
   startup and retries setup from the verifier when needed. If setup cannot
   complete (for example, while offline), the goal remains resumable; restore
   connectivity and resume it.
5. A maximum cycle count bounds work per invocation. If two adjacent failed
   cycles produce the same changed-file contents and the same failure, Goal
   stops early with `needs_attention` rather than spending the remaining cycles
   on an unchanged result. A missing verifier is reported as `blocked`, not
   mistaken for repeated model failure.
6. A nonzero `--budget` is a lifetime token cap shared by all cycles. Goal
   passes only the remaining allowance to each pipeline run and blocks before
   starting another cycle when the cap is spent. Parallel sub-agent caps are
   divided from the remaining run allowance, and their reported usage is added
   back to the parent run. With sub-agents enabled, planning is read-only:
   only file/search observations are allowed; writes, REPL execution, and test
   commands are rejected. Only successful mutations count as episode output,
   so a rejected write attempt cannot suppress fan-out. When two or three
   explicit independent code files are identified, GigaForge uses a
   deterministic delegation plan instead of spending model calls on redundant
   parent planning. Independent child worktrees therefore start from the same
   base as the final merge; if planning has actually changed code, fan-out is
   skipped rather than applying stale child diffs. Resuming with no `--budget`
   keeps the saved cap; pass a larger total cap to extend it.

   Python Goals also verify explicitly requested public function names, reject
   duplicate public definitions in changed modules, and enforce fixed-width
   `HH:MM:SS` input when the goal requires that format. Sub-agent fan-out
   assigns a requested function to its related module and rejects additions
   that leak the symbol into sibling files.
7. ReAct executes its tool loop inside the plan node, so it has a separate
   `GF_CORE__REACT_TIMEOUT` wall-clock limit (480 seconds by default); the
   overall pipeline deadline and Goal token budget still bound the run.
8. Progress is atomically saved under `.gigaforge/goals/<goal-id>.json` after
   each cycle and can be continued with `gigaforge goal --resume <goal-id>`.
9. After publication, the large project copy is removed. The goal report and
   workspace manifest remain under `.gigaforge`.

In the desktop chat, Agent runs receive a bounded transcript of earlier user
and assistant turns, so requests to refine existing work retain their context.
If a Goal is paused and the user sends a different message in the same session,
the saved objective is resumed and that message is stored as a clarification;
the original objective remains unchanged. This context is limited to 8,000
characters and is passed to the LangGraph goal engine.

The desktop **Settings → file changes** control has two persisted modes. **Ask**
shows one approval for the complete diff (all files together) at each edit cycle;
**Always allow** skips those dialogs. In both modes Goal still verifies the
isolated project copy and publishes only verified, conflict-free files.

The goal loop invokes only built-in verification commands, with argument arrays
and `shell=False`; it never runs a model-authored shell command. Test output is
bounded and common credential labels are redacted before being persisted or fed
back to the model. Run reports contain compact check evidence, file names,
resource totals, and repair summaries rather than full diffs or transcripts.

## Browser verifier

Browser verification is a core Goal capability. Application startup checks and
provisions its parser/test packages, Playwright, and Chromium automatically when
they are absent.

The verifier serves the selected local HTML entry point on loopback, loads it in
headless Chromium, tries a start/play control when one exists, and sends common
game keys. Uncaught page errors and console errors fail the cycle and become
repair feedback. If Playwright or Chromium is absent, the goal is marked
`blocked` only if automatic provisioning cannot make the browser available. Once
the runtime is available, resume the goal to recheck the existing artifact before
another model call.

## CLI examples

```bash
gigaforge goal "Build an accessible cyber-neon Tetris game" --max-cycles 4
gigaforge goal --resume goal-20261018T180000-a1b2c3d4 --max-cycles 2
gigaforge goal "Implement a backend feature" --subagents --json
gigaforge goal "Update HTML docs" --static-only
```

Statuses are `completed`, `needs_attention` (cycle limit reached), `blocked`
(required verifier unavailable), `cancelled`, and `failed`. A goal is accepted
only in `completed` state.
