"""Sticky evaluation-only uncaptured-read evidence, independent of app fallbacks."""

from threading import Lock

_lock = Lock()
_failures = []


def uncaptured_read(kind):
    with _lock:
        _failures.append({"code": "uncaptured_repository_read", "repository": kind})
    raise RuntimeError("Uncaptured repository read: no live warehouse fallback")


def failures():
    with _lock:
        return list(_failures)
