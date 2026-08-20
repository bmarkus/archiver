"""Domain-specific exceptions exposed by Archiver."""


class InvalidCatalogError(Exception):
    """Raised when a catalog database is missing, invalid, or unsupported."""


class ScanFailure(Exception):
    """Raised when a directory scan cannot complete successfully."""


class RefreshFailure(ScanFailure):
    """Raised when filesystem reconciliation or refresh application fails."""


class StaleRefreshError(RefreshFailure):
    """Raised when a change set no longer matches the catalog-current baseline."""
