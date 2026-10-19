"""Create evaluation-owned private artifacts with restrictive initial permissions."""

import os
from pathlib import Path


def private_directory(path):
    path = Path(path)
    if not path.parent.exists():
        private_directory(path.parent)
    # Apply permissions at mkdir, never expose a permissive intermediate file.
    path.mkdir(mode=0o700)
    return path


def private_open(path, *, append=False):
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if append else os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "a" if append else "w", encoding="utf-8")


def private_write(path, content):
    with private_open(path) as stream:
        stream.write(content)
