"""Typed public query grammar for current file observations."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, TypeAlias

from .models import (
    ContentId,
    ContentRecord,
    ContentTagView,
    CurrentFileTagView,
    DuplicateGroupView,
    DuplicateSummary,
    FileObservation,
    Location,
    TagProvenanceKind,
    TagUsage,
    validate_tag_name,
)

if TYPE_CHECKING:
    from .catalog import Catalog


class _CurrentFileField(Enum):
    PATH = "path"
    SIZE = "size"
    MTIME = "mtime"
    DUPLICATE_FILE_COUNT = "duplicate_file_count"
    DUPLICATE_REDUNDANT_BYTES = "duplicate_redundant_bytes"
    TAG_NAME = "tag_name"
    TAG_CONTENT_COUNT = "tag_content_count"
    TAG_ASSERTION_COUNT = "tag_assertion_count"


class _SortDirection(Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class _TagMatchMode(Enum):
    ALL = "all"
    ANY = "any"


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
class ContentPredicate:
    """An exact full-content-identity predicate created by content.is_()."""

    content_id: ContentId

    def __post_init__(self) -> None:
        _validate_content_predicate(self)


@dataclass(frozen=True, slots=True)
class TagPredicate:
    """An active-tag predicate created by tags.all_of() or tags.any_of()."""

    names: tuple[str, ...]
    provenance: TagProvenanceKind | None
    match: _TagMatchMode

    def __post_init__(self) -> None:
        _validate_tag_predicate(self)


@dataclass(frozen=True, slots=True)
class TagNameRegexPredicate:
    """A Python regular-expression predicate over canonical tag names."""

    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("a tag-name regular expression must be a string")
        re.compile(self.source)


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


@dataclass(frozen=True, slots=True)
class _ContentField:
    def is_(self, content_id: ContentId) -> ContentPredicate:
        """Match one exact full content identity."""
        return ContentPredicate(content_id=content_id)


@dataclass(frozen=True, slots=True)
class _TagsField:
    def all_of(self, *names: str, provenance: TagProvenanceKind | None = None) -> TagPredicate:
        """Match content having every requested active tag."""
        return _make_tag_predicate(names, provenance, _TagMatchMode.ALL)

    def any_of(self, *names: str, provenance: TagProvenanceKind | None = None) -> TagPredicate:
        """Match content having at least one requested active tag."""
        return _make_tag_predicate(names, provenance, _TagMatchMode.ANY)


@dataclass(frozen=True, slots=True)
class _TagNameField:
    def regex(self, source: str) -> TagNameRegexPredicate:
        return TagNameRegexPredicate(source)

    def asc(self) -> SortExpression:
        return SortExpression(_CurrentFileField.TAG_NAME, _SortDirection.ASCENDING)

    def desc(self) -> SortExpression:
        return SortExpression(_CurrentFileField.TAG_NAME, _SortDirection.DESCENDING)


@dataclass(frozen=True, slots=True)
class _TagUsageFields:
    content_count: _SortableField = field(default_factory=lambda: _SortableField(_CurrentFileField.TAG_CONTENT_COUNT))
    assertion_count: _SortableField = field(
        default_factory=lambda: _SortableField(_CurrentFileField.TAG_ASSERTION_COUNT)
    )


@dataclass(frozen=True, slots=True)
class _DuplicateFields:
    file_count: _SortableField = field(default_factory=lambda: _SortableField(_CurrentFileField.DUPLICATE_FILE_COUNT))
    redundant_bytes: _SortableField = field(
        default_factory=lambda: _SortableField(_CurrentFileField.DUPLICATE_REDUNDANT_BYTES)
    )


path: Final[_PathField] = _PathField()
size: Final[_SortableField] = _SortableField(_CurrentFileField.SIZE)
mtime: Final[_SortableField] = _SortableField(_CurrentFileField.MTIME)
content: Final[_ContentField] = _ContentField()
tags: Final[_TagsField] = _TagsField()
duplicate: Final[_DuplicateFields] = _DuplicateFields()
tag_name: Final[_TagNameField] = _TagNameField()
tag_usage: Final[_TagUsageFields] = _TagUsageFields()

CurrentFilePredicate: TypeAlias = PathGlobPredicate | ContentPredicate | TagPredicate
ContentQueryPredicate: TypeAlias = ContentPredicate | TagPredicate


@dataclass(frozen=True, slots=True)
class CurrentFileQuery:
    """An immutable, lazy, bounded query for one root's current observations.

    Start with Catalog.files(). Build the query with where(), sort(), and
    limit(), then materialize its rows with fetch(). See
    docs/query-grammar.md for the public grammar.
    """

    root: Path
    predicates: tuple[CurrentFilePredicate, ...] = ()
    ordering: SortExpression = field(default_factory=path.asc)
    row_limit: int = 20
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("a current-file query requires a canonical absolute root")
        if not isinstance(self.predicates, tuple):
            raise TypeError("current-file query predicates must be a tuple")
        for predicate in self.predicates:
            _validate_current_file_predicate(predicate)
        _validate_current_file_sort_expression(self.ordering)
        _validate_limit(self.row_limit)

    def where(self, predicate: CurrentFilePredicate) -> Self:
        """Return a query with an additional current-file predicate.

        Predicates are created by path.glob(), content.is_(), tags.all_of(), or
        tags.any_of(). Repeated calls are combined with logical AND. This
        method returns a new query and leaves this query unchanged.
        """
        _validate_current_file_predicate(predicate)
        return replace(self, predicates=(*self.predicates, predicate))

    def sort(self, ordering: SortExpression) -> Self:
        """Return a query with a replacement ordering.

        Ordering must be created by one of the public path, size, or mtime
        field methods. Equal sizes or modification times are ordered by
        ascending relative path. This method returns a new query and leaves
        this query unchanged.
        """
        _validate_current_file_sort_expression(ordering)
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

    def distinct_content_count(self) -> int:
        """Return the complete number of matching content identities.

        This reduction ignores the query's display limit.
        """
        return self._catalog._distinct_content_count_current_file_query(self)

    def with_tags(self, *, limit: int = 3, all_tags: bool = False) -> CurrentFileTagQuery:
        """Add active-tag counts and bounded alphabetical previews to rows."""
        _validate_limit(limit)
        _validate_all_tags(all_tags)
        return CurrentFileTagQuery(base=self, tag_limit=limit, all_tags=all_tags)

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
class CurrentFileTagQuery:
    """A typed detail view over a bounded current-file query."""

    base: CurrentFileQuery
    tag_limit: int = 3
    all_tags: bool = False

    def __post_init__(self) -> None:
        if type(self.base) is not CurrentFileQuery:
            raise TypeError("a current-file tag view requires a CurrentFileQuery")
        _validate_limit(self.tag_limit)
        _validate_all_tags(self.all_tags)

    def fetch(self) -> tuple[CurrentFileTagView, ...]:
        """Materialize bounded observations and their active tag previews."""
        return self.base._catalog._fetch_current_file_tag_query(self)


@dataclass(frozen=True, slots=True)
class ContentQuery:
    """An immutable, lazy, bounded catalog-wide content query."""

    predicates: tuple[ContentQueryPredicate, ...] = ()
    row_limit: int = 20
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.predicates, tuple):
            raise TypeError("content-query predicates must be a tuple")
        for predicate in self.predicates:
            _validate_content_query_predicate(predicate)
        _validate_limit(self.row_limit)

    def where(self, predicate: ContentQueryPredicate) -> Self:
        """Return a query with an additional content or active-tag predicate."""
        _validate_content_query_predicate(predicate)
        return replace(self, predicates=(*self.predicates, predicate))

    def limit(self, count: int) -> Self:
        """Return a query with a replacement positive row bound."""
        _validate_limit(count)
        return replace(self, row_limit=count)

    def fetch(self) -> tuple[ContentRecord, ...]:
        """Return bounded matching identities in full identity order."""
        return self._catalog._fetch_content_query(self)

    def count(self) -> int:
        """Return the complete number of matching content identities."""
        return self._catalog._count_content_query(self)

    def with_details(
        self,
        *,
        root: Path,
        path_limit: int = 20,
        tag_limit: int = 3,
        all_tags: bool = False,
    ) -> ContentDetailQuery:
        """Add root-scoped path and catalog-wide active-tag previews."""
        _validate_limit(path_limit)
        _validate_limit(tag_limit)
        _validate_all_tags(all_tags)
        return ContentDetailQuery(
            base=self,
            root=self._catalog._canonical_root(root),
            path_limit=path_limit,
            tag_limit=tag_limit,
            all_tags=all_tags,
        )


@dataclass(frozen=True, slots=True)
class ContentDetailQuery:
    """A typed detail view over a bounded catalog-wide content query."""

    base: ContentQuery
    root: Path
    path_limit: int = 20
    tag_limit: int = 3
    all_tags: bool = False

    def __post_init__(self) -> None:
        if type(self.base) is not ContentQuery:
            raise TypeError("a content detail view requires a ContentQuery")
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("a content detail view requires a canonical absolute root")
        _validate_limit(self.path_limit)
        _validate_limit(self.tag_limit)
        _validate_all_tags(self.all_tags)

    def fetch(self) -> tuple[ContentTagView, ...]:
        """Materialize bounded contents and their path/tag previews."""
        return self.base._catalog._fetch_content_detail_query(self)

    def current_path_count(self) -> int:
        """Count paths at the root for every matching identity."""
        return self.base._catalog._count_content_detail_paths(self)


@dataclass(frozen=True, slots=True)
class TagVocabularyQuery:
    """An immutable, lazy, bounded active-tag vocabulary query."""

    predicates: tuple[TagNameRegexPredicate, ...] = ()
    ordering: SortExpression = field(default_factory=tag_name.asc)
    row_limit: int = 20
    provenance_kind: TagProvenanceKind | None = None
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.predicates, tuple):
            raise TypeError("vocabulary predicates must be a tuple")
        for predicate in self.predicates:
            _validate_tag_name_predicate(predicate)
        _validate_tag_vocabulary_sort_expression(self.ordering)
        _validate_limit(self.row_limit)
        _validate_tag_provenance(self.provenance_kind)

    def where(self, predicate: TagNameRegexPredicate) -> Self:
        _validate_tag_name_predicate(predicate)
        return replace(self, predicates=(*self.predicates, predicate))

    def sort(self, ordering: SortExpression) -> Self:
        _validate_tag_vocabulary_sort_expression(ordering)
        return replace(self, ordering=ordering)

    def limit(self, count: int) -> Self:
        _validate_limit(count)
        return replace(self, row_limit=count)

    def provenance(self, kind: TagProvenanceKind | None) -> Self:
        _validate_tag_provenance(kind)
        return replace(self, provenance_kind=kind)

    def fetch(self) -> tuple[TagUsage, ...]:
        return self._catalog._fetch_tag_vocabulary_query(self)

    def count(self) -> int:
        return self._catalog._count_tag_vocabulary_query(self)


@dataclass(frozen=True, slots=True)
class DuplicateGroupQuery:
    """An immutable, lazy, bounded query for current duplicate groups."""

    root: Path
    ordering: SortExpression = field(default_factory=lambda: duplicate.redundant_bytes.desc())
    group_limit: int = 20
    member_row_limit: int = 20
    group_offset: int = 0
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("a duplicate-group query requires a canonical absolute root")
        _validate_duplicate_sort_expression(self.ordering)
        _validate_limit(self.group_limit)
        _validate_limit(self.member_row_limit)
        _validate_offset(self.group_offset)

    def limit(self, count: int) -> Self:
        """Return a query with a replacement positive group bound."""
        _validate_limit(count)
        return replace(self, group_limit=count)

    def offset(self, count: int) -> Self:
        """Skip count ordered groups before applying the group limit."""
        _validate_offset(count)
        return replace(self, group_offset=count)

    def sort(self, ordering: SortExpression) -> Self:
        """Return a query with a replacement duplicate-group ordering."""
        _validate_duplicate_sort_expression(ordering)
        return replace(self, ordering=ordering)

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

    def stream(self) -> Iterator[DuplicateGroupHandle]:
        """Yield at most this query's group bound without loading members."""
        return self._catalog._stream_duplicate_group_query(self, limit=self.group_limit)

    def stream_all(self) -> Iterator[DuplicateGroupHandle]:
        """Yield every group after the offset without loading members."""
        return self._catalog._stream_duplicate_group_query(self, limit=None)


@dataclass(frozen=True, slots=True)
class DuplicateGroupHandle:
    """One pinned duplicate group whose members are streamed on request."""

    content_id: ContentId
    size_bytes: int
    file_instance_count: int
    potential_redundant_bytes: int
    _catalog: Catalog = field(repr=False, compare=False, kw_only=True)
    _location: Location = field(repr=False, compare=False, kw_only=True)
    _scan_id: int = field(repr=False, compare=False, kw_only=True)
    _member_row_limit: int = field(repr=False, compare=False, kw_only=True)

    def stream_members(self) -> Iterator[FileObservation]:
        """Yield at most the inherited member bound in relative-path order."""
        return self._catalog._stream_duplicate_group_members(self, limit=self._member_row_limit)

    def stream_all_members(self) -> Iterator[FileObservation]:
        """Yield every pinned member in relative-path order."""
        return self._catalog._stream_duplicate_group_members(self, limit=None)


def _validate_path_glob_predicate(predicate: object) -> None:
    if type(predicate) is not PathGlobPredicate:
        raise TypeError("where() requires a path GLOB predicate")
    if predicate.field is not _CurrentFileField.PATH:
        raise ValueError("a path GLOB predicate requires the path field")
    if not isinstance(predicate.pattern, str):
        raise TypeError("a path GLOB pattern must be a string")


def _validate_content_predicate(predicate: object) -> None:
    if type(predicate) is not ContentPredicate:
        raise TypeError("content.is_() requires a content identity")
    if type(predicate.content_id) is not ContentId:
        raise TypeError("content.is_() requires a ContentId")


def _validate_tag_predicate(predicate: object) -> None:
    if type(predicate) is not TagPredicate:
        raise TypeError("tag filtering requires a tag predicate")
    if not isinstance(predicate.names, tuple):
        raise TypeError("tag predicate names must be a tuple")
    if not predicate.names:
        raise ValueError("tag predicates require at least one tag")
    canonical_names = _canonical_tag_names(predicate.names)
    if predicate.names != canonical_names:
        raise ValueError("tag predicate names must be unique and in canonical order")
    _validate_tag_provenance(predicate.provenance)
    if not isinstance(predicate.match, _TagMatchMode):
        raise ValueError("unsupported tag predicate match mode")


def _validate_current_file_predicate(predicate: object) -> None:
    if type(predicate) is PathGlobPredicate:
        _validate_path_glob_predicate(predicate)
    elif type(predicate) is ContentPredicate:
        _validate_content_predicate(predicate)
    elif type(predicate) is TagPredicate:
        _validate_tag_predicate(predicate)
    else:
        raise TypeError("where() requires a current-file predicate")


def _validate_content_query_predicate(predicate: object) -> None:
    if type(predicate) is ContentPredicate:
        _validate_content_predicate(predicate)
    elif type(predicate) is TagPredicate:
        _validate_tag_predicate(predicate)
    else:
        raise TypeError("where() requires a content or tag predicate")


def _canonical_tag_names(names: tuple[str, ...]) -> tuple[str, ...]:
    if any(not isinstance(name, str) for name in names):
        raise TypeError("tag names must be strings")
    return tuple(sorted({validate_tag_name(name) for name in names}))


def _validate_tag_provenance(provenance: object) -> None:
    if provenance is not None and not isinstance(provenance, str):
        raise TypeError("tag provenance must be a string or None")
    if provenance not in (None, "user", "system"):
        raise ValueError("tag provenance must be 'user', 'system', or None")


def _make_tag_predicate(
    names: tuple[str, ...], provenance: TagProvenanceKind | None, match: _TagMatchMode
) -> TagPredicate:
    canonical_names = _canonical_tag_names(names)
    if not canonical_names:
        raise ValueError("tag predicates require at least one tag")
    _validate_tag_provenance(provenance)
    return TagPredicate(names=canonical_names, provenance=provenance, match=match)


def _validate_sort_expression(ordering: object) -> None:
    if type(ordering) is not SortExpression:
        raise TypeError("sort() requires a current-file sort expression")
    if not isinstance(ordering.field, _CurrentFileField):
        raise ValueError("unsupported current-file sort field")
    if not isinstance(ordering.direction, _SortDirection):
        raise ValueError("unsupported current-file sort direction")


def _validate_current_file_sort_expression(ordering: object) -> None:
    _validate_sort_expression(ordering)
    assert isinstance(ordering, SortExpression)
    if ordering.field not in {
        _CurrentFileField.PATH,
        _CurrentFileField.SIZE,
        _CurrentFileField.MTIME,
    }:
        raise ValueError("unsupported current-file sort field")


def _validate_duplicate_sort_expression(ordering: object) -> None:
    if type(ordering) is not SortExpression:
        raise TypeError("sort() requires a duplicate-group sort expression")
    if ordering.field not in {
        _CurrentFileField.SIZE,
        _CurrentFileField.DUPLICATE_FILE_COUNT,
        _CurrentFileField.DUPLICATE_REDUNDANT_BYTES,
    }:
        raise ValueError("unsupported duplicate-group sort field")
    if not isinstance(ordering.direction, _SortDirection):
        raise ValueError("unsupported duplicate-group sort direction")


def _validate_tag_name_predicate(predicate: object) -> None:
    if type(predicate) is not TagNameRegexPredicate:
        raise TypeError("where() requires a tag-name regex predicate")
    re.compile(predicate.source)


def _validate_tag_vocabulary_sort_expression(ordering: object) -> None:
    if type(ordering) is not SortExpression:
        raise TypeError("sort() requires a tag-vocabulary sort expression")
    if ordering.field not in {
        _CurrentFileField.TAG_NAME,
        _CurrentFileField.TAG_CONTENT_COUNT,
        _CurrentFileField.TAG_ASSERTION_COUNT,
    }:
        raise ValueError("unsupported tag-vocabulary sort field")
    if not isinstance(ordering.direction, _SortDirection):
        raise ValueError("unsupported tag-vocabulary sort direction")


def _validate_limit(count: object) -> None:
    if type(count) is not int:
        raise TypeError("limit must be an integer")
    if count < 1:
        raise ValueError("limit must be at least 1")


def _validate_offset(count: object) -> None:
    if type(count) is not int:
        raise TypeError("group offset must be an integer")
    if not 0 <= count <= 2**63 - 1:
        raise ValueError("group offset must be between 0 and 9223372036854775807")


def _validate_all_tags(all_tags: object) -> None:
    if type(all_tags) is not bool:
        raise TypeError("all_tags must be a bool")


__all__ = [
    "ContentPredicate",
    "ContentDetailQuery",
    "ContentQuery",
    "ContentQueryPredicate",
    "CurrentFileQuery",
    "CurrentFileTagQuery",
    "CurrentFilePredicate",
    "DuplicateGroupQuery",
    "DuplicateGroupHandle",
    "PathGlobPredicate",
    "SortExpression",
    "TagPredicate",
    "TagNameRegexPredicate",
    "TagVocabularyQuery",
    "content",
    "duplicate",
    "mtime",
    "path",
    "size",
    "tags",
    "tag_name",
    "tag_usage",
]
