"""Typed public query grammar for current file observations."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

from .models import DuplicateGroupView, DuplicateSummary, FileObservation

if TYPE_CHECKING:
    from .catalog import Catalog


class _CurrentFileField(Enum):
    PATH = "path"
    SIZE = "size"
    MTIME = "mtime"


class _SortDirection(Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True, slots=True)
class PathGlobPredicate:
    """A case-sensitive SQLite GLOB predicate over a complete relative path.

    Create predicates with path.glob() rather than constructing this class
    directly.
    """

    field: _CurrentFileField
    pattern: str

    def __post_init__(self) -> None:
        _validate_path_glob_predicate(self)


@dataclass(frozen=True, slots=True)
class SortExpression:
    """A typed ordering over one supported current-file field.

    Create orderings with the documented path, size, and mtime field methods
    rather than by constructing this class directly.
    """

    field: _CurrentFileField
    direction: _SortDirection

    def __post_init__(self) -> None:
        _validate_sort_expression(self)


@dataclass(frozen=True, slots=True)
class _PathField:
    token: _CurrentFileField = field(default=_CurrentFileField.PATH, init=False)

    def glob(self, pattern: str) -> PathGlobPredicate:
        """Create a case-sensitive SQLite GLOB predicate for where().

        Pattern matches the complete POSIX relative path recorded for an
        observation. See docs/query-grammar.md for SQLite GLOB syntax and
        examples.
        """
        return PathGlobPredicate(field=self.token, pattern=pattern)

    def asc(self) -> SortExpression:
        """Create an ascending relative-path ordering for sort()."""
        return SortExpression(field=self.token, direction=_SortDirection.ASCENDING)

    def desc(self) -> SortExpression:
        """Create a descending relative-path ordering for sort()."""
        return SortExpression(field=self.token, direction=_SortDirection.DESCENDING)


@dataclass(frozen=True, slots=True)
class _SortableField:
    token: _CurrentFileField

    def asc(self) -> SortExpression:
        """Create an ascending ordering for sort()."""
        return SortExpression(field=self.token, direction=_SortDirection.ASCENDING)

    def desc(self) -> SortExpression:
        """Create a descending ordering for sort()."""
        return SortExpression(field=self.token, direction=_SortDirection.DESCENDING)


path: Final[_PathField] = _PathField()
size: Final[_SortableField] = _SortableField(_CurrentFileField.SIZE)
mtime: Final[_SortableField] = _SortableField(_CurrentFileField.MTIME)


@dataclass(frozen=True, slots=True)
class CurrentFileQuery:
    """An immutable, lazy, bounded query for one root's current observations.

    Start with Catalog.files(). Build the query with where(), sort(), and
    limit(), then materialize its rows with fetch(). See
    docs/query-grammar.md for the public grammar.
    """

    root: Path
    predicates: tuple[PathGlobPredicate, ...] = ()
    ordering: SortExpression = field(default_factory=path.asc)
    row_limit: int = 20
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("a current-file query requires a canonical absolute root")
        if not isinstance(self.predicates, tuple):
            raise TypeError("current-file query predicates must be a tuple")
        for predicate in self.predicates:
            _validate_path_glob_predicate(predicate)
        _validate_sort_expression(self.ordering)
        _validate_limit(self.row_limit)

    def where(self, predicate: PathGlobPredicate) -> Self:
        """Return a query with an additional path.glob() predicate.

        Predicate must be created by path.glob(). Repeated calls are combined
        with logical AND. This method returns a new query and leaves this query
        unchanged.
        """
        _validate_path_glob_predicate(predicate)
        return replace(self, predicates=(*self.predicates, predicate))

    def sort(self, ordering: SortExpression) -> Self:
        """Return a query with a replacement ordering.

        Ordering must be created by one of the public path, size, or mtime
        field methods. Equal sizes or modification times are ordered by
        ascending relative path. This method returns a new query and leaves
        this query unchanged.
        """
        _validate_sort_expression(ordering)
        return replace(self, ordering=ordering)

    def limit(self, count: int) -> Self:
        """Return a query with a replacement positive row bound.

        Count must be an int of at least one. New queries default to 20 rows.
        This method returns a new query and leaves this query unchanged; fetch()
        takes no limit argument.
        """
        _validate_limit(count)
        return replace(self, row_limit=count)

    def fetch(self) -> tuple[FileObservation, ...]:
        """Materialize this query from the root's latest successful scan.

        Results are a tuple of at most this query's bound. A valid root with no
        persisted location or successful scan returns an empty tuple.
        """
        return self._catalog._fetch_current_file_query(self)

    def first(self) -> FileObservation | None:
        """Return the first matching observation under this query's ordering."""
        return self._catalog._first_current_file_query(self)

    def count(self) -> int:
        """Return the complete number of matching current observations.

        This reduction ignores the query's display limit.
        """
        return self._catalog._count_current_file_query(self)

    def total_size_bytes(self) -> int:
        """Return the complete logical size of matching current observations.

        This reduction ignores the query's display limit.
        """
        return self._catalog._total_size_current_file_query(self)

    def stream(self) -> Iterator[FileObservation]:
        """Yield at most this query's bound in deterministic order."""
        return self._catalog._stream_current_file_query(self, limit=self.row_limit)

    def stream_all(self) -> Iterator[FileObservation]:
        """Yield every matching observation in deterministic order.

        This is deliberately the explicit unbounded terminal; use fetch() or
        stream() for bounded consumption.
        """
        return self._catalog._stream_current_file_query(self, limit=None)


@dataclass(frozen=True, slots=True)
class DuplicateGroupQuery:
    """An immutable, lazy, bounded query for current duplicate groups."""

    root: Path
    group_limit: int = 20
    member_row_limit: int = 20
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("a duplicate-group query requires a canonical absolute root")
        _validate_limit(self.group_limit)
        _validate_limit(self.member_row_limit)

    def limit(self, count: int) -> Self:
        """Return a query with a replacement positive group bound."""
        _validate_limit(count)
        return replace(self, group_limit=count)

    def member_limit(self, count: int) -> Self:
        """Return a query with a replacement positive member bound per group."""
        _validate_limit(count)
        return replace(self, member_row_limit=count)

    def fetch(self) -> tuple[DuplicateGroupView, ...]:
        """Materialize bounded duplicate groups from one resolved current scan."""
        return self._catalog._fetch_duplicate_group_query(self)

    def summary(self) -> DuplicateSummary:
        """Return complete duplicate metrics independent of display bounds."""
        return self._catalog._summarize_duplicate_group_query(self)


def _validate_path_glob_predicate(predicate: object) -> None:
    if type(predicate) is not PathGlobPredicate:
        raise TypeError("where() requires a path GLOB predicate")
    if predicate.field is not _CurrentFileField.PATH:
        raise ValueError("a path GLOB predicate requires the path field")
    if not isinstance(predicate.pattern, str):
        raise TypeError("a path GLOB pattern must be a string")


def _validate_sort_expression(ordering: object) -> None:
    if type(ordering) is not SortExpression:
        raise TypeError("sort() requires a current-file sort expression")
    if not isinstance(ordering.field, _CurrentFileField):
        raise ValueError("unsupported current-file sort field")
    if not isinstance(ordering.direction, _SortDirection):
        raise ValueError("unsupported current-file sort direction")


def _validate_limit(count: object) -> None:
    if type(count) is not int:
        raise TypeError("limit must be an integer")
    if count < 1:
        raise ValueError("limit must be at least 1")


__all__ = [
    "CurrentFileQuery",
    "DuplicateGroupQuery",
    "PathGlobPredicate",
    "SortExpression",
    "mtime",
    "path",
    "size",
]
