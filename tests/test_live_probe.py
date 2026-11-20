"""Tests for the live probe — the parts that must work without a key.

The probe itself needs real credentials, so these tests cover what can be
verified offline: that it refuses to pretend, that it never writes a secret to
the report, and that its report shape is stable enough to diff between runs.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


def test_probe_reports_no_credentials_instead_of_failing(monkeypatch: Any) -> None:
    """Without a key the probe must say so, not raise and not fake a pass."""
    from gigaforge.harness.live_probe import run_probes

    for name in ("GIGACHAT_CREDENTIALS", "GIGACHAT_AUTH_KEY", "GF_CLIENT__AUTH_KEY"):
        monkeypatch.delenv(name, raising=False)

    report = asyncio.run(run_probes())
    assert report.credentials_present is False
    assert report.probes == []
    assert report.ok is False, "an empty report is not a pass"


def test_probe_exit_code_is_two_without_credentials(monkeypatch: Any, tmp_path: Any) -> None:
    from gigaforge.harness.live_probe import main

    for name in ("GIGACHAT_CREDENTIALS", "GIGACHAT_AUTH_KEY", "GF_CLIENT__AUTH_KEY"):
        monkeypatch.delenv(name, raising=False)

    code = main(["--out", str(tmp_path / "live.json")])
    assert code == 2


def test_report_redacts_credentials(monkeypatch: Any) -> None:
    """A server that echoes the key back must not get it into the report."""
    from gigaforge.harness.live_probe import ProbeResult, _redact

    secret = "super-secret-credential-value"
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", secret)

    assert _redact(f"auth failed for {secret}") == "auth failed for [REDACTED]"
    probe = ProbeResult(name="x", ok=False, error=f"boom {secret}")
    assert secret not in json.dumps(probe.to_dict())


def test_report_round_trips_through_json(tmp_path: Any) -> None:
    from gigaforge.harness.live_probe import LiveProbeReport, ProbeResult

    report = LiveProbeReport(model="GigaChat-2-Pro", credentials_present=True)
    report.probes.append(ProbeResult(name="atokens_count", ok=True, detail={"short": 1}))
    path = report.write(tmp_path / "live.json")

    payload = json.loads(path.read_text())
    assert payload["model"] == "GigaChat-2-Pro"
    assert payload["passed"] == 1
    assert payload["total"] == 1
    assert payload["ok"] is True


def test_report_is_not_ok_when_any_probe_fails() -> None:
    from gigaforge.harness.live_probe import LiveProbeReport, ProbeResult

    report = LiveProbeReport(credentials_present=True)
    report.probes.append(ProbeResult(name="a", ok=True))
    report.probes.append(ProbeResult(name="b", ok=False, error="nope"))
    assert report.ok is False


def test_probe_module_imports_without_a_key() -> None:
    """Importing the probe must never require credentials."""
    import importlib

    module = importlib.import_module("gigaforge.harness.live_probe")
    assert hasattr(module, "run_probes")
    assert hasattr(module, "main")


@pytest.mark.parametrize("name", ["GIGACHAT_CREDENTIALS", "GIGACHAT_AUTH_KEY"])
def test_credentials_present_detects_each_variable(monkeypatch: Any, name: str) -> None:
    from gigaforge.harness.live_probe import _credentials_present

    for other in ("GIGACHAT_CREDENTIALS", "GIGACHAT_AUTH_KEY", "GF_CLIENT__AUTH_KEY"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "value")
    assert _credentials_present() is True
