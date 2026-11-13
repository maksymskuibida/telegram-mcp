"""The catalogue itself and what its tools leave in the activity log (test-after). Covers BR20.1 to
BR20.3, NFR1.1, NFR1.4 and NFR8.4 to NFR8.6: exactly the 30 tools of C5 with C2's kinds, honest
descriptions, no secret in any argument, result field or log entry, every call and result logged,
and files named by path, name and size only."""

from __future__ import annotations

from typing import Any

from support.catalog import (
    CAROL,
    CLUB,
    PHOTO_BYTES,
    answer,
    call,
    check,
    confirmed_run,
    log_entries,
    put_file,
)
from support.rpc import RawClient
from support.samples import ALICE, FAMILY, FORUM, TEAM
from telegram_mcp.actions import default_registry
from telegram_mcp.consent import ActionKind
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths
from telegram_mcp.tools import ASK_AGAIN_SENTENCE, TOOL_SPECS, WRITE_FIRST_CALL
from telegram_mcp.vault import SessionVault
from test_catalog_reads import READS
from test_catalog_writes import WRITES

# C5: every tool and its kind (R read, W write, M may mark); C2: each write's kind-table row.
C5 = {
    "tg_chats_list": "R",
    "tg_messages_send": "W",
    "tg_folders_list": "R",
    "tg_chats_resolve": "R",
    "tg_chats_info": "R",
    "tg_messages_read": "M",
    "tg_messages_pinned": "R",
    "tg_messages_search": "R",
    "tg_messages_edit": "W",
    "tg_messages_delete": "W",
    "tg_messages_forward": "W",
    "tg_messages_react": "W",
    "tg_messages_pin": "W",
    "tg_messages_unpin": "W",
    "tg_chats_mark_read": "M",
    "tg_topics_list": "R",
    "tg_topics_create": "W",
    "tg_chats_create": "W",
    "tg_chats_join": "W",
    "tg_chats_leave": "W",
    "tg_chats_edit": "W",
    "tg_invites_create": "W",
    "tg_members_list": "R",
    "tg_admins_set": "W",
    "tg_contacts_list": "R",
    "tg_users_info": "R",
    "tg_profile_get": "R",
    "tg_profile_edit": "W",
    "tg_media_download": "R",
    "tg_files_send": "W",
}
C2_WRITE_KINDS = {
    "send": ["tg_messages_send"],
    "file_send": ["tg_files_send"],
    "edit": ["tg_messages_edit"],
    "delete": ["tg_messages_delete"],
    "forward": ["tg_messages_forward"],
    "react": ["tg_messages_react"],
    "pin": ["tg_messages_pin", "tg_messages_unpin"],
    "topic_create": ["tg_topics_create"],
    "chat_create": ["tg_chats_create"],
    "join": ["tg_chats_join"],
    "leave": ["tg_chats_leave"],
    "chat_edit": ["tg_chats_edit"],
    "invite": ["tg_invites_create"],
    "admin": ["tg_admins_set"],
    "profile_edit": ["tg_profile_edit"],
}
SECRET_NAMES = {
    "session",
    "api_hash",
    "api_id",
    "code",
    "login_code",
    "password",
    "phone_code_hash",
    "authorization",
    "authorizations",
    "privacy",
    "approval_id",
    "method",
    "request",
}


def test_the_registry_is_exactly_c5_with_c2_kinds(paths: FixedPaths) -> None:
    decls = default_registry(paths).decls()
    assert set(decls) == set(C5)
    for tool, kind in C5.items():
        decl = decls[tool]
        if kind == "R":
            assert (decl.kind, decl.marks_read_option) == (ActionKind.READ, False), tool
        elif tool == "tg_messages_read":
            assert (decl.kind, decl.marks_read_option) == (ActionKind.READ, True)
        elif tool == "tg_chats_mark_read":
            assert decl.kind is ActionKind.MARK
        else:
            assert decl.kind is ActionKind.WRITE, tool
    kinds: dict[str, list[str]] = {}
    for tool, decl in decls.items():
        if decl.write_kind is not None:
            kinds.setdefault(decl.write_kind, []).append(tool)
    assert {k: sorted(v) for k, v in kinds.items()} == {
        k: sorted(v) for k, v in C2_WRITE_KINDS.items()
    }
    assert {spec.name for spec in TOOL_SPECS} == set(C5)


def test_descriptions_say_whether_a_tool_asks_or_marks() -> None:
    for spec in TOOL_SPECS:
        kind = C5[spec.name]
        text = spec.description
        if kind == "W" and spec.name != "tg_messages_send":
            assert text.startswith(WRITE_FIRST_CALL), spec.name
            assert text.endswith(ASK_AGAIN_SENTENCE), spec.name
            assert "ask_again" in spec.input_schema["properties"]
        elif kind == "R" and spec.name != "tg_chats_list":
            assert "This asks nothing and marks nothing as read." in text, spec.name
            assert "content from Telegram" in text, spec.name
    specs = {spec.name: spec.description for spec in TOOL_SPECS}
    assert "only when the chat or topic has unread messages" in specs["tg_chats_mark_read"]
    assert "unless mark_read is true" in specs["tg_messages_read"]
    assert "older unread ones included" in specs["tg_messages_read"]


def test_no_argument_names_a_secret_or_a_telegram_method() -> None:
    for spec in TOOL_SPECS:
        assert SECRET_NAMES.isdisjoint(spec.input_schema["properties"]), spec.name
        assert spec.input_schema["additionalProperties"] is False


def field_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = set(value)
        for item in value.values():
            names |= field_names(item)
        return names
    if isinstance(value, list):
        return set().union(*(field_names(item) for item in value)) if value else set()
    return set()


async def run_every_u4_tool(
    hook: RawClient, client: RawClient, world: FakeGateway, paths: FixedPaths
) -> list[dict[str, Any]]:
    """Every U4 tool once (reads, writes, the mark), returning each result."""
    results = [await call(client, tool, args) for tool, args in READS]
    results.append(await call(client, "tg_media_download", {"chat": ALICE.id, "message_id": 3}))
    for tool, args, _, _ in sorted(WRITES, key=lambda write: write[0] == "tg_messages_delete"):
        results.append(await confirmed_run(hook, client, tool, args))
    results.append(
        await confirmed_run(
            hook,
            client,
            "tg_admins_set",
            {"chat": TEAM.id, "user": CAROL, "rights": {"pin_messages": True}},
        )
    )
    path = put_file(paths, "report.pdf", b"the file content 0123")
    args = {"chat": ALICE.id, "path": str(path)}
    results.append(await confirmed_run(hook, client, "tg_files_send", args))
    world.invite_targets["SecretHash42"] = CLUB.id
    join = {"link_or_username": "https://t.me/+SecretHash42"}
    results.append(await confirmed_run(hook, client, "tg_chats_join", join))
    world.add_message(FORUM.id, "unread idea", topic_id=9)
    mark = {"chat": FORUM.id, "topic_id": 9}
    asked = await check(hook, "tg_chats_mark_read", mark)
    await answer(hook, asked)
    results.append(await call(client, "tg_chats_mark_read", mark))
    return results


async def test_every_tool_call_and_result_is_logged_without_secrets(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    results = await run_every_u4_tool(approval_hook, claude, world, paths)
    assert all(result["ok"] for result in results), [r for r in results if not r["ok"]]
    for result in results:
        assert SECRET_NAMES.isdisjoint(field_names(result["data"]))
    entries = log_entries(paths)
    for tool in set(C5) - {"tg_chats_list", "tg_messages_send"}:
        events = [e for e in entries if e["tool"] == tool]
        assert {e["event"] for e in events} >= {"call", "result"}, tool
        assert any(e["event"] == "result" and e["result"] == "ok" for e in events), tool
    leave = [e for e in entries if e["tool"] == "tg_chats_leave" and e["event"] == "result"]
    assert leave[-1]["chat_id"] == FAMILY.id
    loaded = SessionVault().load().session
    assert loaded is not None
    text = paths.log.read_text(encoding="utf-8") + paths.diagnostics.read_text(encoding="utf-8")
    assert loaded.session not in text
    assert loaded.api_hash not in text
    assert "SecretHash42" not in text
    assert "FakeInvite" not in text


async def test_files_are_logged_by_path_name_and_size_only(
    approval_hook: RawClient, claude: RawClient, paths: FixedPaths
) -> None:
    path = put_file(paths, "report.pdf", b"PRIVATE-CONTENT-1234")
    args = {"chat": ALICE.id, "path": str(path)}
    assert (await confirmed_run(approval_hook, claude, "tg_files_send", args))["ok"]
    download = await call(claude, "tg_media_download", {"chat": ALICE.id, "message_id": 3})
    saved = download["data"]["path"]
    entries = log_entries(paths)
    prompt = next(e for e in entries if e["tool"] == "tg_files_send" and e["event"] == "prompt")
    assert f"File: {path}" in prompt["content"]
    assert "report.pdf, 0.0 KB (20 bytes)" in prompt["content"]
    sent = [e for e in entries if e["tool"] == "tg_files_send" and e["event"] == "result"][-1]
    assert sent["content"] == f"{path} (report.pdf, 20 bytes)"
    got = [e for e in entries if e["tool"] == "tg_media_download" and e["event"] == "result"][-1]
    assert got["content"] == f"{saved} (holiday.jpg, {len(PHOTO_BYTES)} bytes)"
    text = paths.log.read_text(encoding="utf-8")
    assert "PRIVATE-CONTENT-1234" not in text
    assert PHOTO_BYTES.decode("latin-1") not in text


async def test_what_claude_looked_for_is_logged(claude: RawClient, paths: FixedPaths) -> None:
    await call(claude, "tg_messages_search", {"query": "budget 2026"})
    await call(claude, "tg_chats_resolve", {"query": "@team_chat"})
    calls = [e for e in log_entries(paths) if e["event"] == "call"]
    assert {"tool": "tg_messages_search", "content": "budget 2026"}.items() <= calls[0].items()
    assert {"tool": "tg_chats_resolve", "content": "@team_chat"}.items() <= calls[1].items()
