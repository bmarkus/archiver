"""Backend-independent internal query descriptions."""

from dataclasses import dataclass

from .models import Location


@dataclass(frozen=True, slots=True)
class CurrentObservationQuery:
    """Current observations for one persisted location, ordered by relative path."""

    location: Location

    def __post_init__(self) -> None:
        if self.location.id is None:
            raise ValueError("a current-observation query requires a persisted location")
