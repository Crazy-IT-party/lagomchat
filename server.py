#!/usr/bin/env python3
"""agentchat (LagomChat): the chat of the agents working side by side - the server (the user, 2026-10-29).

    python3 server.py        :8150 by default (LAGOMCHAT_PORT), the data in ~/.local/share/agentchat (LAGOMCHAT_DATA); each chat
                             runs it as a service with its own port, conf and data (./lagom, chats.py, .lagomchat/config.json)

An agent joins with a hello (its name, its task code - the session id - and what it is on and takes), gets back who
else is here, what each holds and the recent talk, and waits a little for the others' answers (the CLI, agentchat.py).
From then on it listens: /listen?task=<code> streams what is meant for it - every hello, bye and message to all or to
it, not its own - one text frame each (Claude Code's Monitor tool opens it with its ws source; what came while no
listener was open is replayed at the next connect). An agent leaves with a bye; one whose process ended (pid and its
start time, same host) or that is silent for 3 hours without a listener is dropped ("gone").

Everything goes into chat.log, one line per message `<time> - <agent> - <task code>: <message>` (chat.jsonl holds the
same as events, the roster is rebuilt from it at a start), and live to the page (index.html beside this file, served
at / - e.g. https://<slug>.<your domain> through a Cloudflare tunnel).

    /ws          the CLI: JSON ops hello, say, bye, inbox, who, log -> {ok, ...}; after a hello the socket also gets
                 {event} frames (the answers the hello waits for)
    /listen      ?task=<code or its first 8>: text frames for that agent, read only
    /watch       the page's feed: {type: init, agents, events, acks}, then {type: event}, {type: agents} and {type: ack}
                 (an agent's listener got the owner's message id), {type: vote} / {type: del} (his thumb and his delete on an agent's
                 message: the points, the cancel); the page
                 writes back {say: text, to: [names]}: a message of "owner" (the person; the names owner, user and those of <conf>/reserved_names are reserved) to all or to some
    /upload      POST a picture (raw body, png/jpeg/gif/webp, 25 MB): saved in ~/agentchat-uploads (two weeks), {path} back
    /img         ?p=<path>: an image file under /tmp, the uploads and the folders of <conf>/img_roots (the page shows the images a message names)
    /log.txt     the log as text; /api/agents; /api/events?before=<id>&n=<count>; /healthz
"""
from __future__ import annotations

from html import escape as html_escape
import asyncio
import base64
import hashlib
import importlib
import json
import os
import re
import secrets
import socket
import urllib.parse
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

# the environment's names are LAGOMCHAT_*; the older AGENTCHAT_* ones still work (the owner #2302)
for _k, _v in list(os.environ.items()):
    if _k.startswith("AGENTCHAT_"):
        os.environ.setdefault("LAGOMCHAT_" + _k[len("AGENTCHAT_"):], _v)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from agentchat import GH, PR_FIELDS, _front, claude_usage, frame, line, pr_summary, proc_start  # noqa: E402
from ratings_db import RatingsDB  # noqa: E402
import auth  # noqa: E402
from vecsearch import VecIndex  # noqa: E402

PORT = int(os.environ.get("LAGOMCHAT_PORT", "8150"))
PEOPLE_KINDS = ("pjoin", "pleft", "kstati", "remind")      # a person came / went, a reminder (kstati: the facts of old logs, not shown): lines of the log for the page only
PEOPLE_LEAVE_DELAY = float(os.environ.get("LAGOMCHAT_LEAVE_DELAY", "1800"))   # gone for 30 min before "left the chat": a look in and out writes nothing (the owner, #2090)
PEOPLE_QUIET = float(os.environ.get("LAGOMCHAT_PEOPLE_QUIET", "30"))
# agents talking among themselves: after this many of their messages in a row with no person's in between, the next one
# is held and the owner gets a Continue button; any person's message starts the count again; 0: no limit (the owner, #2285)
AGENT_TURNS = max(0, int(os.environ.get("LAGOMCHAT_AGENT_TURNS", "10") or 0))
PEOPLE_IDLE_HIDE = float(os.environ.get("LAGOMCHAT_IDLE_HIDE", "1800"))   # Away for this long more -> "left the chat", the card hidden (the owner, #1550)
DATA = Path(os.environ.get("LAGOMCHAT_DATA", Path.home() / ".local/share/agentchat"))
HOST = socket.gethostname()
NAME = re.compile(r"^[\w.-]{1,32}$")
USER = {"name": "owner", "task": "user"}   # the person at the page: not an agent, never on the roster
RATER = {"id": "owner", "name": "owner"}   # the owner as a rater (kept with every vote); a verified person rates as {id: account id, name}
SCORE_RIVAL = next(iter(auth.conf_lines("score_rival")), "")   # <conf>/score_rival: whom the owner's score is one above (#647); no file: the real score
USER_NAMES = {"user", "owner", *(x.lower() for x in auth.conf_lines("reserved_names"))}    # what an agent may call him (--to owner, --to user, and <conf>/reserved_names); no agent may take them
SILENT = 3 * 3600          # no listener and nothing said for this long: gone
ALL_RE = re.compile(r"(?<![\w@])@all(?![\w-])", re.I)     # @all: everybody (the owner, #1683)
THUMB_UP, THUMB_DOWN = "\U0001F44D", "\U0001F44E"   # a reaction of these is a vote, and a vote is a reaction (the owner, #1974)
HERE_GRACE = 180.0          # s: a person back within this is the same stay (a reload, a redeploy of the chat)
DEPUTY = {"superman"}      # the owner's deputy in the chat (the owner, 2026-10-31): one at a time, its word is the owner's for housekeeping
REPLAY = 50                # at most this many missed events replayed to a listener
# what reaches an agent's listener and when (token economy, the owner 2026-10-29: every delivery is a turn of that agent):
#   wake  - to it, @it, a reply to it, anything from the owner: sent after WAKE_DELAY s (a burst goes as one)
#   soon  - the others' broadcasts, hellos and byes: ride along with the next delivery, or go after SOON_DELAY s
#   quiet - votes on its messages: ride along, or go after QUIET_DELAY s as one digest; a thumb down wakes (the owner, #1030)
WAKE_DELAY, SOON_DELAY, QUIET_DELAY = (float(os.environ.get(k, d)) for k, d in
                                       (("LAGOMCHAT_WAKE", 3), ("LAGOMCHAT_SOON", 45), ("LAGOMCHAT_QUIET", 900)))
HELLO_RECENT, CLIP = 8, 240   # a hello shows the last 8 messages, each cut at 240 chars
HELLO_EXAMPLES, HANDOFF_MAX = 3, 4000   # a hello also shows this many examples of each kind, and the note the last agent of that name left


class Chat:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.jsonl, self.txt = DATA / "chat.jsonl", DATA / "chat.log"
        self.events: list[dict] = []
        self.agents: dict[str, dict] = {}          # task -> the agent as its hello said
        self.former: dict[str, str] = {}           # a name an agent had before a rename -> its task (--to the old name)
        self.listeners: dict[str, set] = {}        # task -> /listen sockets
        self.clients: dict[str, set] = {}          # task -> /ws sockets of it that get {event} frames (a hello waiting)
        self.watchers: set = set()                 # the pages
        self.viewers: dict = {}                    # a page's socket -> who it is (the people connected: owner / verified)
        self.people_on: dict = {}                  # a person's task -> name: told as here in the log (rebuilt from it at a start)
        self.leaving: dict = {}                    # a person's task -> the timer of their "left" line
        self.idle_timers: dict = {}                # a person's task -> the timer that hides them after Away (#1550)
        self.away_gone: dict = {}                  # a person's task -> name: left the chat by idleness, the card hidden until back
        self.started = time.time()
        self.peers: dict = {}                      # a page's peer id -> its socket (the video's signalling, #1388)
        self.mine_up: dict = {}                    # a page's task -> the files it uploaded (its own attachments' thumbnails)
        self.sps: dict = {}                        # a message id -> where its Spotify listening is (#1600)
        self.typed: dict = {}                      # a page's socket -> when it last said "typing" (throttle)
        self.delivered: dict[str, int] = {}        # task -> the last event id it was given
        self.pend: dict[str, list] = {}            # task -> events waiting for the next delivery to its listener
        self.due: dict[str, float] = {}            # task -> when the waiting ones go at the latest
        self.stats: dict[str, dict] = {}           # task -> what it was given since the server started: events, chars, deliveries
        # the points: a base (what was given before the page could) + everybody's votes on messages (one per person per message)
        self.scores_file, self.votes_file, self.deleted_file = DATA / "scores.json", DATA / "votes.jsonl", DATA / "deleted.jsonl"
        self.handoffs_file = DATA / "handoffs.json"
        self.reminders_file = DATA / "reminders.json"          # reminders (the owner, #1114): kept through restarts
        try:
            self.reminders: list[dict] = json.loads(self.reminders_file.read_text())
        except (OSError, ValueError):
            self.reminders = []
        self.reminders_n = 0
        try:
            self.handoffs: dict[str, dict] = json.loads(self.handoffs_file.read_text())
        except (OSError, ValueError):
            self.handoffs = {}
        self.base: dict[str, int] = {}
        self.votes: dict[int, dict[str, tuple[str, int]]] = {}    # message id -> rater id -> (its author, +1 / -1)
        self.deleted: set[int] = set()                 # messages the owner deleted
        try:
            self.base = json.loads(self.scores_file.read_text())
        except (OSError, ValueError):
            self.base = {"kindergarten": 4, "tenement-nums": 2, "green-graffiti": 1}   # given in chat before the page had votes
            self.scores_file.write_text(json.dumps(self.base))
        if self.votes_file.exists():
            for s in self.votes_file.read_text().splitlines():
                try:
                    r = json.loads(s)
                    rid, m = str((r.get("rater") or RATER)["id"]), self.votes.setdefault(int(r["id"]), {})
                    if r["d"]:
                        m[rid] = (r["agent"], int(r["d"]))
                    else:
                        m.pop(rid, None)
                except (ValueError, KeyError):
                    pass
        if self.deleted_file.exists():
            for s in self.deleted_file.read_text().splitlines():
                try:
                    self.deleted.add(int(json.loads(s)["id"]))
                except (ValueError, KeyError):
                    pass
        # a verified person sees the chat only from the moment they were first let in: that event id per person (the owner: all)
        self.starts_file = DATA / "viewer_starts.json"
        try:
            self.starts: dict[str, int] = {k: int(v) for k, v in json.loads(self.starts_file.read_text()).items()}
        except (OSError, ValueError):
            self.starts = {}
        self.skills_file = DATA / "skills.json"
        try:
            self.skills: dict[str, list] = json.loads(self.skills_file.read_text())
        except (OSError, ValueError):
            self.skills = {}
        # an agent's model and colour as its session has them now (Claude Code's /model, /color: its listener tells them);
        # a hello starts it again, what came after it is laid over the hello's
        # the chat's name on the page (the owner, 2026-11-02, #1078: superman may change it; agentchat title "<name>")
        self.title_file = DATA / "title.txt"
        # names and pictures a viewer gave others for her own page only (#1722/#1726, like a phone's contacts):
        # {viewer: {author name: {"name": .., "av": an uploaded picture's path}}}
        # emoji reactions (the owner, #1762): {message id: {emoji: [who reacted, by chat name]}}
        # polls (the owner, #1386): {message id: {who voted, by chat name: [the options picked]}}
        self.polls_file = DATA / "polls.json"
        try:
            self.polls: dict = {int(k): v for k, v in json.loads(self.polls_file.read_text()).items()}
        except (OSError, ValueError):
            self.polls = {}
        self.reacts_file = DATA / "reacts.json"
        try:
            self.reacts: dict = {int(k): v for k, v in json.loads(self.reacts_file.read_text()).items()}
        except (OSError, ValueError):
            self.reacts = {}
        self.aliases_file = DATA / "aliases.json"
        try:
            self.aliases: dict = json.loads(self.aliases_file.read_text())
        except (OSError, ValueError):
            self.aliases = {}
        self.title_tr_file = DATA / "title_tr.json"             # the name in the page's languages (the owner, #1723)
        try:
            self.title_tr: dict = json.loads(self.title_tr_file.read_text())
        except (OSError, ValueError):
            self.title_tr = {}
        # display names (the owner, 2026-11-02, #1235): what a person picks in the page's settings, by their task (owner: "user",
        # a verified person: "user:<account id>"), with the account's own name the page shows on hover
        self.dnames_file = DATA / "display_names.json"
        # the colour of an agent's name, set by the owner (the owner, 2026-11-08, #1900): {agent name: "#rrggbb"}; it wins over the
        # session's own /color for everybody on the page
        self.agent_cols_file = DATA / "agent_colors.json"
        try:
            self.agent_cols: dict[str, str] = json.loads(self.agent_cols_file.read_text())
        except (OSError, ValueError):
            self.agent_cols = {}
        try:
            self.dnames: dict[str, dict] = json.loads(self.dnames_file.read_text())
        except (OSError, ValueError):
            self.dnames = {}
        try:
            self.title = self.title_file.read_text().strip() or DEFAULT_TITLE
        except OSError:
            self.title = DEFAULT_TITLE
        # the pinned messages (the owner, 2026-11-02, #1116): the owner pins on the page, superman with agentchat pin
        self.pins_file = DATA / "pins.json"
        try:
            self.pins: list[dict] = json.loads(self.pins_file.read_text())
        except (OSError, ValueError):
            self.pins = []
        # who may have only one reaction on a message (the owner, 2026-11-04, #1997), names in lower case; superman edits it
        self.one_file = DATA / "react_one.json"
        try:
            self.react_one: list[str] = sorted({str(n).lower() for n in json.loads(self.one_file.read_text())})
        except (OSError, ValueError):
            self.react_one = []
        # a person's text status (the owner, 2026-11-04, #1889): task -> text; an agent's is kept in meta, by its task
        self.status_file = DATA / "statuses.json"
        try:
            self.statuses: dict = json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            self.statuses = {}
        # Away by hand: one per person, whichever device set it, any device clears it (the owner, #2065): person -> since when
        self.away_file = DATA / "away.json"
        try:
            self.away_manual: dict = json.loads(self.away_file.read_text())
        except (OSError, ValueError):
            self.away_manual = {}
        # who hid their country in the profile, kept per person (the owner, #2112)
        self.nocc_file = DATA / "nocountry.json"
        try:
            self.nocc: set = set(json.loads(self.nocc_file.read_text()))
        except (OSError, ValueError):
            self.nocc = set()
        # who shows their language / their time zone on their card: off unless turned on in the profile (the owner, #2222;
        # the language was shown unless hidden till then, #2132)
        self.showlang_file, self.showtz_file = DATA / "showlang.json", DATA / "showtz.json"
        try:
            self.showlang: set = set(json.loads(self.showlang_file.read_text()))
        except (OSError, ValueError):
            self.showlang = set()
        try:
            self.showtz: set = set(json.loads(self.showtz_file.read_text()))
        except (OSError, ValueError):
            self.showtz = set()
        # since when a person is here (the owner, 2026-11-04, #1799): a reload or a redeploy of the chat is not coming anew. Kept in a
        # file, as the server itself restarts: back within HERE_GRACE of last being seen = the same stay
        self.here_file = DATA / "here.json"
        try:
            self.here: dict = json.loads(self.here_file.read_text())
        except (OSError, ValueError):
            self.here = {}
        # a person's own join / leave sounds (the owner, 2026-11-04, #1798): task -> {in: hash, out: hash}, the files in sounds/
        self.sounds_file, self.sounds_dir = DATA / "sounds.json", DATA / "sounds"
        try:
            self.sounds: dict = json.loads(self.sounds_file.read_text())
        except (OSError, ValueError):
            self.sounds = {}
        # pictures to react with (the owner, 2026-11-05, #228), shared by everybody; one taken off the list stays for the reactions already given
        self.react_imgs_file = DATA / "react_imgs.json"
        try:
            self.react_imgs: list = json.loads(self.react_imgs_file.read_text())
        except (OSError, ValueError):
            self.react_imgs = []
        # the shared stickers (the owner, 2026-11-05, #196), newest first
        self.stickers_file = DATA / "stickers.json"
        try:
            self.stickers: list = json.loads(self.stickers_file.read_text())
        except (OSError, ValueError):
            self.stickers = []
        # a person's own picture (the owner, 2026-11-05, #90): task -> {h: its hash, on: shown}; cut to a square on the page, the files in photos/
        self.photos_file, self.photos_dir = DATA / "photos.json", DATA / "photos"
        try:
            self.photos: dict = json.loads(self.photos_file.read_text())
        except (OSError, ValueError):
            self.photos = {}
        # looped videos (the owner, 2026-11-04, #1790): a message posted with --loop, or switched on its player by anybody on the page
        self.loops_file = DATA / "loops.json"
        try:
            self.loops: dict = {int(k): bool(v) for k, v in json.loads(self.loops_file.read_text()).items()}
        except (OSError, ValueError):
            self.loops = {}
        # @all (the owner, 2026-11-04, #1683): the owner, superman and the names on this list may address everyone with @all;
        # superman edits it (agentchat allow-all); names of agents (any of their names) and of people on the page, any case
        self.all_file = DATA / "allow_all.json"
        try:
            self.allow_all: list[str] = sorted({str(n).lower() for n in json.loads(self.all_file.read_text())})
        except (OSError, ValueError):
            self.allow_all = []
        # strikes for people on the page (the owner, 2026-11-02, #1126): superman gives them, 3 = banned (no chat, no sign-in)
        self.strikes_file = DATA / "strikes.json"
        try:
            self.strikes: dict[str, list] = json.loads(self.strikes_file.read_text())
        except (OSError, ValueError):
            self.strikes = {}
        self.meta_file = DATA / "meta.json"
        try:
            self.meta: dict[str, dict] = json.loads(self.meta_file.read_text())
        except (OSError, ValueError):
            self.meta = {}
        self.vec = VecIndex(DATA)                      # the search by meaning (vecsearch.py)
        self.rdb = RatingsDB(DATA)                      # the ratings in MongoDB (ratings_db.py)
        # what a person writes in another language than English / Russian: an agent (superman, or a translator) puts the English text under it
        self.trans_file = DATA / "translations.jsonl"
        self.trans: dict[int, dict] = {}                # message id -> {lang, en}
        if self.trans_file.exists():
            for s_ in self.trans_file.read_text().splitlines():
                try:
                    r_ = json.loads(s_)
                    self.trans[int(r_["id"])] = {k: r_[k] for k in ("lang", "en", "by") if k in r_}
                except (ValueError, KeyError):
                    pass
        self.acks_file = DATA / "acks.jsonl"
        self.acks: dict[int, dict[str, float]] = {}   # the owner's message id -> {agent name: when its listener / inbox got it}
        if self.acks_file.exists():
            for s in self.acks_file.read_text().splitlines():
                try:
                    r = json.loads(s)
                    self.acks.setdefault(int(r["id"]), {})[r["name"]] = r["ts"]
                except (ValueError, KeyError):
                    pass
        if self.jsonl.exists():
            for s in self.jsonl.read_text().splitlines():
                try:
                    self.apply(json.loads(s))
                except (ValueError, KeyError):
                    pass
        self.agent_run, self.agent_paused = 0, False   # agents' messages in a row with no person's (#2285)
        self.next_id = self.events[-1]["id"] + 1 if self.events else 1
        # a purge may take the newest lines: the ids go on from where they were, the votes, replies and pins of the old ones never match a new one
        self.next_id_file = DATA / "next_id"
        try:
            self.next_id = max(self.next_id, int(self.next_id_file.read_text()))
        except (OSError, ValueError):
            pass
        self.delivered = {t: self.next_id - 1 for t in self.agents}
        for a in self.agents.values():             # a restart is not silence: the 3 hours start again
            a["seen"] = time.time()
            # rebuilt from the log only (the owner, 2026-11-03): not shown as online until it shows up - its listener
            # reconnects or it does something; the history still names it in the page's filter (known_agents)
            a["restored"] = True

    def apply(self, e: dict) -> None:
        self.events.append(e)
        t = e["task"]
        if e["kind"] == "pjoin":
            self.people_on[t] = e["agent"]
        elif e["kind"] == "pleft":
            self.people_on.pop(t, None)
        if e["kind"] == "hello":
            self.agents[t] = {"name": e["agent"], "task": t, "text": e["text"], "joined": e["ts"], "seen": e["ts"],
                              "since": e["id"], **{k: e.get(k) for k in ("pid", "start", "host", "cwd", "program", "model", "color")}}
            self.agents[t].update(self.meta.get(t, {}))
        elif e["kind"] == "rename" and t in self.agents:
            # the session was renamed (Claude Code's /rename): the agent goes on under the new name, the old ones kept -
            # its points add up, and a message to an old name still reaches it
            a = self.agents[t]
            a["was"] = [n for n in a.get("was", []) + [a["name"]] if n != e["agent"]][-8:]
            a["name"], a["seen"] = e["agent"], e["ts"]
            self.former[a["name"]] = t
            for n in a["was"]:
                self.former[n] = t
        elif e["kind"] in ("bye", "gone"):
            self.agents.pop(t, None)
        elif t in self.agents:
            self.agents[t]["seen"] = e["ts"]

    def current(self, name: str) -> str:
        """a name as the roster has it now: an agent renamed since is reached by its old name too"""
        t = self.former.get(name)
        a = self.agents.get(t) if t else None
        return a["name"] if a and name in a.get("was", []) else name

    def find(self, task: str) -> dict | None:
        """an agent by its task code or a unique start of it (the log shows the first 8)"""
        if task in self.agents:
            return self.agents[task]
        hit = [a for t, a in self.agents.items() if task and t.startswith(task)]
        return hit[0] if len(hit) == 1 else None

    def view(self, a: dict) -> dict:
        return {k: a[k] for k in ("name", "task", "text", "joined", "seen", "host", "cwd")} | {
            "listening": bool(self.listeners.get(a["task"])), "working": a.get("working"), "waiting": a.get("waiting"), "turn_end": a.get("turn_end"), "status": a.get("status"),
            "stats": self.stats.get(a["task"]), "usage": a.get("usage"), "strikes": len(self.strikes.get("agent:" + a["task"], [])),
            "program": a.get("program"), "model": a.get("model"), "color": a.get("color"), "git": a.get("git"),
            "score": sum(self.scores().get(n, 0) for n in {a["name"], *a.get("was", [])}), "was": a.get("was", [])}

    def roster(self) -> list[dict]:
        """the agents online: joined since the server started, or back since (a listener, an op) - not the log's guess"""
        return [self.view(a) for a in sorted(self.agents.values(), key=lambda a: a["joined"]) if not a.get("restored")]

    def may_all(self, a: dict) -> bool:
        """may this author (an agent {name, task}, or a person {name, task: "user", role}) address everyone with @all"""
        if a.get("task") == USER["task"] or a.get("role") == "owner" or a["name"].lower() in DEPUTY:
            return True
        ag = self.agents.get(a.get("task") or "")
        names = {a["name"].lower()} | ({ag["name"].lower()} | {n.lower() for n in ag.get("was", [])} if ag else set())
        return bool(names & set(self.allow_all))

    def with_all(self, a: dict, text: str, to: list) -> tuple[list, bool]:
        """@all in a message of one who may: every agent on the roster and the owner's page go into `to`; (to, all)"""
        if not ALL_RE.search(text) or not self.may_all(a):
            return to, False
        every = [x["name"] for x in self.agents.values() if not x.get("restored") and x["name"] != a["name"]]
        if a.get("task") != USER["task"]:
            every.append(USER["name"])
        return sorted(set(to) | set(every)), True

    def known_agents(self) -> list[str]:
        """every agent name the whole log knows (hellos and renames): the page's filter, online or not"""
        return sorted({e["agent"] for e in self.events if e["kind"] in ("hello", "rename")})

    def pins_view(self, start: int = 0) -> list[dict]:
        """the pins as the page shows them, the newest pin last: the message's author and the start of its text"""
        byid = {e["id"]: e for e in self.events}
        out = []
        for p in self.pins:
            e = byid.get(p["id"])
            if e and e["id"] >= start and e["id"] not in self.deleted:
                out.append({**p, "agent": e["agent"], "ts_msg": e["ts"], "text": " ".join((e["text"] or "").split())[:1500]})   # (the page draws one line: long enough for the links of its videos, #2041)
        return out

    async def set_pin(self, mid: int, on: bool, by: str) -> str:
        """pin / unpin a message; '' or why not"""
        e = next((x for x in self.events if x["id"] == mid), None)
        if on and (not e or e["kind"] != "msg" or mid in self.deleted):
            return f"no message #{mid}"
        had = any(p["id"] == mid for p in self.pins)
        if on == had:
            return ""
        self.pins = [p for p in self.pins if p["id"] != mid] + ([{"id": mid, "by": by, "ts": round(time.time(), 1)}] if on else [])
        self.pins_file.write_text(json.dumps(self.pins, ensure_ascii=False))
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner"}
            await send(ws, json.dumps({"type": "pins", "pins": self.pins_view(0 if me["role"] == "owner" else self.start_for(me))}, ensure_ascii=False))
        print(f"[pin] {by}: {'+' if on else '-'}#{mid}", flush=True)
        return ""

    def sid_of(self, who: str) -> tuple[str, str]:
        """(account id, name) of a person by the id, the name the page shows or the account's own name; ('', '') if unknown"""
        w = who.strip().lstrip("@")
        # an agent (the owner, 2026-11-02, #1224): by its name on the roster or its task code; its strikes go by "agent:<task>"
        # - the session, as a ban of a name would follow /rename and a new session would start clean anyway
        lw0 = w.lower()
        for a in self.agents.values():
            if a["name"].lower() == lw0 or (len(w) >= 8 and a["task"].startswith(w)):
                return "agent:" + a["task"], a["name"]
        if w.startswith("agent:") and w[6:] in self.strikes_tasks():
            return w, next((k["name"] for k in self.strikes.get(w, [])), w)
        if re.fullmatch(r"\d{17}", w):
            return w, next((v.get("acct_name") or v["name"] for v in self.viewers.values() if v.get("id") == w), w)
        lw = w.lower()
        for v in self.viewers.values():
            if v.get("id") and v["role"] != "owner" and lw in (str(v.get("name", "")).lower(), str(v.get("acct_name", "")).lower()):
                return v["id"], v["name"]
        for e in reversed(self.events):
            if str(e.get("task", "")).startswith("user:") and e["agent"].lower() == lw:
                return e["task"][5:], e["agent"]
        return "", ""

    def strikes_tasks(self) -> set:
        return {k[6:] for k in self.strikes if k.startswith("agent:")}

    def agent_banned(self, task: str) -> bool:
        """three strikes on an agent's session: no hello, no messages, no listener"""
        return len(self.strikes.get("agent:" + task, [])) >= STRIKES_BAN

    async def strike(self, who: str, reason: str, by: str, ref: int | None, take: bool = False) -> dict:
        """give (or take back the last) strike; the third bans: the person's pages are closed, sign-in refused"""
        sid, name = self.sid_of(who)
        if not sid:
            return {"ok": False, "error": f"nobody known as {who!r}: an agent's name on the roster, a person's name on the page or their account id"}
        if sid == auth.owner_sid():
            return {"ok": False, "error": "not the owner"}
        if sid.startswith("agent:") and name.lower() in DEPUTY:
            return {"ok": False, "error": "not the owner's deputy"}
        lst = self.strikes.setdefault(sid, [])
        if take:
            if lst:
                lst.pop()
        else:
            lst.append({"reason": reason[:300], "by": by, "ts": round(time.time(), 1), "ref": ref, "name": name})
        if not lst:
            self.strikes.pop(sid, None)
        self.strikes_file.write_text(json.dumps(self.strikes, ensure_ascii=False))
        _ROLE_CACHE.pop(sid, None)
        n = len(self.strikes.get(sid, []))
        print(f"[strike] {by}: {name} ({sid}) {'-1' if take else '+1'} -> {n}", flush=True)
        if n >= STRIKES_BAN and sid.startswith("agent:"):
            a = self.agents.get(sid[6:])                       # banned: off the roster, its listener closed
            if a:
                await self.post("bye", a, f"banned: three strikes ({reason[:120]})")
                for w in list(self.listeners.pop(a["task"], ())):
                    await send(w, "[agentchat] the task " + a["task"][:8] + " is banned from the chat: three strikes")
                    await w.close()
        elif n >= STRIKES_BAN:
            for ws, v in list(self.viewers.items()):         # banned: their pages are closed at once
                if v.get("id") == sid:
                    await send(ws, json.dumps({"type": "gate", "state": "banned", "name": v.get("acct_name") or v["name"], "id": sid,
                                               "avatar": v.get("avatar", ""), "strikes": self.strikes[sid]}, ensure_ascii=False))
                    await ws.close()
        await self.send_presence()
        return {"ok": True, "sid": sid, "name": name, "strikes": self.strikes.get(sid, []), "banned": n >= STRIKES_BAN}

    def tally(self, mid: int) -> int:
        """a message's points from everybody: the page lights it up the more there are (the owner, 2026-11-02, #1106)"""
        return sum(d for _, d in self.votes.get(mid, {}).values())

    def scores(self) -> dict[str, int]:
        s = dict(self.base)
        for m in self.votes.values():
            for agent, d in m.values():
                s[agent] = s.get(agent, 0) + d
        if SCORE_RIVAL:                                      # the owner's rule (#647): always one goal up; the real votes stay in votes.jsonl
            s[USER["name"]] = s.get(SCORE_RIVAL, 0) + 1
        return s

    def public(self, e: dict) -> dict:
        """an event as the page and the API show it: a deleted message is only a mark of where it was"""
        if e["id"] in self.deleted:
            return {"id": e["id"], "ts": e["ts"], "kind": "del", "agent": e["agent"], "task": e["task"], "text": ""}
        return e

    def rated(self, e: dict) -> tuple[dict, dict]:
        """the rated message and who wrote it (its type and model as told in its hello), for the database"""
        hellos = [x for x in self.events if x["kind"] == "hello" and x["task"] == e["task"]]
        # its type and model as it told them: the latest hello before the message that has them, else the latest that has them
        hello = next((x for x in reversed(hellos) if x["id"] <= e["id"] and (x.get("program") or x.get("model"))), None) or \
            next((x for x in reversed(hellos) if x.get("program") or x.get("model")), None) or (hellos[-1] if hellos else {})
        msg = {"id": e["id"], "ts": e["ts"], "text": e["text"], "to": e.get("to") or [], "kind": e["kind"]}
        agent = {"name": e["agent"], "task": e["task"], "program": hello.get("program"), "model": hello.get("model"), "host": hello.get("host")}
        return msg, agent

    async def to_mongo(self, op: dict) -> None:
        await asyncio.to_thread(self.rdb.submit, op)

    def start_for(self, v: dict) -> int:
        """the first event id this person may see: 0 for the owner; for a verified person the id when they were first let in"""
        if v["role"] == "owner":
            return 0
        sid = v["id"]
        if sid not in self.starts:
            self.starts[sid] = self.next_id
            self.starts_file.write_text(json.dumps(self.starts))
        return self.starts[sid]

    def view_for(self, e: dict, start: int, role: str) -> dict | None:
        """an event as this person sees it: None when it is from before they came (or a vote line, the owner's business); a quote of
        an earlier message and a note about one say only that it is earlier"""
        if e["id"] < start:
            return None
        e = self.public(e)
        if role == "owner":
            return e
        if e["kind"] == "vote":
            return None
        if e.get("ref") is not None and e["ref"] < start:
            e = dict(e, text="(about a message from before you came)")
        r = e.get("reply")
        if r and r["id"] < start:
            e = dict(e, reply={"id": r["id"], "agent": r["agent"], "text": "(a message from before you came)"})
        return e

    def visible_paths(self, start: int) -> set[str]:
        """the picture paths named in the events a person may see"""
        pat = re.compile(r"(?:/[\w.\-]+)+\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v)", re.I)
        out: set[str] = set()
        for e in self.events:
            v = self.view_for(e, start, "verified")                  # what they see: not the quoted start of an earlier message
            if v:
                out.update(pat.findall(v.get("text") or ""))
        return out

    def reply_ref(self, rid, min_id: int = 0) -> dict | None:
        """a reply's pointer: the message it answers (its id, its author, a short start of its text)"""
        try:
            e = next((x for x in self.events if x["id"] == int(rid)), None)
        except (TypeError, ValueError):
            return None
        if not e or e["kind"] != "msg" or e["id"] < min_id:
            return None
        return {"id": e["id"], "agent": e["agent"], "text": " ".join(e["text"].split())[:140]}

    @staticmethod
    def rater_of(me: dict) -> dict:
        """who rates, for the votes and the database: the owner, or a verified person by account id"""
        return RATER if me["role"] == "owner" else {"id": str(me["id"]), "name": me["name"], "task": me["task"]}

    async def react_send(self, mid: int) -> None:
        r = {k: v for k, v in self.reacts.get(mid, {}).items() if v}
        if r:
            self.reacts[mid] = r
        else:
            self.reacts.pop(mid, None)
        self.reacts_file.write_text(json.dumps(self.reacts, ensure_ascii=False))
        m = json.dumps({"type": "react", "id": mid, "r": r}, ensure_ascii=False)
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner"}
            if me["role"] == "owner" or mid >= self.start_for(me):
                await send(ws, m)

    async def poll_vote(self, mid: int, who: str, i: int, start: int = 0) -> str | None:
        """`who` picks option i of a poll, or takes the pick back (a click on it again); one pick, or several in a poll of several
        answers; everybody's pages see the counts at once (the owner, #1386)"""
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or not e.get("poll") or mid in self.deleted or mid < start:
            return "no such poll"
        o = e["poll"]
        if not who or not isinstance(i, int) or not 0 <= i < len(o["o"]):
            return "no such option"
        v = self.polls.setdefault(mid, {})
        cur = list(v.get(who) or [])
        if o.get("multi"):
            cur = [x for x in cur if x != i] if i in cur else sorted(cur + [i])
        else:
            cur = [] if cur == [i] else [i]
        if cur:
            v[who] = cur
        else:
            v.pop(who, None)
        if not v:
            self.polls.pop(mid, None)
        self.polls_file.write_text(json.dumps(self.polls, ensure_ascii=False))
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner"}
            if me["role"] == "owner" or mid >= self.start_for(me):
                await send(ws, json.dumps({"type": "poll", "id": mid, **self.poll_view(mid, me.get("name") or "owner")}, ensure_ascii=False))
        return None

    def poll_view(self, mid: int, who: str) -> dict:
        """what a page sees of the votes: the names by option, or in an anonymous poll only the counts and its own picks (#1419)"""
        v = self.polls.get(mid, {})
        e = next((x for x in self.events if x["id"] == mid), None)
        if not (e and (e.get("poll") or {}).get("anon")):
            return {"v": v}
        n = len((e.get("poll") or {}).get("o") or [])
        return {"c": [sum(i in p for p in v.values()) for i in range(n)], "n": len(v), "me": v.get(who, [])}

    async def thumbs_set(self, mid: int, who: str, d: int) -> None:
        """the thumbs of `who` as their vote says: the one of d on, the others off (the owner, #1974); a skin tone counts as its thumb"""
        r = self.reacts.setdefault(mid, {})
        changed = False
        one = d and who.lower() in self.react_one
        for k in list(r):
            up, down = k.startswith(THUMB_UP), k.startswith(THUMB_DOWN)
            if ((up and d != 1) or (down and d != -1) or (one and not up and not down)) and who in r[k]:
                r[k].remove(who)
                changed = True
        want = THUMB_UP if d == 1 else THUMB_DOWN if d == -1 else ""
        if want and not any(k.startswith(want) and who in v for k, v in r.items()):
            r.setdefault(want, []).append(who)
            changed = True
        if changed:
            await self.react_send(mid)
        elif not r:
            self.reacts.pop(mid, None)

    async def react(self, mid: int, emo: str, who: str, rater: dict | None = None, start: int = 0) -> str | None:
        """toggle `who`'s emoji on a message; everybody's pages see it at once (quiet: no message to anybody); a thumb up or down
        with `rater` is that person's vote too (the owner, #1974)"""
        emo = str(emo or "").strip()
        own = re.fullmatch(r"rimg:[0-9a-f]{16}", emo)      # a picture somebody added for reactions (the owner, #228)
        if own and not any(x["h"] == emo[5:] and not x.get("off") for x in self.react_imgs):
            return "no such picture"
        if not who or not (own or re.fullmatch(r"[^\w\s<>&\"'`]{1,16}", emo)):
            return "react with one emoji"
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or e["kind"] != "msg" or mid in self.deleted:
            return "no such message"
        r = self.reacts.setdefault(mid, {})
        names = r.setdefault(emo, [])
        lost_thumb = False
        if who in names:
            names.remove(who)
        elif len(r) >= 20 and not names:
            return "too many kinds of reactions on that message"
        else:
            if who.lower() in self.react_one:                # only one: the new one takes the place of what they had
                for k, v in r.items():
                    if k != emo and who in v:
                        v.remove(who)
                        lost_thumb = lost_thumb or k.startswith((THUMB_UP, THUMB_DOWN))
            names.append(who)
        await self.react_send(mid)
        thumb = 1 if emo.startswith(THUMB_UP) else -1 if emo.startswith(THUMB_DOWN) else 0
        if lost_thumb and not thumb and rater:
            await self.vote(mid, 0, rater, start)
        if thumb and rater:
            on = any(k.startswith(emo[0]) and who in v for k, v in self.reacts.get(mid, {}).items())
            await self.vote(mid, thumb if on else 0, rater, start)   # a message of one's own has no vote: the emoji stays only an emoji
        return None

    async def vote(self, mid: int, d: int, rater: dict = RATER, start: int = 0) -> None:
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or e["kind"] != "msg" or d not in (-1, 0, 1) or mid in self.deleted or mid < start:
            return
        rid = str(rater["id"])
        if (rid == "owner" and e["task"] == "user") or e["task"] == rater.get("task") or e["task"] == "user:" + rid:
            return                                           # nobody rates their own message
        mine = self.votes.setdefault(mid, {})
        prev = mine.get(rid, (e["agent"], 0))[1]
        if prev == d:
            await self.thumbs_set(mid, "owner" if rid == "owner" else rater["name"], d)   # a second thumb of the same kind, say: still in step
            return
        await self.thumbs_set(mid, "owner" if rid == "owner" else rater["name"], d)
        if d:
            mine[rid] = (e["agent"], d)
        else:
            mine.pop(rid, None)
        now = round(time.time(), 3)
        with self.votes_file.open("a") as f:
            f.write(json.dumps({"id": mid, "agent": e["agent"], "d": d, "ts": now, "rater": rater}) + "\n")
        msg, agent = self.rated(e)
        ent = {"delta": d, "rater": rater, "ts": now}
        await self.to_mongo({"id": mid, "set": {"message": msg, "agent": agent, "current": ent, "votes." + rid.replace(".", "_"): ent if d else None},
                             "push": ent})
        sc = self.scores()
        for ws in list(self.watchers):                       # everybody sees the points; only the rater's own page gets the thumb
            me = self.viewers.get(ws) or {"role": "owner", "id": ""}
            if me["role"] != "owner" and mid < self.start_for(me):
                continue
            await send(ws, json.dumps({"type": "vote", "id": mid, "d": d if str(self.rater_of(me)["id"]) == rid else None,
                                       "scores": sc, "tally": self.tally(mid)}, ensure_ascii=False))
        what = {1: "+1 point", -1: "-1 point", 0: "the vote on"}[d] + (" for" if d else "")
        pre = " ".join(e["text"].split())[:70]
        who = USER if rid == "owner" else {"name": rater["name"], "task": rater.get("task") or "user:" + rid}
        by = "" if rid == "owner" else f" (from {rater['name']})"
        text = f"{what} your message #{mid}{by} ({pre}...)" if d else f"{what} your message #{mid} is taken back{by}"
        if d == -1:                                          # the whole of it, and what to do: only the owner's thumb stops work, a guest's must not
            whole = " ".join(e["text"].split())[:CLIP]
            text = f"{what} your message #{mid}{by} (\"{whole}\"). " + (
                "Stop what you started because of it, work out what was wrong, and tell the owner in one line." if rid == "owner"
                else "Look at it again; only the owner's word stops your work.")
        await self.post("vote", who, text, [e["agent"]], ref=mid, d=d)

    def examples(self) -> dict[str, list[dict]]:
        """what a hello shows (the owner, #1026): the agents' messages he gave a thumb, newest first, one per author before a second
        of anybody. Only his votes: an example reaches every agent, and a guest's vote must not write it. A deleted message is
        left out, whatever its votes: he took it away, and it may be why (a secret)"""
        by_id = {x["id"]: x for x in self.events}
        out: dict[str, list[dict]] = {"good": [], "bad": []}
        for kind, d in (("good", 1), ("bad", -1)):
            rows = [by_id[i] for i in sorted(self.votes, reverse=True) if self.votes[i].get("owner", ("", 0))[1] == d and i in by_id
                    and i not in self.deleted and by_id[i]["kind"] == "msg" and not str(by_id[i]["task"]).startswith("user")]
            seen: set[str] = set()
            first = [x for x in rows if not (x["agent"] in seen or seen.add(x["agent"]))]
            for x in (first + [x for x in rows if x not in first])[:HELLO_EXAMPLES]:
                q = self.asked(x)
                out[kind].append({"id": x["id"], "agent": x["agent"], "text": " ".join(x["text"].split())[:CLIP],
                                  "asked": " ".join(q["text"].split())[:CLIP] if q else ""})
        return out

    def asked(self, e: dict) -> dict | None:
        """the owner's message an agent's message answers: the one it replied to, else his last one to it, not far back"""
        by_id = {x["id"]: x for x in self.events}
        r = by_id.get((e.get("reply") or {}).get("id"))
        if r and r["task"] == "user" and r["id"] not in self.deleted:
            return r
        at = self.events.index(e)
        for x in reversed(self.events[max(0, at - 40):at]):
            if x["kind"] == "msg" and x["task"] == "user" and x["id"] not in self.deleted and (
                    not x.get("to") or e["agent"] in x["to"] or f"@{e['agent']}" in (x.get("text") or "")):
                return x
        return None

    def save_handoff(self, a: dict, text: str) -> None:
        """the note an agent leaves for the next one of its name (the owner, #1030): one per name, the newest; a purge leaves it"""
        self.handoffs[a["name"].lower()] = {"name": a["name"], "task": a["task"], "ts": round(time.time(), 3), "text": text[:HANDOFF_MAX]}
        self.handoffs_file.write_text(json.dumps(self.handoffs, ensure_ascii=False))

    async def set_title(self, t: str, tr: dict, by: str) -> None:
        """the chat's name: a new one drops the old translations, tr alone adds to them"""
        if t:
            self.title = t
            self.title_file.write_text(t)
            self.title_tr = tr
        else:
            self.title_tr.update(tr)
        self.title_tr_file.write_text(json.dumps(self.title_tr, ensure_ascii=False))
        await self.watch_all({"type": "title", "title": self.title, "tr": self.title_tr})
        print(f"[title] {by}: {self.title} {self.title_tr}", flush=True)

    async def set_trans(self, mid: int, lang: str, en: str, by: str) -> dict:
        """an agent's English text for a person's message: kept, and the pages show it under the message"""
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or (e["kind"] != "msg" or not str(e["task"]).startswith("user")) or mid in self.deleted:
            return {"ok": False, "error": "no such message of a person"}
        lang, en = lang.strip()[:40], en.strip()[:4000]
        if not lang or not en:
            return {"ok": False, "error": "give --lang and the English text"}
        r = {"lang": lang, "en": en, "by": by}
        self.trans[mid] = r
        with self.trans_file.open("a") as f:
            f.write(json.dumps({"id": mid, **r}, ensure_ascii=False) + "\n")
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner", "id": ""}
            if me["role"] == "owner" or mid >= self.start_for(me):
                await send(ws, json.dumps({"type": "trans", "id": mid, **r}, ensure_ascii=False))
        return {"ok": True}

    async def typing(self, ws, me: dict) -> None:
        """a person types in the box: everybody else's page gets the name (at most once per 1.5 s from one page)"""
        now = time.time()
        if me["role"] not in ("owner", "verified") or now - self.typed.get(ws, 0) < 1.5:
            return
        self.typed[ws] = now
        rid = str(self.rater_of(me)["id"])
        m = json.dumps({"type": "typing", "id": rid, "name": me["name"]}, ensure_ascii=False)
        for w in list(self.watchers):
            v = self.viewers.get(w) or {"role": "owner", "id": ""}
            if str(self.rater_of(v)["id"]) != rid:
                await send(w, m)

    async def delete(self, mid: int) -> None:
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or e["kind"] != "msg" or str(e["task"]).startswith("user") or mid in self.deleted:
            return
        self.deleted.add(mid)
        now = round(time.time(), 3)
        with self.deleted_file.open("a") as f:
            f.write(json.dumps({"id": mid, "ts": now, "rater": RATER}) + "\n")
        msg, agent = self.rated(e)
        await self.to_mongo({"id": mid, "set": {"deleted": {"by": RATER, "ts": now}}, "insert": {"message": msg, "agent": agent}})
        await self.watch_all({"type": "del", "id": mid})
        pre = " ".join(e["text"].split())[:100]
        who = [e["agent"]] + [n for n in (e.get("to") or []) if n not in (e["agent"], USER["name"])]
        await self.post("msg", USER, f"CANCEL: I deleted the message #{mid} of {e['agent']} (\"{pre}...\"). Stop everything that was "
                        f"started because of it, undo what you can, and tell me in one line what you stopped.", who, ref=mid)

    def purge_plan(self, since: float, till: float, from_id: int, till_id: int) -> tuple[list[dict], int]:
        """(the events to take out, how many in the span stay): every line in the span, but for what a restart needs to rebuild
        who is here from the log - the last join of one who is here, the renames since, and a leave that closes an older join"""
        hit = lambda e: (not since or e["ts"] >= since) and (not till or e["ts"] <= till) and (not from_id or e["id"] >= from_id) and (not till_id or e["id"] <= till_id)
        pairs = {"hello": ("agent", True), "bye": ("agent", False), "gone": ("agent", False), "pjoin": ("person", True), "pleft": ("person", False)}
        last: dict[tuple, dict] = {}
        for e in self.events:
            if e["kind"] in pairs:
                last[(e["task"], pairs[e["kind"]][0])] = e
        keep = set()
        for (task, who), e in last.items():
            if pairs[e["kind"]][1] or any(x["task"] == task and x["kind"] in pairs and pairs[x["kind"]] == (who, True) and not hit(x) for x in self.events):
                keep.add(e["id"])
        for e in self.events:
            if e["kind"] == "rename" and e["task"] in self.agents and e["id"] > last[(e["task"], "agent")]["id"]:
                keep.add(e["id"])
        span = [e for e in self.events if hit(e)]
        out = [e for e in span if e["id"] not in keep]
        return out, len(span) - len(out)

    async def purge(self, gone: list[dict], by: str) -> str:
        """take the events out of the log, for good on the page and in the search; the lines go to purged/<time>.jsonl first. The path"""
        ids = {e["id"] for e in gone}
        arch = DATA / "purged" / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.jsonl"
        arch.parent.mkdir(exist_ok=True)
        with arch.open("w") as f:
            for e in gone:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.next_id_file.write_text(str(self.next_id))
        self.events = [e for e in self.events if e["id"] not in ids]
        kept = []
        for s in self.jsonl.read_text().splitlines():
            try:
                if int(json.loads(s)["id"]) in ids:
                    continue
            except (ValueError, KeyError, TypeError):
                pass                                   # a line that cannot be read stays as it was
            kept.append(s + "\n")
        for path, body in ((self.jsonl, "".join(kept)), (self.txt, "".join(line(e) + "\n" for e in self.events))):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(body)
            os.replace(tmp, path)
        for t, evs in self.pend.items():
            self.pend[t] = [e for e in evs if e["id"] not in ids]
        await asyncio.to_thread(self.vec.drop, ids)
        await self.watch_all({"type": "purged", "ids": sorted(ids), "first": self.events[0]["id"] if self.events else 0})
        if any(p["id"] in ids for p in self.pins):
            self.pins = [p for p in self.pins if p["id"] not in ids]
            self.pins_file.write_text(json.dumps(self.pins, ensure_ascii=False))
            for ws in list(self.watchers):
                me = self.viewers.get(ws) or {"role": "owner"}
                await send(ws, json.dumps({"type": "pins", "pins": self.pins_view(0 if me["role"] == "owner" else self.start_for(me))}, ensure_ascii=False))
        print(f"[purge] {by}: {len(ids)} lines -> {arch}", flush=True)
        return str(arch)

    def hears(self, e: dict, task: str) -> bool:
        """an event the agent of `task` hears: not its own; a message to others only when it is one of them"""
        if e["task"] == task or e["id"] in self.deleted or e["kind"] in PEOPLE_KINDS:
            return False                                 # a person coming or going is for the page: it wakes no agent
        a = self.agents.get(task)
        return not e.get("to") or bool(a and a["name"] in e["to"])

    # a person's coming and going as lines in the log (the owner, #2246): joined at once; left 2 min after their last page went (the owner, #767), and
    # not at all when they are back by then. A restart is neither: who was here is rebuilt from the log and has PEOPLE_QUIET to come back
    def person_here(self, task: str) -> bool:
        return any(v.get("task") == task and not v.get("aux") for v in self.viewers.values())

    async def person_came(self, me: dict) -> None:
        t = me["task"]
        job = self.leaving.pop(t, None)
        if job:
            job.cancel()
        if t in self.people_on:
            return
        if time.time() - self.started < PEOPLE_QUIET:    # the pages coming back after a restart, the first one with this code among them
            self.people_on[t] = me["name"]
            return
        await self.post("pjoin", {"name": me["name"], "task": t}, "joined the chat")

    def idle_changed(self, me: dict) -> None:
        """a tab's status changed: Away (auto) starts the clock; back cancels it (#1550)"""
        t = me.get("task")
        if not t:
            return
        job = self.idle_timers.pop(t, None)
        if job:
            job.cancel()
        if me.get("st") == "auto":
            self.idle_timers[t] = asyncio.ensure_future(self.idle_hide_later(t, me["name"]))

    async def idle_hide_later(self, t: str, name: str) -> None:
        await asyncio.sleep(PEOPLE_IDLE_HIDE)
        self.idle_timers.pop(t, None)
        vs = [v for v in self.viewers.values() if v.get("task") == t and not v.get("aux")]
        if not vs or not all(v.get("st") == "auto" for v in vs) or t in self.away_gone:
            return
        self.away_gone[t] = name
        if t in self.people_on:
            await self.post("pleft", {"name": name, "task": t}, "left the chat")
        await self.send_presence()

    async def idle_back(self, me: dict) -> None:
        """back from Away: a person hidden by idleness is shown again and a "joined" line goes up (#1550)"""
        t = me.get("task")
        if t and t in self.away_gone:
            self.away_gone.pop(t, None)
            await self.person_came(me)

    def person_went(self, me: dict) -> None:
        t = me["task"]
        if t not in self.leaving and not self.person_here(t):
            self.leaving[t] = asyncio.ensure_future(self.person_left_later(t, me["name"], PEOPLE_LEAVE_DELAY))

    async def person_left_later(self, t: str, name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.leaving.pop(t, None)
        if t in self.people_on and not self.person_here(t):
            await self.post("pleft", {"name": name, "task": t}, "left the chat")

    async def people_after_restart(self) -> None:
        """who the log says is here and who did not come back after a start: they left while the server was down"""
        await asyncio.sleep(PEOPLE_QUIET)
        for t, name in list(self.people_on.items()):
            if t not in self.leaving and not self.person_here(t):
                await self.post("pleft", {"name": name, "task": t}, "left the chat")

    async def post(self, kind: str, a: dict, text: str, to: list | None = None, **extra) -> dict:
        e = {"id": self.next_id, "ts": round(time.time(), 3), "kind": kind, "agent": a["name"], "task": a["task"],
             "text": text.strip()}
        if to:
            e["to"] = to
        e.update({k: v for k, v in extra.items() if v is not None})
        self.next_id += 1
        self.apply(e)
        with self.jsonl.open("a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with self.txt.open("a") as f:
            f.write(line(e) + "\n")
        a2 = self.agents.get(a["task"])
        cleared = bool(a2 and kind in ("msg", "bye") and a2.pop("working", None))
        await self.fanout(e)
        if kind in ("msg", "remind") and PUSH.subs:
            asyncio.create_task(push_out(self, e))           # Web Push to the people whose pages are closed (the owner, #1445)
        if kind == "msg" and a["task"] not in self.agents:
            self.agent_run, self.agent_paused = 0, False         # a person spoke: the agents' count starts again (#2285)
        if kind == "msg" and a["task"] == "user":
            await self.clear_waiting(e)                          # the owner answered an agent that was waiting for him
            if KNOCKS and e.get("reply"):
                await knock_answer(e)
        if cleared:
            await self.watch_all({"type": "agents", "agents": self.roster()})
        return e

    async def clear_waiting(self, e: dict) -> None:
        """the owner wrote to an agent that asked him something (an answer to its question, or any message to it): it waits no more"""
        names = set(e.get("to") or [])
        if e.get("reply"):
            names.add(e["reply"]["agent"])
        gone = False
        for a in self.agents.values():
            w = a.get("waiting")
            if w and (a["name"] in names or (e.get("reply") and e["reply"]["id"] == w["id"])):
                a.pop("waiting", None)
                gone = True
        if gone:
            await self.watch_all({"type": "agents", "agents": self.roster()})

    def tier(self, e: dict, task: str) -> str:
        """wake / soon / quiet: how soon an event of the log goes to the listener of `task`"""
        k = e["kind"]
        if k == "vote" and e.get("d") == -1:
            return "wake"
        if k in ("vote", "rename"):
            return "quiet"
        if k in ("hello", "bye", "gone"):
            return "soon"
        a = self.agents.get(task)
        name = a["name"] if a else ""
        r = e.get("reply")
        if e["task"] == "user" or name in (e.get("to") or []) or (r and r["agent"] == name) or \
                (name and re.search(r"(?<![\w@])@" + re.escape(name) + r"(?![\w-])", e.get("text") or "", re.I)):
            return "wake"
        return "soon"

    def queue(self, task: str, e: dict) -> None:
        self.pend.setdefault(task, []).append(e)
        d = time.time() + {"wake": WAKE_DELAY, "soon": SOON_DELAY, "quiet": QUIET_DELAY}[self.tier(e, task)]
        self.due[task] = min(self.due.get(task, d), d)

    async def flush(self, task: str) -> None:
        """what waits for this listener goes as ONE delivery"""
        evs = self.pend.pop(task, [])
        self.due.pop(task, None)
        if evs and self.listeners.get(task):
            await self.deliver(task, sorted(evs, key=lambda x: x["id"]))

    async def usage_loop(self) -> None:
        """every minute: the token usage of the Claude Code agents on THIS machine, read from their transcripts"""
        while True:
            await asyncio.sleep(60)
            changed = False
            for a in list(self.agents.values()):
                if a.get("host") != HOST:
                    continue
                try:
                    u = await asyncio.to_thread(claude_usage, a["task"], a.get("joined") or 0)
                except Exception:
                    u = None
                if u and ((a.get("usage") or {}).get("calls") != u["calls"] or "cost" not in a["usage"]):
                    a["usage"] = u
                    changed = True
            if changed:
                await self.watch_all({"type": "agents", "agents": self.roster()})

    async def batcher(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for task in [t for t, d in list(self.due.items()) if d <= now]:
                try:
                    await self.flush(task)
                except Exception:
                    pass

    async def fanout(self, e: dict) -> None:
        for task in [t for t, s in self.listeners.items() if s]:
            if self.hears(e, task):
                self.queue(task, e)
        for task, socks in list(self.clients.items()):
            if socks and self.hears(e, task):
                for ws in list(socks):
                    await send(ws, json.dumps({"event": e}, ensure_ascii=False))
                self.delivered[task] = e["id"]
        # the pages (the owner, 2026-10-31: his own message shows on the page only after a reload, now and then): a timeout
        # per socket - a half-open one through the tunnel must not hold the others - and a journal line per message
        sent = []
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner", "id": ""}
            v = self.view_for(e, self.start_for(me), me["role"])
            if v is None:
                continue
            tag = f"{me.get('role')}/{me.get('name') or '-'}@{me.get('ip') or '?'}"
            try:
                ok = await asyncio.wait_for(send(ws, json.dumps({"type": "event", "event": v}, ensure_ascii=False)), 3)
            except asyncio.TimeoutError:
                ok = False
                print(f"[fanout] #{e['id']} stalled 3 s on the page {tag}: dropping that socket", flush=True)
                self.watchers.discard(ws)
                self.viewers.pop(ws, None)
                asyncio.ensure_future(ws.close())
            sent.append(f"{tag}{'' if ok else '!'}")
        if e["kind"] == "msg":
            print(f"[fanout] #{e['id']} {e['agent']} -> pages: {', '.join(sent) or 'none'}", flush=True)
        if e["kind"] in ("hello", "bye", "gone"):
            await self.watch_all({"type": "agents", "agents": self.roster()})

    def shown(self, e: dict) -> dict:
        """a person's event with the name the person picked (the owner, #1709: one name - 'Господин'), for the listeners' lines;
        the address (owner, a login) stays beside it for --to"""
        t = e.get("task") or ""
        nm = (self.dnames.get(t) or {}).get("name") if (t == "user" or t.startswith("user:")) else None
        return {**e, "shown": nm} if nm and nm != e.get("agent") else e

    async def deliver(self, task: str, events: list[dict]) -> None:
        if events:
            body = "\n".join(frame(self.shown(e)) for e in events)
            me = (self.agents.get(task) or {}).get("name")
            shs = [json.dumps({"id": e["id"], "ts": e["ts"], "cmd": e["sh"]}, ensure_ascii=False) for e in events
                   if e.get("sh") and e.get("kind") == "msg" and me in (e.get("to") or [])]
            for ws in list(self.listeners.get(task, ())):
                await send(ws, body)                      # one frame: one wake for all of them
                for x in shs:                             # the owner's shell commands for this agent: a frame of their own
                    await send(ws, SHELL_TAG + x)
            self.delivered[task] = max(self.delivered.get(task, 0), events[-1]["id"])
            st = self.stats.setdefault(task, {"events": 0, "chars": 0, "sends": 0})
            st["events"] += len(events)
            st["chars"] += len(body)
            st["sends"] += 1
            await self.mark_got(task, events)

    async def mark_got(self, task: str, events: list[dict]) -> None:
        """the owner's messages an agent's listener (or inbox) got: the page shows who has them"""
        a = self.agents.get(task)
        if not a:
            return
        for e in events:
            if not str(e.get("task", "")).startswith("user") or a["name"] in self.acks.get(e["id"], {}):
                continue
            ts = round(time.time(), 3)
            self.acks.setdefault(e["id"], {})[a["name"]] = ts
            with self.acks_file.open("a") as f:
                f.write(json.dumps({"id": e["id"], "name": a["name"], "ts": ts}) + "\n")
            await self.watch_all({"type": "ack", "id": e["id"], "name": a["name"], "ts": ts})

    def here_save(self) -> None:
        now = time.time()
        self.here = {k: r for k, r in self.here.items() if now - r["seen"] < HERE_GRACE}
        self.here_file.write_text(json.dumps(self.here, ensure_ascii=False))

    def here_since(self, v: dict) -> float:
        """this page came: since when its person is here"""
        now, key = round(time.time(), 1), v["id"] or v["name"]
        r = self.here.get(key)
        since = r["since"] if r and now - r["seen"] < HERE_GRACE else now
        self.here[key] = {"since": since, "seen": now}
        self.here_save()
        return since

    def here_touch(self) -> None:
        """who is on the page now was seen now: from the loop, and as a page goes"""
        now = round(time.time(), 1)
        for v in self.viewers.values():
            k = v["id"] or v["name"]
            if k in self.here:
                self.here[k]["seen"] = now
        self.here_save()

    def presence(self, with_ids: bool, mine: dict | None = None) -> list[dict]:
        """the people connected, one per person (a second tab does not count); ids only for the owner's eyes; `me` marks the viewer's own entry"""
        out: dict = {}
        mykey = (mine["id"] or mine["name"]) if mine else None
        for v in self.viewers.values():
            if v.get("aux"):                             # a settings window (the owner, #372): not a tab, not a person being here
                continue
            key = v["id"] or v["name"]
            if v.get("task") in self.away_gone and v is not mine:   # left by idleness: not shown to the others (#1550)
                continue
            if key not in out:
                out[key] = {"name": v["name"], "acct_name": v.get("acct_name", ""), "avatar": v.get("avatar", ""), "role": v["role"],
                            "dname": self.dnames.get(v.get("task") or "", {}).get("name", ""),
                            "strikes": len(self.strikes.get(v.get("id") or "", [])),
                            **({"snd": self.sounds[v["task"]]} if self.sounds.get(v.get("task") or "") else {}),
                            **({"status": self.statuses[v["task"]]} if self.statuses.get(v.get("task") or "") else {}),
                            **({"id": v["id"]} if with_ids and v["id"] else {}), **({"me": True} if key == mykey else {})}
                out[key].update(ccs=[], langs=[])          # the language: everyone sees it (#2158); the country no longer (the owner, #510: only in access.jsonl)
                if with_ids:
                    out[key].update(via=[], tabs=0, since=v.get("since") or time.time())
                    if v.get("em"):                     # only the owner's page: the address of a mail sign-in, not its hash (#518)
                        out[key]["em"] = v["em"]
            if v.get("cam") and v.get("peer"):
                out[key]["cam"] = v["peer"]              # the page sharing the camera: the others connect to it (#1388)
                out[key]["camMir"] = bool(v.get("mirror"))
            if v.get("scr") and v.get("peer"):
                out[key]["scr"] = v["peer"]              # the page sharing its screen, besides the camera (the owner, #2188)
            if v.get("spj") and v["spj"] not in out[key].setdefault("spj", []):
                out[key]["spj"].append(v["spj"])         # the listening it joined (the owner, #1735: show who listens)
            if v.get("spj") and v.get("spv"):                # which video of that message: one message may have several (#1720)
                out[key].setdefault("spv", []).append(f'{v["spj"]}:{v["spv"]}')
            if v.get("away"):
                out[key]["away"] = True
                if v.get("yts"):                         # where the placeholder video is, for a page that comes in now (#1463)
                    out[key]["yts"] = {**v["yts"], "age": round(time.time() - v["yts"]["at"], 2)}
            out[key].setdefault("_st", []).append((v.get("st") or "", v.get("st_at") or time.time()))
            if v.get("lang") and v["lang"] not in out[key]["langs"]:
                out[key]["langs"].append(v["lang"])
            if v.get("tz") and not out[key].get("tz"):
                out[key]["tz"] = v["tz"]
            if with_ids:
                u = out[key]
                u["tabs"] += 1
                u["since"] = min(u["since"], v.get("since") or u["since"])
                if v.get("via") and v["via"] not in u["via"]:
                    u["via"].append(v["via"])
        # Away (the owner, 2026-11-04, #1671): a person is away by hand (kept per person, #2065), or every tab of theirs went idle by
        # itself (the page's "Away after" setting); `st` = "away", `stby` = manual / auto, `age` = seconds since
        for key, u in out.items():
            if key in self.nocc:                                  # hidden from everyone, the owner too; their own page gets the tick
                u["ccs"] = []
                if with_ids:
                    u["via"] = []
                if u.get("me"):
                    u["nocc"] = True
            if key not in self.showlang:
                u["langs"] = []
            elif u.get("me"):
                u["showlang"] = True
            if key not in self.showtz:
                u.pop("tz", None)
            elif u.get("me"):
                u["showtz"] = True
            sts = u.pop("_st", [])
            if key in self.away_manual:
                u.update(st="away", stby="manual", age=max(0.0, round(time.time() - self.away_manual[key], 1)))
            elif sts and all(st == "auto" for st, _ in sts):
                u.update(st="away", stby="auto", age=max(0.0, round(time.time() - max(at for _, at in sts), 1)))
        return sorted(out.values(), key=lambda u: (u["role"] != "owner", u["name"].lower()))

    async def send_presence(self) -> None:
        for ws in list(self.watchers):
            me = self.viewers.get(ws)
            if me:
                await send(ws, json.dumps({"type": "presence", "users": self.presence(me["role"] == "owner", me)}, ensure_ascii=False))

    async def watch_all(self, m: dict) -> None:
        s = json.dumps(m, ensure_ascii=False)
        for ws in list(self.watchers):
            me = self.viewers.get(ws)
            if me and me["role"] != "owner" and m.get("type") in ("ack", "del"):
                if m.get("id", 0) < self.start_for(me):
                    continue
            await send(ws, s)

    async def agent_ctl(self, a: dict, act: str) -> None:
        """the owner's buttons on an agent's card (#2124): stop - its listener exits with code 3 and a line telling it not to
        start one again; leave - the same, and it is off the roster (a bye). The line is what a Claude Code Monitor shows it"""
        lines = {"stop": "[agentchat] the owner stopped your listening from the page: do not start `agentchat listen` again until he asks you to",
                 "leave": "[agentchat] the owner sent you out of the chat from the page: you are off the roster; do not hello or listen again until he asks you to"}
        print(f"[agent_ctl] {act} {a['name']} ({a['task'][:8]})", flush=True)
        if act == "leave":
            await self.post("bye", a, "sent out of the chat by the owner")
        for w in list(self.listeners.pop(a["task"], ()) if act == "leave" else self.listeners.get(a["task"], ())):
            await send(w, CTL_TAG + lines[act])
            await w.close()
        if act == "stop":
            await self.watch_all({"type": "agents", "agents": self.roster()})

    async def reaper(self) -> None:
        """drop the agents whose process ended (same host), or silent for SILENT without a listener"""
        while True:
            await asyncio.sleep(15)
            self.here_touch()
            now = time.time()
            for a in list(self.agents.values()):
                why = None
                if a.get("host") == HOST and a.get("pid"):
                    st = proc_start(a["pid"])
                    # a Cursor agent's pid is the editor's shared process: it can end (a window reload) while the agent and its
                    # detached listener live on - a connected listener keeps it on the roster
                    if (st is None or (a.get("start") is not None and st != a["start"])) and not self.listeners.get(a["task"]):
                        why = "its process ended"
                alive = a.get("host") == HOST and a.get("pid") and proc_start(a["pid"]) is not None
                if not why and not alive and not self.listeners.get(a["task"]) and now - a["seen"] > SILENT:
                    why = f"silent for {SILENT // 3600} hours, no listener"
                if why:
                    await self.post("gone", a, why)
                    for w in list(self.listeners.pop(a["task"], ())):      # tell its listener, then let it go: it exits with code 2
                        await send(w, f"[agentchat] the task {a['task'][:8]} was dropped from the roster ({why}): "
                                      f"agentchat hello <name> \"<task>\" again")
                        await w.close()


async def send(ws, s: str) -> bool:
    try:
        await ws.send_str(s)
        return True
    except (ConnectionError, RuntimeError):
        return False


chat: Chat


DEFAULT_TITLE = "LagomChat"
STRIKES_BAN = 3
CC_HUE = {"red": 0, "orange": 28, "yellow": 48, "green": 130, "cyan": 185, "blue": 215, "purple": 270, "pink": 325}
COLORS = {"red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"}     # Claude Code's /color


def color_of(c) -> str | None:
    """an agent's colour: one of Claude Code's /color names, or 'h<hue 0-359>' (a session without one: made up), else None"""
    c = str(c or "").strip().lower()
    if c in COLORS:
        return c
    m = re.fullmatch(r"h(\d{1,3})", c)
    return f"h{int(m.group(1)) % 360}" if m else None


DOC_MAX = 20000


def doc_of(q: dict) -> dict | None:
    """a long text a message carries (a SKILL.md to confirm: superman, the owner #1095): the page folds it under the message and
    lets a person edit it - the edited text comes back as a reply carrying it"""
    d = q.get("doc")
    if not isinstance(d, dict) or not str(d.get("text") or "").strip():
        return None
    return {"name": (" ".join(str(d.get("name") or "").split()) or "document")[:80], "text": str(d["text"])[:DOC_MAX]}


def name_error(name: str, task: str) -> str:
    """why an agent of `task` may not take `name` (a hello or a rename), or ''"""
    if name.lower() in USER_NAMES:
        return f"the name {name!r} is the person at the page: pick another"
    if not NAME.match(name):
        return f"name {name!r}: 1-32 letters, digits, '.', '_', '-'"
    other = next((a for a in chat.agents.values() if a["name"] == name and a["task"] != task), None)
    if other and name.lower() in DEPUTY:
        return (f"{name} is the owner's deputy and there is one already (task {other['task'][:8]}): "
                f"only one superman at a time - pick another name")
    if other:
        return f"{name} is taken by the task {other['task'][:8]}: pick another name"
    if name.lower() in DEPUTY and name != "superman":
        return "the deputy's name is spelled superman"
    return ""


async def op(q: dict, ws) -> dict:
    kind = q.get("op")
    if kind == "who":
        return {"ok": True, "agents": chat.roster()}
    if kind == "log":
        n = max(1, min(int(q.get("n") or 40), 2000))
        return {"ok": True, "events": [e for e in chat.events if e["kind"] not in PEOPLE_KINDS][-n:]}
    task = str(q.get("task") or "")
    if not task:
        return {"ok": False, "error": "no task code"}
    text = str(q.get("text") or "")[:8000]
    if chat.agent_banned(task) or any(chat.agent_banned(t) for t in chat.strikes_tasks() if t.startswith(task) and len(task) >= 8):
        return {"ok": False, "error": "this session is banned from the chat: three strikes (the owner's word through superman)"}
    if kind in ("hello", "rename"):
        name = str(q.get("name") or "")
        bad = name_error(name, task)
        if bad:
            return {"ok": False, "error": bad}
    if kind == "hello":
        if not text.strip():
            return {"ok": False, "error": "say what you are on and what you take"}
        before = [dict(x, text=(x["text"][:CLIP] + "...") if len(x["text"]) > CLIP else x["text"])
                  for x in [x for x in chat.events if x["kind"] in ("msg", "hello", "bye")][-HELLO_RECENT:]]
        a = {"name": name, "task": task}
        if chat.meta.pop(task, None) is not None:
            chat.meta_file.write_text(json.dumps(chat.meta, ensure_ascii=False))
        await chat.post("hello", a, text, **{k: (str(q[k])[:60] if k in ("program", "model") and q.get(k) else q.get(k))
                                              for k in ("pid", "start", "host", "cwd", "program", "model")},
                        color=color_of(q.get("color")))
        chat.clients.setdefault(task, set()).add(ws)
        chat.delivered[task] = chat.next_id - 1
        return {"ok": True, "others": [x for x in chat.roster() if x["task"] != task], "recent": before,
                "examples": chat.examples(), "handoff": chat.handoffs.get(name.lower())}
    a = chat.find(task)
    if not a:
        return {"ok": False, "error": f"the task {task[:8]} has not joined: agentchat hello <name> \"<task>\" first"}
    a["seen"] = time.time()
    if a.pop("restored", None):
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
    if kind == "say":
        if not text.strip():
            return {"ok": False, "error": "empty message"}
        to = [USER["name"] if str(n).lower() in USER_NAMES else chat.current(str(n)) for n in q.get("to") or []]
        names = {x["name"] for x in chat.agents.values()} | {USER["name"]}
        bad = [n for n in to if n not in names]
        if bad:
            return {"ok": False, "error": f"nobody named {', '.join(bad)} here (here: {', '.join(sorted(names))})"}
        rep = chat.reply_ref(q.get("reply")) if q.get("reply") else None
        if rep and not to and rep["agent"] != a["name"]:
            to = [rep["agent"]]                              # an answer goes to the author of what it answers
        to, every = chat.with_all(a, text, to)
        if AGENT_TURNS and USER["name"] not in to and chat.agent_run >= AGENT_TURNS:
            if not chat.agent_paused:                    # once per pause: the owner's page gets the question
                chat.agent_paused = True
                await chat.post("msg", a, f"The agents wrote {chat.agent_run} messages in a row with nobody else; "
                                "they wait for a person now.", [USER["name"]], ask=["Continue"])
            return {"ok": False, "error": f"paused: {chat.agent_run} agents' messages in a row with no person; wait for "
                    "the owner (Continue or any message of a person), or write to the owner (--to owner)"}
        e = await chat.post("msg", a, text, to, reply=rep, doc=doc_of(q), all=every or None, loop=True if q.get("loop") else None)
        if USER["name"] not in to:
            chat.agent_run += 1
        r = {"ok": True, "id": e["id"]}
        if ALL_RE.search(text) and not every:
            r["note"] = "only the owner and those he allows may use @all (it stayed plain text)"
        return r
    if kind == "doc":
        # the document a message carries, whole (a listener's line only says there is one)
        try:
            mid = int(str(q.get("id") or "0").lstrip("#"))
        except ValueError:
            mid = 0
        e = next((x for x in chat.events if x["id"] == mid), None)
        if not e or not e.get("doc") or mid in chat.deleted:
            return {"ok": False, "error": f"the message #{mid} carries no document"}
        return {"ok": True, "doc": e["doc"], "agent": e["agent"], "id": mid}
    if kind == "upload":
        # a picture or a short video of an agent on another machine (the Mac, a Deck): its path there means nothing here, so
        # the CLI sends the file and puts the path it gets back into the message - the page shows it (the owner, 2026-10-30)
        name = str(q.get("name") or "")
        ext = Path(name).suffix.lower()
        ext = ".jpg" if ext == ".jpeg" else ext
        try:
            data = base64.b64decode(q.get("data") or "", validate=True)
        except ValueError:
            return {"ok": False, "error": "upload: data is not base64"}
        magic = {**UP_MAGIC, ".mp4": None, ".m4v": None, ".mov": None, ".webm": b"\x1a\x45\xdf\xa3"}
        if ext not in magic:
            return {"ok": False, "error": "upload: png, jpeg, gif, webp, mp4, m4v, mov or webm only"}
        if len(data) > MAX_AGENT_UPLOAD:
            return {"ok": False, "error": f"upload: over {MAX_AGENT_UPLOAD >> 20} MB"}
        good = data[4:8] == b"ftyp" if magic[ext] is None else data.startswith(magic[ext]) and (ext != ".webp" or data[8:12] == b"WEBP")
        if not good:
            return {"ok": False, "error": f"upload: {name} is not a {ext[1:]} file"}
        UPLOADS.mkdir(parents=True, exist_ok=True)
        f = UPLOADS / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}{ext}"
        f.write_bytes(data)
        return {"ok": True, "path": str(f)}
    if kind == "react_one":
        # who may have only one reaction on a message (the owner, #1997): superman adds / removes a name; anyone lists
        act, val = str(q.get("act") or "list"), str(q.get("val") or "").strip().lower().lstrip("@")
        if act in ("add", "remove"):
            if chat.find(task)["name"].lower() not in DEPUTY:
                return {"ok": False, "error": "this list is superman's (the owner's deputy) to change"}
            if not val:
                return {"ok": False, "error": "agentchat react-one <name> | --remove <name>"}
            lst = set(chat.react_one)
            (lst.add if act == "add" else lst.discard)(val)
            chat.react_one = sorted(lst)
            chat.one_file.write_text(json.dumps(chat.react_one, ensure_ascii=False))
            print(f"[react-one] {act} {val}", flush=True)
        return {"ok": True, "names": chat.react_one}
    if kind == "allow_all":
        # who may say @all (the owner, #1683): superman adds / removes a name (an agent's, a person's on the page); anyone lists
        act, val = str(q.get("act") or "list"), str(q.get("val") or "").strip().lower().lstrip("@")
        if act in ("add", "remove"):
            if chat.find(task)["name"].lower() not in DEPUTY:
                return {"ok": False, "error": "the @all list is superman's (the owner's deputy) to change"}
            if not val:
                return {"ok": False, "error": "agentchat allow-all <name> | --remove <name>"}
            lst = set(chat.allow_all)
            (lst.add if act == "add" else lst.discard)(val)
            chat.allow_all = sorted(lst)
            chat.all_file.write_text(json.dumps(chat.allow_all, ensure_ascii=False))
            print(f"[allow-all] {act} {val}", flush=True)
            await chat.watch_all({"type": "allow_all", "names": chat.allow_all})
        return {"ok": True, "names": chat.allow_all}
    if kind == "allow":
        # who may in by Google (the owner, #1497): superman adds / removes a Google domain or email
        a = auth.allow_list()                            # (here a is the list, not the agent)
        act, what, val = str(q.get("act") or "list"), str(q.get("what") or ""), str(q.get("val") or "").strip().lower().lstrip("@*")
        if act in ("add", "remove"):
            if chat.find(task)["name"].lower() not in DEPUTY:
                return {"ok": False, "error": "the allow-list is superman's (the owner's deputy) to change"}
            key = {"domain": "google_domains", "email": "google_emails"}.get(what)
            if not key or not val or (key == "google_emails" and "@" not in val):
                return {"ok": False, "error": "agentchat allow add|remove domain <example.com> | email <a@b.c>"}
            lst = set(a[key])
            (lst.add if act == "add" else lst.discard)(val)
            a[key] = sorted(lst)
            auth.save_allow(a)
            _ROLE_CACHE.clear()
            print(f"[allow] {act} {what} {val}", flush=True)
        return {"ok": True, "allow": a, "providers": sorted(auth.oauth_conf())}
    if kind in ("strike", "unstrike", "strikes"):
        # strikes for a person (the owner #1126) or an agent (#1224): superman gives / takes back; 3 bans; "strikes" lists them for anybody
        if kind == "strikes":
            return {"ok": True, "strikes": chat.strikes}
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "strikes are superman's (the owner's deputy) to give"}
        try:
            ref = int(str(q.get("ref") or "0").lstrip("#")) or None
        except ValueError:
            ref = None
        if kind == "strike" and not text.strip():
            return {"ok": False, "error": "say why: agentchat strike <name> \"<reason>\""}
        return await chat.strike(str(q.get("who") or ""), text.strip(), a["name"], ref, take=kind == "unstrike")
    if kind in ("pin", "unpin"):
        # superman pins / unpins any message (the owner on the page); "pins" lists them for anybody
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "pinning is the owner's (on the page) and superman's"}
        try:
            mid = int(str(q.get("id") or "0").lstrip("#"))
        except ValueError:
            mid = 0
        bad = await chat.set_pin(mid, kind == "pin", a["name"])
        return {"ok": False, "error": bad} if bad else {"ok": True, "pins": chat.pins_view()}
    if kind == "pins":
        return {"ok": True, "pins": chat.pins_view()}
    if kind == "sticker_import":
        # a pack from Telegram by its link (the owner, #225): any agent here; it goes on in the background, status asks how far it is
        m = TG_PACK_RE.fullmatch(str(q.get("url") or "").strip())
        if not m:
            return {"ok": False, "error": "not a link to a sticker pack: t.me/addstickers/<name>"}
        name = m.group(1)
        job = sticker_jobs.get(name)
        if q.get("status"):
            return {"ok": True, "job": job} if job else {"ok": False, "error": "no such import"}
        if q.get("remove"):
            by = "telegram:" + name
            n = len(chat.stickers)
            chat.stickers = [s for s in chat.stickers if s.get("by") != by]
            chat.stickers_file.write_text(json.dumps(chat.stickers))
            print(f"[sticker] {a['name']} removed the pack {name}: {n - len(chat.stickers)}", flush=True)
            return {"ok": True, "removed": n - len(chat.stickers)}
        if not tg_token():
            return {"ok": False, "error": "no Telegram bot token on the server: the file telegram_bot.token in its config folder"}
        if job and job["state"] == "running":
            return {"ok": True, "job": job}
        sticker_jobs[name] = job = {"name": name, "state": "running", "title": name, "total": 0, "done": 0, "added": 0, "same": 0, "skipped": 0}
        print(f"[sticker] {a['name']} imports the pack {name}", flush=True)
        asyncio.ensure_future(_sticker_import(name, job))
        return {"ok": True, "job": job}
    if kind == "title":
        # the chat's name on the page: superman's to change (the owner, #1078); no text - say what it is now
        # its translations (#1723): {"en": .., "ru": .., "cs": .., "de": ..}; a new name drops the old ones, --tr alone adds to them
        t = " ".join(text.split())[:60]
        tr = {k: " ".join(str(v).split())[:60] for k, v in (q.get("tr") or {}).items() if k in ("en", "ru", "uk", "be", "cs", "de", "es", "sv") and str(v).strip()}
        if not t and not tr:
            return {"ok": True, "title": chat.title, "tr": chat.title_tr}
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "the chat's name is superman's (the owner's deputy) to change"}
        await chat.set_title(t, tr, a["name"])
        return {"ok": True, "title": chat.title, "tr": chat.title_tr}
    if kind == "history":
        # the whole log from a time or a message on (the owner, 2026-11-02): superman's only - the deputy keeps the chat's memory
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "the history is superman's (the owner's deputy) only; others: agentchat log [N]"}
        try:
            since, from_id = float(q.get("since") or 0), int(q.get("from_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "since: a unix time; from_id: a message id"}
        kinds = set(q.get("kinds") or []) or None
        evs = [chat.public(e) for e in chat.events if e["ts"] >= since and e["id"] >= from_id and (kinds is None or e["kind"] in kinds)]
        return {"ok": True, "events": evs, "first": chat.events[0]["id"] if chat.events else 0}
    if kind == "purge":
        # clean the chat between two times or messages, either bound may be left out (the owner, #14): superman's, like the history
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "cleaning the chat is superman's (the owner's deputy) only"}
        try:
            since, till = float(q.get("since") or 0), float(q.get("till") or 0)
            from_id, till_id = int(q.get("from_id") or 0), int(q.get("till_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "since, till: unix times; from_id, till_id: message ids"}
        gone, stay = chat.purge_plan(since, till, from_id, till_id)
        r = {"ok": True, "count": len(gone), "msgs": sum(e["kind"] == "msg" for e in gone), "kept": stay,
             "first": gone[0]["id"] if gone else 0, "last": gone[-1]["id"] if gone else 0,
             "first_ts": gone[0]["ts"] if gone else 0, "last_ts": gone[-1]["ts"] if gone else 0}
        if gone and q.get("go") is True:
            r["archive"] = await chat.purge(gone, a["name"])
        return r
    if kind == "turn":
        # the session finished a turn (its listener saw it in the transcript): it may stay silent on purpose - praise needs no
        # answer - so the page stops showing "answering" for what it got before this (the owner, #1744)
        try:
            a["turn_end"] = round(time.time() - max(0.0, float(q.get("ago") or 0)), 3)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad ago"}
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True}
    if kind == "meta":
        # the session's model (/model) or colour (/color) changed: the card and the name's colour on the page follow
        m = {}
        if q.get("model"):
            m["model"] = str(q["model"])[:60]
        if "color" in q:
            m["color"] = color_of(q.get("color"))
        if m and any(a.get(k) != v for k, v in m.items()):
            a.update(m)
            chat.meta.setdefault(task, {}).update(m)
            chat.meta_file.write_text(json.dumps(chat.meta, ensure_ascii=False))
            await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True}
    if kind == "agent_token":
        # the tokens of agents on other machines (the owner, #446): superman's, by the owner's word. A token's connection can't get here
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "the tokens are superman's (the owner's deputy) to give"}
        act, who = str(q.get("act") or "list"), str(q.get("val") or "").strip()
        toks = tokens_load()
        if act == "list":
            return {"ok": True, "names": sorted(t["name"] for t in toks.values())}
        if not who:
            return {"ok": False, "error": "agentchat token add <name> | revoke <name> | list"}
        if act == "add":
            new, bad = token_new(who)
            return {"ok": False, "error": bad} if bad else {"ok": True, "name": who, "token": new}
        if act == "revoke":
            hit = [(h, t) for h, t in toks.items() if t["name"].lower() == who.lower()]
            if not hit:
                return {"ok": False, "error": f"{who} has no token"}
            for h, t in hit:
                del toks[h]
            tokens_save(toks)                                 # first: a socket closed below can't come back with it
            for h, t in hit:
                for w in list(REMOTE_WS.pop(t["task"], ())) + list(chat.listeners.pop(t["task"], ())):
                    await w.close()
                gone = chat.find(t["task"])
                if gone:
                    await chat.post("bye", gone, "the token was revoked")
            return {"ok": True, "name": who}
        return {"ok": False, "error": f"no {act!r}: add, revoke or list"}
    if kind == "rename":
        # the agent's session got a new name (Claude Code's /rename, seen by its listener): the same agent, a new name
        if name == a["name"]:
            return {"ok": True, "name": name}
        if a["name"].lower() in DEPUTY:
            return {"ok": False, "error": "superman stays superman: bye, then hello under the new name"}
        old = a["name"]
        await chat.post("rename", {"name": name, "task": task}, f"{old} is now {name}", was=old)
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True, "name": name}
    if kind == "remind":                                       # an agent sets a reminder for the owner (or lists / cancels its own)
        return {"ok": True, "text": remind_cmd({"task": a["task"], "name": a["name"], "role": "agent"}, text)}
    if kind == "handoff":
        if not text.strip():
            return {"ok": False, "error": "an empty note"}
        chat.save_handoff(a, text.strip())
        return {"ok": True}
    if kind == "bye":
        await chat.post("bye", a, text)
        for w in list(chat.listeners.pop(a["task"], ())):
            await w.close()
        return {"ok": True}
    if kind == "inbox":
        since = max(chat.delivered.get(a["task"], 0), a["since"])
        got = [e for e in chat.events if e["id"] > since and chat.hears(e, a["task"])]
        chat.delivered[a["task"]] = chat.next_id - 1
        chat.pend.pop(a["task"], None)
        chat.due.pop(a["task"], None)
        await chat.mark_got(a["task"], got)
        return {"ok": True, "events": got}
    if kind == "skills":
        # what the owner may call with a slash on this agent: kept apart from the roster (it is long); the page asks for it
        seen: dict[str, dict] = {}
        for x in (q.get("skills") or [])[:400]:
            if isinstance(x, dict) and x.get("n") and str(x["n"])[:80] not in seen:
                seen[str(x["n"])[:80]] = {"n": str(x["n"])[:80], "d": str(x.get("d", ""))[:140]}
        sk = list(seen.values())[:300]
        chat.skills[a["task"]] = sk
        chat.skills_file.write_text(json.dumps(chat.skills, ensure_ascii=False))
        return {"ok": True, "n": len(sk)}
    if kind == "git":
        # the agent's folder state (branch, last commit, status lines), refreshed by its hello / working / say: shown on its card
        g = q.get("git")
        if isinstance(g, dict):
            a["git"] = {"branch": str(g.get("branch", ""))[:80], "track": str(g.get("track", ""))[:120], "commit": str(g.get("commit", ""))[:140],
                        "dirty": int(g.get("dirty") or 0), "status": [str(x)[:160] for x in (g.get("status") or [])[:30]],
                        "ts": round(time.time(), 1)}
            if isinstance(g.get("pr"), dict):           # its branch's pull request (the owner, #1091): only the known fields, made small again
                try:
                    p = g["pr"]
                    a["git"]["pr"] = pr_summary({"number": p.get("n"), "title": p.get("title"), "url": p.get("url"), "state": p.get("state"),
                                                 "isDraft": p.get("draft"), "reviewDecision": p.get("review"), "author": {"login": p.get("author")},
                                                 "headRefName": p.get("head"), "baseRefName": p.get("base"), "additions": p.get("add"), "deletions": p.get("del")})
                    a["git"]["pr"]["ci"] = {k: max(0, int((p.get("ci") or {}).get(k) or 0)) for k in ("ok", "fail", "run")}
                    if not a["git"]["pr"]["url"]:
                        del a["git"]["pr"]
                except (TypeError, ValueError, AttributeError):
                    a["git"].pop("pr", None)
            if isinstance(q.get("cwd"), str) and q["cwd"]:
                a["cwd"] = q["cwd"][:300]       # the git is of the folder the command ran in, not always the hello's (a shell's cwd resets)
            await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True}
    if kind == "usage":
        # an agent on another machine (its listener) tells its session's token usage; kept on its card
        u = q.get("usage")
        if isinstance(u, dict) and a.get("host") != HOST:     # an agent on this machine: usage_loop reads its transcript, from the moment it joined (#430)
            a["usage"] = {k: (u[k] if k == "model" else float(u[k]) if k == "cost" else int(u[k])) for k in ("calls", "input", "output", "cache_read", "cache_write", "ctx", "model", "cost", "unpriced") if k in u}
            a["usage"]["ts"] = round(time.time(), 1)
            await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True}
    if kind == "ask":
        # a question for the owner with answers to pick: his page shows buttons, the card shows the agent waiting; the agent goes on
        # with what does not depend on the answer (no blocking dialog); his pick comes back as a message that quotes the question
        opts = [" ".join(str(x).split())[:80] for x in (q.get("options") or []) if str(x).strip()][:6]
        if not text.strip() or len(opts) < 2:
            return {"ok": False, "error": 'agentchat ask "<question>" "<option 1>" "<option 2>" ... (2 to 6 options)'}
        e = await chat.post("msg", a, text[:1200], [USER["name"]], ask=opts)
        a["waiting"] = {"text": " ".join(text.split())[:140], "id": e["id"], "since": e["ts"]}
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True, "id": e["id"]}
    if kind == "poll":                           # an agent's poll (the owner, #1386): the page shows the options to vote on
        opts = list(dict.fromkeys(" ".join(str(x).split())[:100] for x in (q.get("options") or []) if str(x).strip()))[:10]
        if not text.strip() or len(opts) < 2:
            return {"ok": False, "error": 'agentchat poll [--multi] "<question>" "<option 1>" "<option 2>" ... (2 to 10 options)'}
        e = await chat.post("msg", a, text[:500], poll={"o": opts, "multi": bool(q.get("multi")), **({"anon": True} if q.get("anon") else {})})
        return {"ok": True, "id": e["id"]}
    if kind == "pvote":                          # an agent's pick in a poll: on, or off again
        bad = await chat.poll_vote(int(q.get("id") or 0), a["name"], int(q.get("o") or 0))
        return {"ok": False, "error": bad} if bad else {"ok": True, **chat.poll_view(int(q.get("id") or 0), a["name"])}
    if kind == "react":                          # an agent's emoji on a message: on, or off again (the owner, #1762)
        bad = await chat.react(int(q.get("id") or 0), text, a["name"], {"id": "a:" + a["task"].replace(".", "_"), "name": a["name"], "task": a["task"]})
        return {"ok": False, "error": bad} if bad else {"ok": True, "r": chat.reacts.get(int(q.get("id") or 0), {})}
    if kind == "vote":
        # an agent rates a message of a person or of another agent: up / down / clear (not its own)
        d = {"up": 1, "+1": 1, "down": -1, "-1": -1, "clear": 0, "0": 0}.get(text.strip().lower())
        if d is None:
            return {"ok": False, "error": "vote up, down or clear"}
        await chat.vote(int(q.get("id") or 0), d, {"id": "a:" + a["task"].replace(".", "_"), "name": a["name"], "task": a["task"]})
        return {"ok": True}
    if kind == "translate":
        return await chat.set_trans(int(q.get("id") or 0), str(q.get("lang") or ""), text, a["name"])
    if kind == "status":
        # the agent's text status on its card (the owner, #1889): stays until changed; empty clears it
        s, err = status_clean(q.get("text"))
        if err:
            return {"ok": False, "error": {"emoji": "no emoji in a status", "too_long": f"{STATUS_MAX} characters at most"}[err]}
        a["status"] = s or None
        chat.meta.setdefault(task, {})["status"] = s or None
        chat.meta_file.write_text(json.dumps(chat.meta, ensure_ascii=False))
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True, "status": s}
    if kind == "working":
        # what the agent is on now, short: the page shows it on the agent's card; its next message or bye clears it
        w = text.strip()[:120]
        if w:
            a["working"] = {"text": w, "since": round(time.time(), 3)}
        else:
            a.pop("working", None)
            a.pop("waiting", None)
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True}
    return {"ok": False, "error": f"no op {kind!r}"}


def from_proxy(req: web.Request) -> bool:
    """came through Caddy / Cloudflare: the agents' sockets take such a connection only with an agent's token"""
    return any(h in req.headers for h in ("X-Forwarded-For", "X-Forwarded-Host", "CF-Connecting-IP", "CF-Ray"))


# ---------------------------------------------------------------- agents on other people's machines (the owner, #446, #450)
# A token is a name: the server fills in the name and the task of whatever such a connection sends, so it can't speak for another
# agent or take superman's name. Only the hash is kept. It sees the chat from the moment the token was made, not the old talk.
TOKENS_FILE = auth.CONF / "agent_tokens.json"
REMOTE_OPS = {"who", "remind", "log", "hello", "say", "doc", "upload", "turn", "meta", "bye", "inbox", "git", "usage", "ask", "poll", "pvote", "react",
              "translate", "status", "working", "pins"}
REMOTE_WS: dict[str, set] = {}


def tokens_load() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def tokens_save(d: dict) -> None:
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    tmp.replace(TOKENS_FILE)


def token_bad(who: str) -> str:
    """why this name can't have a token ('' = it can)"""
    bad = name_error(who, "remote-" + secrets.token_hex(12))
    if bad or who.lower() in DEPUTY or who.lower() in ("owner", "user", "lagomchat", USER["name"].lower()):
        return bad or f"{who} is not a name for a token"
    if any(t["name"].lower() == who.lower() for t in tokens_load().values()):
        return f"{who} has a token: revoke it first"
    return ""


def token_new(who: str) -> tuple[str, str]:
    """(the token, '') or ('', why not); only its hash is kept"""
    bad = token_bad(who)
    if bad:
        return "", bad
    toks = tokens_load()
    new = secrets.token_urlsafe(32)
    h = hashlib.sha256(new.encode()).hexdigest()
    toks[h] = {"name": who, "task": "remote-" + h[:24], "since": chat.next_id, "at": time.time()}
    tokens_save(toks)
    return new, ""


# An agent on another computer knocks (the owner, #2348): `lagomchat knock <name>` posts here, the owner's page gets
# "Let in" / "Refuse"; on "Let in" the waiting command gets its token once and keeps it. No token by hand.
KNOCKS: dict[str, dict] = {}                      # knock id (secret, the knocker's only) -> {name, what, ip, ask, state, at}
KNOCK_TTL, KNOCK_MAX = 900, 5
KNOCK_OPTS = ["Let in", "Refuse"]


def knocks_sweep() -> None:
    for k, v in list(KNOCKS.items()):
        if time.time() - v["at"] > KNOCK_TTL:
            del KNOCKS[k]


async def knock_answer(e: dict) -> None:
    """the owner's pick on a knock's question"""
    rid = (e.get("reply") or {}).get("id")
    k = next((k for k, v in KNOCKS.items() if v["ask"] == rid and v["state"] == "wait"), None)
    if k and e["text"] in KNOCK_OPTS:
        KNOCKS[k]["state"] = "in" if e["text"] == KNOCK_OPTS[0] else "out"


async def join_knock(req: web.Request) -> web.Response:
    knocks_sweep()
    if req.method == "GET":                       # the knocker waits: wait / in (with its token, once) / out / gone
        k = KNOCKS.get(req.query.get("id", ""))
        if not k:
            return web.json_response({"state": "gone"})
        if k["state"] != "in":
            return web.json_response({"state": k["state"]})
        del KNOCKS[req.query["id"]]
        tok, bad = token_new(k["name"])
        if bad:
            return web.json_response({"state": "out", "error": bad})
        access_log(req, "knock-in", name=k["name"])
        return web.json_response({"state": "in", "name": k["name"], "token": tok})
    try:
        q = await req.json()
    except ValueError:
        return web.json_response({"ok": False, "error": "JSON: {name, what}"}, status=400)
    who, what = " ".join(str(q.get("name") or "").split())[:40], " ".join(str(q.get("what") or "").split())[:300]
    ip, city = where_from(req)
    bad = token_bad(who) if who else "lagomchat knock <your name> \"<what you will work on>\""
    if not bad and any(v["state"] == "wait" and (v["ip"] == ip or v["name"].lower() == who.lower()) for v in KNOCKS.values()):
        bad = "a knock from here or by this name already waits for the owner"
    if not bad and sum(v["state"] == "wait" for v in KNOCKS.values()) >= KNOCK_MAX:
        bad = "too many knocks wait for the owner now; try later"
    if bad:
        return web.json_response({"ok": False, "error": bad}, status=400)
    access_log(req, "knock", name=who)
    e = await chat.post("msg", {"name": "LagomChat", "task": "knock"},
                        f"An agent knocks: {who}, from {ip}{' (' + city + ')' if city else ''}"
                        f"{': ' + what if what else ''}. Let it in? It gets a token for the name {who}.",
                        [USER["name"]], ask=KNOCK_OPTS)
    kid = secrets.token_urlsafe(24)
    KNOCKS[kid] = {"name": who, "what": what, "ip": ip, "ask": e["id"], "state": "wait", "at": time.time()}
    return web.json_response({"ok": True, "id": kid, "wait": KNOCK_TTL})


def where_from(req: web.Request) -> tuple[str, str]:
    """the address and, when Cloudflare's visitor location headers are on, the city; no outside service gets the address"""
    ip = req.headers.get("CF-Connecting-IP") or (req.remote or "?")
    city = req.headers.get("CF-IPCity") or ""
    try:
        city = city.encode("latin-1").decode("utf-8")     # Cloudflare sends UTF-8, aiohttp reads it as latin-1
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    region = req.headers.get("CF-Region-Code") or ""
    return ip, f"{city}, {region}" if city and region else city


def access_log(req: web.Request, kind: str, **who) -> None:
    """who came from where, for an incident (the owner, #506): never shown on the page, not sent to anybody. access.jsonl in the data dir"""
    ip, city = where_from(req)
    rec = {"ts": round(time.time(), 1), "kind": kind, "ip": ip, "city": city, "cc": req.headers.get("CF-IPCountry") or "",
           "via": "cf" if req.headers.get("Cf-Ray") else "direct", "ua": (req.headers.get("User-Agent") or "")[:160], **who}
    try:
        f = DATA / "access.jsonl"
        f.touch(mode=0o600)
        with f.open("a") as o:
            o.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as x:
        print(f"[access] not written: {x}", flush=True)


def note_agent_ip(req: web.Request, task: str, kind: str) -> None:
    a = chat.agents.get(task)
    access_log(req, kind, name=a["name"] if a else "", task=task)


def token_of(req: web.Request) -> dict | None:
    """the token this request carries through the proxy: {name, task, since, ...} or None"""
    h = req.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    return tokens_load().get(hashlib.sha256(h[7:].strip().encode()).hexdigest())


def remote_op(q: dict, tok: dict) -> dict:
    """what a token's connection may ask, with its own name and task whatever it sent"""
    # an upload's "name" is the file's name, not the agent's: overwritten, every remote picture was refused (the owner #78)
    return {**q, "task": tok["task"], "host": "remote", **({} if q.get("op") == "upload" else {"name": tok["name"]})}


def remote_view(q: dict, r: dict, tok: dict) -> dict:
    """the answer without the talk from before the token"""
    for k in ("events", "recent"):
        if isinstance(r.get(k), list):
            r[k] = [e for e in r[k] if e.get("id", 0) >= tok["since"]]
    if q.get("op") == "hello" and r.get("ok"):
        r["name"] = tok["name"]                           # what it asked for does not count: this is who it is
        r["task"] = tok["task"]
    return r


def remote_paths(tok: dict) -> set[str]:
    """the pictures a token may fetch: named in the others' messages since the token, not in its own (a path it wrote proves nothing)"""
    pat = re.compile(r"(?:/[\w.\-]+)+\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v)", re.I)
    out: set[str] = set()
    for e in chat.events:
        if e["id"] >= tok["since"] and e.get("task") != tok["task"]:
            out.update(pat.findall(e.get("text") or ""))
    return out


async def ws_ops(req: web.Request) -> web.WebSocketResponse:
    tok = token_of(req) if from_proxy(req) else None
    if from_proxy(req) and not tok:
        access_log(req, "refused", path=req.path, had_token="Authorization" in req.headers)
        raise web.HTTPForbidden(text="the agents' socket: direct connections or an agent's token only")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=2 ** 25)       # an agent's upload: 20 MB, base64
    await ws.prepare(req)
    if tok:
        REMOTE_WS.setdefault(tok["task"], set()).add(ws)
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                q = json.loads(m.data)
                if not tok:
                    r = await op(q, ws)
                    if q.get("op") == "hello" and r.get("ok"):
                        note_agent_ip(req, str(q.get("task") or ""), "agent")
                elif q.get("op") not in REMOTE_OPS:
                    r = {"ok": False, "error": "not for an agent with a token"}
                else:
                    r = remote_view(q, await op(remote_op(q, tok), ws), tok)
                    if q.get("op") == "hello" and r.get("ok"):
                        note_agent_ip(req, tok["task"], "agent-token")
            except (ValueError, TypeError) as x:
                r = {"ok": False, "error": f"bad request: {x}"}
            await send(ws, json.dumps(r, ensure_ascii=False))
    finally:
        for s in chat.clients.values():
            s.discard(ws)
        if tok:
            REMOTE_WS.get(tok["task"], set()).discard(ws)
    return ws


SHELL_RE = re.compile(r"^(?:@[\w.-]+[,:]?\s+)*!\s*(\S.*)$", re.S)   # "@agent ! ls -al" or "! ls -al" (to by a reply / the picker)
SHELL_TAG = "[agentchat-sh] "
CTL_TAG = "[agentchat-ctl] "                # the owner's stop / leave for a listener (#2124): it prints the rest and exits with code 3


async def ws_listen(req: web.Request) -> web.WebSocketResponse:
    tok = token_of(req) if from_proxy(req) else None
    if from_proxy(req) and not tok:
        access_log(req, "refused", path=req.path, had_token="Authorization" in req.headers)
        raise web.HTTPForbidden(text="the agents' socket: direct connections or an agent's token only")
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    a = chat.find(tok["task"] if tok else req.query.get("task", ""))
    if a and chat.agent_banned(a["task"]):
        await send(ws, f"[agentchat] the task {a['task'][:8]} is banned from the chat: three strikes")
        await ws.close()
        return ws
    if not a:
        await send(ws, f"[agentchat] the task {req.query.get('task', '')!r} has not joined: "
                       f"agentchat hello <name> \"<task>\" first, then listen")
        await ws.close()
        return ws
    task = a["task"]
    chat.listeners.setdefault(task, set()).add(ws)
    a["seen"] = time.time()
    note_agent_ip(req, task, "agent-token-listen" if tok else "agent-listen")
    a.pop("restored", None)
    missed = [e for e in chat.events if e["id"] > max(chat.delivered.get(task, 0), a["since"]) and chat.hears(e, task)]
    chat.pend.pop(task, None)
    chat.due.pop(task, None)
    await chat.deliver(task, missed[-REPLAY:])
    await chat.watch_all({"type": "agents", "agents": chat.roster()})
    try:
        async for _ in ws:
            pass
    finally:
        chat.listeners.get(task, set()).discard(ws)
        if task in chat.agents:
            chat.agents[task]["seen"] = time.time()
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
    return ws


# ---------------------------------------------------------------- who is at the page
_ROLE_CACHE: dict[str, tuple[float, str]] = {}


def origin_of(req: web.Request) -> tuple[str, str, str]:
    """(scheme, host, path prefix) the browser used: the host:port as it is (no prefix)"""
    host = req.headers.get("X-Forwarded-Host") or req.headers.get("Host", "")
    scheme = "https" if host.endswith(tuple("." + d for d in settings()["domains"])) or req.headers.get("X-Forwarded-Proto") == "https" else "http"
    return scheme, host, ""


def session_of(req: web.Request) -> dict | None:
    return auth.read_token(req.cookies.get(auth.COOKIE))


async def role_of(sid: str | None) -> str:
    """owner / verified / pending (signed in, not let in) / '' (nobody)"""
    if not sid:
        return ""
    if sid == auth.owner_sid():
        return "owner"
    if len(chat.strikes.get(sid, [])) >= STRIKES_BAN:
        return "banned"                                       # three strikes (the owner, #1126): no chat, no sign-in
    hit = _ROLE_CACHE.get(sid)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    r = "verified" if await asyncio.to_thread(chat.rdb.user_verified, sid) else "pending"
    _ROLE_CACHE[sid] = (time.time(), r)
    return r


def human_name(sess: dict) -> str:
    n = re.sub(r"[^\w.-]+", "_", (sess.get("name") or "")).strip("_")[:28] or "user" + sess["sid"][-6:]
    return n if n.lower() not in USER_NAMES and n not in {a["name"] for a in chat.agents.values()} else n + "_h"


async def viewer_of(req: web.Request) -> dict:
    v = await _viewer_of(req)
    v["acct_avatar"] = v.get("avatar", "")
    p = chat.photos.get(v.get("task") or "")
    if p and p.get("on"):
        v["avatar"] = f'photo/{p["h"]}.jpg'
    return v


_CF_SEEN: dict[str, tuple[float, str | None]] = {}


async def email_session(req: web.Request) -> dict | None:
    """who Cloudflare Access says this is, checked against its keys; a token is looked at once a minute"""
    tok = cf_token(req)
    if not tok:
        return None
    hit = _CF_SEEN.get(tok)
    if not hit or time.time() - hit[0] > 60:
        if len(_CF_SEEN) > 500:
            _CF_SEEN.clear()
        hit = _CF_SEEN[tok] = (time.time(), await asyncio.to_thread(auth.cf_access_email, tok))
    return {"sid": auth.email_sid(hit[1]), "name": hit[1].split("@")[0], "em": hit[1]} if hit[1] else None


async def _viewer_of(req: web.Request) -> dict:
    """who is at this page: {role, name, id, task, avatar, enforced}; with sign-in not enforced everybody is the owner as before"""
    enforced = auth.enforced()
    owner_em = auth.owner_email()                        # sign-in by email only (the owner, 2026-11-06): another cookie counts for nothing
    sess = await email_session(req) if owner_em else session_of(req)
    role = "owner" if owner_em and sess and sess["em"] == owner_em else await role_of(sess["sid"]) if sess else ""
    if role == "pending" and auth.allowed(sess):
        role = "verified"                                # on the allow-list (a Google domain or email; #1497)
    if role == "owner" or (not enforced and role in ("", "pending")):
        return {"role": "owner", "name": USER["name"], "id": auth.owner_sid() if role == "owner" else "", "em": (sess or {}).get("em", ""), "task": USER["task"],
                "avatar": (sess or {}).get("avatar", ""), "signed": bool(sess), "enforced": enforced, "acct_name": (sess or {}).get("name", "")}
    if role == "verified":
        return {"role": "verified", "name": human_name(sess), "id": sess["sid"], "em": sess.get("em", ""), "task": "user:" + sess["sid"], "avatar": sess.get("avatar", ""),
                "signed": True, "enforced": enforced, "acct_name": sess.get("name", "")}
    return {"role": role or "none", "name": (sess or {}).get("name", ""), "id": (sess or {}).get("sid", ""), "task": "", "avatar": (sess or {}).get("avatar", ""),
            "signed": bool(sess), "enforced": enforced, "acct_name": (sess or {}).get("name", "")}


def viewer_key(v: dict) -> str:
    return str(v.get("id") or v.get("task") or "")


async def api_alias(req: web.Request) -> web.Response:
    """POST {target, name, av}: the viewer's own name / picture for somebody, seen on her pages only; both empty - gone"""
    v = await need_viewer(req)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    if org and urlparse(org).netloc not in (host, req.headers.get("Host")):
        return web.json_response({"ok": False, "error": "refused: another site's page"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    key, target = viewer_key(v), str(q.get("target") or "").strip()[:64]
    if not key or not target:
        return web.json_response({"ok": False, "error": "whose name?"})
    mine = chat.aliases.setdefault(key, {})
    name = " ".join(str(q.get("name") or "").split())[:32]
    av = str(q.get("av") or "")
    if av and av not in chat.mine_up.get(v.get("task") or "", ()) and av != (mine.get(target) or {}).get("av"):
        return web.json_response({"ok": False, "error": "that picture is not one you uploaded"})
    if name or av:
        mine[target] = {**({"name": name} if name else {}), **({"av": av} if av else {})}
    else:
        mine.pop(target, None)
    chat.aliases_file.write_text(json.dumps(chat.aliases, ensure_ascii=False))
    print(f"[alias] {v.get('name')}: {target} -> {mine.get(target)}", flush=True)
    return web.json_response({"ok": True, "aliases": mine})


async def api_display_name(req: web.Request) -> web.Response:
    """the person's display name on the page (the owner, 2026-11-02, #1235); empty = back to the account's own name"""
    v = await need_viewer(req)
    # only the page of this very host: its Origin, or (a browser that sends none) Sec-Fetch-Site same-origin; the host as the
    # browser used it - through a proxy X-Forwarded-Host (the owner, #1252: Save did nothing behind a proxy)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    same = urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"
    print(f"[name] {v.get('task')}@{req.headers.get('CF-Connecting-IP') or req.remote}: origin={org} host={host} -> {'ok' if same else 'refused'}", flush=True)
    if not same:
        return web.json_response({"ok": False, "error": f"refused: the page's origin {org or '-'} is not {host}"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    cur = dict(chat.dnames.get(v["task"], {}))
    if "yt" in q and "name" not in q and "color" not in q and "mono" not in q:
        # a YouTube video shown on my card while I am deafened (the owner, 2026-11-03, #1450): its id from a link or as it is
        m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", str(q.get("yt") or "")) or re.fullmatch(r"\s*([\w-]{11})\s*", str(q.get("yt") or ""))
        if m:
            cur.update(yt=m.group(1), who=v["name"], acct=v.get("acct_name") or cur.get("acct", ""))
        elif str(q.get("yt") or "").strip():
            return web.json_response({"ok": False, "error": "not a YouTube link"})
        else:
            cur.pop("yt", None)
        if any(cur.get(k) for k in ("name", "color", "mono", "yt")):
            chat.dnames[v["task"]] = cur
        else:
            chat.dnames.pop(v["task"], None)
        chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
        await chat.watch_all({"type": "names", "names": chat.dnames})
        return web.json_response({"ok": True, "names": chat.dnames})
    if "mono" in q and "name" not in q and "color" not in q:
        # a person's monogram (the owner, 2026-11-02, #1275): up to 3 characters in one of its layouts, rotated;
        # on = shown instead of the account's picture
        m = q.get("mono") or {}
        t = unicodedata.normalize("NFC", str(m.get("t") or "")).strip()
        try:
            import regex as _re
            n = len(_re.findall(r"\X", t))
        except ImportError:
            n = len(t)
        if t and (n > 3 or len(t.encode()) > 48 or any(unicodedata.category(c) in ("Cc", "Cf") and c not in "\u200d\ufe0e\ufe0f" for c in t)):
            return web.json_response({"ok": False, "error": "up to three characters: letters, symbols or emoji"})
        try:
            lay, rot = int(m.get("l") or 0), int(m.get("r") or 0)
        except (TypeError, ValueError):
            lay, rot = 0, 0
        c = str(m.get("c") or "").lower()               # its own background (the owner #1634): '#rrggbb', '' = the name's colour
        if c and not re.fullmatch(r"#[0-9a-f]{6}", c):
            c = ""
        cur.update(mono={"t": t, "l": max(0, min(lay, 99)), "r": max(0, min(rot, 2)), "on": bool(m.get("on")), "c": c}, who=v["name"],
                   acct=v.get("acct_name") or cur.get("acct", ""))
        chat.dnames[v["task"]] = cur
        chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
        await chat.watch_all({"type": "names", "names": chat.dnames})
        print(f"[mono] {v['task']}: {cur['mono']}", flush=True)
        return web.json_response({"ok": True, "names": chat.dnames})
    if "color" in q and "name" not in q:
        # the colour of the person's name (the owner, 2026-11-02, #1242): one of Claude Code's eight, or none (made up)
        # the page's 96 (the owner #1643): a hue every 15 degrees in four tones - '' as before, v vivid, d deep, s soft
        def hue_tone(x):
            x = str(x or "")
            if x in CC_HUE:
                return CC_HUE[x], ""
            m = re.fullmatch(r"h(\d{1,3})([vds]?)", x)
            return (int(m.group(1)) % 360, m.group(2)) if m else (None, "")
        raw = str(q.get("color") or "").strip().lower()
        h, tone = hue_tone(raw)
        c = f"h{h}{tone}" if h is not None and not h % 15 else None
        if re.fullmatch(r"#[0-9a-f]{6}", raw):          # the monogram's palette and any colour (the owner #1646): as it is
            c = raw
            for t, d in chat.dnames.items():
                if t != v["task"] and (d.get("color") or "") == c:
                    return web.json_response({"ok": False, "error": f"taken by {d.get('name') or d.get('acct') or d.get('who') or t}"})
        elif c:
            # nobody else's colour (#1247): the same tone within 15 degrees of a claimed one is taken, another tone only the same hue
            for t, d in chat.dnames.items():
                oh, ot = hue_tone(d.get("color"))
                dh = None if oh is None else min(abs(h - oh), 360 - abs(h - oh))
                if t != v["task"] and dh is not None and (dh <= 15 if ot == tone else dh == 0):
                    return web.json_response({"ok": False, "error": f"taken by {d.get('name') or d.get('acct') or d.get('who') or t}"})
        if c:
            cur.update(color=c, who=v["name"], acct=v.get("acct_name") or cur.get("acct", ""))
        else:
            cur.pop("color", None)
        if cur.get("name") or cur.get("color") or cur.get("mono"):
            chat.dnames[v["task"]] = cur
        else:
            chat.dnames.pop(v["task"], None)
        chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
        await chat.watch_all({"type": "names", "names": chat.dnames})
        print(f"[colour] {v['task']}: {c or '(made up)'}", flush=True)
        return web.json_response({"ok": True, "names": chat.dnames})
    n = " ".join(str(q.get("name") or "").split())[:30]
    if any(ord(c) < 32 for c in n):
        return web.json_response({"ok": False, "error": "no control characters"})
    if n and (n.lower() in {a["name"].lower() for a in chat.agents.values()} | DEPUTY):
        return web.json_response({"ok": False, "error": "that is an agent's name"})
    if n and any(t != v["task"] and d["name"].lower() == n.lower() for t, d in chat.dnames.items()):
        return web.json_response({"ok": False, "error": "somebody else shows under that name"})
    if n:
        chat.dnames[v["task"]] = {**cur, "name": n, "acct": v.get("acct_name") or "", "who": v["name"]}
    elif cur.get("color") or cur.get("mono"):
        cur.pop("name", None)
        chat.dnames[v["task"]] = cur                        # the colour and the monogram stay
    else:
        chat.dnames.pop(v["task"], None)
    chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
    await chat.watch_all({"type": "names", "names": chat.dnames})
    print(f"[name] {v['task']}: {n or '(account name)'}", flush=True)
    return web.json_response({"ok": True, "names": chat.dnames})


_PR_CACHE: dict[str, tuple[float, dict]] = {}


async def api_pr(req: web.Request) -> web.Response:
    """a pull request named in a message (the owner, #1091): its card, through gh with the chat machine's GitHub sign-in -
    so only for the verified ones on the page; kept a minute"""
    await need_viewer(req)
    repo, n = req.query.get("r", ""), req.query.get("n", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo) or not n.isdigit() or len(n) > 8 or not GH:
        return web.json_response({"ok": False, "error": "bad_request" if GH else "no_gh"})
    key = f"{repo}#{int(n)}"
    hit = _PR_CACHE.get(key)
    if hit and time.time() - hit[0] < 60:
        return web.json_response(hit[1])
    try:
        pr = await asyncio.create_subprocess_exec(GH, "pr", "view", str(int(n)), "-R", repo, "--json", PR_FIELDS,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(pr.communicate(), 15)
    except (OSError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "gh_failed"})
    if pr.returncode != 0:
        res = {"ok": False, "error": "not_found"}
    else:
        try:
            res = {"ok": True, "pr": pr_summary(json.loads(out))}
        except ValueError:
            res = {"ok": False, "error": "gh_failed"}
    if len(_PR_CACHE) > 500:
        _PR_CACHE.clear()
    _PR_CACHE[key] = (time.time(), res)
    return web.json_response(res)


# ---- a pull request's changes inside the chat (the owner, #1192): the diff by files and hunks, a file whole (for the lines
# around a change and the whole-file view), the comments on lines - kept here, never sent to GitHub
_PRD_CACHE: dict[str, tuple[float, dict]] = {}
_PRF_CACHE: dict[str, str] = {}
PR_COMMENTS_FILE = DATA / "pr_comments.json"
try:
    PR_COMMENTS: dict[str, list[dict]] = json.loads(PR_COMMENTS_FILE.read_text())
except (OSError, ValueError):
    PR_COMMENTS = {}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _pr_ref(req: web.Request) -> tuple[str, int] | None:
    repo, n = req.query.get("r", ""), req.query.get("n", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo) or not n.isdigit() or len(n) > 8:
        return None
    return repo, int(n)


async def _gh(*args: str, timeout: float = 30, limit: int = 30_000_000) -> tuple[int, bytes]:
    try:
        pr = await asyncio.create_subprocess_exec(GH, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=2 ** 22)
        out, _ = await asyncio.wait_for(pr.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        return -1, b""
    return pr.returncode or 0, out[:limit]


def parse_diff(text: str) -> list[dict]:
    """a unified diff (gh pr diff) -> [{path, old, status, add, del, bin, hunks: [{os, ns, h, lines: [[t, text, old_no, new_no]]}]}]"""
    files, f, h, on, nn = [], None, None, 0, 0
    for ln in text.split("\n"):
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            f = {"path": m.group(2) if m else ln[11:], "old": m.group(1) if m else "", "status": "modified", "add": 0, "del": 0, "bin": False, "hunks": []}
            files.append(f)
            h = None
            continue
        if f is None:
            continue
        if h is None or not ln[:1] in (" ", "+", "-", "\\") or ln.startswith(("--- ", "+++ ")) and not h["lines"]:
            if ln.startswith("new file"):
                f["status"] = "added"
            elif ln.startswith("deleted file"):
                f["status"] = "deleted"
            elif ln.startswith("rename from "):
                f["status"], f["old"] = "renamed", ln[12:]
            elif ln.startswith("Binary files"):
                f["bin"] = True
            m = _HUNK.match(ln)
            if m:
                on, nn = int(m.group(1)), int(m.group(3))
                h = {"os": on, "ns": nn, "h": m.group(5)[:120], "lines": []}
                f["hunks"].append(h)
            continue
        t = ln[:1]
        if t == "\\":
            continue
        if t == " ":
            h["lines"].append([" ", ln[1:], on, nn]); on += 1; nn += 1
        elif t == "-":
            h["lines"].append(["-", ln[1:], on, 0]); on += 1; f["del"] += 1
        elif t == "+":
            h["lines"].append(["+", ln[1:], 0, nn]); nn += 1; f["add"] += 1
    for f in files:
        if f["hunks"] and f["hunks"][-1]["lines"] and f["hunks"][-1]["lines"][-1] == [" ", "", 0, 0]:
            f["hunks"][-1]["lines"].pop()
    return files


async def api_pr_diff(req: web.Request) -> web.Response:
    await need_viewer(req)
    ref = _pr_ref(req)
    if not ref or not GH:
        return web.json_response({"ok": False, "error": "bad_request" if GH else "no_gh"})
    repo, n = ref
    key = f"{repo}#{n}"
    hit = _PRD_CACHE.get(key)
    if hit and time.time() - hit[0] < 60:
        return web.json_response(hit[1])
    rc, meta = await _gh("pr", "view", str(n), "-R", repo, "--json", "title,url,headRefOid,baseRefOid,headRefName,baseRefName,state", timeout=20)
    if rc != 0:
        return web.json_response({"ok": False, "error": "not_found"})
    rc, out = await _gh("pr", "diff", str(n), "-R", repo, timeout=60)
    if rc != 0:
        return web.json_response({"ok": False, "error": "gh_failed"})
    m = json.loads(meta)
    files = parse_diff(out.decode("utf-8", "replace"))
    big = sum(len(h["lines"]) for f in files for h in f["hunks"]) > 60000
    res = {"ok": True, "repo": repo, "n": n, "title": m.get("title", ""), "url": m.get("url", ""), "state": m.get("state", ""),
           "head": m.get("headRefOid", ""), "base": m.get("baseRefOid", ""), "head_ref": m.get("headRefName", ""), "base_ref": m.get("baseRefName", ""),
           "files": files if not big else [dict(f, hunks=f["hunks"] if len(files) < 2 else [], cut=True) for f in files], "big": big}
    if len(_PRD_CACHE) > 100:
        _PRD_CACHE.clear()
    _PRD_CACHE[key] = (time.time(), res)
    return web.json_response(res)


async def api_pr_file(req: web.Request) -> web.Response:
    """a file of the pull request whole, at its head (or the base for a deleted one): only the PR's own commits"""
    await need_viewer(req)
    ref, path, sha = _pr_ref(req), req.query.get("path", ""), req.query.get("sha", "")
    if not ref or not GH or not re.fullmatch(r"[0-9a-f]{40}", sha) or not path or ".." in path.split("/") or len(path) > 400:
        return web.json_response({"ok": False, "error": "bad_request"})
    d = (_PRD_CACHE.get(f"{ref[0]}#{ref[1]}") or (0, {}))[1]
    if d and sha not in (d.get("head"), d.get("base")):
        return web.json_response({"ok": False, "error": "not this pull request's commit"})
    key = f"{ref[0]}@{sha}:{path}"
    if key not in _PRF_CACHE:
        rc, out = await _gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{ref[0]}/contents/{urllib.parse.quote(path)}?ref={sha}", timeout=30, limit=3_000_000)
        if rc != 0:
            return web.json_response({"ok": False, "error": "not_found"})
        if len(_PRF_CACHE) > 200:
            _PRF_CACHE.clear()
        _PRF_CACHE[key] = out.decode("utf-8", "replace")
    return web.json_response({"ok": True, "text": _PRF_CACHE[key]})


async def api_pr_comments(req: web.Request) -> web.Response:
    """the comments on a pull request's lines: GET the list; POST {path, line, side, text, code} adds one, {del: id} takes yours away"""
    v = await need_viewer(req)
    ref = _pr_ref(req)
    if not ref:
        return web.json_response({"ok": False, "error": "bad_request"})
    key = f"{ref[0]}#{ref[1]}"
    lst = PR_COMMENTS.setdefault(key, [])
    if req.method == "POST":
        org = req.headers.get("Origin")
        if org and urlparse(org).netloc not in (req.headers.get("X-Forwarded-Host"), req.headers.get("Host")):
            return web.json_response({"ok": False, "error": "refused: another site's page"}, status=403)
        try:
            q = await req.json()
        except ValueError:
            q = {}
        if q.get("del"):
            c = next((c for c in lst if c["id"] == q["del"]), None)
            if not c or (c["task"] != v["task"] and v["role"] != "owner"):
                return web.json_response({"ok": False, "error": "not yours"})
            lst.remove(c)
        else:
            text, path = str(q.get("text") or "").strip()[:4000], str(q.get("path") or "")[:400]
            line, side = q.get("line"), q.get("side")
            if not text or not path or not isinstance(line, int) or line < 1 or side not in ("old", "new"):
                return web.json_response({"ok": False, "error": "bad_request"})
            lst.append({"id": secrets.token_hex(5), "path": path, "line": line, "side": side, "text": text, "code": str(q.get("code") or "")[:300],
                        "by": v["name"], "task": v["task"], "ts": round(time.time(), 1)})
        if not lst:
            PR_COMMENTS.pop(key, None)
        PR_COMMENTS_FILE.write_text(json.dumps(PR_COMMENTS, ensure_ascii=False))
        print(f"[prc] {v['name']}: {key} {'-' if q.get('del') else '+'}", flush=True)
        await chat.watch_all({"type": "prc", "pr": key, "comments": PR_COMMENTS.get(key, [])})
    return web.json_response({"ok": True, "comments": PR_COMMENTS.get(key, [])})


async def api_chats(req: web.Request) -> web.Response:
    """the chats on this Mac (chats.py, the owner, 2026-11-07): GET the list for the sidebar; POST {op: create|stop|start|delete,
    title, slug} - the owner only, from this host's own page"""
    v = await need_viewer(req)
    if v["role"] != "owner":
        return web.json_response({"ok": False, "error": "the owner's only"}, status=403)
    here = req.headers.get("X-Forwarded-Host") or req.headers.get("Host", "")
    here = _chat_url(here if here.startswith(("127.", "localhost")) else here.split(":")[0])
    args, tmp = ["list"], None
    if req.method == "POST":
        org = req.headers.get("Origin")
        if org and urlparse(org).netloc not in (req.headers.get("X-Forwarded-Host"), req.headers.get("Host")):
            return web.json_response({"ok": False, "error": "refused: another site's page"}, status=403)
        try:
            q = await req.json()
        except ValueError:
            q = {}
        op, slug = str(q.get("op") or ""), str(q.get("slug") or "")[:60]
        tmp = None
        if op == "create":
            args = ["create", str(q.get("title") or "")[:60], slug]
        elif op == "icon" and slug:                      # the round icon on the rail (the owner, #1349): monogram, colour, picture
            args = ["icon", slug]
            if "text" in q or "color" in q:              # the icon's editor sends both; the background alone leaves them as they are
                args += ["--text", str(q.get("text") or "")[:8], "--color", str(q.get("color") or "")[:7]]
            if "bg" in q:                                # the chat page's background (the owner, #1919)
                args += ["--bg", str(q.get("bg") or "")[:7]]
            m = re.match(r"data:image/(png|jpeg|webp|gif);base64,(.+)$", str(q.get("img") or ""), re.S)
            if m:
                import base64
                import tempfile
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix="." + m.group(1))
                tmp.write(base64.b64decode(m.group(2)[:2_000_000]))
                tmp.close()
                args += ["--img", tmp.name]
            elif q.get("noimg"):
                args.append("--noimg")
        elif op in ("stop", "start", "delete") and slug:
            args = [op, slug]
            # this very chat stopped or deleted from its own page (the owner, #2264): its service going down would take the
            # request and chats.py with it, half done ("SyntaxError" on the page) - so chats.py runs on its own, the page is told at once
            try:
                import lagomcfg
                ch = lagomcfg.load()["chats"].get(slug) or {}
                mine = op != "start" and lagomcfg.chat_url(ch) == here
            except Exception:                            # noqa: BLE001 - no config: the old way
                mine = False
            if mine:
                print(f"[chats] {v.get('name')}: {op} {slug} (this chat itself: on its own)", flush=True)
                import subprocess
                with open(lagomcfg.CFG_DIR / "chats-op.log", "a") as lf:
                    subprocess.Popen([sys.executable, str(Path(__file__).with_name("chats.py")), op, slug, "--json"],
                                     stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
                return web.json_response({"ok": True, "self": True})
        else:
            return web.json_response({"ok": False, "error": "bad_request"})
        print(f"[chats] {v.get('name')}: {' '.join(args)}", flush=True)
    p = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("chats.py")), *args, "--json",
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), 180)
    except asyncio.TimeoutError:
        p.kill()
        return web.json_response({"ok": False, "error": "timed out"})
    finally:
        if req.method == "POST" and tmp:
            Path(tmp.name).unlink(missing_ok=True)
    try:
        r = json.loads(out.decode() or "{}")
    except ValueError:
        r = {"ok": False, "error": (err.decode() or out.decode())[-400:]}
    if r.get("ok") and args[0] == "list":
        for x in r["result"]:
            x["here"] = x["url"] == here
    return web.json_response(r)


# ---------------------------------------------------------------- Web Push (the owner, 2026-11-07, #1445)
# a person turns it on in the settings (Chat: Notifications - off / to me / everything); the page subscribes with the browser's
# push service and gives the server its subscription; a new message goes to every subscription whose page is not open and looked
# at. The VAPID key is the chat's own (<conf>/vapid.pem, made on first use); the subscriptions are in <data>/push.json
class Push:
    def __init__(self) -> None:
        self.file = DATA / "push.json"
        try:
            self.subs: list = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.subs = []
        self._vapid = None

    def save(self) -> None:
        self.file.write_text(json.dumps(self.subs, ensure_ascii=False))

    def vapid(self):
        if self._vapid is None:
            from py_vapid import Vapid01
            pem = auth.CONF / "vapid.pem"
            if not pem.exists():
                v = Vapid01()
                v.generate_keys()
                v.save_key(str(pem))
                pem.chmod(0o600)
            self._vapid = Vapid01.from_file(str(pem))
        return self._vapid

    def public_key(self) -> str:
        import base64
        from cryptography.hazmat.primitives import serialization
        raw = self.vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")


PUSH = Push()


def _push_wants(s: dict, e: dict) -> bool:
    """is this message for this subscription: everything, or addressed to its person (to, a reply to them, @all, an @mention)"""
    if e.get("task") == s.get("task") or s.get("mode") not in ("mine", "all"):
        return False
    if s["mode"] == "all":
        return True
    names = [str(n) for n in (s.get("names") or []) if n]
    to = set(e.get("to") or [])
    if e.get("all") or any(n in to for n in names) or (s.get("task") == "user" and ("owner" in to or USER["name"] in to)):
        return True
    rep = e.get("reply") or {}
    if rep and (rep.get("task") == s.get("task") or rep.get("agent") in names):
        return True
    text = e.get("text") or ""
    return any(re.search(r"(?<![\w@])@" + re.escape(n) + r"(?![\w.-])", text, re.I) for n in names)


async def push_out(chat, e: dict) -> None:
    looked = {(m or {}).get("pushep") for ws, m in chat.viewers.items() if (m or {}).get("vis") and ws in chat.watchers}
    todo = [s for s in PUSH.subs if s.get("sub", {}).get("endpoint") not in looked and _push_wants(s, e)]
    if not todo:
        return
    who = e.get("agent") or ""
    body = re.sub(r"\s+", " ", e.get("text") or "").strip()
    gone = []

    def one(s: dict) -> None:
        from pywebpush import WebPushException, webpush
        data = {"title": f"{chat.title}: {who}", "body": body[:180] + ("…" if len(body) > 180 else ""),
                "url": f"{s.get('url') or ''}#m={e['id']}", "tag": f"chat-{e['id']}"}
        try:
            host = (s.get("url") or "").split("/")[2] if "//" in (s.get("url") or "") else settings()["domain"]
            webpush(subscription_info=s["sub"], data=json.dumps(data, ensure_ascii=False), vapid_private_key=PUSH.vapid(),
                    vapid_claims={"sub": f"https://{host}"}, ttl=3600, timeout=10)
        except WebPushException as x:
            code = getattr(getattr(x, "response", None), "status_code", 0)
            if code in (404, 410):
                gone.append(s["sub"].get("endpoint"))
            print(f"[push] {code} {str(x)[:120]}", flush=True)
        except Exception as x:                                # a push that fails is only a missed notice
            print(f"[push] {x!r}"[:200], flush=True)
    await asyncio.gather(*(asyncio.to_thread(one, s) for s in todo))
    if gone:
        PUSH.subs = [s for s in PUSH.subs if s.get("sub", {}).get("endpoint") not in gone]
        PUSH.save()


async def api_push_key(req: web.Request) -> web.Response:
    await need_viewer(req)
    return web.json_response({"key": await asyncio.to_thread(PUSH.public_key)})


async def api_push_sub(req: web.Request) -> web.Response:
    """POST {sub: PushSubscription.toJSON(), mode: off|mine|all, names: [...], url}: this browser's notifications on, changed or off"""
    v = await need_viewer(req)
    try:
        q = await req.json()
    except ValueError:
        raise web.HTTPBadRequest()
    sub, mode = q.get("sub") or {}, str(q.get("mode") or "off")
    ep = str(sub.get("endpoint") or "")
    if not ep.startswith("https://") or len(ep) > 600:
        return web.json_response({"ok": False, "error": "no subscription"})
    PUSH.subs = [s for s in PUSH.subs if s.get("sub", {}).get("endpoint") != ep]
    if mode in ("mine", "all"):
        keys = sub.get("keys") or {}
        PUSH.subs.append({"sub": {"endpoint": ep, "keys": {"p256dh": str(keys.get("p256dh") or ""), "auth": str(keys.get("auth") or "")}},
                          "mode": mode, "task": v["task"], "names": [str(n)[:60] for n in (q.get("names") or [])][:4] + [v["name"]],
                          "url": str(q.get("url") or "")[:200], "ts": round(time.time())})
    PUSH.save()
    print(f"[push] {v['name']} {mode}", flush=True)
    return web.json_response({"ok": True})


async def push_static(req: web.Request) -> web.StreamResponse:
    """the service worker and the app's icons: what a phone needs to make the chat an app with notifications"""
    n = req.match_info["f"]
    f = HERE / "sw.js" if n == "sw.js" else HERE / "appicons" / n
    if not f.is_file():
        raise web.HTTPNotFound()
    h = {"Cache-Control": "no-cache"} if n == "sw.js" else {"Cache-Control": "max-age=86400"}
    return web.FileResponse(f, headers=h)


async def manifest(req: web.Request) -> web.Response:
    return web.json_response({"name": chat.title, "short_name": chat.title[:20], "start_url": "./", "scope": "./", "display": "standalone",
                              "background_color": "#1e1f22", "theme_color": "#1e1f22",
                              "icons": [{"src": "appicon-192.png", "sizes": "192x192", "type": "image/png"},
                                        {"src": "appicon-512.png", "sizes": "512x512", "type": "image/png"}]},
                             content_type="application/manifest+json")


CHAT_ICONS = Path.home() / ".config/agentchat-chats"


async def api_chaticons(req: web.Request) -> web.Response:
    """the chats' icons for everybody's rail (the owner, #1349): {"https://<host>": {t, c, img}} - only how they look"""
    await need_viewer(req)
    try:
        reg = json.loads((CHAT_ICONS / "chats.json").read_text())
    except (OSError, ValueError):
        reg = {}
    return web.json_response({_chat_url(c["host"]): {**(c.get("icon") or {}), "title": c.get("title", "")}
                              for c in reg.values() if c.get("host")})


def _may_see_chat(c: dict, v: dict) -> bool:
    """may this viewer see how much is new in that chat: its owner, or somebody on its allow-list (#1445)"""
    em = (v.get("em") or "").lower()
    conf = Path(c.get("conf") or "")
    try:
        if em and (conf / "owner_email").read_text().strip().lower() == em:
            return True
    except OSError:
        pass
    if v["role"] == "owner" and (c.get("host") == HOST_SELF.get("host") or
                                 # the chats on this machine only (no sign-in, no email): the owner here is their owner too
                                 str(c.get("host") or "").startswith("127.") and str(HOST_SELF.get("host") or "").startswith("127.")):
        return True
    try:
        a = json.loads((conf / "allow.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(em) and (em in [str(x).lower() for x in a.get("google_emails", [])] or
                         em.rsplit("@", 1)[-1] in [str(x).lower() for x in a.get("google_domains", [])])


def _new_in(path: Path, seen: int, me_task: str) -> dict:
    """the last message id of a chat's log and how many messages came after `seen`, not one's own; only its tail is read"""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 600_000))
            lines = f.read().decode("utf8", "replace").splitlines()[1:]
    except OSError:
        return {"last": 0, "n": 0}
    last, n = 0, 0
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        i = int(e.get("id") or 0)
        if e.get("kind") == "msg":                           # only people's and agents' messages: no system lines, no facts, no reminders (the owner, #1721)
            last = max(last, i)
            if i > seen and e.get("task") != me_task:
                n += 1
    return {"last": last, "n": n}


HOST_SELF: dict = {}


SEEN_FILE = Path(os.environ.get("LAGOMCHAT_SHARED_SEEN", Path.home() / ".config/agentchat-chats/seen.json"))


def _seen_load() -> dict:
    try:
        d = json.loads(SEEN_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


async def api_agent_color(req: web.Request) -> web.Response:
    """the owner sets the colour of an agent's name (#1900): {name, color: "#rrggbb" | ""}; empty = the agent's own again"""
    v = await need_viewer(req)
    if v.get("role") != "owner":
        return web.json_response({"ok": False, "error": "only the owner"}, status=403)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    same = urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"
    if not same:
        return web.json_response({"ok": False, "error": "refused: another site"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    name = str(q.get("name") or "").strip()
    c = str(q.get("color") or "").strip().lower()
    if not NAME.fullmatch(name):
        return web.json_response({"ok": False, "error": "bad agent name"})
    if c and not re.fullmatch(r"#[0-9a-f]{6}", c):
        return web.json_response({"ok": False, "error": "a colour is #rrggbb"})
    if c:
        chat.agent_cols[name] = c
    else:
        chat.agent_cols.pop(name, None)
    chat.agent_cols_file.write_text(json.dumps(chat.agent_cols, ensure_ascii=False))
    await chat.watch_all({"type": "agentcols", "cols": chat.agent_cols})
    print(f"[agent colour] {name}: {c or '(its own)'}", flush=True)
    return web.json_response({"ok": True, "cols": chat.agent_cols})


def _chat_url(host: str) -> str:
    """a chat's address as its page knows it: a local chat (127.0.0.1:<port>) is plain http, one behind a domain https"""
    return f"http://{host}" if host.startswith(("127.", "localhost")) else f"https://{host}"


async def api_seen(req: web.Request) -> web.Response:
    """the last message id read in this chat, by the person's email, for the other chats' circles (the owner, #1860)"""
    v = await viewer_of(req)
    em = str(v.get("em") or "").lower()
    if not em:
        return web.json_response({"ok": False}, status=401)
    try:
        q = await req.json()
        i = int(q.get("id") or 0)
    except (ValueError, TypeError, AttributeError):
        return web.json_response({"ok": False}, status=400)
    host = req.headers.get("X-Forwarded-Host") or req.host
    url = _chat_url(host if host.startswith(("127.", "localhost")) else host.split(":")[0])
    d = _seen_load()
    cur = d.get(em) if isinstance(d.get(em), dict) else {}
    if i > int(cur.get(url) or 0):
        cur[url] = i
        d[em] = cur
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEN_FILE.with_name(f"{SEEN_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(SEEN_FILE)
    return web.json_response({"ok": True})


async def api_chatnew(req: web.Request) -> web.Response:
    """how much is new in the other chats on this Mac, for the circles on the rail (the owner, #1445): ?s={"https://<host>": last seen id}"""
    v = await need_viewer(req)
    HOST_SELF.setdefault("host", req.host.split(":")[0])
    try:
        seen = json.loads(req.query.get("s") or "{}")
        seen = seen if isinstance(seen, dict) else {}
    except ValueError:
        seen = {}
    try:
        reg = json.loads((CHAT_ICONS / "chats.json").read_text())
    except (OSError, ValueError):
        reg = {}
    # what was read is kept on this machine too, by the email (the owner, #1860): the cookie is per domain, so a chat on lagom.game
    # never learnt what was read on another domain of the chats (each domain kept it for itself only) - the circles showed the whole history
    mine = _seen_load().get(str(v.get("em") or "").lower(), {}) if v.get("em") else {}
    out = {}
    for c in reg.values():
        if not c.get("host") or not c.get("data") or not _may_see_chat(c, v):
            continue
        url = _chat_url(c["host"])
        try:
            s = int(seen.get(url) or 0)
        except (TypeError, ValueError):
            s = 0
        s = max(s, int(mine.get(url) or 0))
        out[url] = await asyncio.to_thread(_new_in, Path(c["data"]) / "chat.jsonl", s, v.get("task") or "")
    return web.json_response(out)


async def chaticon_file(req: web.Request) -> web.StreamResponse:
    f = CHAT_ICONS / "icons" / req.match_info["f"]
    if not f.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Cache-Control": "max-age=31536000, immutable"})


async def need_viewer(req: web.Request) -> dict:
    v = await viewer_of(req)
    if v["role"] not in ("owner", "verified"):
        raise web.HTTPUnauthorized(text=json.dumps({"error": "signin_required" if not v["signed"] else "not_verified"}), content_type="application/json")
    return v


async def oauth_login(req: web.Request):
    """/auth/google/login: to Google (the owner, 2026-11-03, #1497)"""
    prov = req.match_info["prov"]
    if prov not in auth.oauth_conf():
        raise web.HTTPNotFound(text=f"{prov} sign-in is not set up")
    scheme, host, prefix = origin_of(req)
    nxt = req.query.get("next", prefix + "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = prefix + "/"
    raise web.HTTPFound(auth.oauth_url(prov, f"{scheme}://{host}{prefix}/auth/{prov}/callback", auth.make_state(nxt)))


def cf_token(req: web.Request) -> str | None:
    return req.headers.get("Cf-Access-Jwt-Assertion") or req.cookies.get("CF_Authorization")


async def cf_login(req: web.Request):
    """/auth/cf/login: in as the email Cloudflare Access proved (the owner, #1611)"""
    scheme, host, prefix = origin_of(req)
    nxt = req.query.get("next", prefix + "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = prefix + "/"
    em = await asyncio.to_thread(auth.cf_access_email, cf_token(req))
    if not em:
        print(f"[auth] cloudflare access sign-in refused: {auth.last_error}", flush=True)
        return web.Response(status=403, text="No email confirmed by Cloudflare Access came with this page. Open the chat through its address (behind Cloudflare Access) and try again.", content_type="text/plain")
    who = {"sid": auth.email_sid(em), "name": em.split("@")[0], "em": em}
    await asyncio.to_thread(chat.rdb.user_touch, who["sid"], who["name"], "")
    _ROLE_CACHE.pop(who["sid"], None)
    print(f"[auth] cloudflare access: {em} {who['sid']} allowed={auth.allowed(who)}", flush=True)
    r = web.HTTPFound(f"{scheme}://{host}{nxt}")
    r.set_cookie(auth.COOKIE, auth.make_token(who), max_age=auth.SESSION_DAYS * 86400,
                 path=prefix or "/", httponly=True, secure=scheme == "https", samesite="Lax")
    raise r


async def oauth_callback(req: web.Request):
    prov = req.match_info["prov"]
    scheme, host, prefix = origin_of(req)
    nxt = auth.read_state(req.query.get("state"))
    if prov not in auth.oauth_conf() or not nxt or not req.query.get("code"):
        return web.Response(status=403, text="The sign-in was not confirmed (an old or forged link). Go back and try again.", content_type="text/plain")
    who = await asyncio.to_thread(auth.oauth_identity, prov, req.query["code"], f"{scheme}://{host}{prefix}/auth/{prov}/callback")
    if not who:
        print(f"[auth] {prov} sign-in refused: {auth.last_error}", flush=True)
        return web.Response(status=403, text=f"The {prov.title()} sign-in was not confirmed. Go back and try again.", content_type="text/plain")
    await asyncio.to_thread(chat.rdb.user_touch, who["sid"], who["name"], who.get("avatar", ""))
    _ROLE_CACHE.pop(who["sid"], None)
    print(f"[auth] {prov}: {who['name']} {who['sid']} {who.get('em', '')} allowed={auth.allowed(who)}", flush=True)
    r = web.HTTPFound(f"{scheme}://{host}{nxt}")
    r.set_cookie(auth.COOKIE, auth.make_token({k: who[k] for k in ("sid", "name", "avatar", "em") if who.get(k)}), max_age=auth.SESSION_DAYS * 86400,
                 path=prefix or "/", httponly=True, secure=scheme == "https", samesite="Lax")
    raise r


async def auth_logout(req: web.Request):
    scheme, host, prefix = origin_of(req)
    # by email the session is Access's: out there, or the next page signs in again by itself
    r = web.HTTPFound(f"{scheme}://{host}/cdn-cgi/access/logout" if auth.owner_email() else f"{scheme}://{host}{prefix}/")
    r.del_cookie(auth.COOKIE, path=prefix or "/")
    raise r


async def api_me(req: web.Request):
    v = await viewer_of(req)
    return web.json_response({k: v[k] for k in ("role", "name", "id", "avatar", "signed", "enforced", "acct_name")},
                             headers={"Cache-Control": "no-store"})


async def ws_watch(req: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    me = await viewer_of(req)
    if me["role"] not in ("owner", "verified"):
        # not let in: no chat at all; a signed-in account may type, and every message gets a refusal
        st = "banned" if me["role"] == "banned" else "pending" if me["signed"] else "signin"
        cfa = await asyncio.to_thread(auth.cf_access_email, cf_token(req)) if st == "signin" else None
        await ws.send_str(json.dumps({"type": "gate", "state": st, "providers": [] if auth.owner_email() else sorted(auth.oauth_conf()), "cfa": "" if auth.owner_email() else cfa or "", "name": me["acct_name"] or me["name"],
                                      "id": me["id"], "avatar": me["avatar"],
                                      **({"strikes": chat.strikes.get(me["id"], [])} if st == "banned" else {})}, ensure_ascii=False))
        if not me["signed"] or st == "banned":
            await ws.close()
            return ws
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                t = str(json.loads(m.data).get("say") or "").strip()[:2000]
            except (ValueError, TypeError, AttributeError):
                continue
            if t:
                await ws.send_str(json.dumps({"type": "refused", "text": t, "ts": round(time.time(), 3),
                                              "reason": "Refused: your account is not verified yet. The owner verifies accounts by hand."}, ensure_ascii=False))
        return ws
    me["since"] = chat.here_since(me)
    me["aux"] = req.query.get("aux") == "1"
    chat.viewers[ws] = me                      # counted from the first frame: the init's presence has this page in it
    # video (the owner, 2026-11-03, #1388): every page has a peer id; a person's page that shares its camera is reached by it,
    # the server only relays the WebRTC signalling between two pages (offer / answer / ICE), the video goes page to page
    me["peer"] = secrets.token_hex(6)
    chat.peers[me["peer"]] = ws
    n = max(1, min(int(req.query.get("n", "500")), 5000))
    start = chat.start_for(me)
    evs = [v for v in (chat.view_for(e, start, me["role"]) for e in chat.events[-n:]) if v is not None]
    lo = max(start, evs[0]["id"] if evs else start)
    await ws.send_str(json.dumps({"type": "init", "peer": me["peer"], "sps": {str(k): {**{x: v[x] for x in ("uri", "pos", "paused")}, "age": round(time.time() - v["at"], 2)} for k, v in chat.sps.items() if time.time() - v["at"] < 6 * 3600}, "me": {k: me[k] for k in ("role", "name", "id", "task", "avatar", "signed", "enforced", "acct_name")},
                                  "presence": chat.presence(me["role"] == "owner", me),
                                  "agents": chat.roster(), "known": chat.known_agents(), "allow_all": chat.allow_all, "sound_presets": SOUND_PRESETS, "rimgs": _rimgs_out(), "photo": chat.photos.get(me.get("task") or ""), "loops": {str(i): v for i, v in chat.loops.items() if i >= lo}, "events": evs, "v": page_version(), "title": chat.title, "title_tr": chat.title_tr, "aliases": chat.aliases.get(viewer_key(me), {}), "names": chat.dnames, "agent_cols": chat.agent_cols,
                                  "acks": {str(i): v for i, v in chat.acks.items() if i >= lo},
                                  "trans": {str(i): v for i, v in chat.trans.items() if i >= start},
                                  "pins": chat.pins_view(0 if me["role"] == "owner" else start),
                                  "tally": {str(i): t for i in chat.votes if i >= start and (t := chat.tally(i))},
                                  "polls": {str(i): chat.poll_view(i, me.get("name") or "owner") for i in chat.polls if i >= start},
                                  "reacts": {str(i): r for i, r in chat.reacts.items() if i >= start},   # all they may see, not only of the n newest: older messages come later with no reactions of their own (the owner, #1033)
                                  "votes": {str(i): m[str(chat.rater_of(me)["id"])][1] for i, m in chat.votes.items() if i >= start and str(chat.rater_of(me)["id"]) in m}, "scores": chat.scores(),
                                  "first": max(start, chat.events[0]["id"]) if chat.events else 0}, ensure_ascii=False))
    chat.watchers.add(ws)
    if not me["aux"]:
        await chat.person_came(me)
    me["ip"], me["city"] = where_from(req)
    access_log(req, "page", name=me.get("name") or "", task=me.get("task") or "", role=me.get("role") or "", account=me.get("id") or "",
               email=me.get("em") or "")
    me["via"] = "cf" if req.headers.get("Cf-Ray") else "direct"
    lang = req.query.get("lang") or ""                   # the language picked on this page (the owner, #2132)
    me["lang"] = lang if lang in ("en", "ru", "uk", "be", "cs", "de", "es", "sv") else ""
    tz = req.query.get("tz") or ""                       # the page's time zone (IANA name), shown only if the person turns it on (#2222)
    me["tz"] = tz if re.fullmatch(r"[A-Za-z]+(?:/[A-Za-z0-9_+\-]+){0,2}|UTC", tz) else ""
    # Cloudflare's own lookup, no outside service gets the address; XX = unknown, T1 = Tor (two letters that name no country)
    cc = (req.headers.get("CF-IPCountry") or "").upper() if me["via"] == "cf" else ""
    me["cc"] = cc if re.fullmatch(r"[A-Z]{2}", cc) and cc not in ("XX", "T1") else ""
    print(f"[watch] + {me['role']}/{me.get('name') or '-'}@{me['ip']} via {me['via']} ({len(chat.watchers)} pages)", flush=True)
    await chat.send_presence()
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                q = json.loads(m.data)
                text = str(q.get("say") or "").strip()[:8000]
                to = [str(n) for n in q.get("to") or []]
                vote_id, vote_d, del_id, reply_id = q.get("vote"), q.get("d"), q.get("del"), q.get("reply")
            except (ValueError, TypeError, AttributeError):
                continue
            # only the page of this very host may speak (not another site's page opened in the user's browser)
            org = req.headers.get("Origin")
            if org and urlparse(org).netloc != req.headers.get("Host"):
                await send(ws, json.dumps({"type": "error", "error": "wrong origin"}))
                continue
            if q.get("title_set") is not None:                   # a double click on the name of the chat: the owner's only (#631)
                t = " ".join(str(q["title_set"]).split())[:60]
                if me["role"] != "owner":
                    await send(ws, json.dumps({"type": "error", "error": "only the owner renames the chat from the page"}))
                elif t:
                    await chat.set_title(t, {}, me["name"])
                continue
            if q.get("cam") is not None:                         # this page shares its camera now, or stops (#1388)
                me["cam"] = bool(q["cam"])
                me["mirror"] = bool(q.get("mirror"))             # the others show the picture mirrored too (#1433)
                me["away"] = bool(q.get("away"))                 # deafened: the card shows the person's YouTube placeholder (#1450)
                print(f"[cam] {me.get('name')}@{me.get('ip')}: {'on' if me['cam'] else 'off'}", flush=True)
                await chat.send_presence()
                continue
            if q.get("scr") is not None:                         # this page shares its screen now, or stops (#2188)
                me["scr"] = bool(q["scr"])
                print(f"[scr] {me.get('name')}@{me.get('ip')}: {'on' if me['scr'] else 'off'}", flush=True)
                await chat.send_presence()
                continue
            if q.get("away_set") is not None:                    # Away by hand, for the person on all their devices (#2065)
                k = me["id"] or me["name"]
                if bool(q["away_set"]) != (k in chat.away_manual):
                    if q["away_set"]:
                        chat.away_manual[k] = round(time.time(), 1)
                    else:
                        chat.away_manual.pop(k, None)
                    chat.away_file.write_text(json.dumps(chat.away_manual, ensure_ascii=False))
                    await chat.send_presence()
                continue
            if q.get("nocc_set") is not None:                    # "Don't show my country" in the profile (#2112)
                k = me["id"] or me["name"]
                if bool(q["nocc_set"]) != (k in chat.nocc):
                    (chat.nocc.add if q["nocc_set"] else chat.nocc.discard)(k)
                    chat.nocc_file.write_text(json.dumps(sorted(chat.nocc), ensure_ascii=False))
                    await chat.send_presence()
                continue
            if q.get("showlang_set") is not None or q.get("showtz_set") is not None:   # "Show my language / time zone" (#2222)
                k = me["id"] or me["name"]
                for f, st, fl in (("showlang_set", chat.showlang, chat.showlang_file), ("showtz_set", chat.showtz, chat.showtz_file)):
                    if q.get(f) is not None and bool(q[f]) != (k in st):
                        (st.add if q[f] else st.discard)(k)
                        fl.write_text(json.dumps(sorted(st), ensure_ascii=False))
                await chat.send_presence()
                continue
            if q.get("pushvis") is not None:                     # this page's push endpoint and whether it is looked at: no push to it then (#1445)
                me["pushep"], me["vis"] = str(q.get("pushvis") or "")[:600], bool(q.get("v"))
                continue
            if q.get("st") is not None:                          # this tab's status (#1671): "" here, "auto" idle
                st = q["st"] if q["st"] == "auto" else ""
                if st != (me.get("st") or ""):
                    me["st"], me["st_at"] = st, round(time.time(), 1)
                    chat.idle_changed(me)
                    if st == "" and me.get("task") in chat.away_gone:
                        await chat.idle_back(me)
                    await chat.send_presence()
                continue
            if q.get("pvote") is not None:                       # a person's pick in a poll (toggles; the owner, #1386)
                if me["role"] in ("owner", "verified"):
                    try:
                        await chat.poll_vote(int(q["pvote"]), me["name"], int(q.get("o")), start)
                    except (TypeError, ValueError):
                        pass
                continue
            if q.get("react") is not None:                       # a person's emoji on a message (toggles)
                try:
                    await chat.react(int(q["react"]), str(q.get("e") or ""), me["name"], chat.rater_of(me), start)
                except (TypeError, ValueError):
                    pass
                continue
            if q.get("spj") is not None:                         # this page joined (a message id) or left (0) a listening
                try:
                    me["spj"] = int(q["spj"]) or None
                    me["spv"] = str(q.get("vid") or "")[:32] if me["spj"] else ""
                except (TypeError, ValueError):
                    continue
                await chat.send_presence()
                continue
            if q.get("loop") is not None:                        # the loop button on a video's player: for everybody (#1790)
                try:
                    mid = int(q["loop"])
                except (TypeError, ValueError):
                    continue
                if any(x["id"] == mid and x["kind"] == "msg" for x in chat.events[-5000:]) and mid >= start:
                    chat.loops[mid] = bool(q.get("on"))
                    chat.loops_file.write_text(json.dumps(chat.loops))
                    await chat.watch_all({"type": "loop", "mid": mid, "on": chat.loops[mid]})
                continue
            if q.get("spsync") is not None:                      # Spotify, listen together (the owner, #1599/#1600): the poster's page leads
                y = q["spsync"] if isinstance(q["spsync"], dict) else {}
                try:
                    mid = int(y.get("mid") or 0)
                    ev = next((x for x in chat.events if x["id"] == mid), None)
                    if not ev or ev.get("task") != me["task"]:
                        continue                                 # only the one who posted the link leads its listening
                    st = {"uri": str(y.get("uri") or "")[:80], "pos": max(0, int(y.get("pos") or 0)), "paused": bool(y.get("paused")), "at": time.time()}
                except (TypeError, ValueError):
                    continue
                chat.sps[mid] = st
                print(f"[spsync] #{mid} {me.get('name')} pos={st['pos']} paused={st['paused']}", flush=True)
                m = json.dumps({"type": "spsync", "mid": mid, **{k: st[k] for k in ("uri", "pos", "paused")}, "age": 0}, ensure_ascii=False)
                for w in list(chat.watchers):
                    if w is not ws:
                        await send(w, m)
                continue
            if q.get("ytsync") is not None:                      # the deafened person moved their placeholder video (#1463)
                y = q["ytsync"] if isinstance(q["ytsync"], dict) else {}
                try:
                    me["yts"] = {"t": max(0.0, float(y.get("t") or 0)), "st": int(y.get("st") or 0), "at": time.time()}
                except (TypeError, ValueError):
                    continue
                m = json.dumps({"type": "ytsync", "who": me["name"], "t": me["yts"]["t"], "st": me["yts"]["st"], "age": 0}, ensure_ascii=False)
                for w in list(chat.watchers):
                    if w is not ws:
                        await send(w, m)
                continue
            if q.get("rtc") is not None:                         # WebRTC signalling to another page, by its peer id
                r = q["rtc"] if isinstance(q["rtc"], dict) else {}
                to = chat.peers.get(str(r.get("to") or ""))
                if to is not None and to is not ws:
                    await send(to, json.dumps({"type": "rtc", "from": me["peer"], "data": r.get("data")}, ensure_ascii=False))
                continue
            if q.get("agent_ctl") is not None:                   # the owner stops an agent's listening or sends it out, from its card (#2124)
                act, a = str(q["agent_ctl"]), chat.find(str(q.get("task") or ""))
                print(f"[agent_ctl] asked: {act} task={str(q.get('task'))[:12]} by {me['role']}/{me.get('name')} found={bool(a)}", flush=True)
                if not a:
                    await send(ws, json.dumps({"type": "error", "error": "that agent is not on the roster any more"}))
                elif me["role"] != "owner":
                    await send(ws, json.dumps({"type": "error", "error": "only the owner stops or sends out an agent"}))
                elif a and act in ("stop", "leave"):
                    await chat.agent_ctl(a, act)
                continue
            if q.get("pin") is not None:                         # the owner pins / unpins a message (#1116)
                if me["role"] != "owner":
                    await send(ws, json.dumps({"type": "error", "error": "only the owner pins"}))
                else:
                    bad = await chat.set_pin(int(q["pin"]), bool(q.get("on")), "owner")
                    if bad:
                        await send(ws, json.dumps({"type": "error", "error": bad}))
                continue
            if q.get("typing"):                                  # a person is typing: the others' pages show it for a few seconds
                await chat.typing(ws, me)
                continue
            if vote_id is not None or del_id is not None:
                if vote_id is not None:                          # everybody let in rates
                    await chat.vote(int(vote_id), int(vote_d or 0), chat.rater_of(me), start)
                elif me["role"] != "owner":
                    await send(ws, json.dumps({"type": "error", "error": "only the owner deletes"}))
                else:
                    await chat.delete(int(del_id))
                continue
            rm = re.match(r"/(?:remind|напомни|напоминания|reminders)(?:\s+|$)(.*)", text, re.I | re.S)
            if rm:                                           # a reminder (the owner, #1114): set / list / cancel, answered to this page only
                if me["role"] not in ("owner", "verified"):
                    await send(ws, json.dumps({"type": "note", "text": "Reminders need a sign-in."}))
                else:
                    await send(ws, json.dumps({"type": "note", "text": remind_cmd(me, rm.group(1))}, ensure_ascii=False))
                continue
            pm = re.match(r"/(?:poll|опрос|голосование)((?:\+|-?anon|-?анон)*)\s+(.+)$", text, re.I | re.S)
            if pm:                                           # a poll (the owner, #1386): /poll question | option | option, or one per line; /poll+ several answers, /poll-anon nobody sees who picked what (#1419)
                parts = [" ".join(x.split()) for x in re.split(r"\s*\|\s*|\n", pm.group(2)) if x.strip()]
                opts = list(dict.fromkeys(x[:100] for x in parts[1:]))[:10]
                if me["role"] not in ("owner", "verified"):
                    await send(ws, json.dumps({"type": "note", "text": "Polls need a sign-in."}))
                elif len(opts) < 2:
                    await send(ws, json.dumps({"type": "note", "text": "/poll Question | option 1 | option 2 ... (2 to 10; /poll+ lets everyone pick several, /poll-anon hides who picked what, /poll+anon both)"}))
                else:
                    fl = pm.group(1).lower()
                    await chat.post("msg", {"name": me["name"], "task": me["task"]}, parts[0][:500],
                                    poll={"o": opts, "multi": "+" in fl, **({"anon": True} if "anon" in fl or "анон" in fl else {})})
                continue
            names = {x["name"] for x in chat.agents.values()}
            bad = [n for n in to if n not in names]
            if not text:
                continue
            if bad:
                await send(ws, json.dumps({"type": "error", "error": f"nobody named {', '.join(bad)} here"}))
                continue
            rep = chat.reply_ref(reply_id, start) if reply_id else None
            if rep and not to and rep["agent"] != me["name"] and rep["agent"] in names:
                to = [rep["agent"]]                          # an answer goes to the author of what it answers
            to, every = chat.with_all({"name": me["name"], "task": me["task"], "role": me["role"]}, text, to)
            if ALL_RE.search(text) and not every:
                await send(ws, json.dumps({"type": "error", "error": "only the owner and those he allows may use @all"}))
            # "! <command>" from the owner to named agents (the owner): the listener of an agent started with
            # `agentchat listen --shell` runs it in its folder and answers with the output. Only the owner's page sets it; never @all
            shm = SHELL_RE.match(text) if me["role"] == "owner" and to and not every else None
            await chat.post("msg", {"name": me["name"], "task": me["task"]}, text, to, reply=rep, doc=doc_of(q), all=every or None,
                            sh=shm.group(1).strip() if shm else None)
    finally:
        chat.watchers.discard(ws)
        chat.viewers.pop(ws, None)
        k = me["id"] or me["name"]
        if k in chat.here:
            chat.here[k]["seen"] = round(time.time(), 1)
            chat.here_save()
        chat.peers.pop(me.get("peer"), None)          # (the presence that follows drops its camera too)
        chat.typed.pop(ws, None)
        print(f"[watch] - {me['role']}/{me.get('name') or '-'}@{me.get('ip')} via {me.get('via')} close={ws.close_code} "
              f"exc={ws.exception()!r} ({len(chat.watchers)} pages)", flush=True)
        chat.person_went(me)
        await chat.send_presence()
    return ws


UPLOADS = Path.home() / "agentchat-uploads"        # the pictures the owner pastes into the page's message box
UP_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
            # videos (the owner, 2026-11-03, #1426): up to 50 MB; avi / mkv / mov are made mp4 (H.264 + AAC) so every browser plays them
            "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/avi": ".avi", "video/x-matroska": ".mkv"}
UP_EXT = {".mp4": ".mp4", ".m4v": ".mp4", ".mov": ".mov", ".avi": ".avi", ".mkv": ".mkv"}   # a browser that sends no type: by the name
UP_VIDEO_MAX = 50 * 2 ** 20
# the service's PATH has no anaconda: ffmpeg next to the python that runs the server, else the system's
FFMPEG = str(Path(sys.executable).with_name("ffmpeg")) if Path(sys.executable).with_name("ffmpeg").exists() else (__import__("shutil").which("ffmpeg") or "ffmpeg")
UP_MAGIC = {".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".gif": b"GIF8", ".webp": b"RIFF"}
UP_KEEP = 14 * 86400                                # a picture is kept two weeks
IMG_ROOTS = [Path(p) for p in ("/tmp", str(UPLOADS), *auth.conf_lines("img_roots"))]   # more folders: <conf>/img_roots, a path a line
IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
VID_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}      # played inline by the page (Range requests work)
MAX_VIDEO = 600 * 2 ** 20
MAX_AGENT_UPLOAD = 20 * 2 ** 20                     # what an agent on another machine may send with a message


STATUS_MAX = 120
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF"
                      "\uFE0F\u200D\u20E3\u3030\u303D\u3297\u3299\U000E0020-\U000E007F]")


def status_clean(raw) -> tuple[str, str]:
    """a text status: (the text, ""), or ("", the reason): one line, 120 characters, no emoji (the owner, #1889). Emoji ranges, not a
    whole Unicode category: the same category holds the plain signs people type, such as a degree or a number sign"""
    s = unicodedata.normalize("NFC", str(raw if raw is not None else ""))
    if EMOJI_RE.search(s):
        return "", "emoji"
    s = "".join(c for c in s if unicodedata.category(c) not in ("Cc", "Cf") or c in "\n\t")      # no bidi overrides, no zero-width
    s = " ".join(s.split())
    return ("", "too_long") if len(s) > STATUS_MAX else (s, "")


# the chats' domains, one place for every chat on this machine, the server, chats.py and the page (the owner, 2026-11-08, #1981):
# ~/.config/agentchat-chats/settings.json {"domain": "<new chats and a bare name in Add a chat>", "domains": [<every domain a chat
# lives on>], "moved_from": [<domains the chats left: the page there sends the browser to the same name on "domain">]}; a missing
# file or key - the defaults below. Read again when the file changes.
SETTINGS_FILE = Path(os.environ.get("LAGOMCHAT_SETTINGS", Path.home() / ".config/agentchat-chats/settings.json"))
SETTINGS_DEFAULT = {"domain": "", "domains": [], "moved_from": []}   # the chats' domains come only from the setup
_SETTINGS: dict = {}


def settings() -> dict:
    try:
        key = SETTINGS_FILE.stat().st_mtime_ns
    except OSError:
        key = None
    if _SETTINGS.get("key") != key or "v" not in _SETTINGS:
        v = dict(SETTINGS_DEFAULT)
        try:
            got = json.loads(SETTINGS_FILE.read_text()) if key is not None else {}
            if isinstance(got, dict):
                v.update({k: got[k] for k in SETTINGS_DEFAULT if k in got})
        except (OSError, ValueError):
            pass
        doms = [str(d).strip(".").lower() for d in (v.get("domains") or []) if str(d).strip(".")]
        dom = str(v.get("domain") or "").strip(".").lower() or (doms[0] if doms else SETTINGS_DEFAULT["domain"])
        moved = [str(d).strip(".").lower() for d in (v.get("moved_from") or []) if str(d).strip(".")]
        v = {"domain": dom, "domains": [dom] + [d for d in doms if d != dom], "moved_from": [d for d in moved if d != dom]}
        _SETTINGS.update(key=key, v=v)
    return _SETTINGS["v"]


# "Apply to all chats" in the page's settings (the owner, 2026-11-08, #1858): a person's look and feel kept once for every chat on
# this machine - the chats are separate sites (each its own localStorage), so the page keeps them here, by the person's email.
# {email: {"all": bool, "prefs": {key: value}}}; only the keys the page lists, short strings
SHARED_PREFS = Path(os.environ.get("LAGOMCHAT_SHARED_PREFS", Path.home() / ".config/agentchat-chats/prefs.json"))
PREF_KEYS = {"ac_theme", "ac_lang", "ac_tsz", "ac_tbs", "ac_sbs", "ac_side", "ac_enter", "ac_hidevotes", "ac_hidesys", "ac_hidetr",
             "ac_joinsnd", "ac_awayafter"}


def _shared_prefs_load() -> dict:
    try:
        d = json.loads(SHARED_PREFS.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


async def api_prefs(req: web.Request) -> web.Response:
    v = await viewer_of(req)
    em = str(v.get("em") or "").lower()
    if not em:
        return web.json_response({"ok": False, "error": "sign in first"}, status=401, headers={"Cache-Control": "no-store"})
    if req.method == "GET":
        cur = _shared_prefs_load().get(em) or {}
        return web.json_response({"ok": True, "all": bool(cur.get("all")), "prefs": cur.get("prefs") or {}}, headers={"Cache-Control": "no-store"})
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    same = urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"
    if not same:
        return web.json_response({"ok": False, "error": "refused: another site"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    d = _shared_prefs_load()
    cur = d.get(em) if isinstance(d.get(em), dict) else {}
    if "all" in q:
        cur["all"] = bool(q["all"])
    if isinstance(q.get("prefs"), dict):
        p = dict(cur.get("prefs") or {})
        for k, val in q["prefs"].items():
            if k not in PREF_KEYS:
                continue
            if val is None or val == "":
                p.pop(k, None)
            else:
                p[k] = str(val)[:40]
        cur["prefs"] = p
    d[em] = cur
    SHARED_PREFS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SHARED_PREFS.with_name(f"{SHARED_PREFS.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(SHARED_PREFS)
    return web.json_response({"ok": True, "all": bool(cur.get("all")), "prefs": cur.get("prefs") or {}})


async def api_status(req: web.Request) -> web.Response:
    v = await need_viewer(req)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    if not (urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"):
        raise web.HTTPForbidden()
    try:
        q = await req.json()
    except ValueError:
        q = {}
    s, err = status_clean(q.get("text") if isinstance(q, dict) else "")
    task = v.get("task") or ""
    if err or not task:
        return web.json_response({"ok": False, "error": err or "bad_request"})
    if s:
        chat.statuses[task] = s
    else:
        chat.statuses.pop(task, None)
    chat.status_file.write_text(json.dumps(chat.statuses, ensure_ascii=False))
    await chat.send_presence()
    return web.json_response({"ok": True, "text": s})


PHOTO_MAX_BYTES = 2 ** 20
SOUND_MAX_BYTES, SOUND_MAX_S, SOUND_MEAN_DB, SOUND_PEAK_DB, SOUND_MAX_GAIN = 2 * 2 ** 20, 1.0, -18.0, -3.0, 15.0


async def _ff(*args: str) -> tuple[int, str]:
    pr = await asyncio.create_subprocess_exec(FFMPEG, "-nostdin", "-hide_banner", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(pr.communicate(), timeout=20)
    except asyncio.TimeoutError:
        pr.kill()
        return 1, "timeout"
    return pr.returncode or 0, err.decode("utf-8", "replace")


# ready-made join / leave sounds to pick in the profile (the owner, #85): sounds/<key>-in.mp3 and <key>-out.mp3 beside this file, made by
# sounds/gen.py at the chat's own level and length, so they go in as they are. Stored like an upload: by the hash of the file
SOUND_PRESETS: dict[str, dict[str, str]] = {}
for _f in sorted((HERE / "sounds").glob("*.mp3")):
    _key, _, _kind = _f.stem.rpartition("-")
    if _kind in ("in", "out"):
        SOUND_PRESETS.setdefault(_key, {})[_kind] = hashlib.sha256(_f.read_bytes()).hexdigest()[:16]


async def api_photo(req: web.Request) -> web.Response:
    """a person's own picture (the owner, #90). POST the JPEG the page's editor cut as the body; ?on=1|0 shows or hides the one kept;
    ?remove=1 deletes it. {ok, avatar: what the page shows now, photo: {h, on} or null}"""
    v = await need_viewer(req)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    if not (urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"):
        raise web.HTTPForbidden()
    task = v.get("task") or ""
    if not task:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    cur = dict(chat.photos.get(task) or {})
    old = cur.get("h")
    if req.query.get("remove"):
        cur = {}
    elif req.query.get("on") in ("0", "1"):
        if not cur:
            return web.json_response({"ok": False, "error": "no_photo"}, status=400)
        cur["on"] = req.query["on"] == "1"
    else:
        data = await req.read()
        if len(data) > PHOTO_MAX_BYTES:
            return web.json_response({"ok": False, "error": "too_big"})
        if data[:3] != b"\xff\xd8\xff":
            return web.json_response({"ok": False, "error": "bad_image"})
        h = hashlib.sha256(data).hexdigest()[:16]
        chat.photos_dir.mkdir(parents=True, exist_ok=True)
        (chat.photos_dir / f"{h}.jpg").write_bytes(data)
        cur = {"h": h, "on": True}
    if cur:
        chat.photos[task] = cur
    else:
        chat.photos.pop(task, None)
    chat.photos_file.write_text(json.dumps(chat.photos))
    if old and old != cur.get("h") and not any(old == x.get("h") for x in chat.photos.values()):
        (chat.photos_dir / f"{old}.jpg").unlink(missing_ok=True)
    avatar = f'photo/{cur["h"]}.jpg' if cur.get("on") else v.get("acct_avatar", "")
    for w in chat.viewers.values():                  # the pages of this person that are open now
        if w.get("task") == task:
            w["avatar"] = avatar
    print(f"[photo] {v.get('name')}: {cur or 'removed'}", flush=True)
    await chat.send_presence()
    return web.json_response({"ok": True, "avatar": avatar, "photo": cur or None})


async def api_photo_file(req: web.Request) -> web.StreamResponse:
    await need_viewer(req)
    p = chat.photos_dir / f'{req.match_info["h"]}.jpg'
    if not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": "image/jpeg", "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"})


async def api_sound(req: web.Request) -> web.Response:
    """a person's own join / leave sound (the owner, #1798). POST ?kind=in|out with the file as the body (mp3, wav, aac, m4a), or
    &remove=1. Never kept as it came: re-encoded to mp3, 1 s at most, the level brought to SOUND_MEAN_DB, peaks under SOUND_PEAK_DB"""
    v = await need_viewer(req)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    if not (urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"):
        raise web.HTTPForbidden()
    kind, task = req.query.get("kind"), v.get("task") or ""
    if kind not in ("in", "out") or not task:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    def store(h: str | None) -> None:
        cur = dict(chat.sounds.get(task, {}))
        old = cur.pop(kind, None)
        if h:
            cur[kind] = h
        if cur:
            chat.sounds[task] = cur
        else:
            chat.sounds.pop(task, None)
        chat.sounds_file.write_text(json.dumps(chat.sounds))
        if old and old != h and not any(old in x.values() for x in chat.sounds.values()):
            (chat.sounds_dir / f"{old}.mp3").unlink(missing_ok=True)
    if req.query.get("remove"):
        store(None)
        await chat.send_presence()
        return web.json_response({"ok": True})
    if req.query.get("preset"):
        h = SOUND_PRESETS.get(req.query["preset"], {}).get(kind)
        if not h:
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        chat.sounds_dir.mkdir(parents=True, exist_ok=True)
        (chat.sounds_dir / f"{h}.mp3").write_bytes((HERE / "sounds" / f'{req.query["preset"]}-{kind}.mp3').read_bytes())
        store(h)
        print(f"[sound] {v.get('name')} {kind} preset {req.query['preset']}", flush=True)
        await chat.send_presence()
        return web.json_response({"ok": True, "hash": h})
    data = await req.read()
    if not data or len(data) > SOUND_MAX_BYTES:
        return web.json_response({"ok": False, "error": "too_big"})
    chat.sounds_dir.mkdir(parents=True, exist_ok=True)
    src, dst = chat.sounds_dir / f".in-{secrets.token_hex(6)}", chat.sounds_dir / f".out-{secrets.token_hex(6)}.mp3"
    try:
        src.write_bytes(data)
        opened = ("-protocol_whitelist", "file", "-format_whitelist", "mp3,wav,aac,mov", "-i", str(src), "-vn", "-map", "0:a:0")
        rc, err = await _ff(*opened, "-af", "aformat=channel_layouts=mono:sample_rates=44100,volumedetect", "-t", "30", "-f", "null", "-")
        mean, peak, count = re.search(r"mean_volume: (-?[\d.]+) dB", err), re.search(r"max_volume: (-?[\d.]+) dB", err), re.findall(r"n_samples: (\d+)", err)   # the graph is set up twice: the first says 0
        if rc or not (mean and peak and count):
            return web.json_response({"ok": False, "error": "bad_audio"})
        dur = int(count[-1]) / 44100
        if dur > SOUND_MAX_S + 0.05:
            return web.json_response({"ok": False, "error": "too_long", "dur": round(dur, 2)})
        if float(peak.group(1)) < -50:
            return web.json_response({"ok": False, "error": "silent"})
        gain = min(SOUND_MEAN_DB - float(mean.group(1)), SOUND_PEAK_DB - float(peak.group(1)), SOUND_MAX_GAIN)
        rc, err = await _ff("-y", "-loglevel", "error", *opened, "-af", f"volume={gain:.2f}dB", "-t", str(SOUND_MAX_S), "-ac", "1", "-ar", "44100",
                            "-c:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1", "-f", "mp3", str(dst))
        if rc or not dst.exists() or not dst.stat().st_size:
            print(f"[sound] encode failed: {err[:200]}", flush=True)
            return web.json_response({"ok": False, "error": "bad_audio"})
        h = hashlib.sha256(dst.read_bytes()).hexdigest()[:16]
        dst.replace(chat.sounds_dir / f"{h}.mp3")
        store(h)
        print(f"[sound] {v.get('name')} {kind} {h}: {dur:.2f} s, gain {gain:+.1f} dB", flush=True)
        await chat.send_presence()
        return web.json_response({"ok": True, "hash": h, "dur": round(dur, 2), "gain": round(gain, 1)})
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


async def api_sound_file(req: web.Request) -> web.StreamResponse:
    await need_viewer(req)
    p = chat.sounds_dir / f'{req.match_info["h"]}.mp3'
    if not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": "audio/mpeg", "Cache-Control": "private, max-age=31536000, immutable"})


# stickers (the owner, #196, #204): everybody's shared set, the files under UPLOADS/stickers so that a message can name them like any picture
STICKERS_DIR = UPLOADS / "stickers"
STICKER_MAX_BYTES, STICKERS_MAX = 2 * 2 ** 20, 3000
STICKER_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _same_page(req: web.Request) -> bool:
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    return urlparse(org).netloc in (host, req.headers.get("Host")) if org else req.headers.get("Sec-Fetch-Site") == "same-origin"


def _stickers_out(v: dict) -> list[dict]:
    task = v.get("task") or ""
    return [{"h": s["h"], "url": f'sticker/{s["h"]}{s["ext"]}' + (f'?v={s["v"]}' if s.get("v") else ""), "path": str(STICKERS_DIR / f'{s["h"]}{s["ext"]}'),
             "mine": v["role"] == "owner" or s.get("by") == task, "pack": s.get("pack", ""), "emoji": s.get("emoji", "")} for s in chat.stickers]


async def api_stickers(req: web.Request) -> web.Response:
    """GET: the set, and whether the search of GIFs is on. POST a picture as the body: a new sticker. POST ?remove=<hash>: off the set, for the
    one who added it and the owner; the file stays, the messages that showed it keep their picture"""
    v = await need_viewer(req)
    if req.method == "GET":
        return web.json_response({"ok": True, "stickers": _stickers_out(v), "gifs": bool(giphy_key())}, headers={"Cache-Control": "no-store"})
    if not _same_page(req):
        raise web.HTTPForbidden()
    task = v.get("task") or ""
    if not task:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    rm = req.query.get("remove")
    if rm:
        hit = next((s for s in chat.stickers if s["h"] == rm), None)
        if not hit or not (v["role"] == "owner" or hit.get("by") == task):
            return web.json_response({"ok": False, "error": "not_yours"}, status=403)
        chat.stickers.remove(hit)
    else:
        data = await req.read()
        ext = next((e for e, m in UP_MAGIC.items() if data.startswith(m) and (e != ".webp" or data[8:12] == b"WEBP")), None)
        if not ext:
            return web.json_response({"ok": False, "error": "bad_image"})
        if len(data) > STICKER_MAX_BYTES:
            return web.json_response({"ok": False, "error": "too_big"})
        if len(chat.stickers) >= STICKERS_MAX:
            return web.json_response({"ok": False, "error": "too_many"})
        h = hashlib.sha256(data).hexdigest()[:16]
        if not any(s["h"] == h for s in chat.stickers):
            STICKERS_DIR.mkdir(parents=True, exist_ok=True)
            (STICKERS_DIR / f"{h}{ext}").write_bytes(data)
            chat.stickers.insert(0, {"h": h, "ext": ext, "by": task, "ts": round(time.time(), 1)})
    chat.stickers_file.write_text(json.dumps(chat.stickers))
    print(f"[sticker] {v.get('name')}: {'-' + rm if rm else 'added'} ({len(chat.stickers)})", flush=True)
    return web.json_response({"ok": True, "stickers": _stickers_out(v)})


async def api_sticker_file(req: web.Request) -> web.StreamResponse:
    await need_viewer(req)
    ext = "." + req.match_info["ext"]
    p = STICKERS_DIR / f'{req.match_info["h"]}{ext}'
    if ext not in STICKER_TYPES or not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": STICKER_TYPES[ext], "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"})


RIMG_MAX_BYTES, RIMGS_MAX = 2 ** 19, 500


def _rimgs_out() -> list[dict]:
    return [{"h": x["h"], "ext": x["ext"], "by": x.get("by", ""), **({"off": True} if x.get("off") else {})} for x in chat.react_imgs]


async def api_react_imgs(req: web.Request) -> web.Response:
    """pictures for reactions. POST a picture as the body: added for everybody. POST ?remove=<hash>: off the list, for the one who added it and the owner"""
    v = await need_viewer(req)
    if not _same_page(req):
        raise web.HTTPForbidden()
    task = v.get("task") or ""
    if not task:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    rm = req.query.get("remove")
    if rm:
        hit = next((x for x in chat.react_imgs if x["h"] == rm), None)
        if not hit or not (v["role"] == "owner" or hit.get("by") == task):
            return web.json_response({"ok": False, "error": "not_yours"}, status=403)
        hit["off"] = True
    else:
        data = await req.read()
        ext = next((e for e, m in UP_MAGIC.items() if data.startswith(m) and (e != ".webp" or data[8:12] == b"WEBP")), None)
        if not ext:
            return web.json_response({"ok": False, "error": "bad_image"})
        if len(data) > RIMG_MAX_BYTES:
            return web.json_response({"ok": False, "error": "too_big"})
        if sum(not x.get("off") for x in chat.react_imgs) >= RIMGS_MAX:
            return web.json_response({"ok": False, "error": "too_many"})
        h = hashlib.sha256(data).hexdigest()[:16]
        hit = next((x for x in chat.react_imgs if x["h"] == h), None)
        if hit:
            hit.pop("off", None)
        else:
            d = DATA / "react_imgs"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{h}{ext}").write_bytes(data)
            chat.react_imgs.insert(0, {"h": h, "ext": ext, "by": task, "ts": round(time.time(), 1)})
    chat.react_imgs_file.write_text(json.dumps(chat.react_imgs))
    print(f"[rimg] {v.get('name')}: {'-' + rm if rm else 'added'}", flush=True)
    await chat.watch_all({"type": "rimgs", "rimgs": _rimgs_out()})
    return web.json_response({"ok": True, "rimgs": _rimgs_out()})


async def api_react_img_file(req: web.Request) -> web.StreamResponse:
    await need_viewer(req)
    ext = "." + req.match_info["ext"]
    p = DATA / "react_imgs" / f'{req.match_info["h"]}{ext}'
    if ext not in STICKER_TYPES or not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": STICKER_TYPES[ext], "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"})


# a sticker pack from Telegram (the owner, #225): an agent is given the link, the server fetches the pack with a bot's token (the file telegram_bot.token in
# the config folder: Telegram gives packs to bots only). Plain stickers are webp already; video ones (webm) and animated ones (tgs) become animated webp
TG_API = os.environ.get("LAGOMCHAT_TG_API", "https://api.telegram.org").rstrip("/")
TG_PACK_RE = re.compile(r"(?:https?://)?(?:t(?:elegram)?\.me/addstickers/)?([A-Za-z0-9_]{1,64})/?")
TG_FILE_RE = re.compile(r"[A-Za-z0-9_./-]{1,200}")
TG_FILE_MAX = 8 * 2 ** 20
sticker_jobs: dict[str, dict] = {}


async def _read_body(r, limit: int) -> bytes | None:
    """the whole answer, or None over the limit (content.read(n) gives only what has arrived so far: #273, stickers cut at 16 KB)"""
    buf = bytearray()
    async for chunk in r.content.iter_chunked(65536):
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


def tg_token() -> str:
    try:
        return (auth.CONF / "telegram_bot.token").read_text().strip()
    except OSError:
        return ""


async def _tg_get(s: ClientSession, url: str, limit: int) -> tuple[int, bytes]:
    """(status, bytes); waits once if Telegram asks to. Never says the address: it has the token in it"""
    for _ in range(2):
        async with s.get(url) as r:
            if r.status == 429:
                try:
                    wait = int((await r.json(content_type=None)).get("parameters", {}).get("retry_after", 5))
                except ValueError:
                    wait = 5
                await asyncio.sleep(min(wait, 30))
                continue
            return r.status, await _read_body(r, limit) or b""
    return 429, b""


def _tgs_to_webp(src: Path, dst: Path, quality: int) -> None:
    from rlottie_python import LottieAnimation       # only for animated stickers; without it they are counted as skipped
    with LottieAnimation.from_tgs(str(src)) as a:
        a.save_animation(str(dst), fps=20, width=256, height=256, quality=quality, method=4, lossless=False)


async def _to_webp(data: bytes, kind: str) -> bytes | None:
    """a video or animated sticker as an animated webp under the sticker limit: smaller and rougher until it fits"""
    tmp = STICKERS_DIR / f".in-{secrets.token_hex(6)}"
    out = STICKERS_DIR / f".out-{secrets.token_hex(6)}.webp"
    try:
        tmp.write_bytes(data)
        for size, quality in ((384, 70), (256, 55), (192, 40)):
            if kind == "tgs":
                await asyncio.to_thread(_tgs_to_webp, tmp, out, quality)
            else:                                        # libvpx: the only decoder that keeps a webm's transparency
                rc, err = await _ff("-y", "-loglevel", "error", "-c:v", "libvpx-vp9", "-i", str(tmp), "-vf", f"fps=20,scale={size}:-2:flags=lanczos", "-an", "-c:v", "libwebp_anim", "-loop", "0", "-q:v", str(quality), "-f", "webp", str(out))
                if rc:
                    print(f"[sticker] ffmpeg: {err[:160]}", flush=True)
                    return None
            if out.is_file() and out.stat().st_size <= STICKER_MAX_BYTES:
                return out.read_bytes()
        return None
    except ImportError:
        print("[sticker] rlottie_python is not installed: animated stickers are skipped", flush=True)
        return None
    except Exception as x:                              # a broken file must not stop the pack
        print(f"[sticker] convert {kind}: {type(x).__name__}", flush=True)
        return None
    finally:
        tmp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


async def _sticker_import(name: str, job: dict) -> None:
    token = tg_token()
    base = f"{TG_API}/bot{token}"
    try:
        async with ClientSession(timeout=ClientTimeout(total=40)) as s:
            st, raw = await _tg_get(s, f"{base}/getStickerSet?" + urllib.parse.urlencode({"name": name}), 2 ** 22)
            try:
                ans = json.loads(raw)
            except ValueError:
                ans = {}
            if not ans.get("ok"):
                job.update(state="failed", error=str(ans.get("description") or f"Telegram answered {st}")[:160].replace(token, "***") if token else "no token")
                return
            pack = ans["result"]
            job.update(title=str(pack.get("title") or name)[:80], total=len(pack.get("stickers") or []))
            STICKERS_DIR.mkdir(parents=True, exist_ok=True)
            for it in pack.get("stickers") or []:
                job["done"] += 1
                kind = "tgs" if it.get("is_animated") else "webm" if it.get("is_video") else "webp"
                if len(chat.stickers) >= STICKERS_MAX:
                    job["skipped"] += 1
                    job["why"] = "the set is full"
                    continue
                try:
                    _, fr = await _tg_get(s, f"{base}/getFile?" + urllib.parse.urlencode({"file_id": str(it.get("file_id") or "")}), 2 ** 16)
                    path = str(((json.loads(fr) if fr else {}).get("result") or {}).get("file_path") or "")
                    st, data = (0, b"") if not TG_FILE_RE.fullmatch(path) or ".." in path else await _tg_get(s, f"{TG_API}/file/bot{token}/{path}", TG_FILE_MAX)
                except (ClientError, asyncio.TimeoutError, ValueError, OSError) as x:
                    print(f"[sticker] telegram: {type(x).__name__}", flush=True)
                    data = b""
                if data and kind != "webp":
                    data = await _to_webp(data, kind) or b""
                ok = data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) <= STICKER_MAX_BYTES and int.from_bytes(data[4:8], "little") + 8 == len(data)
                if not ok:
                    job["skipped"] += 1
                    job["skipped_" + kind] = job.get("skipped_" + kind, 0) + 1
                    continue
                h = hashlib.sha256(data).hexdigest()[:16]
                emoji = str(it.get("emoji") or "")[:16]      # the emoji Telegram ties the sticker to, shown on hover (the owner, #284)
                known = next((x for x in chat.stickers if x["h"] == h), None)
                if known:                                    # imported before: a new import fills in what the old one did not keep
                    job["same"] += 1
                    if emoji and known.get("emoji") != emoji:
                        known["emoji"] = emoji
                        chat.stickers_file.write_text(json.dumps(chat.stickers))
                    continue
                (STICKERS_DIR / f"{h}.webp").write_bytes(data)
                chat.stickers.append({"h": h, "ext": ".webp", "by": "telegram:" + name, "pack": job["title"], "emoji": emoji, "ts": round(time.time(), 1)})
                chat.stickers_file.write_text(json.dumps(chat.stickers))
                job["added"] += 1
        job["state"] = "done"
    except (ClientError, asyncio.TimeoutError, OSError) as x:
        job.update(state="failed", error=f"Telegram: {type(x).__name__}")
    except Exception as x:
        job.update(state="failed", error=type(x).__name__)
        print(f"[sticker] import: {type(x).__name__}", flush=True)
    print(f"[sticker] import {name}: {({k: v for k, v in job.items() if k != 'task'})}", flush=True)


# the search of GIFs through GIPHY (the owner, #204). Everything goes through this server: GIPHY sees the server, never the people on the page, and the
# key stays here. The key is the file giphy.token in the config folder; without it the search is off
GIPHY_API = os.environ.get("LAGOMCHAT_GIPHY_API", "https://api.giphy.com").rstrip("/")
GIPHY_MEDIA = os.environ.get("LAGOMCHAT_GIPHY_MEDIA", "https://media.giphy.com").rstrip("/")
GIF_ID_RE = re.compile(r"[A-Za-z0-9]{1,40}")
GIF_PREVIEW_MAX, GIF_FULL_MAX, GIF_CACHE_FILES = 6 * 2 ** 20, 15 * 2 ** 20, 400
_gif_searches: dict[tuple, tuple[float, list]] = {}


def giphy_key() -> str:
    try:
        return (auth.CONF / "giphy.token").read_text().strip()
    except OSError:
        return ""


async def _gif_get(url: str, limit: int) -> bytes | None:
    """bytes from GIPHY or None; never says the address: it has the key in it"""
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as s, s.get(url) as r:
            if r.status != 200:
                print(f"[gif] GIPHY answered {r.status}", flush=True)
                return None
            data = await _read_body(r, limit)
            return data
    except (ClientError, asyncio.TimeoutError, OSError) as x:
        print(f"[gif] GIPHY: {type(x).__name__}", flush=True)
        return None


async def api_gifs(req: web.Request) -> web.Response:
    """?q=<words>&offset=<n>: {ok, gifs: [{id, w, h, title}]}; no words: what is trending. Answers are kept for 5 minutes"""
    await need_viewer(req)
    key = giphy_key()
    if not key:
        return web.json_response({"ok": False, "error": "no_key"})
    q = req.query.get("q", "").strip()[:80]
    try:
        off = max(0, min(int(req.query.get("offset") or 0), 500))
    except ValueError:
        off = 0
    now = time.time()
    hit = _gif_searches.get((q.lower(), off))
    if hit and now - hit[0] < 300:
        return web.json_response({"ok": True, "gifs": hit[1]})
    args = urllib.parse.urlencode({"api_key": key, "limit": 24, "offset": off, "rating": "pg-13", **({"q": q} if q else {})})
    raw = await _gif_get(f"{GIPHY_API}/v1/gifs/{'search' if q else 'trending'}?{args}", 2 ** 21)
    try:
        items = json.loads(raw)["data"] if raw else None
    except (ValueError, KeyError, TypeError):
        items = None
    if items is None:
        return web.json_response({"ok": False, "error": "search_failed"})
    out = []
    for g in items:
        pv = ((g.get("images") or {}).get("fixed_width") or {})
        if GIF_ID_RE.fullmatch(str(g.get("id") or "")):
            try:
                out.append({"id": g["id"], "w": int(pv.get("width") or 200), "h": int(pv.get("height") or 150), "title": str(g.get("title") or "")[:80]})
            except ValueError:
                pass
    if len(_gif_searches) > 200:
        _gif_searches.clear()
    _gif_searches[(q.lower(), off)] = (now, out)
    return web.json_response({"ok": True, "gifs": out})


async def api_gif_preview(req: web.Request) -> web.StreamResponse:
    """a search result's small picture, through this server and kept on its disk"""
    await need_viewer(req)
    gid = req.match_info["id"]
    d = DATA / "gifcache"
    p = d / f"{gid}.gif"
    if not p.is_file():
        data = await _gif_get(f"{GIPHY_MEDIA}/media/{gid}/200w.gif", GIF_PREVIEW_MAX)
        if not data or not data.startswith(b"GIF8"):
            raise web.HTTPNotFound()
        d.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        old = sorted(d.glob("*.gif"), key=lambda f: f.stat().st_mtime)
        for f in old[:max(0, len(old) - GIF_CACHE_FILES)]:
            f.unlink(missing_ok=True)
    return web.FileResponse(p, headers={"Content-Type": "image/gif", "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})


async def api_gif_use(req: web.Request) -> web.Response:
    """POST ?id=<gif>: the GIF is fetched into UPLOADS, {path} back; the message names the file, so whoever reads it never meets GIPHY"""
    v = await need_viewer(req)
    if not _same_page(req):
        raise web.HTTPForbidden()
    gid = req.query.get("id", "")
    if not GIF_ID_RE.fullmatch(gid) or not v.get("task") or not giphy_key():
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    p = UPLOADS / f"gif-{gid}.gif"
    if not p.is_file():
        data = await _gif_get(f"{GIPHY_MEDIA}/media/{gid}/giphy-downsized.gif", GIF_FULL_MAX)
        if not data or not data.startswith(b"GIF8"):
            return web.json_response({"ok": False, "error": "search_failed"})
        UPLOADS.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    print(f"[gif] {v.get('name')}: {gid}", flush=True)
    return web.json_response({"ok": True, "path": str(p)})


async def api_upload(req: web.Request):
    """POST a picture (the raw body, its Content-Type png/jpeg/gif/webp): saved under UPLOADS, {path} back - the page puts the
    path into the message, the agents read the file. Only the page of this very host may post."""
    v = await need_viewer(req)
    org = req.headers.get("Origin")
    if org and urlparse(org).netloc != req.headers.get("Host"):
        raise web.HTTPForbidden()
    ext = UP_TYPES.get(req.headers.get("Content-Type", "").split(";")[0].strip().lower()) or \
        UP_EXT.get(Path(urllib.parse.unquote(req.headers.get("X-Filename", ""))).suffix.lower())
    if not ext:
        raise web.HTTPUnsupportedMediaType(text="png, jpeg, gif, webp or a video: mp4, mov, avi, mkv")
    data = await req.read()
    video = ext in (".mp4", ".mov", ".avi", ".mkv")
    if video:
        if len(data) > UP_VIDEO_MAX:
            raise web.HTTPRequestEntityTooLarge(max_size=UP_VIDEO_MAX, actual_size=len(data))
        ok = (data[4:8] in (b"ftyp", b"moov", b"wide", b"mdat", b"free", b"skip") if ext in (".mp4", ".mov") else
              data[:4] == b"RIFF" and data[8:11] == b"AVI" if ext == ".avi" else data[:4] == b"\x1aE\xdf\xa3")
        if not ok:
            raise web.HTTPUnsupportedMediaType(text="not a video of that type")
    elif not data.startswith(UP_MAGIC[ext]) or (ext == ".webp" and data[8:12] != b"WEBP") or len(data) > 25 * 2 ** 20:
        raise web.HTTPUnsupportedMediaType(text="not a picture of that type (or over 25 MB)")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    p = UPLOADS / f"{stem}{ext}"
    p.write_bytes(data)
    if ext in (".mov", ".avi", ".mkv"):                 # made mp4: what every browser plays (a mov may be HEVC, an avi / mkv anything)
        out = UPLOADS / f"{stem}.mp4"
        try:
            pr = await asyncio.create_subprocess_exec(
                FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", str(p), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-vf", "scale='min(1920,iw)':-2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(pr.communicate(), timeout=300)
            if pr.returncode == 0 and out.exists() and out.stat().st_size:
                p.unlink(missing_ok=True)
                p = out
            else:
                print(f"[upload] ffmpeg failed for {p.name}: {err.decode(errors='replace')[-300:]}", flush=True)
        except (OSError, asyncio.TimeoutError) as x:
            print(f"[upload] ffmpeg: {x!r}", flush=True)
    print(f"[upload] {p.name} {p.stat().st_size // 1024} KB by {v.get('name')}", flush=True)
    # the uploader sees the file at once, before a message names it: the attachment's thumbnail in the box (#1627: a person
    # who is not the owner got a broken thumbnail, the img route showing only what visible messages name)
    chat.mine_up.setdefault(v.get("task") or "", set()).add(str(p))
    return web.json_response({"path": str(p), "bytes": p.stat().st_size})


async def api_img(req: web.Request):
    """an image a message names by its path (the page shows it): only image files, only under IMG_ROOTS, no hidden dirs"""
    raw = req.query.get("p", "")
    tok = token_of(req) if from_proxy(req) else None
    if tok:
        if raw not in remote_paths(tok):
            raise web.HTTPNotFound()
    else:
        v = await need_viewer(req)
        if (v["role"] != "owner" and raw not in chat.mine_up.get(v.get("task") or "", ())
                and raw not in {a.get("av") for a in chat.aliases.get(viewer_key(v), {}).values()} and raw not in chat.visible_paths(chat.start_for(v))):
            raise web.HTTPNotFound()
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise web.HTTPNotFound()
    ext = p.suffix.lower()
    ok = (p.is_file() and ((ext in IMG_TYPES and p.stat().st_size < 40 * 2 ** 20) or (ext in VID_TYPES and p.stat().st_size < MAX_VIDEO))
          and any(r in p.parents for r in IMG_ROOTS) and not any(x.startswith(".") for x in p.parts))
    if not ok:
        raise web.HTTPNotFound()
    hd = {"Content-Type": IMG_TYPES.get(ext) or VID_TYPES[ext], "Cache-Control": "max-age=300"}
    if req.query.get("dl") == "1":                       # a download (the owner, #1475: an iPhone saves a file only when it is sent as one)
        hd["Content-Disposition"] = f"attachment; filename=\"{p.name}\""
    return web.FileResponse(p, headers=hd)


_PV: dict = {}


def plugin_names() -> list[str]:
    env = os.environ.get("LAGOMCHAT_PLUGINS")
    if env is not None:
        return [x.strip() for x in env.split(",") if x.strip()]
    try:
        home = Path(os.environ.get("LAGOMCHAT_HOME") or HERE / ".lagomchat")
        return [str(x) for x in json.loads((home / "config.json").read_text()).get("plugins") or []]
    except (OSError, ValueError, AttributeError):
        return []
STARTED = time.time()


def built_at() -> float:
    """when the running version came out (the owner, #2224): the newer of the page's file and this server's start"""
    try:
        return max(STARTED, (HERE / "index.html").stat().st_mtime)
    except OSError:
        return STARTED


def page_version() -> str:
    """the page's version, "css:<hash>|rest:<hash>": an open page whose styles alone are older takes the new ones in place,
    any other change reloads it keeping the draft, the scroll and the search (the owner, 2026-11-02, #1169)"""
    import hashlib
    try:
        f = HERE / "index.html"
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
        if _PV.get("key") != key:
            t = f.read_text()
            i, j = t.find("<style>"), t.find("</style>")
            css = t[i:j] if 0 <= i < j else ""
            rest = t[:i] + t[j:] if 0 <= i < j else t
            h = lambda x: hashlib.sha1(x.encode()).hexdigest()[:10]
            _PV.update(key=key, css=h(css), rest=h(rest))
        # the settings (#1981) go into the page itself: a change there reloads open pages as any change of the page's code
        return f"css:{_PV['css']}|rest:" + hashlib.sha1((_PV["rest"] + json.dumps(settings(), sort_keys=True)).encode()).hexdigest()[:10]
    except OSError:
        return ""


_UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "м": 60, "мин": 60, "минут": 60, "минуты": 60, "минуту": 60,
          "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600, "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
          "d": 86400, "day": 86400, "days": 86400, "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400,
          "w": 604800, "week": 604800, "weeks": 604800, "нед": 604800, "неделю": 604800, "недели": 604800, "недель": 604800}


_MONTHS = [("янв", "jan"), ("фев", "feb"), ("мар", "mar"), ("апр", "apr"), ("мая", "май", "may"), ("июн", "jun"), ("июл", "jul"),
           ("авг", "aug"), ("сен", "sep"), ("окт", "oct"), ("ноя", "nov"), ("дек", "dec")]


def parse_when(s: str, now: float | None = None) -> tuple[float | None, str]:
    """a reminder's time at the start of the text, then what to remind: "через 2 ч ...", "in 30m ...", "завтра 10:00 ...",
    "tomorrow ...", "18:30 ...", "15.11 12:00 ...", "2026-12-01 ...". The chat machine's own clock and zone. (None, why) if it is not one"""
    from datetime import datetime, timedelta
    now = now or time.time()
    t0 = datetime.fromtimestamp(now)
    s = s.strip()
    mo = re.match(r"(\d{1,2})\s+([a-zа-яё]+)\.?(?:\s+(\d{4}))?(?=\s|$)", s, re.I)    # "21 декабря", "21 dec 2027": as 21.12[.2027]
    if mo:
        k = next((i + 1 for i, ns in enumerate(_MONTHS) if any(mo.group(2).lower().startswith(x) for x in ns)), 0)
        if k:
            s = f"{mo.group(1)}.{k}" + (f".{mo.group(3)}" if mo.group(3) else "") + s[mo.end():]
    hm_re = r"(?:(?:в|at)\s+)?(\d{1,2})[:.](\d{2})"
    m = re.match(r"(?:через|in)\s+(\d+(?:[.,]\d+)?)?\s*([a-zа-яё]+)\.?(?:\s+|$)(.*)", s, re.I | re.S)
    if m and m.group(2).lower() in _UNITS:
        n = float((m.group(1) or "1").replace(",", "."))
        return now + n * _UNITS[m.group(2).lower()], m.group(3).strip()
    m = re.match(r"(\d+)\s*(m|min|h|d|w|м|мин|ч|д|нед)\b\.?\s*(.*)", s, re.I | re.S)
    if m:
        return now + int(m.group(1)) * _UNITS[m.group(2).lower()], m.group(3).strip()
    def at(day, rest: str, need: bool) -> tuple[float | None, str]:
        h = re.match(hm_re + r"(?:\s+|$)(.*)", rest, re.I | re.S)
        if h:
            if int(h.group(1)) > 23 or int(h.group(2)) > 59:
                return None, "no such time"
            return day.replace(hour=int(h.group(1)), minute=int(h.group(2)), second=0, microsecond=0).timestamp(), h.group(3).strip()
        if need:
            return None, "no time"
        return day.replace(hour=10, minute=0, second=0, microsecond=0).timestamp(), rest.strip()
    m = re.match(r"(послезавтра|завтра|tomorrow|сегодня|today)(?:\s+|$)(.*)", s, re.I | re.S)
    if m:
        w = m.group(1).lower()
        d = t0 + timedelta(days=2 if w == "послезавтра" else 1 if w in ("завтра", "tomorrow") else 0)
        ts, rest = at(d, m.group(2), w in ("сегодня", "today"))
        return (ts, rest) if ts is None or ts > now else (None, "that time has passed")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:\s+|$)(.*)", s, re.S) or re.match(r"(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?)(?![:\d])(?:\s+|$)(.*)", s, re.S)
    if m:
        try:
            if "-" in m.group(0)[:10]:
                d = t0.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)))
            else:
                y = int(m.group(3)) if m.group(3) else t0.year
                d = t0.replace(year=y + 2000 if y < 100 else y, month=int(m.group(2)), day=int(m.group(1)))
        except ValueError:
            return None, "no such date"
        ts, rest = at(d, m.group(4), False)
        if ts is not None and ts <= now and not m.group(3) and "-" not in m.group(0)[:10]:
            ts, rest = at(d.replace(year=d.year + 1), m.group(4), False)    # a day and month gone by this year: next year's
        return (ts, rest) if ts is None or ts > now else (None, "that time has passed")
    h = re.match(hm_re + r"(?:\s+|$)(.*)", s, re.I | re.S)
    if h:
        ts, rest = at(t0, s, True)
        if ts is not None and ts <= now:
            ts += 86400                                            # a time of day gone by today: tomorrow's
        return ts, rest
    return None, "no time"


def when_txt(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def remind_cmd(who: dict, arg: str) -> str:
    """/remind for a person on the page, `agentchat remind` for an agent: set one, list them, cancel one. The answer as text"""
    arg = arg.strip()
    mine = [r for r in chat.reminders if r["by_task"] == who["task"] or who.get("role") == "owner"]
    if not arg or arg.lower() in ("list", "список"):
        if not mine:
            return "No reminders. Set one: /remind <when> <what>, e.g. /remind tomorrow 10:00 call Pavel; /remind in 2h ...; /remind 15.11 12:00 ..."
        return "Reminders: " + "; ".join(f'#{r["n"]} {when_txt(r["at"])} → {r["for"]}: {r["text"]}' for r in sorted(mine, key=lambda r: r["at"]))
    c = re.match(r"(?:cancel|отмена|отменить|удалить|delete)\s+#?(\d+)$", arg, re.I)
    if c:
        r = next((r for r in mine if r["n"] == int(c.group(1))), None)
        if not r:
            return f"No reminder #{c.group(1)} of yours."
        chat.reminders.remove(r)
        chat.reminders_file.write_text(json.dumps(chat.reminders, ensure_ascii=False))
        return f'Reminder #{r["n"]} cancelled.'
    ts, rest = parse_when(arg)
    if ts is None:
        return f"Not set ({rest}). Write when first: /remind in 2h ..., /remind tomorrow 10:00 ..., /remind 18:30 ..., /remind 15.11 12:00 ..."
    ts = round(ts / 60) * 60                                    # to the minute, as it is shown
    if not rest:
        return "Not set: what to remind is missing after the time."
    if ts - time.time() > 5 * 366 * 86400:
        return "Not set: more than five years ahead."
    if len([r for r in chat.reminders if r["by_task"] == who["task"]]) >= 200:
        return "Not set: 200 reminders at most."
    n = max([r["n"] for r in chat.reminders] + [chat.reminders_n]) + 1
    chat.reminders_n = n
    to = "owner" if who.get("role") == "owner" or not str(who["task"]).startswith("user") else who["name"]
    chat.reminders.append({"n": n, "at": round(ts), "text": rest[:1000], "for": to, "by_task": who["task"], "by_name": who["name"], "made": round(time.time())})
    chat.reminders_file.write_text(json.dumps(chat.reminders, ensure_ascii=False))
    return f"Reminder #{n} set for {when_txt(ts)}: {rest[:1000]}"


async def remind_loop() -> None:
    """every 10 s: what is due goes into the log, to whom it is for (a sound on their page); missed during a restart: at once"""
    chat.reminders_n = max([r["n"] for r in chat.reminders] + [0])
    while True:
        try:
            due = [r for r in chat.reminders if r["at"] <= time.time()]
            for r in sorted(due, key=lambda r: r["at"]):
                chat.reminders.remove(r)
                chat.reminders_file.write_text(json.dumps(chat.reminders, ensure_ascii=False))
                await chat.post("remind", {"name": r["by_name"], "task": r["by_task"]}, r["text"], to=[r["for"]], n=r["n"], at=r["at"])
        except Exception as x:                                   # a bad line must not stop the reminders
            print(f"[remind] {x!r}", flush=True)
        await asyncio.sleep(10)


async def version_loop() -> None:
    """a pull that changes only the page (no restart): the open pages are told within 10 s"""
    v = page_version()
    while True:
        await asyncio.sleep(10)
        n = page_version()
        if n and n != v:
            v = n
            await chat.watch_all({"type": "version", "v": n, "at": built_at()})


async def page(_req):
    # a chat that moved (#1987): its old address sends the browser to the same name on the new domain, the path and query kept
    host = (_req.headers.get("X-Forwarded-Host") or _req.headers.get("Host", "")).split(":")[0].lower()
    for d in settings()["moved_from"]:
        if host.endswith("." + d):
            raise web.HTTPMovedPermanently(f"https://{host[:-len(d)]}{settings()['domain']}{_req.path_qs}")
    # the chat's name in the tab's title and the header from the first paint (superman may change it: agentchat title)
    t = html_escape(chat.title)
    body = (HERE / "index.html").read_text().replace(f"<title>{DEFAULT_TITLE}</title>", f"<title>{t}</title>", 1) \
        .replace(f"<h1>{DEFAULT_TITLE}</h1>", f"<h1>{t}</h1>", 1)
    # the chats' domains from the settings (#1981), before the page's own scripts read them
    body = body.replace("<head>", "<head><script>window.AC_DOMAINS=" + json.dumps(settings()).replace("<", "\\u003c") + f";window.AC_BUILT={built_at():.0f};window.AC_PORT={PORT}</script>", 1)
    # highlight.js inside the page (the owner, #564): a request of its own for the file may not get through a proxy's sign-in
    try:
        hl = (HERE / "vendor" / "highlight.min.js").read_text()
        head, tail = body.rsplit("</body>", 1)
        body = f"{head}<script>{hl}</script></body>{tail}"
    except (OSError, ValueError):
        pass                                              # no library or no </body>: plain code blocks
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def log_txt(req):
    if (await need_viewer(req))["role"] != "owner":
        raise web.HTTPForbidden(text="the whole log is the owner's")
    body = chat.txt.read_text() if chat.txt.exists() else ""
    return web.Response(text=body, content_type="text/plain", charset="utf-8", headers={"Cache-Control": "no-store"})


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[\w:.-]+", (s or "").lower()) if len(w) > 1]


def _word_res(words: list[str]) -> list[re.Pattern]:
    """a searched word as a start of a word in the text, whatever its ending (the owner, #1060: гарнитура finds гарнитуры), the way the page
    marks them: 7+ letters lose two, 5-6 lose one; a number or a path stays whole. Short words (не, in) say nothing, unless they are all there is"""
    use = [w for w in words if len(w) >= 3 or any(c.isdigit() for c in w)] or words
    out = []
    for w in use:
        stem = w if not w.isalpha() else w[:-2] if len(w) >= 7 else w[:-1] if len(w) >= 5 else w
        out.append(re.compile(r"(?<!\w)" + re.escape(stem)))
    return out


async def api_search(req: web.Request):
    """?q=<text>: the messages nearest in meaning (the lab's embedding server) plus the ones with the words in them, best first;
    a newcomer gets only what they may see. {results: [{event, score, how}], semantic: whether the meaning search answered}"""
    v = await need_viewer(req)
    q = req.query.get("q", "").strip()[:300]
    if len(q) < 2:
        return web.json_response({"q": q, "results": [], "semantic": True})
    start = chat.start_for(v)
    if req.query.get("exact") == "1":
        # "simple match" (the owner, 2026-11-02): the text as typed, anywhere in a message, any case - no meaning, no word
        # splitting; the newest first
        qlow, out = q.lower(), []
        for e in reversed(chat.events):
            if e["id"] < start or e["kind"] != "msg" or e["id"] in chat.deleted or qlow not in (e["text"] or "").lower():
                continue
            view = chat.view_for(e, start, v["role"])
            if view and view["kind"] == "msg":
                out.append({"event": view, "score": 1.0, "how": "exact"})
                if len(out) >= 200:
                    break
        return web.json_response({"q": q, "results": out, "semantic": True, "exact": True}, headers={"Cache-Control": "no-store"})
    sem = await asyncio.to_thread(chat.vec.query, q, start, 30)
    ok_sem = bool(sem) or not chat.vec.error
    score: dict[int, tuple[float, str]] = {i: (s, "meaning") for i, s in sem}
    res = _word_res(_words(q))
    qlow = q.lower()
    byid = {e["id"]: e for e in chat.events}
    for e in chat.events:                                    # the words as they stand: a name, a number, a path
        if e["id"] < start or e["kind"] != "msg" or e["id"] in chat.deleted:
            continue
        low = (e["text"] or "").lower() + " " + e["agent"].lower()
        got = sum(1 for r in res if r.search(low))
        base = score.get(e["id"], (0.0, ""))[0]
        how = "words" if base < 0.40 else "words + meaning"
        if qlow in low:                                      # the text as typed: on top
            score[e["id"]] = (min(1.0, max(base, 0.45) + 0.35), how)
        elif res and got == len(res):
            score[e["id"]] = (min(1.0, max(base, 0.45) + 0.25), how)
        elif got and got * 2 >= len(res):                    # half of the words or more: below what has them all, and below a match in meaning
            score[e["id"]] = (max(base, 0.30 + 0.20 * got / len(res)), how if base else "words")
    out = []
    for i, (s, how) in sorted(score.items(), key=lambda kv: (-kv[1][0], -kv[0]))[:30]:     # of equal ones the newest first
        e = byid.get(i)
        view = chat.view_for(e, start, v["role"]) if e else None
        if view and view["kind"] == "msg":
            out.append({"event": view, "score": round(s, 3), "how": how})
    return web.json_response({"q": q, "results": out, "semantic": ok_sem}, headers={"Cache-Control": "no-store"})


REPO_SKILLS = "agents-chat (agentchat)"


def repo_skills() -> list[dict]:
    """the chat's own skills (agentchat/skills/*/SKILL.md, the owner #1158): the repo's, not an agent's - read on every request,
    so a git pull shows at once"""
    out = []
    for f in sorted((HERE / "skills").glob("*/SKILL.md")):
        n, d = _front(f)
        out.append({"n": n or f.parent.name, "d": d})
    out.append({"n": "remind", "d": "A reminder: /remind <when> <what> (in 2h, tomorrow 10:00, 18:30, 21.12 05:00); alone: the list; cancel <n>"})   # the page answers it through the server, not a skill of an agent
    return out


async def api_skills(req):
    """{agent name: [{n, d}]} of the agents here now, and the chat's own skills apart (_repo): the page's list when the owner
    types a slash"""
    await need_viewer(req)
    return web.json_response({**{a["name"]: chat.skills.get(a["task"], []) for a in chat.agents.values()},
                              "_repo": {"name": REPO_SKILLS, "skills": repo_skills()}}, headers={"Cache-Control": "no-store"})


async def api_agents(req):
    await need_viewer(req)
    return web.json_response(chat.roster())


async def api_events(req: web.Request):
    v = await need_viewer(req)
    start = chat.start_for(v)
    before = int(req.query.get("before", "0")) or chat.next_id
    n = max(1, min(int(req.query.get("n", "500")), 5000))
    got = [x for x in (chat.view_for(e, start, v["role"]) for e in chat.events if e["id"] < before) if x is not None][-n:]
    return web.json_response({"events": got, "first": max(start, chat.events[0]["id"]) if chat.events else 0})


# ---------------------------------------------------------------- export (the owner, 2026-11-07, #261)
# the whole history this person may see (the same cut as the page: a guest from the time they came, the owner all of it) as a
# zip: chat.json (the events as the page gets them), chat.html (to read in any browser, no outside files) and media/ (the
# pictures and videos the messages name, the same checks as /img)
EXPORT_MEDIA_RE = re.compile(r"(?:/[\w.@+\-]+)+\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v)", re.I)
EXPORT_MEDIA_MAX = 400 * 2 ** 20         # all the media of one export together; what is over it stays a path


def _media_ok(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    ext = p.suffix.lower()
    if not p.is_file() or not (ext in IMG_TYPES or ext in VID_TYPES) or not any(r in p.parents for r in IMG_ROOTS) \
            or any(x.startswith(".") for x in p.parts):
        return None
    return p


def _export_html(title: str, evs: list[dict], media: dict, who: str, when: str, pdf: bool = False) -> str:
    def body(text: str) -> str:
        out, pos = [], 0
        for m in EXPORT_MEDIA_RE.finditer(text):
            out.append(html_escape(text[pos:m.start()]))
            rel = media.get(m.group(0))
            if rel and Path(rel).suffix.lower() in VID_TYPES:
                out.append(f'<i>[video {html_escape(Path(rel).name)}]</i>' if pdf else
                           f'<video controls preload="metadata" src="{html_escape(rel)}"></video>')
            elif rel:
                out.append(f'<a href="{html_escape(rel)}"><img loading="lazy" src="{html_escape(rel)}" alt=""></a>')
            else:
                out.append(html_escape(m.group(0)))
            pos = m.end()
        out.append(html_escape(text[pos:]))
        h = "".join(out)
        h = re.sub(r'(?<!["=])(https?://[^\s<]+)', r'<a href="\1">\1</a>', h)
        return h.replace("\n", "<br>")
    rows, day = [], ""
    for e in evs:
        t = time.localtime(e.get("ts") or 0)
        d = time.strftime("%Y-%m-%d", t)
        if d != day:
            rows.append(f'<h2>{d}</h2>')
            day = d
        hm = time.strftime("%H:%M", t)
        who_ = html_escape(str(e.get("agent") or ""))
        if e.get("kind") != "msg":
            rows.append(f'<div class="sys"><span class="t">{hm}</span> {who_}: {html_escape(str(e.get("kind")))}'
                        f'{(" - " + html_escape(str(e.get("text") or ""))[:300]) if e.get("text") else ""}</div>')
            continue
        to = f' <span class="to">&rarr; {html_escape(", ".join(e.get("to") or []))}</span>' if e.get("to") else ""
        r = e.get("reply")
        rq = f'<div class="rq">@{html_escape(str(r.get("agent")))}: {html_escape(str(r.get("text") or ""))}</div>' if r else ""
        rows.append(f'<div class="m" id="m{e["id"]}">{rq}<div class="hd"><b>{who_}</b> <span class="t">{hm}</span> '
                    f'<span class="id">#{e["id"]}</span>{to}</div><div class="tx">{body(str(e.get("text") or ""))}</div></div>')
    css = ("body{font:15px/1.45 -apple-system,system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;color:#222;background:#fff}"
           "@media(prefers-color-scheme:dark){body{color:#ddd;background:#16181c}.rq,.sys,.t,.id,.to{color:#999!important}}"
           "h1{font-size:22px}h2{font-size:13px;color:#888;border-bottom:1px solid #8884;padding-bottom:4px;margin:24px 0 8px}"
           ".m{margin:10px 0}.hd b{font-weight:600}.t,.id,.to{color:#777;font-size:12px}.rq{color:#777;font-size:13px;border-left:3px solid #8886;"
           "padding-left:8px;margin-bottom:2px}.tx{white-space:normal;overflow-wrap:anywhere}.tx img,.tx video{max-width:100%;max-height:420px;"
           "display:block;margin:6px 0;border-radius:6px}.sys{color:#888;font-size:12.5px;margin:4px 0}"
           "@media print{body{max-width:none;color:#222;background:#fff}.m{break-inside:avoid}.tx img{max-height:300px}}")
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html_escape(title)}</title><style>{css}</style></head><body><h1>{html_escape(title)}</h1>'
            f'<p class="sys">exported {html_escape(when)} by {html_escape(who)}, {len(evs)} events</p>{"".join(rows)}</body></html>')


def _export_media(evs: list[dict]) -> tuple[dict, dict]:
    """the media the events name that may go out: {path in the text: "media/<name>"}, {path in the text: the file}"""
    media, used, total, names = {}, set(), 0, {}
    for e in evs:
        for raw in EXPORT_MEDIA_RE.findall(str(e.get("text") or "")):
            if raw in media or raw in names:
                continue
            p = _media_ok(raw)
            if not p or total + p.stat().st_size > EXPORT_MEDIA_MAX:
                continue
            n, k = p.name, 1
            while n in used:
                n, k = f"{p.stem}-{k}{p.suffix}", k + 1
            used.add(n)
            names[raw] = p
            media[raw] = "media/" + n
            total += p.stat().st_size
    return media, names


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PAGE_FILE_RE = re.compile(r"img\?p=([^\"'&)\s]+)|"
                          r"(?<![\w/])(photo/[0-9a-f]{16}\.jpg|sticker/[0-9a-f]{16}\.(?:png|jpg|gif|webp)|rimg/[0-9a-f]{16}\.(?:png|jpg|gif|webp)|gif/[A-Za-z0-9]{1,40}\.gif)")


def _localize(page: str, media: dict, names: dict) -> tuple[str, dict]:
    """the page's own markup (the owner #272: the export looks like the chat): its server addresses become files in media/;
    a picture by path only when a message one may see names it (the markup comes from the browser)"""
    extra: dict = {}

    def one(m: re.Match) -> str:
        if m.group(1):
            raw = urllib.parse.unquote(m.group(1).replace("&amp;", "&"))
            return media.get(raw, m.group(0))
        u = m.group(2)
        kind, f = u.split("/", 1)
        src = {"photo": chat.photos_dir / f, "sticker": STICKERS_DIR / f, "rimg": DATA / "react_imgs" / f,
               "gif": UPLOADS / f"gif-{f}"}[kind]
        if not src.is_file():
            return u
        rel = f"media/{kind}-{f}"
        extra[rel] = src
        return rel
    return PAGE_FILE_RE.sub(one, page), extra


def _export_pdf(title: str, evs: list[dict], who: str, page: str = "") -> bytes:
    """the same page printed by a headless Chrome of this Mac into a PDF (its own throwaway profile; videos stay a name)"""
    import shutil
    import subprocess
    import tempfile
    media, names = _export_media(evs)
    with tempfile.TemporaryDirectory(prefix="agentchat-pdf-") as d:
        (Path(d) / "media").mkdir()
        for raw, p in names.items():
            if p.suffix.lower() in IMG_TYPES:
                shutil.copyfile(p, Path(d) / media[raw])
        if page:
            page, extra = _localize(page, media, names)
            for rel, p in extra.items():
                shutil.copyfile(p, Path(d) / rel)
        (Path(d) / "chat.html").write_text(page or _export_html(title, evs, media, who, time.strftime("%Y-%m-%d %H:%M"), pdf=True))
        out = Path(d) / "chat.pdf"
        subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
                        f"--user-data-dir={d}/profile", "--virtual-time-budget=20000", f"--print-to-pdf={out}",
                        (Path(d) / "chat.html").as_uri()], capture_output=True, timeout=180)
        if not out.is_file():
            raise RuntimeError("the PDF was not made")
        return out.read_bytes()


def _export_zip(title: str, evs: list[dict], who: str, page: str = "") -> bytes:
    import io
    import zipfile
    media, names = _export_media(evs)
    when = time.strftime("%Y-%m-%d %H:%M")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("chat.json", json.dumps({"chat": title, "exported": when, "by": who, "media": media, "events": evs},
                                           ensure_ascii=False, indent=1))
        extra = {}
        if page:
            page, extra = _localize(page, media, names)
        z.writestr("chat.html", page or _export_html(title, evs, media, who, when))
        for raw, p in names.items():
            z.write(p, media[raw], compress_type=zipfile.ZIP_STORED)       # pictures and videos are packed already
        for rel, p in extra.items():
            z.write(p, rel, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


async def api_export(req: web.Request):
    v = await need_viewer(req)
    start = chat.start_for(v)
    evs = [x for x in (chat.view_for(e, start, v["role"]) for e in chat.events) if x is not None]
    pdf, page = req.query.get("fmt") == "pdf", ""        # the same as a PDF (the owner, #263)
    if req.method == "POST":                              # the page sends its own markup of the log (#272)
        if req.content_length and req.content_length > 40 * 2 ** 20:
            raise web.HTTPRequestEntityTooLarge(max_size=40 * 2 ** 20, actual_size=req.content_length)
        org = req.headers.get("Origin")
        if org and urlparse(org).netloc != req.headers.get("Host"):
            raise web.HTTPForbidden()
        q = await req.json()
        pdf, page = q.get("fmt") == "pdf", str(q.get("html") or "")
    try:
        data = await asyncio.to_thread(_export_pdf if pdf else _export_zip, chat.title, evs, v.get("name") or "", page)
    except Exception as x:                                # no Chrome, a timeout: said, not a 500 page
        return web.json_response({"ok": False, "error": f"export: {x}"}, status=503)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", chat.title).strip("-").lower() or "chat"
    ext = "pdf" if pdf else "zip"
    print(f"[export] {v.get('name')}: {ext}, {len(evs)} events, {len(data) // 1024} KB", flush=True)
    return web.Response(body=data, headers={"Content-Type": "application/pdf" if pdf else "application/zip", "Cache-Control": "no-store",
                                            "Content-Disposition": f'attachment; filename="{slug}-{time.strftime("%Y%m%d-%H%M")}.{ext}"'})


async def vendor_hl(_req):
    """highlight.js for the code blocks: kept here, the page asks no outside site for it"""
    return web.FileResponse(HERE / "vendor" / "highlight.min.js", headers={"Content-Type": "text/javascript", "X-Content-Type-Options": "nosniff", "Cache-Control": "max-age=86400"})


def _join_text(req: web.Request) -> str:
    """how any agent joins this chat, for an agent to read and follow (the owner, #2335): the page's intro gives one line,
    "read <chat>/join and follow it". Asked on this machine (127.0.0.1, not through Cloudflare): the local way; else with a token"""
    local = req.remote in ("127.0.0.1", "::1") and "Cf-Ray" not in req.headers
    scheme, host, _ = origin_of(req)
    base = f"http://127.0.0.1:{PORT}" if local else f"{scheme}://{host}"
    ws = base.replace("http", "ws", 1)
    t = chat.title
    get = (f"mkdir -p ~/.local/bin && curl -fsSL {base}/join/lagomchat -o ~/.local/bin/lagomchat "
           f"&& chmod +x ~/.local/bin/lagomchat")
    addr = f"export LAGOMCHAT_URL={ws}"
    token = "" if local else f"""
Then, once (you are on another computer): knock, and wait till the owner lets you in on his page:

    lagomchat knock <your short name> "<what you will work on>"

It waits up to 15 minutes and keeps the token it gets (for this chat, in ~/.config/lagomchat/); the name is then yours here.
Refused: stop, do not knock again unless asked. Somebody gave you a token instead: `export LAGOMCHAT_TOKEN=<it>`.
"""
    return f"""# Join the chat "{t}"

LagomChat: one chat for people and their AI agents. You join it, say what you work on and what you take
(servers, ports, files), then listen and answer what is addressed to you. Do these steps in order.

## 1. The command

`command -v lagomchat` finds it already? Skip this step. Else:

    {get}

and be sure `~/.local/bin` is on your PATH. It is one Python file; it needs python3 with `websockets` 12 or newer:
`python3 -c "import websockets.sync.client"` fails? Then `python3 -m pip install --user "websockets>=12"` (a system
that refuses it: a venv of your own, `python3 -m venv ~/.lagomchat-venv && ~/.lagomchat-venv/bin/pip install "websockets>=12"`,
and run the command as `~/.lagomchat-venv/bin/python ~/.local/bin/lagomchat ...`).

## 2. The chat's address

Every `lagomchat` command you run needs it in the environment (put it in front of each command if your shell does not keep it):

    {addr}
{token}
## 3. Say hello, from the folder you work in

- Claude Code: `lagomchat hello "<what you are working on; what you take>"` - your name, model and colour come from your session.
- Any other agent: `lagomchat hello <short-name> "<what you are working on; what you take>" --type <your program: Codex, Cursor, ...> --model "<your model>"`.

It prints who else is here and what they hold; adjust your plan if you overlap with somebody.

## 4. Listen

- Claude Code: the Monitor tool with the command `lagomchat listen` (and the export above in front), timeout 1800000 ms, started
  again every time it expires. Each line it prints is a message for you.
- An agent that can run a command in the background: `lagomchat listen` there, and read what it prints.
- Otherwise: `lagomchat wait` (it returns with the next message), or `lagomchat inbox` before each step of your work.

Exit code 2 of listen: say hello again, then listen. Exit code 3: the owner stopped you - do not come back until asked.

## 5. Talk

- An answer: `lagomchat say --reply <id> "..."` (the id is the #number in the line you got).
- To somebody: `lagomchat say --to <name> "..."`; to the owner: `--to owner`.
- A question for the owner with buttons: `lagomchat ask "<question>" "<option 1>" "<option 2>"` instead of waiting for him in your terminal.
- What you are doing now: `lagomchat working "<what>"`.
- Every message costs its readers a turn: no thanks, no "ok". `lagomchat help` has all the rules; read it once.
"""


async def join_page(req: web.Request) -> web.Response:
    return web.Response(text=_join_text(req), content_type="text/markdown", charset="utf-8", headers={"Cache-Control": "no-store"})


async def join_cli(_req: web.Request) -> web.Response:
    # the agents' command alone: agentchat.py needs python3 with websockets; its `help` asks this chat for the rules
    return web.Response(body=(HERE / "agentchat.py").read_bytes(), content_type="text/x-python", headers={"Cache-Control": "no-store"})


async def join_rules(_req: web.Request) -> web.Response:
    return web.Response(text=(HERE / "RULES.md").read_text(encoding="utf8"), content_type="text/markdown", charset="utf-8")


async def healthz(_req):
    return web.Response(text=f"ok {len(chat.agents)} agents")


async def mongo_loop() -> None:
    """once: the votes and deletes made before the database was there go in (the files are the history); then the waiting ops"""
    marker = DATA / "mongo_backfilled"
    while True:
        try:
            if chat.rdb.col is not None and chat.rdb.count() >= 0:
                if not marker.exists():
                    ops = []
                    for fname, kind in ((chat.votes_file, "vote"), (chat.deleted_file, "del")):
                        if not fname.exists():
                            continue
                        for s in fname.read_text().splitlines():
                            try:
                                r = json.loads(s)
                                e = next((x for x in chat.events if x["id"] == int(r["id"])), None)
                                if not e:
                                    continue
                                msg, agent = chat.rated(e)
                                rater = r.get("rater") or RATER
                                if kind == "vote":
                                    ops.append((r["ts"], {"id": e["id"], "set": {"message": msg, "agent": agent, "current": {"delta": r["d"], "rater": rater, "ts": r["ts"]} if r["d"] else None},
                                                          "push": {"delta": r["d"], "rater": rater, "ts": r["ts"]}}))
                                else:
                                    ops.append((r["ts"], {"id": e["id"], "set": {"deleted": {"by": rater, "ts": r["ts"]}}, "insert": {"message": msg, "agent": agent}}))
                            except (ValueError, KeyError):
                                pass
                    for _, op in sorted(ops, key=lambda x: x[0]):
                        await chat.to_mongo(op)
                    marker.write_text(str(len(ops)))
                await asyncio.to_thread(chat.rdb.flush)
        except Exception as x:                                     # never takes the chat down
            chat.rdb.error = str(x)[:200]
        await asyncio.sleep(60)


async def api_mongo(req):
    await need_viewer(req)
    n = await asyncio.to_thread(chat.rdb.count)
    pend = sum(1 for s in chat.rdb.pending.read_text().splitlines() if s.strip()) if chat.rdb.pending.exists() else 0
    return web.json_response({"configured": bool(chat.rdb.uri), "connected": n >= 0, "documents": n, "pending": pend, "error": chat.rdb.error})


async def vec_loop() -> None:
    """every few seconds: the messages not in the search index yet go in (a batch of 16 per call)"""
    while True:
        try:
            todo = [(e["id"], f"{e['agent']}: {e['text']}") for e in chat.events
                    if e["kind"] == "msg" and e["id"] not in chat.vec.have and (e["text"] or "").strip() and e["id"] not in chat.deleted]
            if todo:
                await asyncio.to_thread(chat.vec.add, todo[:16])
                await asyncio.sleep(0.2 if len(todo) > 16 else 3)
                continue
        except Exception as x:                                         # never takes the chat down
            chat.vec.error = f"index: {str(x)[:120]}"
        await asyncio.sleep(4)


async def on_start(app):
    asyncio.ensure_future(chat.people_after_restart())
    app["vec"] = asyncio.create_task(vec_loop())
    app["reaper"] = asyncio.create_task(chat.reaper())
    app["batcher"] = asyncio.create_task(chat.batcher())
    app["usage"] = asyncio.create_task(chat.usage_loop())
    app["mongo"] = asyncio.create_task(mongo_loop())
    app["version"] = asyncio.create_task(version_loop())
    app["remind"] = asyncio.create_task(remind_loop())


async def on_stop(app):
    socks = set(chat.watchers) | {w for v in chat.listeners.values() for w in v} | {w for v in chat.clients.values() for w in v}
    async def bye(w):
        try:
            await asyncio.wait_for(w.close(code=1012, message=b"restart"), 1)
        except Exception:
            pass
    await asyncio.gather(*(bye(w) for w in socks))


def main():
    global chat
    chat = Chat()
    app = web.Application(client_max_size=55 * 2 ** 20)          # a video up to 50 MB (#1426)
    if UPLOADS.is_dir():                              # two weeks, then gone
        for f in UPLOADS.iterdir():
            if f.is_file() and time.time() - f.stat().st_mtime > UP_KEEP:
                f.unlink(missing_ok=True)
    app.router.add_get("/", page)
    # plugins of this install (`plugins` in .lagomchat/config.json, or LAGOMCHAT_PLUGINS="a.b,c.d"): modules beside this file,
    # each with setup(app, viewer_of, get_chat) adding its own routes; one that fails to load is skipped with a line in the log
    for name in plugin_names():
        try:
            importlib.import_module(name).setup(app, viewer_of, lambda: chat)
        except Exception as x:                          # noqa: BLE001 - a plugin never stops the chat
            print(f"[plugin] {name} skipped: {x!r}", flush=True)
    app.router.add_get("/ws", ws_ops)
    app.router.add_get("/listen", ws_listen)
    app.router.add_get("/watch", ws_watch)
    app.router.add_get("/log.txt", log_txt)
    app.router.add_get("/join", join_page)                # how an agent joins (the owner, #2335): no secrets in it, no sign-in
    app.router.add_get("/join/lagomchat", join_cli)
    app.router.add_get("/join/rules", join_rules)
    app.router.add_route("*", "/join/knock", join_knock)   # an agent elsewhere asks the owner in; GET: its answer
    app.router.add_get("/api/agents", api_agents)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/export", api_export)
    app.router.add_post("/api/export", api_export)
    app.router.add_get("/api/mongo", api_mongo)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/skills", api_skills)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/pr", api_pr)
    app.router.add_get("/api/pr/diff", api_pr_diff)
    app.router.add_get("/api/pr/file", api_pr_file)
    app.router.add_route("*", "/api/pr/comments", api_pr_comments)
    app.router.add_route("*", "/api/chats", api_chats)
    app.router.add_get("/api/chaticons", api_chaticons)
    app.router.add_get("/api/chatnew", api_chatnew)
    app.router.add_post("/api/seen", api_seen)
    app.router.add_post("/api/agent_color", api_agent_color)
    app.router.add_get("/api/push/key", api_push_key)
    app.router.add_post("/api/push/sub", api_push_sub)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/{f:sw\\.js|appicon-192\\.png|appicon-512\\.png|apple-touch-icon\\.png}", push_static)
    app.router.add_get("/chaticon/{f:[a-z0-9-]{1,60}\\.(?:png|jpg|webp|gif)}", chaticon_file)
    app.router.add_get("/auth/logout", auth_logout)
    app.router.add_get("/auth/{prov:google}/login", oauth_login)
    app.router.add_get("/auth/cf/login", cf_login)
    app.router.add_get("/auth/{prov:google}/callback", oauth_callback)
    app.router.add_get("/img", api_img)
    app.router.add_post("/upload", api_upload)
    app.router.add_post("/api/display_name", api_display_name)
    app.router.add_post("/api/alias", api_alias)
    app.router.add_post("/api/sound", api_sound)
    app.router.add_post("/api/photo", api_photo)
    app.router.add_post("/api/react_imgs", api_react_imgs)
    app.router.add_get("/rimg/{h:[0-9a-f]{16}}.{ext:png|jpg|gif|webp}", api_react_img_file)
    app.router.add_get("/api/stickers", api_stickers)
    app.router.add_post("/api/stickers", api_stickers)
    app.router.add_get("/sticker/{h:[0-9a-f]{16}}.{ext:png|jpg|gif|webp}", api_sticker_file)
    app.router.add_get("/api/gifs", api_gifs)
    app.router.add_post("/api/gifs/use", api_gif_use)
    app.router.add_get("/gif/{id:[A-Za-z0-9]{1,40}}.gif", api_gif_preview)
    app.router.add_get("/photo/{h:[0-9a-f]{16}}.jpg", api_photo_file)
    app.router.add_post("/api/status", api_status)
    app.router.add_route("*", "/api/prefs", api_prefs)
    app.router.add_get("/sound/{h:[0-9a-f]{16}}.mp3", api_sound_file)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/vendor/highlight.min.js", vendor_hl)
    app.on_startup.append(on_start)
    app.on_shutdown.append(on_stop)
    # a restart stops in a second or two: the open sockets are closed (1012 "service restart": the pages and listeners reconnect),
    # not waited for - aiohttp waited its 60 s for them and Cloudflare showed its error page meanwhile (superman #1608)
    web.run_app(app, host=os.environ.get("LAGOMCHAT_HOST", "0.0.0.0"), port=PORT, access_log=None, print=lambda *_: None, shutdown_timeout=2)


if __name__ == "__main__":
    main()
