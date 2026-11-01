"""Explicit offline serving only; importing this package selects no runtime source."""

from app.serving.contract import FactReadStatus, OfflineReadPolicy, ServingError
from app.serving.snapshot import SnapshotHandle

__all__ = ["FactReadStatus", "OfflineReadPolicy", "ServingError", "SnapshotHandle"]
