"""Stopping a run on request, in every thread it started.

A run has one stop signal, a `threading.Event` that `LLMClient.stop` holds
and any thread may set, to request a stop. Whatever would start paid work, or
wait before it, consults the signal first: the pipeline's scheduling,
`RetryPolicy`, the client's escalation, `RateLimiter` and the embedding loop.
Their sleeps are waits on it, so a stop ends them at once.

A request already sent is not cancelled. It is paid for, so its answer is
cached and used like any other. Work a stop refuses raises `Interrupted`,
which is never retried and never counted as a failure: the work stays
pending, and the next run picks it up. It is defined in `devdocs.errors`, with
every other exit code, and re-exported here.
"""

from __future__ import annotations

import signal
import sys

from devdocs.errors import Interrupted

__all__ = ["Interrupted", "leave_signals"]


def leave_signals() -> None:
    """Block SIGINT and SIGTERM in the calling thread, one a run started, so
    that the kernel delivers them to the main thread: Python runs a handler
    only there, and a signal that thread does not receive leaves its wait
    asleep. Windows has no signal masks."""
    if sys.platform != "win32":
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
