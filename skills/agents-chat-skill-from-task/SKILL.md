---
name: agents-chat-skill-from-task
description: >
  Turn the task this session just finished into a reusable skill (SKILL.md), confirmed by the owner in the agents' chat
  first: a short description of how the skill will work, the whole SKILL.md folded under it with an Edit button, and his changes applied before
  it goes live. Use when the owner says "сделай из этого скилл", "создай скилл из последнего задания", "make a skill of
  what you just did", "пусть это будет skill", or invokes /agents-chat-skill-from-task.
---

# A skill from the last finished task

The goal: the next agent that gets the same kind of request does it in one go - without re-discovering the hosts, paths,
commands and traps this session found the hard way.

## 1. What the task was

From this session's own context (not from memory of other sessions):
- the request - the owner's words and the message `#id`; the phrases he used (they become the trigger phrases);
- the steps that worked, in order: commands, files, hosts, services, skills used;
- the dead ends and traps: what failed and why, what must not be done (a rule of CLAUDE.md that applied, a refusal);
- what changes each time (the arguments) and what stays (hosts, paths, the order of steps).

Nothing finished yet, or the last task was a one-off with nothing to repeat: say so to the owner in one line and stop.

## 2. New, or an update

Look for a skill that already covers it: `ls ~/.claude/skills/ <LagomChat repo>/skills/ .claude/skills/ 2>/dev/null`
and grep their `description:` lines. One covers it: propose adding to it (step 4 shows the diff, not a new file).

## 3. Write the draft - not live yet

A folder in `~/.claude/skills/` is live at once, so the draft goes to `~/.cache/agentchat/skill-drafts/<name>/SKILL.md`.

- `name`: short kebab-case, what it does (`agents-chat-title`, `agents-chat-pin`), not taken.
- `description`: what it does + "Use when ..." with the owner's own phrases (Russian and English) - this is what makes it
  trigger. Who may use it (e.g. superman only), if the server limits it.
- The body: in English; the commands exactly as they worked, with placeholders for what changes; the order of steps;
  the traps as short "do not / because" lines; where the result is checked. No one-off values (ids, timestamps, a
  temporary file) unless they are stable. Short: a page, not a diary.
- Where it will live (say it in step 4):
  - about the agents' chat -> `skills/<name>` of the LagomChat repo (the folder this skill lives in: `readlink -f
    ~/.claude/skills/agents-chat-skill-from-task`, two levels up; its skills are symlinked into `~/.claude/skills`) - that folder may be held by another agent: ask it in the chat, do not write there yourself;
  - about one repo only -> `<repo>/.claude/skills/<name>`;
  - else -> `~/.claude/skills/<name>`.

## 4. The owner's confirmation (in the chat, never a blocking dialog)

1. The description and the whole SKILL.md in one message: `agentchat say --to owner --doc <draft> --doc-name SKILL.md
   "<3-5 lines: the name, when it triggers, what it does step by step, where it will live>"` (the doc up to 20000
   characters). The page shows the description with the text folded under it (first lines + Show all) and an Edit
   button; `say` prints the new message's `#id` - keep it.
2. `agentchat ask "Skill <name>: create it?" "Create" "Change" "Cancel"`.
3. His answer:
   - "Create" -> step 5;
   - he pressed Edit: a reply to that message carrying the edited text (your listener line says so) -
     `agentchat doc <id of the reply> --out <draft>`, read what he changed, say it in 1-2 lines, ask again;
   - "Change" or a reply in words -> apply it to the draft, post it again with `--doc` (1-3 lines of what changed),
     ask again;
   - "Cancel" -> delete the draft, one line to him, stop.

## 5. Make it live

- Move the draft into its place (step 3); a repo folder: `git add` + commit (as the repo's CLAUDE.md says), push only if asked.
- `agentchat hello` sends skills only at the hello: after a new one `agentchat skills` sends the list again
  (built-ins by hand: `agentchat skills --add "name=what it does"`).
- `agentchat say --to owner "Skill <name> is live: <path>. /<name> or the phrases: <2-3 of them>."`
- A running Claude Code session usually sees a new skill folder at once; if `/` does not list it, a new session will.
