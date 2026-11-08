"""Public API, documentation, and tutorial compatibility gates for Plan 004.7."""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import archiver
import archiver.errors
import archiver.models
import archiver.pandas
import archiver.query
from archiver import Catalog

_REPOSITORY = Path(__file__).resolve().parents[2]
_RETIRED_TYPES = (
    "CurrentFileSearch",
    "TagAwareCurrentFileSearch",
    "TaggedContentSearch",
    "TaggedContent",
    "MultiTagContentSearch",
    "AvailableTagSearch",
    "DuplicateGroupSearch",
    "CurrentFileSort",
    "AvailableTagSort",
    "TagMatchMode",
)
_RETIRED_METHODS = (
    "current_files",
    "search_current_files",
    "find_by_content",
    "search_current_files_with_tags",
    "search_tagged_content",
    "search_content_by_tags",
    "search_available_tags",
    "duplicate_groups",
    "search_duplicate_groups",
    "duplicate_summary",
)


def test_documented_catalog_signatures_match_every_public_member() -> None:
    reference = (_REPOSITORY / "docs/catalog-api.md").read_text(encoding="utf-8")
    tree = ast.parse(inspect.getsource(Catalog))
    catalog = tree.body[0]
    assert isinstance(catalog, ast.ClassDef)
    documented = set(re.findall(r"^### `([^`]+)`$", reference, re.MULTILINE))
    expected: set[str] = set()
    for node in catalog.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_") and node.name not in {"__init__", "__enter__", "__exit__"}:
            continue
        expected.add(node.name)
        assert node.returns is not None
        if any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list):
            signature = f"{node.name}: {ast.unparse(node.returns)}"
        else:
            node.args.args = node.args.args[1:]
            signature = f"{node.name}({ast.unparse(node.args)}) -> {ast.unparse(node.returns)}"
        assert signature in reference, node.name
    assert documented == expected


def test_public_imports_and_retired_surface() -> None:
    for name in archiver.__all__:
        assert getattr(archiver, name) is not None
    for name in (
        "CurrentFileQuery",
        "CurrentFileTagQuery",
        "ContentQuery",
        "ContentDetailQuery",
        "TagVocabularyQuery",
        "DuplicateGroupQuery",
        "DuplicateGroupHandle",
    ):
        assert getattr(archiver, name) is getattr(archiver.query, name)
    for name in (
        "ContentRecord",
        "ScanHistorySummary",
        "PruneHistorySummary",
        "CurrentFileTagView",
        "ContentTagView",
        "DuplicateGroupView",
        "DuplicateSummary",
        "TagUsage",
        "TagProvenanceKind",
        "validate_tag_name",
    ):
        assert getattr(archiver, name) is getattr(archiver.models, name)
    for name in ("HistoryPruningError", "PrunedScanError"):
        assert getattr(archiver, name) is getattr(archiver.errors, name)
    for name in _RETIRED_TYPES:
        assert not hasattr(archiver, name)
        assert not hasattr(archiver.models, name)
    for name in _RETIRED_METHODS:
        assert not hasattr(Catalog, name)
    assert not hasattr(archiver.pandas, "duplicate_groups_frame")
    for name in ("current_files_frame", "observation_history_frame", "scan_history_frame"):
        assert callable(getattr(archiver.pandas, name))
    for name in ("content", "duplicate", "mtime", "path", "size", "tags", "tag_name", "tag_usage"):
        assert getattr(archiver.query, name) is not None


def test_public_notebook_metadata_and_api_compatibility() -> None:
    for path in sorted((_REPOSITORY / "notebooks/public").glob("*.ipynb")):
        notebook = json.loads(path.read_text(encoding="utf-8"))
        source = "".join("".join(cell["source"]) for cell in notebook["cells"])
        historical = "Historical tutorial — frozen version required" in source
        if historical:
            assert re.search(r"revision `[0-9a-f]{7,40}`", source), path.name
        for cell in notebook["cells"]:
            if cell["cell_type"] != "code":
                continue
            if not historical:
                code = "".join(cell["source"])
                for name in _RETIRED_METHODS:
                    assert not re.search(rf"\.{name}\s*\(", code), (path.name, name)
                for name in _RETIRED_TYPES:
                    assert not re.search(rf"\b{name}\b", code), (path.name, name)
        assert set(notebook["metadata"]) <= {"kernelspec", "language_info"}, path.name
        assert not re.search(r"(?i)(?:[a-z]:[\\/]+Users[\\/]|/home/|/Users/)", source), path.name
