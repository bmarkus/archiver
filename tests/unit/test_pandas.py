from pathlib import Path, PurePosixPath

from archiver import ContentId, FileObservation, HistoricalObservation, Location, ScanRun
from archiver.pandas import current_files_frame, observation_history_frame, scan_history_frame

_CONTENT_ID = ContentId(algorithm="sha256", digest="a" * 64)
_LOCATION = Location(id=7, root=Path("/catalog/source"))
_SCAN = ScanRun(
    id=11,
    location=_LOCATION,
    status="completed",
    started_at_ns=100,
    completed_at_ns=200,
)


def test_current_files_frame_flattens_observations() -> None:
    observation = FileObservation(
        location=_LOCATION,
        relative_path=PurePosixPath("nested/file.txt"),
        content_id=_CONTENT_ID,
        size_bytes=42,
        mtime_ns=300,
    )

    frame = current_files_frame([observation])

    assert list(frame.columns) == [
        "location_id",
        "root_path",
        "relative_path",
        "algorithm",
        "digest",
        "size_bytes",
        "mtime_ns",
    ]
    assert frame.to_dict(orient="records") == [
        {
            "location_id": 7,
            "root_path": str(_LOCATION.root),
            "relative_path": "nested/file.txt",
            "algorithm": "sha256",
            "digest": "a" * 64,
            "size_bytes": 42,
            "mtime_ns": 300,
        }
    ]


def test_history_frames_flatten_scan_context_and_consume_iterators() -> None:
    observation = HistoricalObservation(
        scan=_SCAN,
        relative_path=PurePosixPath("nested/file.txt"),
        content_id=_CONTENT_ID,
        size_bytes=42,
        mtime_ns=300,
    )

    observation_frame = observation_history_frame(iter([observation]))
    scan_frame = scan_history_frame(iter([_SCAN]))

    assert list(observation_frame.columns) == [
        "scan_id",
        "scan_status",
        "scan_started_at_ns",
        "scan_completed_at_ns",
        "location_id",
        "root_path",
        "relative_path",
        "algorithm",
        "digest",
        "size_bytes",
        "mtime_ns",
    ]
    assert observation_frame.to_dict(orient="records") == [
        {
            "scan_id": 11,
            "scan_status": "completed",
            "scan_started_at_ns": 100,
            "scan_completed_at_ns": 200,
            "location_id": 7,
            "root_path": str(_LOCATION.root),
            "relative_path": "nested/file.txt",
            "algorithm": "sha256",
            "digest": "a" * 64,
            "size_bytes": 42,
            "mtime_ns": 300,
        }
    ]
    assert scan_frame.to_dict(orient="records") == [
        {
            "scan_id": 11,
            "scan_status": "completed",
            "scan_started_at_ns": 100,
            "scan_completed_at_ns": 200,
            "location_id": 7,
            "root_path": str(_LOCATION.root),
        }
    ]


def test_frame_functions_return_stable_columns_for_empty_input() -> None:
    assert list(current_files_frame([]).columns) == [
        "location_id",
        "root_path",
        "relative_path",
        "algorithm",
        "digest",
        "size_bytes",
        "mtime_ns",
    ]
    assert list(observation_history_frame([]).columns) == [
        "scan_id",
        "scan_status",
        "scan_started_at_ns",
        "scan_completed_at_ns",
        "location_id",
        "root_path",
        "relative_path",
        "algorithm",
        "digest",
        "size_bytes",
        "mtime_ns",
    ]
    assert list(scan_history_frame([]).columns) == [
        "scan_id",
        "scan_status",
        "scan_started_at_ns",
        "scan_completed_at_ns",
        "location_id",
        "root_path",
    ]
