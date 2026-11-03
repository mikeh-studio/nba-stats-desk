"""Bounded content-addressed local evidence shared by PDF and correction ledgers."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .identity import require

REPORTS = Path(__file__).resolve().parents[3] / "reports"


def retain(body, receipt, directory, suffix):
    directory = Path(directory)
    require(
        not directory.is_symlink()
        and directory.resolve().is_relative_to(REPORTS.resolve()),
        "Evidence directory must resolve inside private reports",
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory.resolve() / (receipt["sha256"] + suffix)
    require(
        not path.is_symlink() and path.resolve().is_relative_to(REPORTS.resolve()),
        "Evidence destination must not be a symlink or escape reports",
    )
    require(
        hashlib.sha256(body).hexdigest() == receipt["sha256"],
        "Retained source hash differs",
    )
    if path.exists():
        require(
            path.read_bytes() == body,
            "Existing content-addressed artifact differs; never overwrite",
        )
    else:
        with path.open("xb") as stream:
            stream.write(body)
        path.chmod(0o600)
    return str(path)


def artifact_bytes(path, receipt, maximum):
    """Read exact retained evidence once, after resolving containment and name."""
    path = Path(path).resolve()
    require(
        path.is_relative_to(REPORTS.resolve()) and path.stem == receipt["sha256"],
        "Evidence reference must be a private content-addressed artifact",
    )
    with path.open("rb") as stream:
        body = stream.read(maximum + 1)
    require(
        0 < len(body) <= maximum
        and hashlib.sha256(body).hexdigest() == receipt["sha256"],
        "Retained artifact size/hash differs",
    )
    return body


def document_bytes(attempt):
    inline, path = attempt["pdf_base64"], attempt.get("pdf_path")
    require(
        (inline is None) != (path is None),
        "Provide exactly one retained PDF representation",
    )
    if path is not None:
        return artifact_bytes(path, attempt["receipt"], 10 * 1024 * 1024)
    body = base64.b64decode(inline, validate=True)
    require(
        0 < len(body) <= 10 * 1024 * 1024
        and hashlib.sha256(body).hexdigest() == attempt["receipt"]["sha256"],
        "PDF original bytes/hash differ",
    )
    return body


def source_text(pbp):
    inline, path = pbp["source_body"], pbp.get("source_path")
    require(
        (inline is None) != (path is None),
        "Provide exactly one retained source representation",
    )
    body = (
        artifact_bytes(path, pbp["receipt"], 8 * 1024 * 1024)
        if path
        else inline.encode()
    )
    require(
        0 < len(body) <= 8 * 1024 * 1024
        and hashlib.sha256(body).hexdigest() == pbp["receipt"]["sha256"],
        "Source original receipt/hash differs",
    )
    return body.decode("utf-8")
