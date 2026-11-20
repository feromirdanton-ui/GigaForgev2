from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

# Git configuration injected by the desktop host points outside this checkout;
# remove only those per-process overrides so isolated test repositories work.
for name in list(os.environ):
    if name == "GIT_CONFIG_COUNT" or name.startswith("GIT_CONFIG_KEY_") or name.startswith("GIT_CONFIG_VALUE_"):
        os.environ.pop(name, None)

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

from gigaforge.core.config import CoreConfig
from gigaforge.runtime import build_runtime

BUDGET = 8000
mode = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
if mode not in {"single", "subagents"}:
    raise SystemExit("usage: python run_live.py single|subagents")

repo = (HERE / ("tetris_single" if mode == "single" else "tetris_subagents")).resolve()
prompt = (HERE / "TASK_PROMPT.txt").read_text(encoding="utf-8")
report_dir = HERE / "reports"
report_dir.mkdir(parents=True, exist_ok=True)
report_path = report_dir / f"{mode}.json"
# The desktop host creates these scratch repos under a different Windows SID.
# Trust only the two test repos and worktrees nested beneath live_runs.
safe_roots = [str(repo).replace("\\", "/"), str(HERE).replace("\\", "/") + "/*"]
os.environ["GIT_CONFIG_COUNT"] = str(len(safe_roots))
for index, safe_root in enumerate(safe_roots):
    os.environ[f"GIT_CONFIG_KEY_{index}"] = "safe.directory"
    os.environ[f"GIT_CONFIG_VALUE_{index}"] = safe_root
started = datetime.now(UTC)
started_perf = time.perf_counter()

def clean_error(exc: BaseException) -> str:
    value = f"{type(exc).__name__}: {exc}"
    try:
        from gigaforge.config import get_settings
        secret = get_settings().auth_key
        if secret:
            value = value.replace(secret, "[REDACTED]")
    except Exception:
        pass
    return value[:1200]

print(json.dumps({
    "event": "run_start",
    "mode": mode,
    "repo": str(repo),
    "started_utc": started.isoformat(),
    "budget_tokens_per_pipeline": BUDGET,
}, ensure_ascii=False), flush=True)

runtime = None
report: dict = {
    "mode": mode,
    "repo": str(repo),
    "started_utc": started.isoformat(),
    "budget_tokens_per_pipeline": BUDGET,
    "use_subagents": mode == "subagents",
}
try:
    config = CoreConfig.from_env()
    config = replace(
        config,
        repo_root=str(repo),
        db_path=str(repo / ".gigaforge" / "core.sqlite3"),
        trace_path=str(repo / ".gigaforge" / "traces" / "core.jsonl"),
        # Keep two isolated agents, but serialize their provider calls: the live
        # GigaChat account returned 429s when two child calls overlapped.
        subagents=replace(
            config.subagents,
            max_parallel=2 if mode == "single" else 1,
            keep_worktrees=False,
        ),
        fixloop=replace(config.fixloop, max_iterations=2),
    )
    runtime = build_runtime(require_provider=True, repo_root=repo, core_config=config)
    report["provider"] = runtime.provider_name
    report["tls_verify"] = bool(runtime.settings.verify_tls)
    report["auth_configured"] = bool(runtime.settings.auth_key)
    report["baseline_tree"] = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    trace_path = repo / ".gigaforge" / "traces" / "core.jsonl"
    trace_offset = trace_path.stat().st_size if trace_path.exists() else 0
    run_id = f"live-{mode}-{started.strftime('%Y%m%dT%H%M%S')}"
    print(json.dumps({
        "event": "provider_ready",
        "mode": mode,
        "provider": report["provider"],
        "tls_verify": report["tls_verify"],
        "use_worktrees": bool(runtime.core_config.subagents.use_worktrees),
        "subagent_parallelism": runtime.core_config.subagents.max_parallel,
    }, ensure_ascii=False), flush=True)
    result = runtime.run(
        prompt,
        run_id=run_id,
        budget_tokens=BUDGET,
        use_cache=False,
        dry_run=False,
        use_subagents=(mode == "subagents"),
        max_fix_iterations=2,
    )
    report.update({
        "run_id": result.run_id,
        "status": str(result.status),
        "accepted": bool(result.accepted),
        "changed_files": list(result.changed_files),
        "summary": str(result.summary or "")[:2500],
        "error": str(result.error or "")[:1200],
        "tokens_reported_by_parent": int(result.tokens),
        "calls_reported_by_parent": int(result.calls),
        "phase_usages_reported_by_parent": result.phase_usages,
        "pipeline_duration_ms": float(result.duration_ms),
        "final_diff_chars": len(str(result.final_diff or "")),
        "final_diff": str(result.final_diff or ""),
    })
    metrics = runtime.metrics
    metrics.flush()
    report["all_pipeline_llm_metrics"] = metrics.aggregate(since=started.strftime("%Y-%m-%dT%H:%M:%S"))
    report["trace_path"] = str(trace_path)
    events = []
    router_calls = []
    if trace_path.exists():
        appended = trace_path.read_bytes()[trace_offset:].decode("utf-8", errors="replace")
        for line in appended.splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            kind = str(item.get("event") or item.get("kind") or item.get("name") or "")
            if kind == "router.call":
                router_calls.append({
                    "run_id": item.get("run_id"),
                    "phase": item.get("phase"),
                    "model": item.get("model"),
                    "tokens": int(item.get("tokens") or 0),
                    "latency_ms": float(item.get("latency_ms") or 0),
                })
            if "subagent" in kind:
                events.append({
                    "event": kind,
                    "run_id": item.get("run_id"),
                    "files": item.get("files"),
                    "diffs": item.get("diffs"),
                    "specs": item.get("specs"),
                    "reason": item.get("reason"),
                    "error": str(item.get("error") or "")[:300],
                })
    report["subagent_trace_events"] = events
    report["trace_router_calls"] = router_calls
    report["trace_totals"] = {
        "calls": len(router_calls),
        "tokens": sum(call["tokens"] for call in router_calls),
        "latency_ms": round(sum(call["latency_ms"] for call in router_calls), 2),
    }
    file_stats = {}
    for rel in ("index.html", "game.js"):
        path = repo / rel
        if path.exists():
            data = path.read_bytes()
            file_stats[rel] = {"bytes": len(data), "lines": len(data.decode("utf-8", errors="replace").splitlines())}
    report["generated_file_stats"] = file_stats
    report["total_wall_seconds"] = time.perf_counter() - started_perf
    report["finished_utc"] = datetime.now(UTC).isoformat()
    print(json.dumps({
        "event": "run_finished",
        "mode": mode,
        "run_id": result.run_id,
        "status": report["status"],
        "accepted": report["accepted"],
        "changed_files": report["changed_files"],
        "tokens_reported_by_parent": report["tokens_reported_by_parent"],
        "calls_reported_by_parent": report["calls_reported_by_parent"],
        "all_pipeline_llm_metrics": report["all_pipeline_llm_metrics"],
        "trace_totals": report["trace_totals"],
        "total_wall_seconds": round(report["total_wall_seconds"], 2),
        "subagent_trace_events": events,
        "generated_file_stats": file_stats,
    }, ensure_ascii=False), flush=True)
except BaseException as exc:
    report["error"] = clean_error(exc)
    report["total_wall_seconds"] = time.perf_counter() - started_perf
    report["finished_utc"] = datetime.now(UTC).isoformat()
    print(json.dumps({
        "event": "run_failed",
        "mode": mode,
        "error": report["error"],
        "total_wall_seconds": round(report["total_wall_seconds"], 2),
    }, ensure_ascii=False), flush=True)
    raise
finally:
    if runtime is not None:
        try:
            runtime.aclose()
        except Exception:
            pass
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"event": "report_saved", "path": str(report_path)}, ensure_ascii=False), flush=True)
