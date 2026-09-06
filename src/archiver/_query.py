"""Backend-independent internal query descriptions."""

from dataclasses import dataclass, field

from .models import Location
from .query import (
    PathGlobPredicate,
    SortExpression,
    _validate_limit,
    _validate_path_glob_predicate,
    _validate_sort_expression,
    path,
)


@dataclass(frozen=True, slots=True)
class CurrentObservationQuery:
    """Current observations for one persisted location."""

    location: Location
    predicates: tuple[PathGlobPredicate, ...] = ()
    ordering: SortExpression = field(default_factory=path.asc)
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.location.id is None:
            raise ValueError("a current-observation query requires a persisted location")
        if not isinstance(self.predicates, tuple):
            raise TypeError("current-observation predicates must be a tuple")
        for predicate in self.predicates:
            _validate_path_glob_predicate(predicate)
        _validate_sort_expression(self.ordering)
        if self.limit is not None:
            _validate_limit(self.limit)
