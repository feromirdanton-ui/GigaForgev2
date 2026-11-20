"""Toy math utilities for the GigaForge end-to-end pipeline test.

This module is the target of the M4c e2e test. The pipeline is asked to fix
``validate_email`` so it accepts standard emails like ``"x@y.z"``. The buggy
version below rejects that input; the e2e test's FakeGigaClient returns a
canned diff that fixes the validation logic.
"""

import re


def add(a: int, b: int) -> int:
    """Add two integers. (Already correct; tests for this should pass.)"""
    return a + b


# BUGGY version: rejects standard emails like "x@y.z" because it requires
# a two-char TLD minimum. The pipeline's fix-loop must replace the body so
# that "x@y.z" is accepted.
_EMAIL_RE = re.compile(r"^[a-z0-9]+@[a-z0-9]+\.[a-z]{2,}$")


def validate_email(email: str) -> bool:
    """Return True iff *email* looks like a standard address.

    The current implementation is buggy: it rejects short TLDs like ``.z``.
    The pipeline (or a developer) should relax the TLD rule to accept any
    non-empty alphabetic TLD so standard addresses like ``x@y.z`` pass.
    """
    return bool(_EMAIL_RE.match(email))


def register_user(email: str) -> str:
    """Register a new user. Calls :func:`validate_email` as a guard."""
    if not validate_email(email):
        raise ValueError(f"invalid email: {email!r}")
    return f"registered:{email}"
