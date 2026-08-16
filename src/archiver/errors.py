"""Domain-specific exceptions exposed by Archiver."""


class InvalidCatalogError(Exception):
    """Raised when a catalog database is missing, invalid, or unsupported."""


class ScanFailure(Exception):
    """Raised when a directory scan cannot complete successfully."""
