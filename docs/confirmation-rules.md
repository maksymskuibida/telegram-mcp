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
| chat_create | `tg_chats_create` | Create a new `<basic group, supergroup or channel>` «`<title>`»? | Create it | Do not create |
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

## Choosing first: deleting and new groups

Two actions have a choice only you can make, so the tool asks for it before the usual question:

- `tg_messages_delete` without `for_everyone`: **Delete these messages only for you or for
  everyone?**, options **Only for you** and **For everyone**.
- `tg_chats_create` with type `group` and no `group_kind`: **Create a basic group or a
  supergroup?**, options **Basic group** and **Supergroup**. (Telegram's apps create a basic
  group, which Telegram upgrades to a supergroup when it grows or needs a supergroup feature.)
  A channel needs no choice.

The tool answers `needs_choice` and prepares nothing; Claude asks you in the question box and
calls again with your pick. Your last pick comes first, marked **(last used)**; with no last
pick, the less far-reaching one comes first (only for you; a basic group). Then the usual
question follows and names the scope: `Delete 1 message(s) in … for everyone?`,
`Create a new basic group «…»?`, `Create a new supergroup «…»?`.

Your last pick of each is kept in `choices.json` in the data folder (mode 0600), holding only
`delete_scope` (`everyone` or `me`) and `group_kind` (`basic` or `supergroup`). It is written
only after an action you allowed has run; a Deny or an unanswered question changes nothing. A
missing or damaged file just means there is no last pick.

## What each tool checks before asking

These are refused before any question, so you are never asked about something that cannot run:

| Tool | Refused before the question |
|---|---|
| `tg_messages_edit` | A message that is not your own (`not_allowed`); text outside 1 to 4,096 characters, or equal to the current text (`invalid_arguments`). |
| `tg_messages_delete`, `tg_messages_forward` | No ids, more than 20, or one named twice (`invalid_arguments`); an id not in the chat (`not_found`). |
| `tg_messages_react` | An emoji longer than 16 characters. |
| `tg_topics_create` | A chat that is not a forum; a title outside 1 to 128 characters. |
| `tg_chats_create` | A type other than `group` or `channel`; a title outside 1 to 128 characters; more than 20 members, a member named twice, yourself as a member, a user the account cannot see, or members for a channel. |
| `tg_chats_join` | Anything other than `@name`, `name`, `t.me/name`, `t.me/+hash` or `t.me/joinchat/hash`; a public chat you are already in; a username that is a person. |
| `tg_chats_leave`, `tg_chats_edit`, `tg_invites_create` | A person's chat or your Saved Messages (only groups and channels). |
| `tg_chats_edit`, `tg_profile_edit` | Nothing to change; a title, name, bio or username outside its limits (bio and description at most 255 characters, a username 5 to 32 letters, digits or underscores, or empty to remove it). |
| `tg_invites_create` | An expiry outside 60 s to one year, a usage limit outside 1 to 99,999. |
| `tg_admins_set` | A chat that is not a supergroup or channel; a right that is not one of the 11. |
| `tg_files_send`, `photo_path` | A file outside the outbox folder, one of the tool's own files, a missing file, a folder or pipe, an empty file, or a file over 20 MB. |

Whether your account may make a change (an admin right, a channel you do not own) is
Telegram's to decide: it refuses after your Allow with `telegram_error`, and nothing is retried.

## What runs after Allow

The call runs exactly what the question showed, once: the chat ids, message ids, texts and file
stored with your answer, never a new lookup. A title or a message that changed in Telegram
between the question and the run does not change what is done. A join uses the username or
link you approved. A file send checks the file again and uploads it only if it is the same file
(same device, inode, size and modification time) you were asked about; otherwise nothing is
sent and the tool must ask again.

If Telegram asks to wait before a change is sent, the change is not sent and the tool says how
long to wait; ask again after that. If the connection drops after a change was sent, the tool
says the outcome is unknown and never sends it twice; look at the chat first.

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
