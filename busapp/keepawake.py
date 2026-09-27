"""Stop Windows from idle-sleeping while the collector runs.

A sleeping laptop suspends the collector and silently loses observations
(this happened overnight on 2026-09-27). SetThreadExecutionState with
ES_SYSTEM_REQUIRED tells Windows the system is in use for as long as the
calling thread keeps the request. Closing the lid still sleeps the machine.
"""
import contextlib
import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_state(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0


@contextlib.contextmanager
def prevent_sleep():
    """Keep the system awake inside the `with` block (no-op off Windows)."""
    active = _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    if active:
        log.info("Idle sleep disabled while collecting (closing the lid still sleeps)")
    else:
        log.warning("Could not prevent system sleep; keep the machine awake manually")
    try:
        yield active
    finally:
        if active:
            _set_state(ES_CONTINUOUS)  # clear the request
