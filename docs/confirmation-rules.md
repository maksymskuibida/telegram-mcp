# Confirmation rules

Every action that changes something on your Telegram account, and every marking of messages as
read, runs only after you pick **Allow** in Claude Code's question box. This page lists exactly
what each question says, how long an answer is good for, and what happens when you say no.

The questions are written by the background broker alone, from typed fields; a tool can only
supply the values. Every question starts with `[tg-<id>] `, where `<id>` is ten random
characters that tie your answer to that one question.

## The kinds of action

Each action belongs to exactly one kind. An action whose kind is not in this table is refused
with `not_allowed` and never asked about.

| Kind | Tools | First line of the question | Under Allow | Under Deny |
|---|---|---|---|---|
| send | `tg_messages_send` | Send this message to `<chat>`? | Send it | Do not send |
| file_send | `tg_files_send` | Send this file to `<chat>`? | Send the file | Do not send |
| edit | `tg_messages_edit` | Edit your message `<n>` in `<chat>`? | Save the edit | Keep the message as it is |
| delete | `tg_messages_delete` | Delete `<k>` message(s) in `<chat>` for everyone? (or: only for you?) | Delete them | Do not delete |
| forward | `tg_messages_forward` | Forward `<k>` message(s) from `<source chat>` to `<chat>`? | Forward them | Do not forward |
| react | `tg_messages_react` | React `<emoji>` to message `<n>` in `<chat>`? | Add the reaction | Do not react |
| pin | `tg_messages_pin` | Pin message `<n>` in `<chat>`? | Pin it | Do not pin |
| pin | `tg_messages_unpin` | Unpin message `<n>` in `<chat>`? | Unpin it | Keep it pinned |
| topic_create | `tg_topics_create` | Create the topic «`<title>`» in `<chat>`? | Create the topic | Do not create |
| chat_create | `tg_chats_create` | Create a new `<group or channel>` «`<title>`»? | Create it | Do not create |
| join | `tg_chats_join` | Join `<chat>`? (or: Join the chat at `<link>`?) | Join | Do not join |
| leave | `tg_chats_leave` | Leave `<chat>`? | Leave | Stay |
| chat_edit | `tg_chats_edit` | Change `<chat>`? | Save the changes | Do not change |
| invite | `tg_invites_create` | Create an invite link for `<chat>`? | Create the link | Do not create |
| admin | `tg_admins_set` | Change the admin rights of «`<name>`» (id `<id>`) in `<chat>`? | Change the rights | Do not change |
| profile_edit | `tg_profile_edit` | Change your own profile? | Save the changes | Do not change |
| mark | `tg_chats_mark_read` | Mark `<k>` unread message(s) in `<chat>` as read? | Mark as read | Leave unread |
| mark | `tg_messages_read` with `mark_read` | After reading, mark `<chat>` as read up to the newest message Claude reads (`<k>` unread now)? | Mark as read | Leave unread |

`<chat>` is the chat's name in «», its type and its id, for example
`«Family» (group, id -202)`. Your own chat is named `your Saved Messages (id <id>)`. A forum
topic adds ` in topic «<title>» (id <n>)`.

Names (chats, topics, senders, members, new titles) have line breaks and other control
characters replaced by spaces, « and » replaced by quotes, and are cut to 64 characters, so a
name cannot pose as another chat or as the question's own text.

## What the rest of the question shows

- **Text is shown in full**, never cut. Line breaks and tabs are kept; every other control
  character, right-to-left override or isolate, and zero-width character is shown as
  `[U+XXXX]`, so what you read is what is sent.
- **An edit** shows `Now:` with the current text and `New:` with the new text.
- **Delete, forward, react, pin and unpin** list every message they act on, oldest first, as
  `#<id> <sender>, <YYYY-MM-DD HH:MM> UTC:` followed by its full text and `[<media>]` when it
  has media. At most **20 messages** per call; more are refused and Claude is told to split
  the call. A message id that is not in the chat is refused with `not_found`.
- **A forward** names both the chat the messages come from and the chat they go to.
- **Every local file** is shown by its full resolved path, its name and its size.
- **Chat and profile changes** show one line per changed field: `field: before → after`.
- **A new chat** lists its members by name and id, or says `no other members`.
- **An invite link** shows when it expires (or `never`) and its usage limit (or `none`).
- **An admin change** lists all 11 rights (`change_info`, `post_messages`, `edit_messages`,
  `delete_messages`, `ban_users`, `invite_users`, `pin_messages`, `add_admins`, `manage_call`,
  `anonymous`, `manage_topics`) as `on` or `off`, with ` (changes)` after each that differs
  from today. A right the call does not name is off; an unknown right is refused.

A question longer than **20,000 characters** is refused, not cut; Claude is told to split the
call into smaller ones.

## How long an answer is good for

- A question you have not answered expires **120 s** after it was asked.
- An **Allow must be used within 60 s** of your pick, even when that goes past the 120 s.
  After that the next call asks again.
- One Allow runs **one call**. Calling again with the same arguments asks again.
- An answer to a question that is unknown, already answered or expired changes nothing.

## Saying no, and asking again

For **60 s** after you pick Deny, or after a question expired unanswered, the same call (same
tool, same arguments) is refused with `declined` or `timed_out` instead of a new question. This
keeps Claude from asking the same thing again and again.

If you tell Claude in the chat to try again, it may call the tool once more with
`ask_again: true`. That only makes the question appear again; it never approves anything, and
you answer it like any other. It works at most **twice per action in 10 minutes**; a third time
is refused and Claude is told to ask you in the chat first. After the 60 s a plain call asks
again anyway.

These memories live only in the background broker; restarting it forgets them.

## Marking as read

Reading never marks anything as read: reading messages, listing chats or forum topics,
searching and the lookups behind a question all use requests that do not tell Telegram you read
anything. Only two calls mark:

- `tg_chats_mark_read`, and
- `tg_messages_read` with `mark_read: true`.

Before either, the broker checks how many messages are unread:

- **Nothing unread:** it runs without a question and marks nothing.
- **Something unread:** one question names the chat and the count. With Allow, messages are
  marked only up to the newest one counted at that moment; a message that arrives later stays
  unread. `tg_messages_read` marks no further than the newest message it returned either.
  Telegram marks everything up to one message, so older unread messages are marked too; the
  question says so.
- **The check fails:** nothing is marked and the tool says why.

In a forum group, a mark must name one topic with `topic_id`; `tg_topics_list` lists them and
marks nothing. Unread mentions and reactions are cleared only together with the messages they
belong to.

When you decline marking for `tg_messages_read`, or do not answer, the messages are still
returned, with `marked: false` and a note saying why. In a client other than Claude Code,
`mark_read` is ignored and the read runs.

## The activity log

Every question, every answer (allowed, declined, timed out, or unknown), every repeat refusal,
every `ask_again`, and every Allow that was not used in time is written to the activity log.
A question that cannot be written to the log is never asked. The session and `api_hash` are
replaced with `[redacted]`, and the secret part of an invite link (after `t.me/+` or
`joinchat/`) with `…`.
