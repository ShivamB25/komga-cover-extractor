from __future__ import annotations

import errno
import hashlib
import io
import json
import tempfile
import textwrap
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_remaining_transaction_boundaries import (
    _events,
    _identity,
    _inspect,
    _kill,
    _operation,
    _png,
    _run,
    _tree,
)
from test_remaining_transaction_boundaries import (
    pytestmark as pytestmark,
)

_PIPELINE = """
from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.config import validate_config

config = validate_config(json.loads((case / 'config.json').read_text(encoding='utf-8')), base_dir=case)
incoming = case / 'downloads' / 'Series Volume 1 (Digital).cbz'
existing = root / 'Series' / 'Series v01 (Scan).cbz'
target = root / 'Series' / 'Series v01 (Digital).cbz'
series_cover = root / 'Series' / 'cover.jpg'
authorization = RunAuthorization(apply=True)
"""


@pytest.fixture
def pipeline_case() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="kce-pipeline-crash-proof-") as directory:
        case = Path(directory).resolve()
        for name in ("library", "downloads", "process", "home", "tmp"):
            (case / name).mkdir()
        values = {
            "paths": [str(case / "library")], "download_folders": [str(case / "downloads")],
            "state_dir": str(case / "state"), "log_dir": str(case / "logs"),
            "correct_file_extensions_toggle": False, "convert_to_cbz_toggle": False,
            "delete_unacceptable_files_toggle": False, "delete_chapters_from_downloads_toggle": False,
            "rename_files_in_download_folders_toggle": True,
            "create_folders_for_items_in_download_folder_toggle": True,
            "check_for_duplicate_volumes_toggle": False, "check_for_existing_series_toggle": True,
            "rename_dirs_in_download_folder_toggle": False, "move_series_to_correct_library_toggle": False,
            "generate_release_group_list_toggle": False, "extract_covers_toggle": True,
            "check_for_missing_volumes_toggle": False, "bookwalker_check": False,
            "manual_rename": False, "manual_delete": False, "log_to_file": False,
            "match_through_image_similarity": False, "match_through_identifiers": False,
            "compare_detected_cover_to_blank_images": False, "required_similarity_score": 1.0,
            "discord_webhook_url": [], "bookwalker_webhook_urls": [], "new_volume_webhook": "",
            "send_scan_request_to_komga_libraries_toggle": False, "library_types": [],
            "restructure_when_renaming": False, "output_covers_as_webp": False,
            "compress_image_option": False,
            "ranked_keywords": [{"name": "Digital", "score": 3, "file_type": "both"},
                                {"name": "Scan", "score": 0, "file_type": "both"}],
        }
        (case / "config.json").write_text(json.dumps(values), encoding="utf-8")
        yield case


def _jpeg(color: tuple[int, int, int]) -> bytes:
    from PIL import Image
    with io.BytesIO() as stream:
        with Image.new("RGB", (16, 16), color) as image:
            image.save(stream, format="JPEG", quality=90)
        return stream.getvalue()


def _book(path: Path, image: bytes, *, publisher: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in (
            ("01.jpg", image), ("cover.jpg", _jpeg((0, 0, 255))),
            ("ComicInfo.xml", f"<ComicInfo><Publisher>{publisher}</Publisher><Year>2015</Year></ComicInfo>".encode()),
        ):
            archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), content)
    return path.read_bytes()


def _seed_upgrade(case: Path) -> dict[str, Any]:
    existing = case / "library" / "Series" / "Series v01 (Scan).cbz"
    incoming = case / "downloads" / "Series Volume 1 (Digital).cbz"
    red, green = _jpeg((255, 0, 0)), _jpeg((0, 255, 0))
    original_existing = _book(existing, red, publisher="Prior Press")
    seed = _run(case, _PIPELINE + textwrap.dedent("""
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest('extract', books=(existing,), authorization=authorization))
    report(result)
    """))
    assert seed["result"]["exit_code"] == 0
    assert seed["result"]["counts"]["committed"] == 1
    assert existing.read_bytes() == original_existing
    assert existing.with_suffix(".jpg").read_bytes() == red
    cover = existing.parent / "cover.jpg"
    assert cover.read_bytes() == red
    assert not list(existing.parent.glob("poster.*")), "The standard series policy creates cover only"
    existing.with_suffix(".jpeg").write_bytes(red)
    original_incoming = _book(incoming, green, publisher="Incoming Press")
    for suffix in (".jpg", ".jpeg"):
        incoming.with_suffix(suffix).write_bytes(green)
    originals = {
        existing: original_existing, existing.with_suffix(".jpg"): red, existing.with_suffix(".jpeg"): red,
        incoming: original_incoming, incoming.with_suffix(".jpg"): green, incoming.with_suffix(".jpeg"): green,
    }
    unrelated = case / "library" / "Unaffected" / "poster.png"
    unrelated.parent.mkdir()
    unrelated.write_bytes(_png(90, 20, 130))
    return {
        "seed": seed, "existing": existing, "incoming": incoming, "red": red, "green": green,
        "cover": cover, "originals": originals, "unrelated": unrelated, "unrelated_identity": _identity(unrelated),
    }


def _recover_pipeline(case: Path, operation_id: str) -> dict[str, Any]:
    return _run(case, _PIPELINE + textwrap.dedent("""
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest('recover', operation_id=sys.argv[2], authorization=authorization))
    report(result)
    """), operation_id)


def _run_again(case: Path) -> dict[str, Any]:
    return _run(case, _PIPELINE + textwrap.dedent("""
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest('run', authorization=authorization))
    report(result)
    """))


def _assert_complete_upgrade(
    case: Path, proof: dict[str, Any], operation_id: str, fixture: dict[str, Any], *, committed: int,
) -> None:
    target = case / "library" / "Series" / "Series v01 (Digital).cbz"
    assert proof["result"]["exit_code"] == 0
    assert proof["result"]["counts"]["committed"] == committed
    assert proof["result"]["counts"]["failed"] == 0
    record = _operation(proof, operation_id)
    assert record["phase"] == "complete"
    assert target.read_bytes() == fixture["originals"][fixture["incoming"]]
    for suffix in (".jpg", ".jpeg"):
        assert target.with_suffix(suffix).read_bytes() == fixture["green"]
    assert fixture["cover"].read_bytes() == fixture["green"]
    assert not list(target.parent.glob("poster.*"))
    assert _identity(fixture["unrelated"]) == fixture["unrelated_identity"]
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert archive.read("01.jpg") == fixture["green"]
        assert archive.read("ComicInfo.xml") == b"<ComicInfo><Publisher>Incoming Press</Publisher><Year>2015</Year></ComicInfo>"
    from PIL import Image
    for image_path in (target.with_suffix(".jpg"), target.with_suffix(".jpeg"), fixture["cover"]):
        with Image.open(image_path) as image:
            image.load()
            assert image.format == "JPEG"
            red, green, blue = image.convert("RGB").getpixel((0, 0))
            assert green >= 250 and red <= 5 and blue <= 5
    by_source = {
        str(Path(member["source"]["root"]) / member["source"]["relative_path"]): member
        for member in record["members"]
        if member["kind"] in {"move", "quarantine"} and member["source"] is not None
    }
    for source, original in fixture["originals"].items():
        assert not source.exists()
        member = by_source[str(source)]
        assert member["status"] == "complete"
        assert Path(member["cleanup_path"]).read_bytes() == original
        assert member["cleanup_fingerprint"]["sha256"] == hashlib.sha256(original).hexdigest()
    cover_member, = [member for member in record["members"] if member["destination"] == str(fixture["cover"]) and member["kind"] == "publish"]
    assert Path(cover_member["backup_path"]).read_bytes() == fixture["red"]
    assert cover_member["published_fingerprint"]["sha256"] == hashlib.sha256(fixture["green"]).hexdigest()
    events = _events(proof, operation_id)
    assert len(events) == len(record["plan"]["operations"])
    assert {event["member_id"] for event in events} == {member["member_id"] for member in record["members"]}
    assert {event["event_type"] for event in events} == {"local_commit"}
    assert all(event["attempts"] == 0 for event in events)
    assert not list(case.rglob(".kce-stage-*"))
    assert not list(case.rglob(".kce-artwork-*"))


@pytest.mark.parametrize("boundary", ["book_published", "sidecars_published", "series_published", "group_committed", "source_cleaned"])
def test_installed_pipeline_group_survives_sigkill_and_fresh_recover_repeat(
    pipeline_case: Path, boundary: str,
) -> None:
    case = pipeline_case
    fixture = _seed_upgrade(case)
    seed_events = fixture["seed"]["journal"]["outbox"]
    subject = _PIPELINE + textwrap.dedent("""
    real_init = TransactionExecutor.__init__
    def initialize(executor, *args, **kwargs):
        def checkpoint(phase, member_id):
            records = executor.state_store.list_operations(phases=('staging', 'staged', 'publishing', 'committed', 'cleanup_pending'))
            for record in records:
                operation = next((item for item in record.plan.operations if item.member_id == member_id), None)
                boundary = sys.argv[2]
                selected = (
                    boundary == 'book_published' and phase == 'member_committed' and operation is not None and operation.destination == target
                    or boundary == 'sidecars_published' and phase == 'before_publish' and operation is not None and operation.destination == series_cover
                    or boundary == 'series_published' and phase == 'published' and operation is not None and operation.destination == series_cover
                    or boundary == 'group_committed' and phase == 'group_committed'
                    or boundary == 'source_cleaned' and phase == 'cleanup_done' and operation is not None and operation.source is not None and operation.source.path == incoming
                )
                if selected:
                    pause(boundary, member_id, record.operation_id)
        kwargs['checkpoint'] = checkpoint
        real_init(executor, *args, **kwargs)
    TransactionExecutor.__init__ = initialize
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest('run', books=(incoming,), authorization=authorization))
    report(result)
    """)
    checkpoint = _kill(case, subject, boundary)
    operation_id = checkpoint["operation_id"]
    interrupted = _inspect(case)
    record = _operation(interrupted, operation_id)
    target = case / "library" / "Series" / "Series v01 (Digital).cbz"
    assert target.read_bytes() == fixture["originals"][fixture["incoming"]]
    target_identity = _identity(target)
    group_committed = boundary in {"group_committed", "source_cleaned"}
    if group_committed:
        assert record["phase"] in {"committed", "cleanup_pending"}
        assert len(_events(interrupted, operation_id)) == len(record["members"])
    else:
        assert record["phase"] == "publishing"
        assert _events(interrupted, operation_id) == []
        assert all(source.read_bytes() == original for source, original in fixture["originals"].items())
    if boundary in {"book_published", "sidecars_published"}:
        assert fixture["cover"].read_bytes() == fixture["red"]
    else:
        assert fixture["cover"].read_bytes() == fixture["green"]
    if boundary == "sidecars_published":
        for suffix in (".jpg", ".jpeg"):
            assert target.with_suffix(suffix).read_bytes() == fixture["green"]
        cover_member, = [member for member in record["members"] if member["destination"] == str(fixture["cover"]) and member["kind"] == "publish"]
        assert Path(cover_member["backup_path"]).read_bytes() == fixture["red"]
        assert cover_member["published_fingerprint"] is None
    assert not list(target.parent.glob("poster.*"))
    assert _identity(fixture["unrelated"]) == fixture["unrelated_identity"]
    recovered = _recover_pipeline(case, operation_id)
    _assert_complete_upgrade(case, recovered, operation_id, fixture, committed=int(not group_committed))
    assert _identity(target) == target_identity
    before = _tree(case / "library")
    repeated = _recover_pipeline(case, operation_id)
    _assert_complete_upgrade(case, repeated, operation_id, fixture, committed=0)
    assert _tree(case / "library") == before
    assert _events(repeated, operation_id) == _events(recovered, operation_id)
    rerun = _run_again(case)
    assert rerun["result"]["exit_code"] == 0
    assert rerun["result"]["counts"]["committed"] == 0
    assert _tree(case / "library") == before
    assert {record["operation_id"] for record in rerun["journal"]["operations"]} == {record["operation_id"] for record in repeated["journal"]["operations"]}
    assert rerun["journal"]["outbox"] == repeated["journal"]["outbox"]
    assert {event["event_id"] for event in seed_events} <= {event["event_id"] for event in rerun["journal"]["outbox"]}


@pytest.mark.parametrize("fault_errno", [errno.ENOSPC, errno.EACCES], ids=["ENOSPC", "EACCES"])
def test_seriesart_syscall_failure_after_book_and_sidecars_has_no_false_group_commit(
    pipeline_case: Path, fault_errno: int,
) -> None:
    case = pipeline_case
    fixture = _seed_upgrade(case)
    before_ids = {record["operation_id"] for record in fixture["seed"]["journal"]["operations"]}
    subject = _PIPELINE + textwrap.dedent("""
    real_replace = os.replace
    def replace_file(source, destination, *args, **kwargs):
        if Path(destination) == series_cover:
            (case / 'series-fault').write_text(str(sys.argv[2]), encoding='utf-8')
            raise OSError(int(sys.argv[2]), 'Injected series-artwork publication failure', str(destination))
        return real_replace(source, destination, *args, **kwargs)
    os.replace = replace_file
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest('run', books=(incoming,), authorization=authorization))
    os.replace = real_replace
    report(result)
    """)
    failed = _run(case, subject, str(fault_errno))
    assert (case / "series-fault").read_text(encoding="utf-8") == str(fault_errno)
    assert failed["result"]["exit_code"] == 1
    assert failed["result"]["counts"]["committed"] == 0
    assert failed["result"]["counts"]["failed"] == 1
    fresh = [record for record in failed["journal"]["operations"] if record["operation_id"] not in before_ids]
    assert len(fresh) == 1
    record, = fresh
    operation_id = record["operation_id"]
    assert record["phase"] == "publishing"
    assert _events(failed, operation_id) == []
    target = case / "library" / "Series" / "Series v01 (Digital).cbz"
    assert target.read_bytes() == fixture["originals"][fixture["incoming"]]
    target_identity = _identity(target)
    for suffix in (".jpg", ".jpeg"):
        assert target.with_suffix(suffix).read_bytes() == fixture["green"]
    assert fixture["cover"].read_bytes() == fixture["red"]
    assert all(source.read_bytes() == original for source, original in fixture["originals"].items())
    cover_member, = [member for member in record["members"] if member["destination"] == str(fixture["cover"]) and member["kind"] == "publish"]
    assert Path(cover_member["backup_path"]).read_bytes() == fixture["red"]
    assert Path(cover_member["stage_path"]).read_bytes() == fixture["green"]
    assert cover_member["published_fingerprint"] is None
    recovered = _recover_pipeline(case, operation_id)
    _assert_complete_upgrade(case, recovered, operation_id, fixture, committed=1)
    assert _identity(target) == target_identity
    before = _tree(case / "library")
    repeated = _recover_pipeline(case, operation_id)
    _assert_complete_upgrade(case, repeated, operation_id, fixture, committed=0)
    assert _tree(case / "library") == before
    assert _events(repeated, operation_id) == _events(recovered, operation_id)


def test_custom_poster_prevents_upgrade_without_losing_originals_or_creating_success_events(pipeline_case: Path) -> None:
    case = pipeline_case
    fixture = _seed_upgrade(case)
    poster = fixture["cover"].parent / "poster.png"
    poster.write_bytes(_png(90, 20, 130))
    poster_identity = _identity(poster)
    before_library, before_downloads = _tree(case / "library"), _tree(case / "downloads")
    before = _inspect(case)
    blocked = _run(case, _PIPELINE + textwrap.dedent("""
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest('run', books=(incoming,), authorization=authorization))
    report(result)
    """))
    assert blocked["result"]["exit_code"] == 3
    assert blocked["result"]["counts"]["committed"] == 0
    assert any(decision["status"] == "review_required" and decision["reason"] == "custom_series_artwork"
               for decision in blocked["result"]["decisions"])
    assert _tree(case / "library") == before_library
    assert _tree(case / "downloads") == before_downloads
    assert _identity(poster) == poster_identity
    assert poster.read_bytes() == _png(90, 20, 130)
    assert fixture["cover"].read_bytes() == fixture["red"]
    assert all(source.read_bytes() == original for source, original in fixture["originals"].items())
    assert blocked["journal"]["operations"] == before["journal"]["operations"]
    assert blocked["journal"]["outbox"] == before["journal"]["outbox"]
    assert not list(case.rglob(".kce-artwork-*"))
