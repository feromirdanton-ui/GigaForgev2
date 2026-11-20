# The GigaForge Desktop Window

The window is the harness's **primary interface**. It is a normal application:
it opens on launch, keeps its own state, and works with no browser and no server.

`gigaforge desktop` · `gigaforge app` · `gigaforge-desktop` · `python -m gigaforge.desktop`
· `start.bat` open the Qt Quick workspace by default. The launcher provisions its
optional Qt runtime on first start. Goal parser/test packages, Playwright and
Chromium are checked and provisioned asynchronously while the window stays usable;
Goal becomes available after verification, with retry in Settings if setup fails.
`--ui tk` opens the legacy Tk fallback.

The composer separates **Chat**, **Agent** and **Goal**. Agent and Goal expose a
persistent sub-agent switch (Goal defaults on; Agent defaults off). Goal runs up
to eight author/verify cycles by default; Settings lets you choose 1–20. An
unfinished Goal keeps its ID and isolated workspace so **Continue Goal** can
resume it after the cycle budget or an application restart. **History** lists
saved goals for the selected project, shows their latest cycle and status, and
lets you resume any unfinished goal. Switching projects restores that project's
own resumable goal instead of carrying a stale ID across workspaces. Goal also
streams each authoring cycle, verification start/result, and final state into the
same live event feed as agent and sub-agent work.

The header model picker selects the working model for Chat and future runs, and
remembers the choice. **Chat** includes a searchable conversation history and
recent project folders; each project's conversations stay with that workspace.
The same inline Chat/Agent selector is used in the composer and floating chat;
the sub-agent control uses an icon toggle, while motion effects remain a themed
switch.

**Settings** edits the GigaChat endpoint, OAuth URL, scope, TLS verification,
file-change approval policy, adaptive workspace preference and Goal cycle limit.
Credential checks run in the background; the secret is entered as a masked value
and is not loaded back into the form. Connection results, Goal environment state
and the Settings summary follow the selected RU/EN interface language.

The visual system uses three palettes: **Pearl** layers cool blue-gray, deep
blue-green and airy mint glass; **Graphite** balances charcoal surfaces with
muted turquoise and watercolor-blue accents; **Verdant** uses a deep blue-green
field with mint and lime highlights. Warm yellow and cobalt are reserved for
attention and focused accents. The selected palette is saved with the desktop
settings.

The QML view uses the same `AppContext`, runtime, sessions, settings and Python
orchestration as the fallback. It adds a project file editor, a shell command
runner, persistent split layouts and the live Chat conversation surface.

## Chat workspace

The empty Chat workspace opens on a centered welcome with four starter cards in
a responsive two-column grid and a larger, animated GigaForge sphere. The
welcome keeps the active project and its indexed-file count visible beside
context-aware scenarios. Choosing a starter fills the composer but does not
send it; the prompt remains editable. A flush, collapsible work sidebar is part
of the main shell rather than an overlay on the Chat page. It groups Chat, Deep
Research, Orchestrate, Files/editor and Agents above project folders and
searchable chat history. Overview, Monitoring and Settings sit in the top bar
beside the GigaForge mark. The sidebar collapses to a narrow strip with an
expand arrow; chat titles stay on one line and can be renamed. “New chat”
starts a conversation and returns to Chat;
the project add action opens the folder picker, where an existing or newly
created project folder can be selected. The active project stays distinct from
the collapsible recent-project list, and history remains grouped by local date.
Chat titles can be edited in place from their row, without opening a separate
dialog; the compact density setting also tightens the sidebar rows and composer
mode selector.
Navigation hover labels use a rounded, theme-aware surface.
The New chat action uses a chat-plus mark, Files/editor has a notebook icon, and
project creation has a larger plus target. Sidebar typography and the composer
attachment/sub-agent icons are scaled for easier recognition.
**Chat** and **Agent** are the visible composer modes. The expanded composer
shows project context, attachments, mode and a full Send/Run action together.
Start Goal by typing `/goal` at the beginning of either the
main or floating composer; the command is removed from the prompt and a small
Goal badge appears above the input. The badge returns the composer to Chat.
Chat and Agent appear as inline text choices in the composer footer, with only
the active choice highlighted. The footer also contains the file-attachment
icon, the Agent-mode icon-only subagent toggle, and Send. Up to eight files can
be attached; text and source contents are bounded before inclusion, and
credential-like files are rejected. Attachment and subagent controls use
larger hit targets and icons in both composers.
Goal starters remain available while Goal is active and focus on measurable
outcomes with iterative verification. Conversation messages, execution steps,
tool calls and errors use distinct visual treatments. A finished run shows its
completed state instead of retaining a partial in-progress bar.
The sidebar can start a new conversation, filter saved chats, rename a chat, or
switch between recent project folders. Chat transcripts and renamed titles
remain in each project's `.gigaforge/chat` directory.
The adaptive workspace classifies the draft locally as writing, codebase work,
or automation. Writing hides history and live context; codebase work opens both;
automation opens the live event context and gives up history width. The composer
explains the change and offers Undo. The setting is visual-only: it never changes
the execution mode, approval policy, or agent permissions, and can be disabled
from Settings. Codebase and automation drafts in Chat offer a switch to Agent
instead of changing modes without consent.
Agent and Goal expose saved agent profiles in the composer. A profile's
user-authored role instructions are included in the actual task, and its
sub-agent preference applies to that run. File-change approval remains a global
setting and cannot be changed by adaptive layout or a profile. Chat remains a
direct, tool-free conversation.
The project context and task activity use the selected workspace and real run
events. Agent/Goal use the project task pipeline; the live event pane and
Orchestrate view show recorded execution rather than a synthetic progress
sequence.
The model picker in the top bar loads choices from the configured provider API
in the background and reports whether its list came from the live API, the
local cache, or configured fallback models. The picker can refresh the catalog
without restarting the desktop app. It prioritizes GigaChat-3-Ultra and the
GigaChat-2 Max, Pro and Lite variants; other catalog choices are available from
the expandable list.

The **Agents** page derives its catalog role and family counts from the runtime
catalog. Rows show the available name, role and latest status; specialists
reported by run events supplement the catalog without changing its totals.

Keyboard navigation uses **Ctrl+1–8** for Overview, Chat, Explore,
Orchestrate, Files, Agents, Monitoring and Settings. **Ctrl+K** opens the
command palette, **Ctrl+L** focuses the task composer, **Ctrl+Shift+F** focuses
project-file search, **Ctrl+N** starts a new chat, **Ctrl+Shift+C** opens the
floating chat, **Ctrl+Shift+J** opens the project terminal, and **Ctrl+/** opens
the shortcut guide. Navigation moves focus to the selected view's primary field.

## Explore and Orchestrate

**Explore** is the Deep Research workspace. It runs up to four project searches
in parallel, keeps their results as separate research lanes, and builds a source
graph from matching files and explicit local Markdown links. Save useful files
to the project's knowledge library and attach notes; the research board persists
in `.gigaforge/research.json`. Searches use the same safe file index as the
editor, and source links open in the single Files & editor workspace, respecting
its unsaved-edit guard.

**Monitoring** is the system-health view: it reports provider connectivity,
latest run status, runtime-reported participants, recorded errors, measured
token use and the sum of measured node durations, tool-call counts, and a
filterable feed of runtime step/tool/error events since the latest user task in
the selected conversation. It does not invent queue depth, wall-clock duration,
latency, or other unavailable metrics.

**Orchestrate** focuses on execution: the runtime's progress value, measured
trace nodes, their actual durations and attempts, reported participants, and
step/tool/error events since the latest user task in the selected conversation.
Selecting a trace node exposes its recorded details.
Catalog roles are not presented as active run participants, and the five-step
illustrative pipeline is removed. Goal history and resume actions call the same
saved Goal store and runtime as Chat. The view does not synthesize agent output
or completion state.

**Files & editor** opens text files from the selected project and keeps separate
tabs for each file. Switching away from unsaved edits is blocked, and saving is
refused when the file changed on disk since it was opened. UTF-8 byte order marks
and the file's original newline style are preserved. Binary, oversized, and
non-UTF-8 files are not loaded into the text editor.
The **Diff** action compares the current editor buffer with the version that was
loaded when that tab was opened, including edits saved during the session.

The file browser hides local environment files, private key files, credential
files, and common secret directories such as `.aws`, `.ssh`, and `secrets`.
Those paths are also blocked from editor open, save, and create operations;
`.env.example` remains visible. The same policy filters files attached to agent
context and rejects protected diff targets. Project paths are resolved before use,
so an editor cannot escape the selected workspace through `..` or an external link.

The console keeps one operating system shell open in the selected project, so
working-directory changes and shell variables carry over between commands. While
a command is running, the input sends a line to its standard input for programs
that read it; **Stop** ends the active process and closes that shell. **Close
shell** exits an idle session.
Choosing another project closes an idle shell and opens the new project as the
working directory. Drag the split handles to resize the file browser, editor,
and terminal; the terminal can move below or beside the editor, and the layout
is saved between launches. **Ctrl+Shift+J** opens a glass terminal window over
any page; drag its header or resize corner, and its geometry is restored between
launches. Closing that panel leaves the shell session available in Files.

---

## Why a window instead of "just the web UI"

The web control room is good, but it is the wrong *default*:

- it needs a port, a browser tab and a server process that can die independently;
- a tab is not an application: it has no window state, no taskbar identity, no icon;
- the machine that needs to see telemetry may have no browser session at all.

The Qt Quick shell reads current state through the shared `AppContext` and
runtime. The legacy Tk fallback also includes a **Browser UI** view that starts
the same local control-room server as the CLI and shows its bound URL.

---

## Visual system

The Qt Quick shell uses three distinct materials: **Pearl** pairs a cool paper
field and deep teal rail with restrained aqua-mint glass; **Graphite** uses a
neutral technical grid with turquoise and cobalt light; **Verdant** uses deep
green surfaces and soft bioluminescent color. Each theme uses readable surface
layers, fine highlights and accessible active states. The floating chat and
project terminal share live application state, support drag and resize, and
restore their window geometry between launches. Clicking a floating panel brings
it to the front. The chat window uses the same
Chat, Agent and Goal handlers and event stream as the main workspace. The editor
and terminal keep recessed dark technical surfaces. `desktop/qml/ThemeTokens.qml`
and `GlassCard.qml` define the shared palette, typography, density and corner scale.
Settings also switch Airy or Compact spacing and enable or reduce interface
motion. The reusable `desktop/qml/LivingCore.qml` uses actual run, terminal and
Goal runtime state to tune the brand orb's glow and orbital tempo; reduced
motion also stops its animation. The orb pauses while its overview page is
inactive to avoid running offscreen animations.

Live Context shows the latest task and its current run activity. It can be hidden
or restored from the header, and its visibility and width persist between
launches. It also shows the selected project, model and mode, plus measured token
usage from the last run against the currently configured task cap; the label
distinguishes persisted usage from the current setting. The task composer stays
docked below the active workspace; its three modes use a single segmented
control so the current interaction contract stays visible without competing
with the task itself.

The Overview task, environment shortcuts and activity panels can be reordered by
dragging their corner handles. The order persists across launches and can be
restored from Settings.

The Overview switches to a compact task summary when the main workspace is
narrow or the window is short. Breakpoints use the available workspace size,
not the outer window width; repeated project metadata and secondary preview
rows yield space to the task, while active or failed run status remains visible.
The animated core stays beside the Overview heading when the workspace has
room, scales down for laptop layouts, and yields to the task on narrow or short
windows. The full task text is available from its truncated title. Live Context
shows up to eight recent run events, reducing to six shorter rows when the
window is short or the context pane is narrow.

## Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ GigaForge · Overview · Monitoring · Settings · project · model · ...     │
├───────────────┬────────────────────────────────────────┬─────────────────┤
│ work sidebar  │ active view                            │ live context    │
│ Chat          │ Chat · Explore · Orchestrate            │ task/run        │
│ Projects      │ overview · files · agents               │ activity        │
│ Chat history  │                                        │                 │
│               ├────────────────────────────────────────┤                 │
│               │ task composer · Chat / Agent / Goal    │                 │
├───────────────┴────────────────────────────────────────┴─────────────────┤
│ runtime / setup status                                                    │
└─────────────────────────────────────────────────────────────────────┘
```

| module | responsibility |
|---|---|
| `desktop/qt_app.py` + `desktop/qml/` | primary Qt Quick shell and typed bridge to the application context |
| `desktop/app.py` | Tk fallback shell: header, sidebar, status bar, toasts, run dialog, keys |
| `desktop/theme.py` | palette, font ladder, ttk styling, DPI awareness |
| `desktop/widgets.py` | `Card`, `StatTile`, `Badge`, `ScrollFrame`, `CodeView`, `Sparkline`, `MeterBar`, `NavButton` |
| `desktop/context.py` | `AppContext` — the model behind the views (snapshot cache, runs, strategies, doctor, browser server) |
| `desktop/worker.py` | one background thread; completions delivered on the UI thread |
| `desktop/views/` | one module per page |

---

## Legacy Tk threading model

Tk is single-threaded, and model calls take seconds. Three rules, enforced by
structure rather than discipline:

1. **All widget work happens on the Tk thread.** Views never call the runtime
   directly for anything slow; they hand a callable to `Worker.submit()`.
2. **`Worker` is serial.** One long job at a time (a run, an index, a provider
   probe). A second request waits — the alternative is two runs competing for the
   same SQLite writer and the same token budget.
3. **Callbacks travel with the result.** The worker pushes
   `(job, value, error, traceback)` into a queue; a `root.after` timer drains it.
   No global registry, no locks in view code.

The chat view is the one place with its own pump: the event stream is produced on
a dedicated thread and drained by a separate `after` loop, so streaming a run never
blocks a dashboard refresh.

---

## Opening without credentials

`AppContext.create()` builds the runtime with `require_provider=False`, which
constructs everything except the model client. The window then shows
`read-only` and:

- dashboards, runs, diffs, the index, the doctor and every `/command` work;
- *new run* explains exactly what is missing instead of failing silently;
- the browser panel still starts (its chat box degrades the same way).

A missing key is a **degraded mode**, not a crash — and never a fallback account.

---

## Legacy Tk theming

`theme.Palette` holds every colour; `theme.Fonts` resolves the type ladder against
the fonts actually installed (Segoe UI / Cascadia Mono → fallbacks). Two
consequences worth knowing:

- Tk has no rounded corners or CSS, so `Card` fakes the panel look with an outer
  frame painted in the border colour and an inner frame inset by one pixel.
- `tk.Scrollbar` on Windows is drawn by the native element and **ignores** `bg` and
  `troughcolor` (it renders as a white bar in a dark window). Every scrolling
  surface therefore uses `widgets.dark_scrollbar()`, a ttk scrollbar with the
  `Dark.Vertical.TScrollbar` style.

`scripts/make_icon.py` regenerates `assets/gigaforge.ico` from the same palette, so
the executable icon cannot drift from the UI.

---

## Extending the Tk fallback

Add a page:

```python
# gigaforge/desktop/views/mypage.py
from .base import View

class MyView(View):
    name = "mypage"
    title = "My page"
    glyph = "★"

    def build(self) -> None:      # create widgets once
        ...

    def refresh(self) -> None:    # re-render from self.ctx.snapshot()
        ...
```

Then append the class to `_VIEWS` in `desktop/app.py` (or `VIEW_CLASSES` in
`desktop/views/__init__.py`). It appears in the sidebar, gets a `ctrl+N` binding and
a status-bar hint automatically.

Slow work always goes through the worker:

```python
self.app.worker.submit(
    "my job",
    lambda: do_the_thing(),
    on_done=lambda value: self._render(value),
    on_error=lambda exc: self.toast(f"failed: {exc}", "err"),
)
```

