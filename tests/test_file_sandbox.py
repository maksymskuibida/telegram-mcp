"""The FileSandbox for file sends, written before the code (team Testing Posture).

Covers BR25.1 to BR25.5, NFR1.2, NFR2.7, NFR3.2, NFR3.3 and NFR11.11: only a regular, non-empty
file within Telegram's limit (2000 MiB, 10 MB for a photo) inside the outbox is admitted, never one
of the tool's own files whatever path or link names it, the question shows its resolved path, name
and size, and the upload reads that same file from the descriptor it checked, or nothing."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from support.brokers import FakeClock
from support.catalog import call, check, confirm, outbox, put_file, question_text
from support.rpc import RawClient
from support.samples import ALICE
from telegram_mcp.broker.server import Broker
from telegram_mcp.files import (
    MAX_PHOTO_BYTES,
    MAX_RECORDS,
    MAX_SEND_BYTES,
    AdmittedFile,
    FileRefused,
    FileStore,
    admit,
    admit_photo,
    open_admitted,
    unchanged,
)
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths

SEND = "tg_files_send"


@pytest.fixture
def data_files(paths: FixedPaths) -> dict[str, Path]:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("activity.log", "config.toml"):
        path = paths.data_dir / name
        path.write_text(f"# {name} content\n")  # a valid (empty) config.toml
        files[name] = path
    return files


def refused(given: str, paths: FixedPaths) -> FileRefused:
    with pytest.raises(FileRefused) as caught:
        admit(given, paths)
    return caught.value


def refused_photo(given: str, paths: FixedPaths) -> FileRefused:
    with pytest.raises(FileRefused) as caught:
        admit_photo(given, paths)
    return caught.value


def test_a_file_in_the_outbox_is_admitted_with_its_identity(paths: FixedPaths) -> None:
    path = put_file(paths, "report.pdf", b"report")
    admitted = admit(str(path), paths)
    info = os.stat(path)
    assert (admitted.resolved_path, admitted.name, admitted.size_bytes) == (
        str(path),
        "report.pdf",
        6,
    )
    assert (admitted.device, admitted.inode, admitted.modified_ns) == (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
    )
    fields = admitted.fields()
    assert (fields.path, fields.name, fields.size_bytes) == (str(path), "report.pdf", 6)


def test_a_relative_path_and_a_home_path_are_taken_inside_the_outbox(paths: FixedPaths) -> None:
    path = put_file(paths, "notes.txt")
    assert admit("notes.txt", paths).resolved_path == str(path)
    assert admit("~/Downloads/telegram-mcp/outbox/notes.txt", paths).resolved_path == str(path)


@pytest.mark.parametrize(
    "given",
    ["/etc/hosts", "~/secret.txt", "../secret.txt", "~/Downloads/telegram-mcp/outbox/../x.txt"],
)
def test_a_path_outside_the_outbox_is_refused(paths: FixedPaths, given: str) -> None:
    (paths.home / "secret.txt").write_text("secret")
    (paths.home / "Downloads" / "telegram-mcp").mkdir(parents=True, exist_ok=True)
    (paths.home / "Downloads" / "telegram-mcp" / "x.txt").write_text("x")
    error = refused(given, paths)
    assert error.code == "not_allowed"
    assert f"Files can be sent only from {outbox(paths)}" in error.message


def test_a_link_out_of_the_outbox_is_refused(paths: FixedPaths) -> None:
    secret = paths.home / ".ssh" / "id_ed25519"
    secret.parent.mkdir()
    secret.write_text("private key")
    (outbox(paths) / "key.txt").symlink_to(secret)
    assert refused("key.txt", paths).code == "not_allowed"


@pytest.mark.parametrize("name", ["activity.log", "config.toml"])
def test_a_link_or_hard_link_to_the_tools_own_files_is_refused(
    paths: FixedPaths, data_files: dict[str, Path], name: str
) -> None:
    (outbox(paths) / f"soft-{name}").symlink_to(data_files[name])
    os.link(data_files[name], outbox(paths) / f"hard-{name}")
    for given in (f"soft-{name}", f"hard-{name}"):
        error = refused(given, paths)
        assert error.code == "not_allowed"
        assert "belongs to the tool" in error.message


def test_a_missing_file_is_not_found(paths: FixedPaths) -> None:
    outbox(paths)
    error = refused("nothing.pdf", paths)
    assert error.code == "not_found"
    assert "nothing.pdf" in error.message


def test_a_folder_and_a_pipe_are_not_regular_files(paths: FixedPaths) -> None:
    (outbox(paths) / "folder").mkdir()
    os.mkfifo(outbox(paths) / "pipe")
    for given in ("folder", "pipe"):
        error = refused(given, paths)
        assert (error.code, "not a regular file" in error.message) == ("invalid_arguments", True)


def test_an_empty_file_is_refused(paths: FixedPaths) -> None:
    put_file(paths, "empty.txt", b"")
    error = refused("empty.txt", paths)
    assert (error.code, "empty" in error.message) == ("invalid_arguments", True)


def test_a_file_over_2000_mib_is_refused_naming_the_limit(paths: FixedPaths) -> None:
    path = put_file(paths, "big.bin", b"x")
    os.truncate(path, MAX_SEND_BYTES + 1)  # a sparse file: no disk space is used
    error = refused("big.bin", paths)
    assert error.code == "invalid_arguments"
    assert "2000 MiB" in error.message
    assert str(MAX_SEND_BYTES) in error.message
    os.truncate(path, MAX_SEND_BYTES)
    assert admit("big.bin", paths).size_bytes == MAX_SEND_BYTES
    assert MAX_SEND_BYTES == 2_097_152_000


def test_a_photo_over_10_mb_is_refused_naming_the_limit(paths: FixedPaths) -> None:
    path = put_file(paths, "big.jpg", b"x")
    os.truncate(path, MAX_PHOTO_BYTES + 1)
    error = refused_photo("big.jpg", paths)
    assert error.code == "invalid_arguments"
    assert "10 MB" in error.message
    os.truncate(path, MAX_PHOTO_BYTES)
    assert admit_photo("big.jpg", paths).size_bytes == MAX_PHOTO_BYTES == 10_485_760


def test_the_open_file_is_compared_again_after_the_upload(paths: FixedPaths) -> None:
    path = put_file(paths, "report.pdf", b"report")
    admitted = admit(str(path), paths)
    with open_admitted(admitted, paths) as opened:
        assert unchanged(admitted, opened)
        with path.open("ab") as more:
            more.write(b"!")
        assert not unchanged(admitted, opened)
    with open_admitted(admit(str(path), paths), paths) as opened:
        path.unlink()
        assert not unchanged(admit_record(path, opened), opened)


def admit_record(path: Path, opened: object) -> AdmittedFile:
    info = os.fstat(opened.fileno())  # type: ignore[attr-defined]
    return AdmittedFile(
        str(path), str(path), path.name, info.st_size, info.st_dev, info.st_ino, info.st_mtime_ns
    )


def test_a_refused_outbox_setting_is_not_allowed_naming_it(paths: FixedPaths) -> None:
    paths.data_dir.mkdir(parents=True)
    paths.config.write_text('outbox_dir = "/etc"\n')
    error = refused("report.pdf", paths)
    assert error.code == "not_allowed"
    assert "outbox_dir" in error.message


def test_a_refused_downloads_setting_does_not_stop_a_send(paths: FixedPaths) -> None:
    paths.data_dir.mkdir(parents=True)
    paths.config.write_text('downloads_dir = "/etc"\n')
    put_file(paths, "report.pdf")
    assert admit("report.pdf", paths).name == "report.pdf"


def test_the_outbox_is_created_when_missing(paths: FixedPaths) -> None:
    assert refused("report.pdf", paths).code == "not_found"
    assert outbox(paths).is_dir()


# Between the question and the upload (BR25.5, NFR3.2) ------------------------------------------


def test_the_checked_descriptor_is_the_file_that_was_admitted(paths: FixedPaths) -> None:
    path = put_file(paths, "report.pdf", b"report")
    admitted = admit(str(path), paths)
    with open_admitted(admitted, paths) as opened:
        assert os.fstat(opened.fileno()).st_ino == admitted.inode
        assert opened.read() == b"report"


def changed(paths: FixedPaths, how: str) -> Path:
    path = outbox(paths) / "report.pdf"
    if how == "replaced":
        path.unlink()
        path.write_bytes(b"other!")
    elif how == "grown":
        with path.open("ab") as more:
            more.write(b" and more")
    elif how == "touched":
        info = os.stat(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    elif how == "relinked":
        twin = outbox(paths) / "twin.pdf"
        os.link(path, twin)
        path.unlink()
        path.symlink_to(twin)
    elif how == "removed":
        path.unlink()
    return path


@pytest.mark.parametrize("how", ["replaced", "grown", "touched", "relinked", "removed"])
def test_a_file_changed_after_the_question_is_refused(paths: FixedPaths, how: str) -> None:
    path = put_file(paths, "report.pdf", b"report")
    admitted = admit(str(path), paths)
    changed(paths, how)
    with pytest.raises(FileRefused) as caught:
        open_admitted(admitted, paths)
    assert caught.value.code == "not_allowed"
    assert "changed after the question" in caught.value.message


def test_file_records_live_180_seconds_and_at_most_100(paths: FixedPaths) -> None:
    clock = FakeClock()
    store = FileStore(clock=clock)
    admitted = admit(str(put_file(paths, "report.pdf")), paths)
    store.put("digest-a", admitted)
    clock.advance(179)
    assert store.take("digest-a") == admitted
    assert store.take("digest-a") is None  # used once
    store.put("digest-b", admitted)
    clock.advance(181)
    assert store.take("digest-b") is None
    for number in range(MAX_RECORDS + 5):
        store.put(f"d{number}", admitted)
    assert len(store) == MAX_RECORDS
    assert store.take("d0") is None
    assert store.take(f"d{MAX_RECORDS + 4}") == admitted
    assert timedelta(seconds=180) == store.lifetime


# Through the broker --------------------------------------------------------------------------


async def test_a_file_changed_after_the_allow_is_not_uploaded(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    path = put_file(paths, "report.pdf", b"report")
    args = {"chat": ALICE.id, "path": str(path)}
    asked = await confirm(approval_hook, SEND, args)
    assert f"File: {path}" in question_text(asked)
    changed(paths, "grown")
    done = await call(claude, SEND, args)
    assert done["error"]["code"] == "not_allowed"
    assert world.sent_files == []
    assert "upload_part" not in world.call_names()  # no transfer was accepted
    again = await call(claude, SEND, args)
    assert again["error"]["code"] == "needs_confirmation"  # the approval was used


async def test_a_send_with_no_file_record_uploads_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    broker: Broker,
    world: FakeGateway,
    paths: FixedPaths,
) -> None:
    path = put_file(paths, "report.pdf", b"report")
    args = {"chat": ALICE.id, "path": str(path)}
    await confirm(approval_hook, SEND, args)
    files = broker.registry.files
    assert files is not None
    files.clear()
    done = await call(claude, SEND, args)
    assert done["error"]["code"] == "not_allowed"
    assert "ask again" in done["error"]["message"]
    assert world.sent_files == []


async def test_a_refused_path_prepares_no_question(
    approval_hook: RawClient, broker: Broker, paths: FixedPaths
) -> None:
    refusal = await check(approval_hook, SEND, {"chat": ALICE.id, "path": "/etc/hosts"})
    assert (refusal["decision"], refusal["code"]) == ("refuse", "not_allowed")
    assert broker.gate.open_count() == 0
