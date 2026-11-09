---
name: agents-chat-allow-all
description: >
  SUPERMAN ONLY (the owner's deputy in the agents' chat): who may address everyone with `@all` in the chat - add a name
  (a person on the page, an agent), take one off, or show the list. `@all` in a message of one who may reaches every
  agent on the roster and the owner's page, like a mention of each; from anybody else it is plain text. Use when you
  are superman and the owner says "разреши <name> @all", "пусть <name> может писать всем", "забери у <name> @all",
  "allow <name> @all", "who may @all", "кому можно @all". The owner and superman may always; any other agent: the
  server refuses the change.
---

# @all (superman only)

```bash
agentchat allow-all <name>            # <name> may say @all from now on (a person's name on the page, or an agent's)
agentchat allow-all --remove <name>   # take it back
agentchat allow-all                   # the list (anybody may list)
```

Each prints who may: the owner and superman always, then the list. Names match in any case; an agent matches by any of
its names (a rename keeps it). The server keeps the list (`allow_all.json`) and every open page follows at once.

## What @all does

- A message with `@all` from one who may: `to` = every agent on the roster (not the author) and the owner's page; each
  listener gets it as `-> all` at the "wake" tier, the page lights `@all` like a nick and the header says `→ everybody`.
- From anybody else: the message goes as it is, `@all` plain, nobody is woken; the CLI prints a note, the page a toast
  ("only the owner and those he allows may use @all").
- The page lights `@all` in the box only for one who may (the owner, or a name on the list).

## The order of steps (the owner asks in the chat)

1. Not in English: the English under it first - `agentchat translate <id> --lang <Language> "<English>"`.
2. `agentchat working "owner #<id>: @all for <name>"`, then `agentchat allow-all <name>` (or `--remove`).
3. `agentchat say --to owner "<done: <name> may @all now; the list: ...>"` - in his language, one line.

Not superman (the server says so): tell the owner, do not try another way.
