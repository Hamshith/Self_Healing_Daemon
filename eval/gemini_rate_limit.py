"""Process-local pacing for Gemini requests made by evaluation tools."""
from __future__ import annotations

import os
import threading
import time

_REQUEST_INTERVAL_MARGIN_SECONDS = 0.1
_RATE_LIMIT_LOCK = threading.Lock()
_NEXT_REQUEST_TIME = 0.0


def wait_for_gemini_slot() -> None:
    """Pace each Gemini API attempt to the configured requests-per-minute cap."""
    global _NEXT_REQUEST_TIME

    try:
        requests_per_minute = int(os.getenv("EVAL_GEMINI_RPM", "15"))
    except ValueError as exc:
        raise ValueError("EVAL_GEMINI_RPM must be a positive integer.") from exc
    if requests_per_minute < 1:
        raise ValueError("EVAL_GEMINI_RPM must be a positive integer.")

    interval = 60.0 / requests_per_minute + _REQUEST_INTERVAL_MARGIN_SECONDS
    with _RATE_LIMIT_LOCK:
        delay = _NEXT_REQUEST_TIME - time.monotonic()
        if delay > 0:
            print(
                f"[eval/rate_limit] Waiting {delay:.2f}s "
                f"to stay within {requests_per_minute} Gemini requests/minute.",
                flush=True,
            )
            time.sleep(delay)
        _NEXT_REQUEST_TIME = time.monotonic() + interval
