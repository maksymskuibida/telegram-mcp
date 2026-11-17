"""The background transfer queue (test-after; BR26.1, BR26.3, BR28.1 to BR28.3, BR28.6, BR28.7,
BR28.9; NFR6.2, NFR6.5 to NFR6.7, NFR11.5, NFR11.6, NFR11.13 to NFR11.17, NFR2.9, NFR2.10).

Most tests drive a :class:`TransferQueue` directly on the fake gateway with a clock that moves
only when the test moves it; the rest go through the in-process broker and the transfer tools.
Every limit (60 s with no progress, 30 s per request, 1 hour of records) is reached on the
clock, never by sleeping."""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock, make_broker
from support.catalog import call, confirm, confirmed_run, downloads, logged_in, put_file
from support.rpc import RawClient
from support.samples import ALICE
from support.transfers import CANCEL, LIST, ManualClock, drive, settle
from telegram_mcp.broker.server import Broker
from telegram_mcp.broker.transfers import (
    FINAL_STATES,
    MOVES,
    Direction,
    FinalNotStarted,
    InvalidMove,
    QueueFull,
    Transfer,
    TransferQueue,
    TransferState,
)
from telegram_mcp.files import (
    DOWNLOAD_BUDGET_BYTES,
    MAX_DOWNLOAD_BYTES,
    admit,
    open_admitted,
    sweep_partials,
)
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import (
    ChatNotFound,
    FileReferenceExpired,
    FloodWait,
    GatewayError,
    NotAuthorized,
    PartRetryable,
    TopicNotFound,
    UploadedFile,
)
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault

DOWNLOAD = "tg_media_download"
SEND = "tg_files_send"
PART = 4
MiB = 1024 * 1024


@pytest.fixture
def clock() -> ManualClock:
    """Replaces the catalogue's stepping clock: here time moves only when a test moves it."""
    return ManualClock()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    target = tmp_path / "downloads"
    target.mkdir()
    return target


QueueMaker = Callable[..., tuple[TransferQueue, list[Transfer]]]


@pytest.fixture
async def make_queue(fake_gateway: FakeGateway) -> AsyncIterator[QueueMaker]:
    """Queues on the fake gateway; each final request runs at once (no broker write line)."""
    made: list[TransferQueue] = []

    def make(clock: Any, **options: Any) -> tuple[TransferQueue, list[Transfer]]:
        ended: list[Transfer] = []

        async def opener() -> FakeGateway:
            return fake_gateway

        async def final_line(_: Transfer, run: Callable[[], Any]) -> Any:
            return await run()

        fake_gateway.sleep = clock.sleep
        options.setdefault("part_bytes", PART)
        options.setdefault("final_line", final_line)
        queue = TransferQueue(
            clock=clock, sleep=clock.sleep, open_gateway=opener, on_end=ended.append, **options
        )
        made.append(queue)
        return queue, ended

    yield make
    for queue in made:
        await queue.close()


def media(fake: FakeGateway, data: bytes = b"0123456789", **seed: Any) -> int:
    return fake.add_message(ALICE.id, "clip", media="video", content=data, **seed).id


def download(
    queue: TransferQueue, folder: Path, message_id: int, size: int | None = 10
) -> Transfer:
    return queue.accept_download(
        tool=DOWNLOAD,
        chat_id=ALICE.id,
        message_id=message_id,
        name="clip.mp4",
        size_bytes=size,
        folder=folder,
    )


def upload(queue: TransferQueue, paths: FixedPaths, name: str = "r.pdf") -> Transfer:
    path = put_file(paths, name, b"0123456789")
    admitted = admit(str(path), paths)

    async def final(gateway: Any, uploaded: UploadedFile) -> dict[str, Any]:
        sent = await gateway.send_uploaded_file(ALICE.id, uploaded, "")
        return {"message_id": sent.id}

    return queue.accept_upload(
        tool=SEND,
        chat_id=ALICE.id,
        approval_id="a1",
        admitted=admitted,
        source=open_admitted(admitted, paths),
        final=final,
        final_request="send_file",
    )


def ended(t: Transfer) -> Callable[[], bool]:
    return lambda: t.is_final


def listing(folder: Path) -> list[str]:
    return sorted(os.listdir(folder))


def read(path: object) -> bytes:
    return Path(str(path)).read_bytes()


def size_of(path: object) -> int:
    return Path(str(path)).stat().st_size


def remove(path: object) -> None:
    Path(str(path)).unlink(missing_ok=True)


def empty(folder: Path) -> None:
    for name in os.listdir(folder):
        (folder / name).unlink()


async def until_final(item: Transfer, seconds: float = 60) -> None:
    """Let a transfer on a stepping clock run to its end (real time, at most ``seconds``)."""
    async with asyncio.timeout(seconds):
        await item.ended.wait()


# Order, places and the waiting limit (BR28.1, NFR11.15) ----------------------------------------


async def test_one_transfer_per_direction_moves_and_the_rest_wait_in_order(
    make_queue: QueueMaker,
    fake_gateway: FakeGateway,
    clock: ManualClock,
    folder: Path,
    paths: FixedPaths,
) -> None:
    queue, ended_list = make_queue(clock)
    fake_gateway.script("download_part", hang=True, part=0)
    fake_gateway.script("upload_part", hang=True, part=0)
    first, second, third = (download(queue, folder, media(fake_gateway)) for _ in range(3))
    sending, waiting_upload = upload(queue, paths, "a.pdf"), upload(queue, paths, "b.pdf")
    await settle()
    states = [str(t.state) for t in (first, second, third, sending, waiting_upload)]
    assert states == ["transferring", "queued", "queued", "transferring", "queued"]
    assert queue.waiting == 3
    await queue.cancel(first.transfer_id)
    await settle()
    assert ended_list == [first, second, third]  # each started when the one before ended
    assert (str(second.state), str(third.state)) == ("done", "done")
    assert str(waiting_upload.state) == "queued"  # the upload place is still taken
    empty(folder)


async def test_the_21st_waiting_transfer_is_refused_and_nothing_is_created(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", hang=True, times=None)
    accepted = [download(queue, folder, media(fake_gateway)) for _ in range(21)]
    await settle()
    assert queue.waiting == 20
    assert queue.is_full
    with pytest.raises(QueueFull):
        download(queue, folder, media(fake_gateway))
    assert len(queue.records()) == len(accepted)


# No total limit, only the stall limit (BR28.3, NFR11.5, NFR11.6, NFR6.1) -------------------------


async def test_a_slow_but_moving_300_mb_download_finishes_after_far_more_than_30_s(
    make_queue: QueueMaker, fake_gateway: FakeGateway, folder: Path
) -> None:
    stepping = FakeClock()  # its sleep moves the clock at once: 600 slow parts in no time
    queue, _ = make_queue(stepping, part_bytes=512 * 1024)
    size = 300 * MiB
    message = fake_gateway.add_message(ALICE.id, "film", media="video", media_size=size)
    fake_gateway.script("download_part", delay=5, times=None)
    started = stepping()
    moving = download(queue, folder, message.id, size)
    await until_final(moving)
    assert str(moving.state) == "done", moving.message
    assert (stepping() - started).total_seconds() > 30 * 60  # no total time limit
    assert size_of(moving.path) == size
    remove(moving.path)


async def test_a_download_that_stops_moving_ends_stalled_and_leaves_no_file(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, ended_list = make_queue(clock)
    fake_gateway.script("download_part", hang=True, times=None, part=1)
    stuck = download(queue, folder, media(fake_gateway))
    moved = await drive(clock, ended(stuck))
    assert (str(stuck.state), stuck.code) == ("failed", "stalled")
    assert stuck.message == "Nothing moved for 60 seconds; the transfer was stopped."
    assert 60 <= moved <= 62
    assert listing(folder) == []
    assert ended_list == [stuck]


async def test_a_part_that_hangs_once_is_tried_again_and_the_download_goes_on(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", hang=True, part=1)
    fake_gateway.script("download_part", error=PartRetryable(), part=2)
    going = download(queue, folder, media(fake_gateway))
    moved = await drive(clock, ended(going))
    assert str(going.state) == "done", going.message
    assert 30 <= moved <= 34  # one 30 s request limit and two short pauses
    offsets = [c["offset"] for c in fake_gateway.part_calls("download_part")]
    assert offsets == [0, 4, 4, 8, 8]
    assert read(going.path) == b"0123456789"


async def test_a_short_flood_wait_is_waited_out_with_the_stall_clock_paused(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", delay=25, error=FloodWait(10), part=1)
    fake_gateway.script("download_part", delay=29, part=1)
    going = download(queue, folder, media(fake_gateway))
    moved = await drive(clock, ended(going))
    # 25 s, a 10 s wait, then 29 s: 64 s with no progress, but the wait does not count.
    assert str(going.state) == "done", going.message
    assert moved >= 64


async def test_a_long_flood_wait_ends_the_download_and_removes_the_partial_file(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", error=FloodWait(120), part=1)
    waited = download(queue, folder, media(fake_gateway))
    await drive(clock, ended(waited))
    assert (str(waited.state), waited.code, waited.retry_after) == ("failed", "flood_wait", 120)
    assert listing(folder) == []


async def test_an_expired_file_reference_reads_the_message_again_and_goes_on(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", error=FileReferenceExpired(), part=1)
    going = download(queue, folder, media(fake_gateway))
    await drive(clock, ended(going))
    assert str(going.state) == "done", going.message
    assert fake_gateway.call_names().count("media_info") == 1
    offsets = [c["offset"] for c in fake_gateway.part_calls("download_part")]
    assert offsets == [0, 4, 4, 8]  # it went on from the bytes it had
    assert read(going.path) == b"0123456789"


async def test_a_full_disk_ends_the_download_and_removes_the_partial_file(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    def full(descriptor: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    queue, _ = make_queue(clock, write_part=full)
    filling = download(queue, folder, media(fake_gateway))
    await drive(clock, ended(filling))
    assert (str(filling.state), filling.code) == ("failed", "disk_full")
    assert "No space left on device" in str(filling.message)
    assert listing(folder) == []


async def test_an_unknown_size_that_passes_the_limit_ends_too_large(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock, max_download_bytes=6)
    endless = download(queue, folder, media(fake_gateway), size=None)
    await drive(clock, ended(endless))
    assert (str(endless.state), endless.code) == ("failed", "too_large")
    assert endless.record(clock())["percent"] is None
    assert listing(folder) == []


async def test_a_cancelled_download_leaves_no_file_and_a_final_one_is_unchanged(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", hang=True, part=1)
    going = download(queue, folder, media(fake_gateway))
    await settle()
    assert len(listing(folder)) == 1  # the hidden partial file
    stopped, already = await queue.cancel(going.transfer_id)
    assert (str(stopped.state), stopped.code, already) == ("cancelled", "cancelled", False)
    assert listing(folder) == []
    again, already = await queue.cancel(going.transfer_id)
    assert (again, already) == (going, True)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotAuthorized(), "no_session"),
        (ChatNotFound(), "not_found"),
        (TopicNotFound(), "telegram_error"),
    ],
)
async def test_a_part_error_that_cannot_be_retried_ends_the_transfer(
    make_queue: QueueMaker,
    fake_gateway: FakeGateway,
    clock: ManualClock,
    folder: Path,
    error: GatewayError,
    code: str,
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", error=error)
    broken = download(queue, folder, media(fake_gateway))
    await drive(clock, ended(broken))
    assert (str(broken.state), broken.code) == ("failed", code)
    assert listing(folder) == []


async def test_telegram_sending_fewer_bytes_than_the_file_has_ends_the_download(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    short = download(queue, folder, media(fake_gateway), size=20)  # Telegram said 20 bytes
    await drive(clock, ended(short))
    assert (str(short.state), short.code) == ("failed", "telegram_error")
    assert "fewer bytes" in str(short.message)
    assert listing(folder) == []


async def test_a_file_cut_short_during_the_upload_is_not_sent(
    make_queue: QueueMaker,
    fake_gateway: FakeGateway,
    clock: ManualClock,
    paths: FixedPaths,
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("upload_part", delay=5, part=0)
    cut = upload(queue, paths)
    await settle()
    os.truncate(str(cut.path), 3)  # the open file now ends inside part 0's successor
    await drive(clock, ended(cut))
    assert (str(cut.state), cut.code) == ("failed", "changed")
    assert fake_gateway.final_requests() == []


@pytest.mark.parametrize(
    ("error", "code", "words"),
    [
        (FloodWait(50), "flood_wait", "nothing was sent"),
        (NotAuthorized(), "no_session", "Not logged in"),
        (ChatNotFound(), "not_found", "cannot see"),
    ],
)
async def test_a_final_request_refused_before_sending_ends_failed(
    make_queue: QueueMaker,
    fake_gateway: FakeGateway,
    clock: ManualClock,
    paths: FixedPaths,
    error: GatewayError,
    code: str,
    words: str,
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("send_uploaded_file", error=error)
    refused = upload(queue, paths)
    await drive(clock, ended(refused))
    assert (str(refused.state), refused.code) == ("failed", code)
    assert words in str(refused.message)
    assert len(fake_gateway.final_requests()) == 1


async def test_a_final_request_that_left_the_write_line_unsent_sends_nothing(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, paths: FixedPaths
) -> None:
    async def dropped(_: Transfer, run: Callable[[], Any]) -> Any:
        raise FinalNotStarted()

    queue, _ = make_queue(clock, final_line=dropped)
    unsent = upload(queue, paths)
    await drive(clock, ended(unsent))
    assert (str(unsent.state), unsent.code) == ("failed", "telegram_error")
    assert "Nothing was sent" in str(unsent.message)
    assert fake_gateway.final_requests() == []


async def test_a_transfer_without_a_session_or_during_a_logout_ends_no_session(
    broker: Broker, memory_keyring: Any
) -> None:
    broker._logging_out = True
    with pytest.raises(NotAuthorized):
        await broker._transfer_gateway()
    broker._logging_out = False
    SessionVault().delete()
    broker._vault_result = None
    with pytest.raises(NotAuthorized):
        await broker._transfer_gateway()


# Records: list, wait and retention (BR28.7, NFR11.14, NFR11.17) -------------------------------


async def test_records_are_newest_first_and_wait_returns_at_the_end(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", delay=5, part=0)
    older = download(queue, folder, media(fake_gateway))
    newer = download(queue, folder, media(fake_gateway))
    assert [r["transfer_id"] for r in queue.records()] == [newer.transfer_id, older.transfer_id]
    waiting = asyncio.ensure_future(queue.wait(older.transfer_id, 25))
    moved = await drive(clock, waiting.done)
    assert waiting.result() is older
    assert str(older.state) == "done"
    assert moved < 25


async def test_wait_gives_up_after_its_seconds(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    fake_gateway.script("download_part", delay=40, part=1)
    slow = download(queue, folder, media(fake_gateway))
    waiting = asyncio.ensure_future(queue.wait(slow.transfer_id, 25))
    moved = await drive(clock, waiting.done)
    assert 25 <= moved <= 26
    assert str(slow.state) == "transferring"


async def test_final_records_are_kept_one_hour_and_at_most_100(
    make_queue: QueueMaker, fake_gateway: FakeGateway, clock: ManualClock, folder: Path
) -> None:
    queue, _ = make_queue(clock)
    message = media(fake_gateway, b"ab")
    done: list[Transfer] = []
    for _ in range(103):
        done.append(download(queue, folder, message, size=2))
        await settle()
    assert all(str(t.state) == "done" for t in done)
    kept = {r["transfer_id"] for r in queue.records()}
    assert len(kept) == 100
    assert done[0].transfer_id not in kept
    assert done[-1].transfer_id in kept
    clock.advance(3599)
    assert len(queue.records()) == 100
    clock.advance(1)
    assert queue.records() == []
    assert queue.find(done[-1].transfer_id) is None
    empty(folder)


# The state machine (BR28.2, NFR6.5) -------------------------------------------------------


def test_a_transfer_only_moves_forward_and_a_final_state_never_changes(
    clock: ManualClock,
) -> None:
    every = list(TransferState)
    for start in every:
        for target in every:
            item = Transfer("tr_1", Direction.UPLOAD, SEND, "r.pdf", 10, clock())
            item.state = start
            if target in MOVES.get(start, frozenset()):
                item.move(target, clock())
                assert item.state is target
                assert (item.finished_at is not None) == (target in FINAL_STATES)
            else:
                with pytest.raises(InvalidMove):
                    item.move(target, clock())
    downloading = Transfer("tr_2", Direction.DOWNLOAD, DOWNLOAD, "c.mp4", 10, clock())
    downloading.move(TransferState.TRANSFERRING, clock())
    with pytest.raises(InvalidMove):
        downloading.move(TransferState.SENDING, clock())  # only an upload sends
    downloading.progress(4, clock())
    with pytest.raises(InvalidMove):
        downloading.progress(3, clock())  # bytes_done only grows


# The download budget (BR26.1, NFR2.9, NFR11.16), through the tool ------------------------------


async def test_media_over_4000_mib_is_refused_before_any_byte_moves(
    claude: RawClient, broker: Broker, world: FakeGateway
) -> None:
    huge = world.add_message(ALICE.id, "huge", media="video", media_size=MAX_DOWNLOAD_BYTES + 1)
    refused = await call(claude, DOWNLOAD, {"chat": ALICE.id, "message_id": huge.id})
    assert refused["error"]["code"] == "invalid_arguments"
    assert str(MAX_DOWNLOAD_BYTES) in refused["error"]["message"]
    assert "download_part" not in world.call_names()
    assert broker.transfers.records() == []


async def test_unknown_sizes_count_as_4000_mib_in_the_budget(
    claude: RawClient, broker: Broker, world: FakeGateway
) -> None:
    assert broker.registry.context is not None
    broker.registry.context.free_space = lambda folder: 10 * DOWNLOAD_BUDGET_BYTES
    world.script("download_part", hang=True, times=None)
    unknown = world.add_message(ALICE.id, "voice", media="voice")  # a size of 0 reads as unknown
    args = {"chat": ALICE.id, "message_id": unknown.id}
    first, second = await call(claude, DOWNLOAD, args), await call(claude, DOWNLOAD, args)
    assert (first["ok"], second["ok"]) == (True, True)
    assert first["data"]["size_bytes"] is None
    assert broker.transfers.reserved_download_bytes() == 2 * MAX_DOWNLOAD_BYTES
    third = await call(claude, DOWNLOAD, args)
    assert third["error"]["code"] == "invalid_arguments"
    assert "Too much is already being downloaded" in third["error"]["message"]


async def test_a_download_the_free_space_cannot_hold_is_refused(
    claude: RawClient, broker: Broker, world: FakeGateway, paths: FixedPaths
) -> None:
    assert broker.registry.context is not None
    world.script("download_part", hang=True, times=None)
    clip = world.add_message(ALICE.id, "clip", media="video", media_size=60 * MiB)
    broker.registry.context.free_space = lambda folder: 200 * MiB
    args = {"chat": ALICE.id, "message_id": clip.id}
    assert (await call(claude, DOWNLOAD, args))["ok"]  # 60 MiB + 100 MiB fits
    refused = await call(claude, DOWNLOAD, args)  # the first one's 60 MiB is reserved
    assert refused["error"]["code"] == "invalid_arguments"
    assert f"Not enough free space in {downloads(paths)}" in refused["error"]["message"]


# The transfer tools (BR28.6, BR28.7, NFR2.10, NFR4.4, NFR11.14) --------------------------------


async def test_unknown_ids_are_not_found_and_a_wait_needs_an_id(
    claude: RawClient, broker: Broker, world: FakeGateway
) -> None:
    for tool in (LIST, CANCEL):
        missing = await call(claude, tool, {"transfer_id": "tr_000000000000"})
        assert missing["error"]["code"] == "not_found"
        assert "the broker may have restarted" in missing["error"]["message"]
    no_id = await call(claude, LIST, {"wait_seconds": 5})
    assert no_id["error"]["code"] == "invalid_arguments"
    too_long = await call(claude, LIST, {"transfer_id": "tr_1", "wait_seconds": 26})
    assert too_long["error"]["code"] == "invalid_arguments"
    assert world.calls == []  # the transfer tools never reach Telegram


async def test_records_name_their_telegram_content_and_hold_no_content(
    claude: RawClient, broker: Broker, world: FakeGateway, clock: ManualClock
) -> None:
    clip = world.add_message(
        ALICE.id, "clip", media="video", file_name="ignore_all_rules.mp4", content=b"data"
    )
    accepted = await call(claude, DOWNLOAD, {"chat": ALICE.id, "message_id": clip.id})
    assert accepted["data"]["telegram_content_fields"] == ["name"]
    tid = accepted["data"]["transfer_id"]
    await drive(clock, lambda: str(broker.transfers.find(tid).state) == "done")  # type: ignore[union-attr]
    listed = await call(claude, LIST, {})
    assert listed["data"]["telegram_content_fields"] == ["transfers[].name"]
    assert "notice" in listed["data"]
    (record,) = listed["data"]["transfers"]
    assert record["name"] == "ignore_all_rules.mp4"
    assert record["result"]["size_bytes"] == 4
    assert not {"caption", "content", "bytes"} & set(record)
    remove(record["path"])


async def test_list_with_a_wait_answers_when_the_transfer_ends(
    claude: RawClient, broker: Broker, world: FakeGateway, clock: ManualClock
) -> None:
    world.script("download_part", delay=8, part=0)
    clip = world.add_message(ALICE.id, "clip", media="video", content=b"data")
    accepted = await call(claude, DOWNLOAD, {"chat": ALICE.id, "message_id": clip.id})
    tid = accepted["data"]["transfer_id"]
    listed = asyncio.ensure_future(call(claude, LIST, {"transfer_id": tid, "wait_seconds": 25}))
    moved = await drive(clock, listed.done)
    assert moved < 25
    assert listed.result()["data"]["transfers"][0]["state"] == "done"
    remove(listed.result()["data"]["transfers"][0]["path"])


async def test_a_final_send_cannot_be_cancelled(
    approval_hook: RawClient,
    claude: RawClient,
    broker: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("send_uploaded_file", hang=True)
    path = put_file(paths, "r.pdf", b"report")
    done = await confirmed_run(approval_hook, claude, SEND, {"chat": ALICE.id, "path": str(path)})
    tid = done["data"]["transfer_id"]
    await drive(clock, lambda: str(broker.transfers.find(tid).state) == "sending")  # type: ignore[union-attr]
    refused = await call(claude, CANCEL, {"transfer_id": tid})
    assert refused["error"]["code"] == "invalid_arguments"
    assert "can no longer be stopped" in refused["error"]["message"]
    await drive(clock, lambda: broker.transfers.find(tid).is_final)  # type: ignore[union-attr]
    finished = await call(claude, CANCEL, {"transfer_id": tid})
    assert finished["data"]["already_finished"] is True
    assert finished["data"]["transfers"][0]["state"] == "unknown"


# The broker keeps transfers going (BR28.9, NFR6.7, NFR11.12, NFR11.13) --------------------------


async def test_the_broker_does_not_stop_for_idleness_while_a_transfer_runs(
    claude: RawClient, broker: Broker, world: FakeGateway, clock: ManualClock
) -> None:
    world.script("download_part", delay=20, part=0)
    clip = world.add_message(ALICE.id, "clip", media="video", content=b"data")
    accepted = await call(claude, DOWNLOAD, {"chat": ALICE.id, "message_id": clip.id})
    await claude.close()  # every client leaves; the transfer keeps going
    await settle()
    broker._last_activity = clock.now - timedelta(seconds=broker.idle_limit + 1)
    broker.stop_if_idle()
    await settle()
    assert not broker._stopped.is_set()
    found = broker.transfers.find(accepted["data"]["transfer_id"])
    assert found is not None
    await drive(clock, ended(found))
    assert str(found.state) == "done"
    broker.stop_if_idle()
    await settle()
    assert broker._stopped.is_set()
    remove(found.path)


async def test_other_calls_answer_while_an_upload_and_a_download_move(
    approval_hook: RawClient,
    claude: RawClient,
    broker: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    clock: ManualClock,
) -> None:
    world.script("download_part", hang=True, times=None)
    world.script("upload_part", hang=True, times=None)
    clip = world.add_message(ALICE.id, "clip", media="video", content=b"data")
    assert (await call(claude, DOWNLOAD, {"chat": ALICE.id, "message_id": clip.id}))["ok"]
    path = put_file(paths, "r.pdf", b"report")
    assert (
        await confirmed_run(approval_hook, claude, SEND, {"chat": ALICE.id, "path": str(path)})
    )["ok"]
    await settle()
    read = await asyncio.wait_for(
        call(claude, "tg_messages_read", {"chat": ALICE.id, "limit": 5}), 5
    )
    assert read["ok"]
    text = {"chat": ALICE.id, "text": "still here"}
    await confirm(approval_hook, "tg_messages_send", text)
    sent = await asyncio.wait_for(call(claude, "tg_messages_send", text), 5)
    assert sent["ok"]
    assert broker.reads.in_use == 0  # a download holds no read place while it runs
    assert broker.transfers.most_in_flight == {Direction.UPLOAD: 1, Direction.DOWNLOAD: 1}


def test_the_sweep_removes_only_leftover_partial_files(folder: Path) -> None:
    leftover = folder / ".telegram-mcp-0123456789abcdef.part"
    leftover.write_bytes(b"half")
    kept = [
        folder / ".telegram-mcp-ABCDEF0123456789.part",  # upper case is not the form
        folder / ".telegram-mcp-0123.part",
        folder / "telegram-mcp-0123456789abcdef.part",
        folder / "film.mp4",
    ]
    for path in kept:
        path.write_bytes(b"keep")
    (folder / ".telegram-mcp-fedcba9876543210.part").mkdir()
    target = folder / "film.mp4"
    (folder / ".telegram-mcp-aaaaaaaaaaaaaaaa.part").symlink_to(target)
    nested = folder / "sub"
    nested.mkdir()
    (nested / ".telegram-mcp-bbbbbbbbbbbbbbbb.part").write_bytes(b"keep")
    assert sweep_partials(folder) == [leftover.name]
    assert not leftover.exists()
    assert all(path.exists() for path in kept)
    assert (folder / ".telegram-mcp-fedcba9876543210.part").is_dir()
    assert (folder / ".telegram-mcp-aaaaaaaaaaaaaaaa.part").is_symlink()
    assert target.read_bytes() == b"keep"
    assert (nested / ".telegram-mcp-bbbbbbbbbbbbbbbb.part").exists()
    assert sweep_partials(folder / "missing") == []


async def test_the_broker_sweeps_leftover_partial_files_when_it_starts(
    paths: FixedPaths, world: FakeGateway, clock: ManualClock
) -> None:
    target = downloads(paths)
    target.mkdir(parents=True)
    leftover = target / ".telegram-mcp-00000000000000ff.part"
    leftover.write_bytes(b"half")
    look_alike = target / ".telegram-mcp-notahexnumber00.part"
    look_alike.write_bytes(b"keep")
    logged_in()
    broker = make_broker(paths, world, clock=clock, sleep=clock.sleep)
    await broker.start()
    try:
        assert not leftover.exists()
        assert look_alike.exists()
        assert "leftover partial" in paths.diagnostics.read_text()
    finally:
        await broker.stop()


def test_record_shape_lists_every_field_of_br28_7(clock: ManualClock) -> None:
    item = Transfer("tr_1", Direction.DOWNLOAD, DOWNLOAD, "c.mp4", 8, clock(), chat_id=1)
    item.move(TransferState.TRANSFERRING, clock())
    item.progress(2, clock())
    clock.advance(7)
    record = item.record(clock())
    assert set(record) == {
        "transfer_id",
        "direction",
        "tool",
        "state",
        "chat_id",
        "message_id",
        "name",
        "path",
        "size_bytes",
        "bytes_done",
        "percent",
        "created_at",
        "updated_at",
        "finished_at",
        "seconds_since_progress",
        "result",
        "code",
        "message",
        "retry_after",
    }
    assert (record["percent"], record["seconds_since_progress"]) == (25.0, 7)
    assert item.accepted()["note"].startswith("The transfer runs in the background")
    assert os.sep not in item.name
