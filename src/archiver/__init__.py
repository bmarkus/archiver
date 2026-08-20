"""Content-oriented file cataloging."""

from .catalog import Catalog
from .errors import InvalidCatalogError, RefreshFailure, ScanFailure, StaleRefreshError
from .models import (
    ContentId,
    CurrentFileSearch,
    CurrentFileSort,
    DuplicateSummary,
    FileObservation,
    HistoricalObservation,
    Location,
    RefreshChange,
    RefreshChangeKind,
    RefreshChangeSet,
    RefreshSummary,
    ScanProgress,
    ScanRun,
    ScanSummary,
)

__all__ = [
    "Catalog",
    "ContentId",
    "CurrentFileSearch",
    "CurrentFileSort",
    "DuplicateSummary",
    "FileObservation",
    "HistoricalObservation",
    "InvalidCatalogError",
    "Location",
    "RefreshChange",
    "RefreshChangeKind",
    "RefreshChangeSet",
    "RefreshFailure",
    "RefreshSummary",
    "StaleRefreshError",
    "ScanFailure",
    "ScanProgress",
    "ScanRun",
    "ScanSummary",
]
