"""End-to-end gaps identified in the Plan 004.8 CLI consistency audit."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, TagProvenance
from archiver.cli import main


def _initialize(root: Path, capsys: pytest.CaptureFixture[str], *, scan: bool = True) -> Path:
    assert main(["catalog", "init", str(root)]) == 0
    if scan:
        assert main(["catalog", "refresh", str(root), "--no-progress"]) == 0
    capsys.readouterr()
    return root / ".archiver" / "catalog.sqlite"


@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize(
    ("sort", "reverse", "expected"),
    [
        ("path", False, ["a.bin", "b.bin", "nested/c.bin"]),
        ("path", True, ["nested/c.bin", "b.bin", "a.bin"]),
        ("size", False, ["nested/c.bin", "a.bin", "b.bin"]),
        ("size", True, ["a.bin", "b.bin", "nested/c.bin"]),
        ("date", False, ["nested/c.bin", "a.bin", "b.bin"]),
        ("date", True, ["a.bin", "b.bin", "nested/c.bin"]),
    ],
)
def test_file_cli_sort_reverse_keeps_ascending_path_ties(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    sort: str,
    reverse: bool,
    expected: list[str],
    tagged: bool,
) -> None:
    (tmp_path / "nested").mkdir()
    for name, data, stamp in [
        ("a.bin", b"aa", 1_000_000_000),
        ("b.bin", b"aa", 1_000_000_000),
        ("nested/c.bin", b"ccc", 2_000_000_000),
    ]:
        source = tmp_path / name
        source.write_bytes(data)
        os.utime(source, ns=(stamp, stamp))
    _initialize(tmp_path, capsys)
    arguments = ["catalog", "files", str(tmp_path), "--sort", sort]
    if reverse:
        arguments.append("--reverse")
    if tagged:
        arguments.append("--show-tags")
    assert main(arguments) == 0
    output = capsys.readouterr()
    paths = [line.split(" | ")[3].strip() for line in output.out.splitlines() if re.match(r"^\d{4}-", line)]
    assert paths == expected
    assert "7 B" in output.out
    assert output.err == ""


@pytest.mark.parametrize("sort", ["content", "assertions"])
@pytest.mark.parametrize("reverse", [False, True])
def test_vocabulary_cli_reverse_keeps_name_ties(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    sort: str,
    reverse: bool,
) -> None:
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "b.bin").write_bytes(b"b")
    database = _initialize(tmp_path, capsys)
    user = TagProvenance("user", "fixture", "1")
    with Catalog.open(database) as catalog:
        first = catalog.content_for_path(tmp_path, PurePosixPath("a.bin"))
        second = catalog.content_for_path(tmp_path, PurePosixPath("b.bin"))
        for tag in ("alpha", "beta", "gamma"):
            catalog.add_content_tag(first, tag, user)
        catalog.add_content_tag(second, "gamma", user)
    args = ["catalog", "tags", "available", str(tmp_path), "--sort", sort]
    if reverse:
        args.append("--reverse")
    assert main(args) == 0
    output = capsys.readouterr().out
    names = [
        line.split(" | ")[0].strip() for line in output.splitlines() if " | " in line and not line.startswith("Tag ")
    ]
    assert names == (["alpha", "beta", "gamma"] if reverse else ["gamma", "alpha", "beta"])


@pytest.mark.parametrize("scanned", [False, True])
def test_all_cli_query_domains_have_stable_empty_states(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    scanned: bool,
) -> None:
    database = _initialize(tmp_path, capsys, scan=scanned)
    before = database.read_bytes()
    cases = [
        (["files", str(tmp_path)], "No matching files.", "showing all 0"),
        (["files", str(tmp_path), "--show-tags"], "No matching files.", "0 distinct contents"),
        (
            ["duplicates", str(tmp_path), "--details"],
            "No duplicate groups." if scanned else "Current scan: none",
            "Duplicate content",
        ),
        (["tags", "find", str(tmp_path), "missing", "--details"], "No matching content.", "0 current paths"),
        (["tags", "available", str(tmp_path)], "No matching active tags.", "showing all 0"),
    ]
    for arguments, message, summary in cases:
        assert main(["catalog", *arguments]) == 0
        output = capsys.readouterr()
        assert message in output.out and summary in output.out
        assert output.err == ""
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["files", "--limit", "0"],
        ["files", "--match-any-tag"],
        ["files", "--provenance", "user"],
        ["duplicates", "--member-limit", "-1"],
        ["duplicates", "--group-limit", "True"],
        ["tags", "find", "valid", "--all-tags", "--display-tag-limit", "1"],
        ["tags", "available", "--regex", "["],
    ],
)
def test_malformed_options_are_rejected_before_catalog_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    def forbidden_open(*args: object, **kwargs: object) -> Catalog:
        pytest.fail("invalid syntax must be rejected before opening a catalog")

    monkeypatch.setattr(Catalog, "open", forbidden_open)
    prefix = 2 if arguments[0] == "tags" else 1
    command = ["catalog", *arguments[:prefix], str(tmp_path), *arguments[prefix:]]
    with pytest.raises(SystemExit) as raised:
        main(command)
    assert raised.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / ".archiver").exists()


def test_cli_default_bounds_keep_complete_totals_and_sources_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One 21-member group and twenty two-member groups cross both default bounds.
    for group in range(21):
        directory = tmp_path / f"g{group:02}"
        directory.mkdir()
        for member in range(21 if group == 0 else 2):
            (directory / f"copy{member:02}.bin").write_bytes(bytes([group]) * (30 if group == 0 else 1))
    database = _initialize(tmp_path, capsys)
    user = TagProvenance("user", "fixture", "1")
    with Catalog.open(database) as catalog:
        for group in range(21):
            content_id = catalog.content_for_path(tmp_path, PurePosixPath(f"g{group:02}/copy00.bin"))
            catalog.add_content_tag(content_id, "common", user)
            catalog.add_content_tag(content_id, f"tag{group:02}", user)
            if group == 0:
                for name in ("bravo", "charlie", "delta"):
                    catalog.add_content_tag(content_id, name, user)
    sources = [p for p in tmp_path.rglob("*.bin")]
    before = [(p, p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in sources]
    catalog_before = database.read_bytes()
    monkeypatch.setattr("archiver.cli.shutil.get_terminal_size", lambda fallback=(120, 24): os.terminal_size(fallback))

    assert main(["catalog", "files", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert len(re.findall(r"^\d{4}-", output, re.MULTILINE)) == 20
    assert "Matched: 61 files" in output and "showing first 20" in output

    assert main(["catalog", "duplicates", str(tmp_path), "--details"]) == 0
    output = capsys.readouterr().out
    assert "Duplicate groups: 21" in output and "Duplicate file instances: 61" in output
    assert "Showing first 20 of 21 duplicate groups." in output
    assert "PATHS" not in output and "g00/copy" not in output
    assert main(["catalog", "duplicates", str(tmp_path), "--details", "--member-limit", "20"]) == 0
    output = capsys.readouterr().out
    assert "20 of 21" in output and "g00/copy19.bin" in output and "g00/copy20.bin" not in output

    assert main(["catalog", "tags", "find", str(tmp_path), "common"]) == 0
    output = capsys.readouterr().out
    assert "Matched: 21 content identities" in output and "61 current paths (showing first 20)" in output

    assert main(["catalog", "tags", "find", str(tmp_path), "tag00", "--details"]) == 0
    output = capsys.readouterr().out
    assert "bravo, charlie, common +2" in output
    assert "21 current paths" in output and "g00/copy19.bin" in output and "g00/copy20.bin" not in output

    assert main(["catalog", "tags", "available", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "Matched: 25 active tags (showing first 20)" in output

    assert main(["catalog", "files", str(tmp_path), "--path", "g00/*", "--limit", "1", "--all-tags"]) == 0
    output = capsys.readouterr().out
    for tag in ("bravo", "charlie", "common", "delta", "tag00"):
        assert tag in output
    assert "showing first 1" in output and "g00/copy01.bin" not in output
    assert database.read_bytes() == catalog_before
    assert [(p, p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in sources] == before
