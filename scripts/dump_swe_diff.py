"""Diagnostic: run one SWE-bench instance and dump the model's diff verbatim.

The bench only reports "corrupt patch at line 9"; the diff itself is what
explains why.  This script keeps the clone and writes the diff next to it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.bench.swebench import _clone, load_instances  # noqa: E402


def main() -> int:
    dataset = sys.argv[1] if len(sys.argv) > 1 else "/tmp/swe.parquet"
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    instances = load_instances(dataset)
    inst = instances[idx]
    root = Path("/tmp/swe_dump")
    root.mkdir(parents=True, exist_ok=True)
    dest = root / inst.instance_id.replace("/", "__")
    ok, err = _clone(inst, dest)
    if not ok:
        print("clone failed:", err)
        return 1

    from gigaforge.client.client import GigaClient
    from gigaforge.client.config import ClientConfig
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline
    from gigaforge.core.sync_adapter import SyncChatClient
    from gigaforge.firewall import ContextFirewall
    from gigaforge.firewall.config import FirewallConfig
    from gigaforge.l0.config import L0Config
    from gigaforge.l0.indexer import Indexer
    from gigaforge.l0.store import L0Store

    cfg = CoreConfig(
        db_path=str(root / f"{dest.name}.db"),
        trace_path=str(root / f"{dest.name}.jsonl"),
        repo_root=str(dest),
    )
    l0 = L0Config.from_env(repo_root=str(dest), db_path=str(root / f"{dest.name}.l0.db"))
    store = L0Store(config=l0)
    Indexer(store, config=l0).index_full()
    firewall = ContextFirewall(store, config=FirewallConfig(repo_root=str(dest)))
    client = SyncChatClient(GigaClient(ClientConfig.from_env()))
    pipeline = TaskPipeline(config=cfg, client=client, firewall=firewall, indexer=Indexer(store, config=l0))
    try:
        run = pipeline.run(inst.problem_statement)
    finally:
        pipeline.close()
        store.close()

    diff = str(getattr(run, "final_diff", "") or "")
    out = root / f"{dest.name}.diff"
    out.write_text(diff, encoding="utf-8")
    print("status:", getattr(run, "status", "?"), "error:", getattr(run, "error", ""))
    print("diff chars:", len(diff), "->", out)
    print("=" * 70)
    print(diff[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
