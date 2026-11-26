"""Probe whether a SWE-bench instance can be graded in *this* sandbox.

**Why this exists.**  A full instance costs a model run, a clone, a test-patch
apply and two test runs.  Measured live: ``pytest-dev__pytest-7571`` burned all
of that and then reported ``environment_error`` because pytest 6.0-dev's
assertion rewriter calls ``ast`` in a way Python 3.11 rejects
(``required field "lineno" missing from alias``).  Nothing about that is the
model's fault, and nothing about it is discoverable from the dataset.

The probe answers the question *before* the model is asked: clone at
``base_commit``, install the repo, apply the maintainer's test patch, and run
the FAIL_TO_PASS tests.  A gradable instance must **run** them and see them
**fail** — that is what "FAIL_TO_PASS" means at the base commit.  If they error
on import, on syntax, or on a missing dependency, the instance is not gradable
here and should be excluded from the denominator rather than scored.

Usage::

    python scripts/probe_gradability.py --repo psf/requests --limit 3
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.bench.swebench import (  # noqa: E402
    _apply,
    _clone,
    _install_test_requirements,
    _looks_like_environment_failure,
    _make_venv,
    _pytest_node_ids,
    _run_tests,
    load_instances,
)

#: A test that fails on an assertion is a *gradable* failure.  A test that
#: cannot be collected is not.
_GRADABLE_FAILURE_MARKERS = ("assert", "AssertionError", "FAILED", "failed")


def probe(instance, work_root: Path) -> dict:
    dest = work_root / instance.instance_id.replace("/", "__")
    out = {"instance_id": instance.instance_id, "repo": instance.repo, "gradable": False,
           "reason": "", "f2p_total": len(instance.fail_to_pass)}
    ok, err = _clone(instance, dest)
    if not ok:
        out["reason"] = f"clone: {err}"
        return out
    try:
        python, venv_err = _make_venv(dest)
        if not python:
            out["reason"] = venv_err
            return out
        _install_test_requirements(dest, python)
        ok, note = _apply(instance.test_patch, dest)
        if not ok:
            out["reason"] = f"test patch: {note}"
            return out
        nodes = _pytest_node_ids(instance.fail_to_pass)
        passed, text = _run_tests(nodes, dest, timeout=600, python=python)
        if passed:
            # The named tests already pass at base_commit: the instance is
            # mis-specified or the environment is not the one it was built for.
            out["reason"] = "FAIL_TO_PASS already passes at base_commit"
            return out
        if _looks_like_environment_failure(text):
            out["reason"] = f"env: {text[-260:]}"
            return out
        out["gradable"] = True
        out["reason"] = "tests run and fail at base_commit"
        out["sample"] = text[-200:]
        return out
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(dest.parent / f"{dest.name}.venv", ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="/tmp/swe.parquet")
    ap.add_argument("--repo", default="")
    ap.add_argument("--instance", default="")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--work-root", default="/tmp/gigaforge_probe")
    args = ap.parse_args()

    instances = load_instances(
        args.dataset,
        limit=args.limit,
        repos=[args.repo] if args.repo else None,
        instance_ids=[args.instance] if args.instance else None,
    )
    root = Path(args.work_root)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for inst in instances:
        print(f"probing {inst.instance_id} ...", flush=True)
        row = probe(inst, root)
        rows.append(row)
        print(f"  gradable={row['gradable']}  {row['reason'][:160]}", flush=True)
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
