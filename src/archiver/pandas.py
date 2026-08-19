"""Pandas adapters for Archiver's typed catalog query results."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .models import FileObservation, HistoricalObservation, ScanRun

_CURRENT_FILES_COLUMNS = (
    "location_id",
    "root_path",
    "relative_path",
    "algorithm",
    "digest",
    "size_bytes",
    "mtime_ns",
)

_DUPLICATE_GROUPS_COLUMNS = (
    "group_id",
    "group_size",
    *_CURRENT_FILES_COLUMNS,
)

_OBSERVATION_HISTORY_COLUMNS = (
    "scan_id",
    "scan_status",
    "scan_started_at_ns",
    "scan_completed_at_ns",
    *_CURRENT_FILES_COLUMNS,
)

_SCAN_HISTORY_COLUMNS = (
    "scan_id",
    "scan_status",
    "scan_started_at_ns",
    "scan_completed_at_ns",
    "location_id",
    "root_path",
)


def current_files_frame(observations: Iterable[FileObservation]) -> pd.DataFrame:
    """Materialize current file observations as a flat pandas DataFrame."""
    records = [
        {
            "location_id": observation.location.id,
            "root_path": str(observation.location.root),
            "relative_path": observation.relative_path.as_posix(),
            "algorithm": observation.content_id.algorithm,
            "digest": observation.content_id.digest,
            "size_bytes": observation.size_bytes,
            "mtime_ns": observation.mtime_ns,
        }
        for observation in observations
    ]
    return pd.DataFrame(records, columns=_CURRENT_FILES_COLUMNS)


def duplicate_groups_frame(groups: Iterable[tuple[FileObservation, ...]]) -> pd.DataFrame:
    """Materialize duplicate groups as a flat pandas DataFrame."""
    records = [
        {
            "group_id": group_id,
            "group_size": len(group),
            "location_id": observation.location.id,
            "root_path": str(observation.location.root),
            "relative_path": observation.relative_path.as_posix(),
            "algorithm": observation.content_id.algorithm,
            "digest": observation.content_id.digest,
            "size_bytes": observation.size_bytes,
            "mtime_ns": observation.mtime_ns,
        }
        for group_id, group in enumerate(groups, start=1)
        for observation in group
    ]
    return pd.DataFrame(records, columns=_DUPLICATE_GROUPS_COLUMNS)


def observation_history_frame(observations: Iterable[HistoricalObservation]) -> pd.DataFrame:
    """Materialize historical observations as a flat pandas DataFrame."""
    records = [
        {
            "scan_id": observation.scan.id,
            "scan_status": observation.scan.status,
            "scan_started_at_ns": observation.scan.started_at_ns,
            "scan_completed_at_ns": observation.scan.completed_at_ns,
            "location_id": observation.scan.location.id,
            "root_path": str(observation.scan.location.root),
            "relative_path": observation.relative_path.as_posix(),
            "algorithm": observation.content_id.algorithm,
            "digest": observation.content_id.digest,
            "size_bytes": observation.size_bytes,
            "mtime_ns": observation.mtime_ns,
        }
        for observation in observations
    ]
    return pd.DataFrame(records, columns=_OBSERVATION_HISTORY_COLUMNS)


def scan_history_frame(scans: Iterable[ScanRun]) -> pd.DataFrame:
    """Materialize scan runs as a flat pandas DataFrame."""
    records = [
        {
            "scan_id": scan.id,
            "scan_status": scan.status,
            "scan_started_at_ns": scan.started_at_ns,
            "scan_completed_at_ns": scan.completed_at_ns,
            "location_id": scan.location.id,
            "root_path": str(scan.location.root),
        }
        for scan in scans
    ]
    return pd.DataFrame(records, columns=_SCAN_HISTORY_COLUMNS)
