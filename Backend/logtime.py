"""Date and time at the start of the app's own log lines ([error ...], [purge], [chat], [live],
[schema] ...), in the same form as uvicorn's lines (log_config.json): 2026-11-18 00:57:03 [error ...]

The time is the container's local time: Philippine time, from TZ in docker-compose.yml.
Imported first by auth.py, so it covers every module."""
import builtins
import sys
import time

_print = builtins.print


def _stamped(*args, **kwargs):
    if kwargs.get("file") in (None, sys.stdout, sys.stderr):
        _print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, **kwargs)
    else:  # writing to a file of its own: leave it as it is
        _print(*args, **kwargs)


if not getattr(builtins.print, "_stamped", False):
    _stamped._stamped = True
    builtins.print = _stamped
