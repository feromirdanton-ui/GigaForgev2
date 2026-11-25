from __future__ import annotations

from pathlib import Path

from gigaforge.core.goal import WorkspaceVerifier

GOAL = (
    "Add the public function hms_to_seconds(hms: str) -> int. "
    "It must accept only strict HH:MM:SS with two-digit minutes and seconds in 00..59."
)


def _check(tmp_path: Path) -> str:
    files = sorted(tmp_path.glob("*.py"))
    result = WorkspaceVerifier._python_api_check(files, GOAL)
    assert result is not None
    return result.summary


def test_rejects_misplaced_and_duplicate_requested_functions(tmp_path: Path) -> None:
    (tmp_path / "temperature.py").write_text(
        "def celsius_to_fahrenheit(value): return value\n"
        "def hms_to_seconds(value): return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "distance.py").write_text(
        "def kilometers_to_miles(value): return value\n",
        encoding="utf-8",
    )
    (tmp_path / "duration.py").write_text(
        "def seconds_to_hms(value): return str(value)\n"
        "def hms_to_seconds(value): return 0\n"
        "def hms_to_seconds(value): return 0\n",
        encoding="utf-8",
    )

    summary = _check(tmp_path)

    assert "defined more than once" in summary
    assert "belongs with the related API in duration.py" in summary


def test_rejects_non_strict_hms_width(tmp_path: Path) -> None:
    (tmp_path / "duration.py").write_text(
        "def seconds_to_hms(value): return str(value)\n"
        "def hms_to_seconds(hms):\n"
        "    parts = hms.split(':')\n"
        "    hours, minutes, seconds = map(int, parts)\n"
        "    if not (0 <= minutes < 60 and 0 <= seconds < 60): raise ValueError\n"
        "    return hours * 3600 + minutes * 60 + seconds\n",
        encoding="utf-8",
    )

    assert "does not enforce two-digit" in _check(tmp_path)


def test_accepts_a_unique_strict_hms_parser_in_its_related_module(tmp_path: Path) -> None:
    (tmp_path / "temperature.py").write_text(
        "def celsius_to_fahrenheit(value): return value\n",
        encoding="utf-8",
    )
    (tmp_path / "distance.py").write_text(
        "def kilometers_to_miles(value): return value\n",
        encoding="utf-8",
    )
    (tmp_path / "duration.py").write_text(
        "def seconds_to_hms(value): return str(value)\n"
        "def hms_to_seconds(hms):\n"
        "    if len(hms) != 8 or hms[2] != ':' or hms[5] != ':': raise ValueError\n"
        "    hours, minutes, seconds = int(hms[:2]), int(hms[3:5]), int(hms[6:8])\n"
        "    if not (0 <= minutes < 60 and 0 <= seconds < 60): raise ValueError\n"
        "    return hours * 3600 + minutes * 60 + seconds\n",
        encoding="utf-8",
    )

    assert _check(tmp_path) == "requested public functions are present and unique"
