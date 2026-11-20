"""A retry must actually wait, and a sandboxed command must be allowlisted.

Both defects were found in the same live run, in the same log, and both are the
same mistake in different clothes: a value was taken at face value without
asking whether it could be true.

* GigaChat answers 429 with ``Retry-After: 0``.  Honouring it literally made
  every retry instantaneous, so the whole retry budget was spent inside the
  same throttled instant and the run failed on a limit a one-second wait would
  have cleared.
* ``python_bin()`` preferred ``sys.executable``, which is
  ``/usr/local/bin/python3.11``.  The sandbox resolves commands against an
  allowlist that contains ``python3`` but not ``python3.11``, so every REPL step
  was rejected while an allowed interpreter sat right there.
"""
from __future__ import annotations

from gigaforge.client.retry import RetryPolicy
from gigaforge.l0.toolchain import python_bin


def test_a_zero_retry_after_does_not_make_the_retry_instantaneous():
    """The measured defect: four retries, all in 0.00s, all throttled."""
    policy = RetryPolicy()
    delay = policy.compute_delay(1, retry_after=0.0)
    assert delay >= policy.min_delay, (
        "a server that says 'retry now' while refusing the request is not "
        "giving a hint; honouring it literally is a busy loop"
    )


def test_a_negative_retry_after_is_also_ignored():
    policy = RetryPolicy()
    assert policy.compute_delay(1, retry_after=-5.0) >= policy.min_delay


def test_a_positive_retry_after_is_still_honoured():
    """The fix must not throw away a real hint."""
    policy = RetryPolicy()
    assert policy.compute_delay(1, retry_after=7.0) == 7.0


def test_a_positive_retry_after_is_still_capped():
    policy = RetryPolicy(max_retry_after=30.0)
    assert policy.compute_delay(1, retry_after=600.0) == 30.0


def test_backoff_grows_and_never_returns_zero():
    policy = RetryPolicy(base=1.0, cap=30.0, jitter=False)
    delays = [policy.compute_delay(attempt) for attempt in range(1, 6)]
    assert delays == sorted(delays)
    assert all(d >= policy.min_delay for d in delays)


def test_python_bin_prefers_an_allowlisted_name():
    """``python3.11`` is not in the sandbox allowlist; ``python3`` is."""
    tool = python_bin()
    assert tool.found
    assert tool.argv_name in {"python", "python3"}, (
        f"resolved {tool.argv_name!r}, which a sandbox allowlist will reject"
    )
