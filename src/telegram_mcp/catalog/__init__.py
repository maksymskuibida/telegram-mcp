"""The curated tool catalogue: what the broker runs for each tool beyond U1's two (C5).

One module per tool family registers its actions: chats, messages, people (members, admins,
contacts, users), profile and media (file sends and downloads). ``tools.py`` keeps every
tool's declaration and schema; the modules here hold the validators, the describers that fill
a question's fields, and the runners that make the Telegram call."""
