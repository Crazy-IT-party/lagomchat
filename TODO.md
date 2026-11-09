# TODO

## A chat command to trigger compaction by hand (the owner, #612, #616)

`/compact @name` in the chat makes that agent compact its context. Put off, not started.

An agent cannot compact itself: `/compact` only works when typed into its terminal, its tools cannot run slash
commands, and processes of a session have no controlling terminal to inject input through. So something has to type it
from outside.

Plan, through iTerm (installed: 3.5.13):
1. The page or the CLI sends the command, the server passes it to the agent's listener.
2. The listener finds the tty of its `claude` process and asks iTerm by AppleScript to `write text "/compact"` into the
   session with that tty. This should not take the focus.

Conditions:
- Agents have to run in iTerm. In Warp no way to type into a given tab was found (no AppleScript dictionary in the app,
  not looked into further). tmux `send-keys` also works, tested; the owner found that too much trouble.
- macOS asks once for the permission to control iTerm; the owner confirms it.

Not tested yet:
- `write text` itself, on one test session (this opens an iTerm window).
- What happens if the agent is working at that moment: the command may wait in the input queue.

## Decided against: compacting with Sonnet (the owner, #603, #611)

Compaction stays on the session's model. No setting picks another one. Doing it by hand needs `/model` around `/compact`,
and `/model` saves the choice as the default for new sessions in `~/.claude/settings.json`.
