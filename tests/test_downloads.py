"""tg_media_download (test-after). Covers BR26.1 to BR26.4, NFR1.3, NFR2.4, NFR2.7, NFR5.1,
NFR6.2 and NFR11.12: downloads go only into the downloads folder under a safe name, never over an
existing file, never leave a partial file behind, return path, name and size only, wait out a
short flood wait, and never hold up a send. Attempt 2: the call returns the accepted transfer at
once, and the file's path, name and size are the finished transfer's result (BR28.7)."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock
from support.catalog import (
    PHOTO,
    PHOTO_BYTES,
    call,
    confirm,
    downloads,
    finished,
    logged_in,
)
from support.rpc import RawClient
from support.samples import ALICE, FAMILY
from telegram_mcp.broker.server import Broker
from telegram_mcp.files import MAX_DOWNLOAD_BYTES, safe_name
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import FloodWait, TelegramError
from telegram_mcp.paths import FixedPaths

DOWNLOAD = "tg_media_download"


def leftovers(folder: Path) -> list[str]:
    return sorted(name for name in os.listdir(folder) if name.endswith(".part"))


async def download(client: RawClient, message_id: int = PHOTO, chat: int = ALICE.id) -> Any:
    return await call(client, DOWNLOAD, {"chat": chat, "message_id": message_id})


async def downloaded(client: RawClient, message_id: int = PHOTO) -> dict[str, Any]:
    """The finished download's record."""
    return await finished(client, await download(client, message_id))


async def test_a_download_returns_at_once_and_lands_in_the_downloads_folder_privately(
    claude: RawClient, paths: FixedPaths
) -> None:
    accepted = await download(claude)
    assert set(accepted["data"]) == {
        "transfer_id",
        "state",
        "direction",
        "tool",
        "chat_id",
        "message_id",
        "name",
        "size_bytes",
        "note",
        "telegram_content_fields",
        "notice",
    }
    assert (accepted["data"]["name"], accepted["data"]["size_bytes"]) == (
        "holiday.jpg",
        len(PHOTO_BYTES),
    )
    record = await finished(claude, accepted)
    expected = downloads(paths) / "holiday.jpg"
    assert record["state"] == "done"
    assert record["result"] == {
        "chat_id": ALICE.id,
        "message_id": PHOTO,
        "path": str(expected),
        "name": "holiday.jpg",
        "size_bytes": len(PHOTO_BYTES),
    }
    assert record["path"] == str(expected)
    assert expected.read_bytes() == PHOTO_BYTES
    assert stat.S_IMODE(os.stat(expected).st_mode) == 0o600
    assert leftovers(downloads(paths)) == []


async def test_a_message_with_no_file_is_refused(claude: RawClient) -> None:
    done = await download(claude, message_id=1)
    assert done["error"]["code"] == "invalid_arguments"
    assert (await download(claude, message_id=99))["error"]["code"] == "not_found"


async def test_a_file_over_4000_mib_is_refused_before_downloading(
    claude: RawClient, world: FakeGateway
) -> None:
    big = world.add_message(ALICE.id, "big", media="video", media_size=MAX_DOWNLOAD_BYTES + 1)
    done = await download(claude, message_id=big.id)
    assert done["error"]["code"] == "invalid_arguments"
    assert str(MAX_DOWNLOAD_BYTES + 1) in done["error"]["message"]
    assert "download_part" not in world.call_names()


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("../../x", "_.._x"),
        (".hidden", "hidden"),
        ("a\nb‮c.txt", "a_b_c.txt"),
        ("my report.pdf", "my_report.pdf"),
        ("x" * 300 + ".pdf", "x" * 196 + ".pdf"),
        ("...", "101_7.jpg"),
        (None, "101_7.jpg"),
    ],
)
def test_names_are_made_safe_or_built_from_the_ids(name: str | None, safe: str) -> None:
    assert safe_name(name, ALICE.id, 7, "photo") == safe


def test_names_from_ids_use_the_media_type() -> None:
    assert safe_name(None, -5, 2, "video") == "-5_2.mp4"
    assert safe_name(None, -5, 2, "voice") == "-5_2.ogg"
    assert safe_name("", -5, 2, "document") == "-5_2.bin"


async def test_a_hostile_name_stays_inside_the_downloads_folder(
    claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    hostile = world.add_message(
        ALICE.id, "", media="document", file_name="../../.ssh/authorized_keys", content=b"k"
    )
    record = await downloaded(claude, hostile.id)
    assert Path(record["path"]).parent == downloads(paths)
    assert record["name"] == "_.._.ssh_authorized_keys"


async def test_a_taken_name_gets_a_number_and_the_old_file_is_kept(
    claude: RawClient, paths: FixedPaths
) -> None:
    folder = downloads(paths)
    folder.mkdir(parents=True)
    (folder / "holiday.jpg").write_bytes(b"mine")
    record = await downloaded(claude)
    assert record["name"] == "holiday (1).jpg"
    assert (folder / "holiday.jpg").read_bytes() == b"mine"


async def test_a_link_at_the_wanted_name_is_never_followed(
    claude: RawClient, paths: FixedPaths
) -> None:
    folder = downloads(paths)
    folder.mkdir(parents=True)
    target = paths.home / "elsewhere.jpg"
    (folder / "holiday.jpg").symlink_to(target)
    record = await downloaded(claude)
    assert record["name"] == "holiday (1).jpg"
    assert not target.exists()


async def test_a_thousand_way_clash_ends_the_download_with_nothing_left(
    claude: RawClient, paths: FixedPaths
) -> None:
    folder = downloads(paths)
    folder.mkdir(parents=True)
    (folder / "holiday.jpg").write_bytes(b"0")
    for number in range(1, 1000):
        (folder / f"holiday ({number}).jpg").write_bytes(b"0")
    record = await downloaded(claude)
    assert record["state"] == "failed"
    assert "too many files" in record["message"]
    assert leftovers(folder) == []
    assert len(os.listdir(folder)) == 1000


async def test_a_failure_leaves_no_file(
    claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    world.script("download_part", error=TelegramError("FILE_PART_INVALID"))
    record = await downloaded(claude)
    assert (record["state"], record["code"]) == ("failed", "telegram_error")
    assert "FILE_PART_INVALID" in record["message"]
    assert os.listdir(downloads(paths)) == []


async def test_a_refused_downloads_setting_is_named_and_other_tools_still_work(
    claude: RawClient, paths: FixedPaths
) -> None:
    paths.config.write_text('downloads_dir = "/etc"\n')
    done = await download(claude)
    assert done["error"]["code"] == "not_allowed"
    assert "downloads_dir" in done["error"]["message"]
    read = await call(claude, "tg_messages_read", {"chat": ALICE.id})
    assert read["ok"] is True


async def test_a_short_flood_wait_is_waited_out_and_a_long_one_returned(
    claude: RawClient, world: FakeGateway, clock: FakeClock
) -> None:
    world.script("download_part", error=FloodWait(10))
    before = clock.now
    assert (await downloaded(claude))["state"] == "done"
    assert (clock.now - before).total_seconds() >= 10
    world.script("download_part", error=FloodWait(120))
    record = await downloaded(claude)
    assert (record["code"], record["retry_after"]) == ("flood_wait", 120)


async def test_a_stalled_download_leaves_no_partial_file(
    claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    world.script("download_part", hang=True, times=None)
    record = await downloaded(claude)
    assert (record["state"], record["code"]) == ("failed", "stalled")
    assert os.listdir(downloads(paths)) == []


@pytest.fixture
async def quick_broker(
    paths: FixedPaths, world: FakeGateway, clock: FakeClock
) -> AsyncIterator[Broker]:
    """A broker whose Telegram calls time out after 0.3 s."""
    logged_in()
    broker = Broker(
        paths,
        gateway_factory=lambda session, api_id, api_hash: world,
        clock=clock,
        sleep=clock.sleep,
        call_limit=0.3,
    )
    await broker.start()
    try:
        yield broker
    finally:
        await broker.stop()


async def test_a_download_does_not_delay_a_send(
    quick_broker: Broker, world: FakeGateway, paths: FixedPaths
) -> None:
    downloader = await RawClient.connect(paths)
    sender = await RawClient.connect(paths)
    hook = await RawClient.connect(paths, client="approval-hook", client_name=None)
    try:
        world.script("download_part", delay=20)
        accepted = await asyncio.wait_for(download(downloader), 0.25)  # returns at once
        assert accepted["ok"] is True
        args = {"chat": FAMILY.id}
        await confirm(hook, "tg_chats_leave", args)
        done = await asyncio.wait_for(call(sender, "tg_chats_leave", args), 0.25)
        assert done["ok"] is True
        assert (await finished(downloader, accepted))["state"] == "done"
    finally:
        for client in (downloader, sender, hook):
            await client.close()
