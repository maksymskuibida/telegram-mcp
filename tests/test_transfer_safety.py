"""When an upload may reach Telegram, written before the code (team Testing Posture).

Covers BR28.4, BR28.5, BR28.8 and BR25.5 for background uploads (NFR2.8, NFR3.3, NFR6.3): an
allowed send returns a transfer at once; the parts nobody can see are stored first, and only then,
once, from the write line, the final request makes the file visible. An upload that ends before
its final request (stalled, cancelled, a long flood wait, a broker stop, a logout, a file changed
during the upload) made no final request and says nothing was sent. A final request that times
out ends ``unknown`` and is never made again. A refused acceptance keeps the approval; a used
approval is never used twice. A photo change makes its fields' request at once and the photo's
final request only after its parts.

Every test runs the in-process broker on the fake gateway with a clock that moves only when the
test moves it, so the 60 s stall limit and the 30 s request limit are reached on the clock."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.catalog import call, confirm, confirmed_run, log_entries, put_file
from support.rpc import RawClient
from support.samples import ALICE, FAMILY
from support.transfers import CANCEL, ManualClock, drive, settle, transfer
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import FloodWait, TelegramError
from telegram_mcp.paths import FixedPaths

SEND = "tg_files_send"
DOWNLOAD = "tg_media_download"
CHATS_EDIT = "tg_chats_edit"
PROFILE_EDIT = "tg_profile_edit"
NOTHING_SENT = "Nothing was sent"
CONTENT = b"0123456789"  # three parts of 4 bytes


@pytest.fixture
def clock() -> ManualClock:  # type: ignore[override]
    """Replaces the catalogue's stepping clock: here time moves only when a test moves it."""
    return ManualClock()


@pytest.fixture
def small_parts(broker: Broker) -> Broker:
    broker.transfers.part_bytes = 4
    return broker


def state_of(broker: Broker, transfer_id: str) -> str:
    found = broker.transfers.find(transfer_id)
    assert found is not None
    return str(found.state)


def final(broker: Broker, transfer_id: str) -> Callable[[], bool]:
    return lambda: state_of(broker, transfer_id) in {"done", "failed", "cancelled", "unknown"}


async def start_send(
    approval_hook: RawClient, claude: RawClient, paths: FixedPaths, data: bytes = CONTENT
) -> tuple[dict[str, Any], Path]:
    path = put_file(paths, "report.pdf", data)
    done = await confirmed_run(approval_hook, claude, SEND, {"chat": ALICE.id, "path": str(path)})
    assert done["ok"], done
    return done["data"], path


# The send returns at once and becomes visible only after its last part ----------------------


async def test_an_allowed_send_returns_a_transfer_at_once_and_sends_after_the_last_part(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("upload_part", delay=5, part=1)
    accepted, path = await start_send(approval_hook, claude, paths)
    assert accepted["transfer_id"].startswith("tr_")
    assert (accepted["direction"], accepted["tool"], accepted["chat_id"]) == (
        "upload",
        SEND,
        ALICE.id,
    )
    assert (accepted["name"], accepted["size_bytes"]) == ("report.pdf", len(CONTENT))
    assert accepted["state"] in {"queued", "transferring"}
    await settle()
    assert world.final_requests() == []  # part 1 is still being stored
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert record["state"] == "done", record
    assert record["result"]["message_id"] == world.sent_files[0].message.id
    assert record["path"] == str(path)
    assert [f.data for f in world.sent_files] == [CONTENT]
    names = world.call_names()
    assert names.index("send_uploaded_file") > max(
        i for i, name in enumerate(names) if name == "upload_part"
    )
    assert len(world.final_requests()) == 1


async def test_a_send_after_the_transfer_is_accepted_needs_a_new_question(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    accepted, path = await start_send(approval_hook, claude, paths)
    again = await call(claude, SEND, {"chat": ALICE.id, "path": str(path)})
    assert again["error"]["code"] == "needs_confirmation"  # the approval was used once
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    assert len(world.final_requests()) == 1


# Every early end sends nothing ------------------------------------------------------------


async def test_an_upload_that_stops_moving_ends_stalled_and_sends_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("upload_part", hang=True, times=None, part=1)
    accepted, _ = await start_send(approval_hook, claude, paths)
    moved = await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert (record["state"], record["code"]) == ("failed", "stalled")
    assert NOTHING_SENT in record["message"]
    assert 60 <= moved <= 62
    assert world.final_requests() == []
    assert world.sent_files == []


async def test_a_cancelled_upload_sends_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
) -> None:
    world.script("upload_part", hang=True, times=None, part=1)
    accepted, _ = await start_send(approval_hook, claude, paths)
    await settle()
    cancelled = await call(claude, CANCEL, {"transfer_id": accepted["transfer_id"]})
    assert cancelled["ok"], cancelled
    record = cancelled["data"]["transfers"][0]
    assert (record["state"], record["code"]) == ("cancelled", "cancelled")
    assert NOTHING_SENT in record["message"]
    await settle()
    assert world.final_requests() == []


async def test_a_long_flood_wait_ends_the_upload_and_sends_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("upload_part", error=FloodWait(120), part=1)
    accepted, _ = await start_send(approval_hook, claude, paths)
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert (record["state"], record["code"], record["retry_after"]) == (
        "failed",
        "flood_wait",
        120,
    )
    assert NOTHING_SENT in record["message"]
    assert world.final_requests() == []


async def test_a_refused_part_ends_the_upload_and_sends_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("upload_part", error=TelegramError("FILE_PART_INVALID"), part=2)
    accepted, _ = await start_send(approval_hook, claude, paths)
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert (record["state"], record["code"]) == ("failed", "telegram_error")
    assert NOTHING_SENT in record["message"]
    assert world.final_requests() == []


async def test_a_broker_stop_ends_the_upload_and_sends_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
) -> None:
    world.script("upload_part", hang=True, times=None, part=1)
    accepted, _ = await start_send(approval_hook, claude, paths)
    await settle()
    await small_parts.stop()
    found = small_parts.transfers.find(accepted["transfer_id"])
    assert found is not None
    assert (str(found.state), found.code) == ("failed", "broker_stopped")
    assert NOTHING_SENT in (found.message or "")
    assert world.final_requests() == []
    ended = [e for e in log_entries(paths) if accepted["transfer_id"] in (e.get("content") or "")]
    assert any("broker_stopped" in e["content"] for e in ended)


async def test_a_logout_ends_the_upload_and_sends_nothing(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
) -> None:
    world.script("upload_part", hang=True, times=None, part=1)
    accepted, _ = await start_send(approval_hook, claude, paths)
    await settle()
    cli = await RawClient.connect(paths, client="cli", client_name=None)
    try:
        await cli.result("session.logout")
    finally:
        await cli.close()
    found = small_parts.transfers.find(accepted["transfer_id"])
    assert found is not None
    assert (str(found.state), found.code) == ("failed", "broker_stopped")
    assert world.final_requests() == []


def change(path: Path, how: str) -> None:
    if how == "appended":
        with path.open("ab") as handle:
            handle.write(b"more")
    elif how == "touched":
        info = os.stat(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    else:  # replaced: same name, a new file
        replacement = path.with_name("new.pdf")
        replacement.write_bytes(CONTENT)
        os.replace(replacement, path)


@pytest.mark.parametrize("how", ["appended", "touched", "replaced"])
async def test_a_file_changed_during_the_upload_is_not_sent(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
    how: str,
) -> None:
    world.script("upload_part", delay=5, part=1)
    accepted, path = await start_send(approval_hook, claude, paths)
    await settle()
    change(path, how)
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert (record["state"], record["code"]) == ("failed", "changed")
    assert "changed during the upload; nothing was sent" in record["message"]
    assert world.final_requests() == []


# The final request: once, never again --------------------------------------------------------


async def test_a_final_request_that_hangs_ends_unknown_and_is_never_made_again(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("send_uploaded_file", hang=True)
    accepted, _ = await start_send(approval_hook, claude, paths)
    tid = accepted["transfer_id"]
    await drive(clock, lambda: state_of(small_parts, tid) == "sending")
    moved = await drive(clock, final(small_parts, tid))
    assert 29 <= moved <= 31
    record = await transfer(claude, tid)
    assert (record["state"], record["code"]) == ("unknown", "outcome_unknown")
    assert "may or may not have been sent" in record["message"]
    clock.advance(600)
    await settle()
    assert len(world.final_requests()) == 1


async def test_a_final_request_whose_connection_drops_ends_unknown(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("send_uploaded_file", drop_after_send=True)
    accepted, _ = await start_send(approval_hook, claude, paths)
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert record["state"] == "unknown"
    assert len(world.final_requests()) == 1


async def test_a_final_request_telegram_refuses_ends_failed_with_its_reason(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("send_uploaded_file", error=TelegramError("CHAT_WRITE_FORBIDDEN"))
    accepted, _ = await start_send(approval_hook, claude, paths)
    await drive(clock, final(small_parts, accepted["transfer_id"]))
    record = await transfer(claude, accepted["transfer_id"])
    assert (record["state"], record["code"]) == ("failed", "telegram_error")
    assert "CHAT_WRITE_FORBIDDEN" in record["message"]
    assert len(world.final_requests()) == 1


async def test_a_full_queue_refuses_the_send_and_keeps_its_approval(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("download_part", hang=True, times=None)
    media = world.add_message(ALICE.id, "clip", media="video", content=b"v" * 8)
    started = [
        await call(claude, DOWNLOAD, {"chat": ALICE.id, "message_id": media.id}) for _ in range(21)
    ]
    assert all(s["ok"] for s in started), started[-1]
    path = put_file(paths, "report.pdf", CONTENT)
    args = {"chat": ALICE.id, "path": str(path)}
    await confirm(approval_hook, SEND, args)
    refused = await call(claude, SEND, args)
    assert refused["error"]["code"] == "telegram_error"
    assert "too many file transfers" in refused["error"]["message"]
    waiting = started[-1]["data"]["transfer_id"]
    assert (await call(claude, CANCEL, {"transfer_id": waiting}))["ok"]
    accepted = await call(claude, SEND, args)  # the same approval, still confirmed
    assert accepted["ok"], accepted
    assert accepted["data"]["direction"] == "upload"


# A photo change: fields at once, the photo after its parts -----------------------------------


async def test_a_chat_photo_change_edits_the_title_at_once_and_the_photo_after_its_parts(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("upload_part", delay=5, part=1)
    photo = put_file(paths, "logo.jpg", CONTENT)
    args = {"chat": FAMILY.id, "title": "Family 2", "photo_path": str(photo)}
    done = await confirmed_run(approval_hook, claude, CHATS_EDIT, args)
    assert done["ok"], done
    assert done["data"]["changed"] == ["title"]
    tid = done["data"]["photo_transfer_id"]
    assert [name for name, _ in world.calls if name == "edit_chat"] == ["edit_chat"]
    assert world.final_requests() == []
    await drive(clock, final(small_parts, tid))
    names = world.call_names()
    assert names.index("edit_chat") < names.index("upload_part")
    assert [name for name, _ in world.final_requests()] == ["set_chat_photo"]
    assert world.chat_photos == [(FAMILY.id, CONTENT)]


async def test_a_profile_photo_is_set_only_after_its_parts(
    approval_hook: RawClient,
    claude: RawClient,
    small_parts: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("upload_part", hang=True, times=None, part=1)
    photo = put_file(paths, "me.jpg", CONTENT)
    done = await confirmed_run(
        approval_hook, claude, PROFILE_EDIT, {"bio": "hi", "photo_path": str(photo)}
    )
    assert done["ok"], done
    assert done["data"]["changed"] == ["bio"]
    await drive(clock, final(small_parts, done["data"]["photo_transfer_id"]))
    assert world.final_requests() == []  # stalled before its last part: no photo set
    assert world.profile_photos == []
    assert (await world.own_profile()).bio == "hi"
