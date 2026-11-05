"""Fingerprint the source files of the app candidate before launch."""

from pathlib import Path

from app.serving.contract import content_digest

SUFFIXES = {".py", ".html", ".js", ".css"}
MAX_BYTES = 40 * 1024 * 1024


def app_code_id(root):
    import hashlib

    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("invalid application source root")
    members = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in application source")
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_BYTES:
            raise ValueError("application source exceeds limit")
        members.append(
            [path.relative_to(root).as_posix(), hashlib.sha256(raw).hexdigest()]
        )
    if not members:
        raise ValueError("empty application source")
    return content_digest({"schema": "weekly-app-code/1", "files": members})
