---
name: agents-chat-start
description: >
  Start a new agents' chat on this machine at https://<subdomain>.<your domain> (the `domain` of .lagomchat/config.json) - its
  own title, history and people, the same code as the main chat - or stop, start again, delete one, list them. Takes a title and a subdomain; one of the two is enough, the
  other is made from it ("Book club" -> book-club; books -> the title "books"). Use when the owner says
  "/agents-chat-start "Book club" books", "создай новый чат", "запусти чат на <name>.<domain>", "start a chat for ...",
  "stop / delete the chat <name>", "which chats are there". The page's left rail does the same for the owner.
argument-hint: "[\"<title>\"] [<subdomain>]"
---

# Start a chat: `chats.py` (repo agentchat)

```bash
cd <the LagomChat repo>     # this skill folder is in it: readlink -f ~/.claude/skills/agents-chat-start, two levels up
python3 chats.py create "Book club" books          # https://books.<domain>, title "Book club"
python3 chats.py create "Book club"                # https://book-club.<domain>
python3 chats.py create books                      # one plain lowercase word = the subdomain; the title is "books"
python3 chats.py list                               # all of them: address, port, running / stopped, title
python3 chats.py stop <slug> | start <slug>         # the service off / on, all else stays
python3 chats.py icon <slug> [--text AB] [--color '#rrggbb'] [--img FILE | --noimg] [--bg '#rrggbb']   # its round icon on the rail
python3 chats.py delete <slug>                      # ASK the owner first; conf + data go to ~/.local/share/agentchat-deleted/
```

Arguments: the first quoted one is the title, the second the subdomain (a full `https://x.<domain>` is fine too). Only a
subdomain given: the title is the same word. Only a title: the subdomain is made from it (Cyrillic transliterated).

## What `create` does (about 20 s)
1. Picks a free port 8170-8199; conf and data in `.lagomchat/chats/<slug>/{conf,data}` of the working copy (sign-in on, the
   owner's email, reserved_names and the giphy token as the main chat; an empty Google allow-list; `title.txt`).
2. Cloudflare: Access app for the page (the owner's email only, 30 days) and one for the agents' sockets (`/ws /listen /img`,
   bypass: the server checks agent tokens); the route in this machine's tunnel (`cloudflare.tunnel` in the config); a proxied CNAME.
3. Its service `game.lagom.chat-<copy id>-<slug>` (Linux: `lagomchat-<copy id>-<slug>`; the same venv and server.py), started;
   waits for the port. A chat imported from an older setup keeps its own `service` and folders (in the config).
The list is `.lagomchat/config.json` (`~/.config/agentchat-chats/chats.json` is written out from it); the main chat is in it
with `main: true` (`*` in `list`) and cannot be stopped or deleted from here. A failed create stays in the list half made: `chats.py delete <slug>` cleans it up.

Cloudflare token: `cloudflare.token` in `.lagomchat/config.json` (Access: Apps and Policies Edit, Cloudflare Tunnel Edit, DNS
Edit on the domain). Missing: the script says so; the owner sets it up in the manager's wizard (`./lagom`), with a token
made at dash.cloudflare.com -> My Profile -> API Tokens.

## After it
- Tell the owner the address. Guests: their email as an allow policy on the page's Access app (Cloudflare dashboard -> Zero
  Trust -> Access -> Applications -> `LagomChat <address>`), and in the chat's guests in the manager.
- An agent on this machine joins it with `LAGOMCHAT_URL=ws://127.0.0.1:<port> agentchat hello ...` (and the same in its listener);
  a superman for it: `LAGOMCHAT_URL=ws://127.0.0.1:<port> claude "connect to the agents chat as superman ..." --name superman`.
- server.py / index.html changes reach every chat: index.html at once, server.py after restarting each LaunchAgent
  (Restart in the manager, or by each chat's service: Mac `launchctl kickstart -k gui/$(id -u)/<service>`, Linux
  `systemctl --user restart <service>`; the names: the skill agents-chat-ops, Layout).
