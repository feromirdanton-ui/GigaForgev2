"""Concrete Firewall compressors for the most common tool outputs.

Importing this package MUST NOT shadow the stdlib / 3rd-party ``pytest`` runner.
For that reason the pytest-output compressor lives in ``pytest_out`` (note the
``_out`` suffix) and is imported as
``from gigaforge.firewall.compressors.pytest_out import compress``.
"""
from __future__ import annotations

from gigaforge.firewall.compressors.builderr_out import BuildErrCompressor
from gigaforge.firewall.compressors.gitdiff_out import GitDiffCompressor
from gigaforge.firewall.compressors.grep_out import GrepCompressor
from gigaforge.firewall.compressors.pytest_out import PytestCompressor

__all__ = [
    "PytestCompressor",
    "GrepCompressor",
    "GitDiffCompressor",
    "BuildErrCompressor",
]
