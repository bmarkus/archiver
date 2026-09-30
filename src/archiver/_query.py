"""Backend-independent internal query descriptions."""

from dataclasses import dataclass, field

from .models import Location
from .query import (
    CurrentFilePredicate,
    SortExpression,
    _validate_current_file_predicate,
    _validate_current_file_sort_expression,
    _validate_duplicate_sort_expression,
    _validate_limit,
    path,
)


@dataclass(frozen=True, slots=True)
class CurrentObservationQuery:
    """Current observations for one persisted location."""

    location: Location
    predicates: tuple[CurrentFilePredicate, ...] = ()
    ordering: SortExpression = field(default_factory=path.asc)
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.location.id is None:
            raise ValueError("a current-observation query requires a persisted location")
        if not isinstance(self.predicates, tuple):
            raise TypeError("current-observation predicates must be a tuple")
        for predicate in self.predicates:
            _validate_current_file_predicate(predicate)
        _validate_current_file_sort_expression(self.ordering)
        if self.limit is not None:
            _validate_limit(self.limit)


@dataclass(frozen=True, slots=True)
class DuplicateGroupQueryDescription:
    """Duplicate groups from one resolved current scan."""

    location: Location
    scan_id: int
    ordering: SortExpression
    group_limit: int | None
    member_limit: int | None

    def __post_init__(self) -> None:
        if self.location.id is None:
            raise ValueError("a duplicate-group query requires a persisted location")
        if type(self.scan_id) is not int:
            raise TypeError("a duplicate-group query scan ID must be an integer")
        if self.scan_id < 1:
            raise ValueError("a duplicate-group query scan ID must be positive")
        _validate_duplicate_sort_expression(self.ordering)
        if self.group_limit is not None:
            _validate_limit(self.group_limit)
        if self.member_limit is not None:
            _validate_limit(self.member_limit)
