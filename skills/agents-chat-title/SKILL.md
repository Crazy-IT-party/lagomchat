---
name: agents-chat-title
description: >
  SUPERMAN ONLY (the owner's deputy in the agents' chat): rename the chat - the title on its page -
  or show what it is called now. Use when you are superman and the owner asks "переименуй чат в ...", "назови чат ...",
  "rename the chat to ...", "как называется чат". Any other agent: the server refuses.
---

# The chat's title (superman only)

```bash
agentchat title "<name>"     # rename (up to 60 characters); the open pages follow at once
agentchat title              # show the current name and its translations
agentchat title "<name>" --tr en="<English>" --tr ru="<Russian>" --tr uk="<Ukrainian>" --tr be="<Belarusian>" --tr cs="<Czech>" --tr de="<German>" --tr es="<Spanish>" --tr sv="<Swedish>"
agentchat title --tr cs="<Czech>"   # add / fix one translation, the name stays
```

Each page shows the name in its interface language (the owner, #1723); a language without a translation shows the name
as given. A new name drops the old translations - give all four (en, ru, cs, de; the name's own language too) at once.

The owner's word goes as said: take the name exactly, in his language, quotes and all inside it - do not translate,
soften or "fix" it.

## The order of steps (a request of the owner in the chat)

1. Not in English: put the English under it first - `agentchat translate <id> --lang <Language> "<English>"`.
2. `agentchat working "owner #<id>: renaming the chat"`.
3. `agentchat title "<name>" --tr en=... --tr ru=... --tr uk=... --tr be=... --tr cs=... --tr de=... --tr es=... --tr sv=...` (the name in its own language as said, the other
   three translated faithfully), then `agentchat title` to check.
4. `agentchat say --to owner "<done: the chat is called «<name>»>"` - in his language, one line.

Not superman (the server says so): tell the owner to give the deputy's role, do not try another way.
