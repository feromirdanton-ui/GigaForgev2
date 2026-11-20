# Examples

Two tiny repositories the harness can be pointed at.  They are fixtures for
`gigaforge bench` and for trying the application against a real (small) tree.

| path | what it is |
|---|---|
| `toy_repo/` | a Python module with a deliberately thin function set — the target of `bench` |
| `mini_site/` | a single-file site builder with a task description in `task.txt` |

## Try the desktop window on one of them

```bash
gigaforge desktop --repo examples/toy_repo
```

The window opens on that repository: the Dashboard shows its runs, **Index** shows
its symbols, **Diff** shows what changed.  Nothing is written until you press
*new run*.

## Try a headless run

```bash
gigaforge run "add a clamp() helper to math_utils" --repo examples/toy_repo --dry-run
```

`--dry-run` runs the whole DAG (index → slice → plan → act → validate → review →
settle) but does not write the diff, so it is safe to run anywhere.

`examples/mini_site/build.py` is a normal Python file: the goal in `task.txt` is
what a earlier dogfooding session used, and it still reads well as a prompt.
