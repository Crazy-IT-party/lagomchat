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
    /log.txt     the log as text; /api/agents; /api/events?before=<order key>&n=<count> (after=, to=, around=); /healthz
"""
from __future__ import annotations

from html import escape as html_escape
import asyncio
import base64
import bisect
import contextlib
import fcntl
import hashlib
import importlib
from email.utils import formatdate, parsedate_to_datetime
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import tempfile
import urllib.parse
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, WSMsgType, web
from aiohttp.resolver import ThreadedResolver

# the environment's names are LAGOMCHAT_*; the older AGENTCHAT_* ones still work (the owner #2302)
for _k, _v in list(os.environ.items()):
    if _k.startswith("AGENTCHAT_"):
        os.environ.setdefault("LAGOMCHAT_" + _k[len("AGENTCHAT_"):], _v)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from agentchat import GH, PR_FIELDS, _front, claude_usage, frame, line, pr_summary, proc_start  # noqa: E402
from ratings_db import RatingsDB  # noqa: E402
import auth  # noqa: E402
import tgimport  # noqa: E402
import imimport  # noqa: E402
from vecsearch import VecIndex  # noqa: E402
from imgdesc import Down as VisionDown, ImgDesc, KINDS as PIC_KINDS  # noqa: E402
import talks as talks_mod  # noqa: E402

PORT = int(os.environ.get("LAGOMCHAT_PORT", "8150"))
PIN_KINDS = ("msg", "hello", "bye", "gone", "rename", "remind", "pjoin", "pleft")   # what may be pinned: the lines of the log too (the owner, #3507)
PEOPLE_KINDS = ("pjoin", "pleft", "kstati", "remind")      # a person came / went, a reminder (kstati: the facts of old logs, not shown): lines of the log for the page only
PEOPLE_LEAVE_DELAY = float(os.environ.get("LAGOMCHAT_LEAVE_DELAY", "1800"))   # gone for 30 min before "left the chat": a look in and out writes nothing (the owner, #2090)
PEOPLE_QUIET = float(os.environ.get("LAGOMCHAT_PEOPLE_QUIET", "30"))
# agents talking among themselves: after this many of their messages in a row with no person's in between, the next one
# is held and the owner gets a Continue button; any person's message starts the count again; 0: no limit (the owner, #2285)
AGENT_TURNS = max(0, int(os.environ.get("LAGOMCHAT_AGENT_TURNS", "10") or 0))
AGENT_AGAIN = 300          # the chat's own agent that ended by itself (it could not join, its model fell) is started again after this long, not in a loop
PEOPLE_IDLE_HIDE = float(os.environ.get("LAGOMCHAT_IDLE_HIDE", "1800"))   # Away for this long more -> "left the chat", the card hidden (the owner, #1550)
DATA = Path(os.environ.get("LAGOMCHAT_DATA", Path.home() / ".local/share/agentchat"))
HOST = socket.gethostname()
NAME = re.compile(r"^[\w.-]{1,32}$")
PNAME = re.compile(r"^[\w.-]{1,32}(#[0-9a-f]{4})?$")   # a person's name: with the tag of one whose name another account shares (#2897)
USER = {"name": "owner", "task": "user"}   # the person at the page: not an agent, never on the roster
RATER = {"id": "owner", "name": "owner"}   # the owner as a rater (kept with every vote); a verified person rates as {id: account id, name}
SCORE_RIVAL = next(iter(auth.conf_lines("score_rival")), "")   # <conf>/score_rival: whom the owner's score is one above (#647); no file: the real score
USER_NAMES_CONF = frozenset({"user", "owner", *(x.lower() for x in auth.conf_lines("reserved_names"))})
USER_NAMES = set(USER_NAMES_CONF)    # what an agent may call him (--to owner, --to user, and <conf>/reserved_names); no agent may take them
SILENT = 3 * 3600          # no listener and nothing said for this long: gone
ALL_RE = re.compile(r"(?<![\w@])@all(?![\w-])", re.I)     # @all: everybody (the owner, #1683)
THUMB_UP, THUMB_DOWN = "\U0001F44D", "\U0001F44E"   # a reaction of these is a vote, and a vote is a reaction (the owner, #1974)
HERE_GRACE = 180.0          # s: a person back within this is the same stay (a reload, a redeploy of the chat)
DEPUTY = {"superman"}      # the owner's deputy in the chat (the owner, 2026-10-31): one at a time, its word is the owner's for housekeeping
PIC_RE = re.compile(r"(?:/[\w.\-]+)+\.(?:png|jpe?g|gif|webp)", re.I)     # a picture's path in a message


def _install_file(name: str) -> Path | None:
    """a file of the whole install (.lagomchat/<name> of the working copy): one setting for every chat; None for a chat run by
    hand with no install around it"""
    try:
        import lagomcfg
        return lagomcfg.CFG_DIR / name
    except Exception:
        return None


REPLAY = 50                # at most this many missed events replayed to a listener
# what reaches an agent's listener and when (token economy, the owner 2026-10-29: every delivery is a turn of that agent):
#   wake  - to it, @it, a reply to it, anything from the owner: sent after WAKE_DELAY s (a burst goes as one)
#   soon  - the others' broadcasts, hellos and byes: ride along with the next delivery, or go after SOON_DELAY s
#   quiet - votes on its messages: ride along, or go after QUIET_DELAY s as one digest; a thumb down wakes (the owner, #1030)
WAKE_DELAY, SOON_DELAY, QUIET_DELAY = (float(os.environ.get(k, d)) for k, d in
                                       (("LAGOMCHAT_WAKE", 3), ("LAGOMCHAT_SOON", 45), ("LAGOMCHAT_QUIET", 900)))
HELLO_RECENT, CLIP = 8, 240   # a hello shows the last 8 messages, each cut at 240 chars
HELLO_EXAMPLES, HANDOFF_MAX = 3, 4000   # a hello also shows this many examples of each kind, and the note the last agent of that name left


# the chat's order is the order of time (the owner, #3883): a history imported into a chat that already had messages takes the
# next ids, yet it was written before them. Such a message gets an order key `o` of its own, between the keys of the messages
# around it in time; every other message's key is its id. The log, the pages and the history's pages go by the key; the ids stay
def okey(e: dict) -> float:
    return e.get("o", e["id"])


def import_order(old: list[dict], new: list[dict]) -> dict[int, float]:
    """the order keys of imported messages put among the log's by their time: {id: key}, only for those whose key is not their id.
    `old` is the log in its order (its times rise with it); `new` the imported messages, with their ids in the log"""
    ts = [x["ts"] for x in old]
    at: dict[int, list[dict]] = {}
    for e in sorted(new, key=lambda x: (x["ts"], x["id"])):
        at.setdefault(bisect.bisect_right(ts, e["ts"]), []).append(e)
    out = {}
    for p, g in at.items():
        lo, hi, k = okey(old[p - 1]) if p else None, okey(old[p]) if p < len(old) else None, len(g)
        for j, e in enumerate(g):
            if hi is None:                                # after everything: its own id, when that is after the last one
                o = e["id"] if lo is None or e["id"] > lo else lo + (j + 1) / (k + 1)
            elif lo is None:                              # before everything: the keys under the first one
                o = hi - k + j
            else:                                         # between two messages: keys between theirs
                o = lo + (hi - lo) * (j + 1) / (k + 1)
            if o != e["id"]:
                out[e["id"]] = int(o) if o == int(o) else o
    return out


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
        self.own = None                            # the chat's own agent (#4001): the process of autoagent.py while it runs
        self.own_task = ""                         # ... its task code (the session's id, given to it)
        self.own_wait = 0.0                        # ... not started before this time (AGENT_AGAIN after one that ended by itself)
        self.own_word = ""                         # ... what it was last told: stay / leave
        self.own_out = False                       # ... the owner stopped it or sent it out: not started again until everybody has left
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
        self.cal_file = DATA / "calendar.json"                 # the chat's calendar (the owner, #2704): events, who is in, who is told when
        try:
            self.cal: list[dict] = json.loads(self.cal_file.read_text())
        except (OSError, ValueError):
            self.cal = []
        self.surveys_file = DATA / "surveys.json"              # the questionnaires (the owner, #3498): a question of his, an answer of every person
        try:
            self.surveys: list[dict] = json.loads(self.surveys_file.read_text())
        except (OSError, ValueError):
            self.surveys = []
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
        # scratchpads (the owner, #3741): a message with a canvas the people draw on together - {message id: [stroke]}, a stroke
        # {"k": the key its page gave it, "n": who drew it, "c": "#rrggbb", "w": its width in thousandths of the canvas's width,
        # "p": [x, y, x, y, ...] in 0..10000 of the canvas}; a file each in pads/, read when first asked for, written a moment
        # after the drawing stops
        self.pad_dir = DATA / "pads"
        self.pads: dict = {}
        self.pad_dirty: set = set()
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
        if (self.dnames.get(USER["task"]) or {}).get("tag"):              # the owner's own @tag (#2871) is one of his names
            USER_NAMES.add(str(self.dnames[USER["task"]]["tag"]).lower())
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
        self.trust_file = DATA / "trust.json"                 # the owner's trust in each person, 0-10 (#2726): only the owner's page gets it
        try:
            self.trust: dict[str, int] = {k: int(v) for k, v in json.loads(self.trust_file.read_text()).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            self.trust = {}
        self.tzlast_file = DATA / "tzlast.json"               # the last time zone of each one who shows it: the clocks keep those not here now (#2710)
        try:
            self.tzlast: dict[str, dict] = json.loads(self.tzlast_file.read_text())
        except (OSError, ValueError):
            self.tzlast = {}
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
        # what is on the pictures (imgdesc.py; the owner, #3177): a model on the lab's GPU describes them, the search reads that
        self.img = ImgDesc(DATA, _install_file("vision_url"))
        self.pvec = VecIndex(DATA, "pvec")             # the pictures' descriptions by meaning, by the id of their message
        self.pics: dict[int, list[str]] = {}           # message id -> the pictures it names
        self.pic_ids: dict[str, set[int]] = {}         # picture -> the messages that name it
        self.pic_text: dict[int, str] = {}             # message id -> what the search reads of its pictures
        # the chat as talks, and a kind for every message (talks.py; the owner, #3186): the same model reads them
        self.talks = talks_mod.Talks(DATA, self.img.url)
        self.tvec = VecIndex(DATA, "tvec")             # the talks' titles and summaries by meaning, by their first message's id
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
        # what a voice message made by live captions says, in other languages (the owner, #3617): message id -> {language: text}
        self.cctr_file = DATA / "cc_trans.jsonl"
        self.cctr: dict[int, dict[str, str]] = {}
        if self.cctr_file.exists():
            for s_ in self.cctr_file.read_text().splitlines():
                try:
                    r_ = json.loads(s_)
                    self.cctr.setdefault(int(r_["id"]), {})[str(r_["lang"])] = str(r_["text"])
                except (ValueError, KeyError):
                    pass
        TR.fallback = self.img.url
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
        # a person changes their own message (the owner, #2930): chat.jsonl keeps it as it was sent, edits.jsonl every change of it;
        # the newest text is the one shown, with the time of the change
        self.edits_file = DATA / "edits.jsonl"
        if self.edits_file.exists():
            byid = {e["id"]: e for e in self.events}
            for s_ in self.edits_file.read_text().splitlines():
                try:
                    r_ = json.loads(s_)
                    if int(r_["id"]) in byid:
                        byid[int(r_["id"])]["text"], byid[int(r_["id"])]["edited"] = str(r_["text"]), float(r_["ts"])
                except (ValueError, KeyError, TypeError):
                    pass
        # who has seen a message (the owner, #2930): message id -> {the person's id: when}. The page tells when a message stood on
        # the person's screen for two seconds; one view per person. Kept here, and in MongoDB when one is set up (ratings_db.py)
        self.views_file = DATA / "views.jsonl"
        self.views: dict[int, dict[str, float]] = {}
        if self.views_file.exists():
            for s_ in self.views_file.read_text().splitlines():
                try:
                    r_ = json.loads(s_)
                    self.views.setdefault(int(r_["id"]), {})[str(r_["v"])] = float(r_["ts"])
                except (ValueError, KeyError, TypeError):
                    pass
        self.agent_run, self.agent_paused = 0, False   # agents' messages in a row with no person's (#2285)
        self.order_load()
        self.next_id = max(e["id"] for e in self.events) + 1 if self.events else 1
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
                              "since": e["id"], **{k: e.get(k) for k in ("pid", "start", "host", "cwd", "program", "model", "effort", "color")}}
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
            "listening": bool(self.listeners.get(a["task"])), "working": a.get("working"), "waiting": a.get("waiting"), "turn_end": a.get("turn_end"), "busy": a.get("busy"), "status": a.get("status"), "prog": a.get("prog"),
            "stats": self.stats.get(a["task"]), "usage": a.get("usage"), "strikes": len(self.strikes.get("agent:" + a["task"], [])),
            "program": a.get("program"), "model": a.get("model"), "effort": a.get("effort"), "color": a.get("color"), "git": a.get("git"),
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

    def pins_view(self) -> list[dict]:
        """the pins as the page shows them, the newest pin last: the message's author and the start of its text. For everybody,
        also the ones let in after the message was written (the owner, #3584): see `sees`"""
        byid = {e["id"]: e for e in self.events}
        out = []
        for p in self.pins:
            e = byid.get(p["id"])
            if e and e["id"] not in self.deleted:
                out.append({**p, "agent": e["agent"], "ts_msg": e["ts"], "text": " ".join((e["text"] or "").split())[:1500],
                            **({k: e[k] for k in ("kind", "sv", "cal") if e.get(k)} if e["kind"] != "msg" else {})})   # (the page draws one line: long enough for the links of its videos, #2041)
        return out

    async def tell_pins(self) -> None:
        """the pins to every open page, with the first message that page may load now (a pin may be older than its person)"""
        pins = self.pins_view()
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner"}
            await send(ws, json.dumps({"type": "pins", "pins": pins, "first": self.first_for(0 if me["role"] == "owner" else self.start_for(me))}, ensure_ascii=False))

    async def set_pin(self, mid: int, on: bool, by: str) -> str:
        """pin / unpin a message; '' or why not"""
        e = next((x for x in self.events if x["id"] == mid), None)
        if on and (not e or e["kind"] not in PIN_KINDS or mid in self.deleted):
            return f"no message #{mid}"
        had = any(p["id"] == mid for p in self.pins)
        if on == had:
            return ""
        self.pins = [p for p in self.pins if p["id"] != mid] + ([{"id": mid, "by": by, "ts": round(time.time(), 1)}] if on else [])
        self.pins_file.write_text(json.dumps(self.pins, ensure_ascii=False))
        await self.tell_pins()
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
            return {"id": e["id"], "ts": e["ts"], "kind": "del", "agent": e["agent"], "task": e["task"], "text": "", **({"o": e["o"]} if "o" in e else {})}
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

    def sees(self, mid: int, start: int) -> bool:
        """whether a person let in at `start` may see the message `mid`: what was written since, and what is pinned - a pin is
        the owner's word that the message is for everybody in the chat (the owner, #3584)"""
        return mid >= start or any(p["id"] == mid for p in self.pins)

    async def tell_seers(self, mid: int, m: dict) -> None:
        """a frame about the message `mid`: to the pages of those who may see it"""
        s = json.dumps(m, ensure_ascii=False)
        for ws in list(self.watchers):
            me = self.viewers.get(ws) or {"role": "owner"}
            if me["role"] == "owner" or self.sees(mid, self.start_for(me)):
                await send(ws, s)

    def first_for(self, start: int) -> int:
        """the order key of the first event such a person may load: where they came, or an older pinned message"""
        if not self.events:
            return 0
        have = {p["id"] for p in self.pins} - self.deleted
        e = next((x for x in self.events if x["id"] >= start or x["id"] in have), None)
        return okey(e) if e else max(start, okey(self.events[-1]) + 1)

    def view_for(self, e: dict, start: int, role: str) -> dict | None:
        """an event as this person sees it: None when it is from before they came (or a vote line, the owner's business); a quote of
        an earlier message and a note about one say only that it is earlier"""
        if not self.sees(e["id"], start):
            return None
        e = self.public(e)
        if role == "owner":
            return e
        if e["kind"] == "vote":
            return None
        if e.get("ref") is not None and not self.sees(e["ref"], start):
            e = dict(e, text="(about a message from before you came)")
        r = e.get("reply")
        if r and not self.sees(r["id"], start):
            e = dict(e, reply={"id": r["id"], "agent": r["agent"], "text": "(a message from before you came)"})
        return e

    def pics_of(self, e: dict) -> list[str]:
        """note the pictures a message names (again after an edit); returns the ones not looked at by the model yet"""
        i = e["id"]
        for p in self.pics.pop(i, ()):
            self.pic_ids.get(p, set()).discard(i)
        self.pic_text.pop(i, None)
        if e["kind"] != "msg" or i in self.deleted:
            return []
        ps = list(dict.fromkeys(PIC_RE.findall(e.get("text") or "")))[:20]
        if not ps:
            return []
        self.pics[i] = ps
        for p in ps:
            self.pic_ids.setdefault(p, set()).add(i)
        self.pic_said(i)
        return [p for p in ps if p not in self.img.d]

    def pic_said(self, i: int) -> None:
        """what the search reads of a message's pictures, from what the model has said so far"""
        t = " ".join(x for x in (self.img.text(p) for p in self.pics.get(i, ())) if x)
        if t:
            self.pic_text[i] = t
        else:
            self.pic_text.pop(i, None)

    def pic_views(self, text: str) -> dict:
        """{path: {en, ru, text, kind, tags}} of the pictures a text names: the page shows it under a found picture"""
        out = {}
        for p in PIC_RE.findall(text or "") if self.img.d else ():
            r = self.img.get(p)
            if r:
                out[p] = {k: r[k] for k in ("en", "ru", "text", "kind", "tags")}
        return out

    def visible_paths(self, start: int) -> set[str]:
        """the picture paths named in the events a person may see"""
        out: set[str] = set()
        for e in self.events:
            v = self.view_for(e, start, "verified")                  # what they see: not the quoted start of an earlier message
            if v:
                out.update(MEDIA_PATH_RE.findall(v.get("text") or ""))
                if e.get("pad"):                                     # the pictures put on a scratchpad (#3751)
                    out.update(o["src"] for o in self.pad_of(e["id"])["o"] if o.get("src"))
        return out

    def order_load(self) -> None:
        """the order keys of imported messages (order.json, {id: key}); made once from the imports' maps for a chat imported into
        before the keys were (#3883). The log is put in the order of the keys"""
        self.order_file = DATA / "order.json"
        try:
            self.order = {int(k): v for k, v in json.loads(self.order_file.read_text()).items()}
        except (OSError, ValueError, AttributeError):
            imp = set()
            for f in DATA.glob("import_*.json"):
                try:
                    imp.update(int(v) for v in json.loads(f.read_text()).values())
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            self.order = import_order([e for e in self.events if e["id"] not in imp], [e for e in self.events if e["id"] in imp]) if imp else {}
            if imp:
                self.order_file.write_text(json.dumps(self.order))
        for e in self.events:
            if e["id"] in self.order:
                e["o"] = self.order[e["id"]]
        if self.order:
            self.events.sort(key=okey)

    def take_in(self, evs: list[dict], reacts: dict) -> int:
        """the messages of an import (tgimport.py; their ids count from 0) join the log under the chat's next ids, with the time
        they were sent at. No listener is woken by them: the agents' place in the log moves past. Returns the first id"""
        base = self.next_id
        for e in evs:
            e["id"] += base
            if e.get("reply"):
                e["reply"]["id"] += base
        od = import_order(self.events, evs)
        with self.jsonl.open("a") as f, self.txt.open("a") as g:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
                g.write(line(e) + "\n")
        self.events.extend(evs)
        if od:                                             # older than what the chat has: among it by time (#3883)
            self.order.update(od)
            self.order_file.write_text(json.dumps(self.order))
            for e in evs:
                if e["id"] in od:
                    e["o"] = od[e["id"]]
            self.events.sort(key=okey)
        self.next_id = base + len(evs)
        self.next_id_file.write_text(str(self.next_id))
        if reacts:
            self.reacts.update({base + int(i): r for i, r in reacts.items()})
            self.reacts_file.write_text(json.dumps(self.reacts, ensure_ascii=False))
        for t in self.delivered:
            self.delivered[t] = self.next_id - 1
        return base

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
        await self.tell_seers(mid, {"type": "react", "id": mid, "r": r})

    async def poll_vote(self, mid: int, who: str, i: int, start: int = 0) -> str | None:
        """`who` picks option i of a poll, or takes the pick back (a click on it again); one pick, or several in a poll of several
        answers; everybody's pages see the counts at once (the owner, #1386)"""
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or not e.get("poll") or mid in self.deleted or not self.sees(mid, start):
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
            if me["role"] == "owner" or self.sees(mid, self.start_for(me)):
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

    def pad_of(self, mid: int) -> dict:
        """the scratchpad `mid` (read from its file the first time): its strokes `s` and its objects `o` - the pictures and
        shapes put on it (#3751), each moved, turned and sized as a whole. A file of the first days holds only the strokes"""
        if mid not in self.pads:
            try:
                d = json.loads((self.pad_dir / f"{mid}.json").read_text())
            except (OSError, ValueError):
                d = None
            if isinstance(d, list):
                d = {"s": d, "o": []}
            self.pads[mid] = d if isinstance(d, dict) and isinstance(d.get("s"), list) and isinstance(d.get("o"), list) else {"s": [], "o": []}
        return self.pads[mid]

    async def pad_save_later(self) -> None:
        await asyncio.sleep(2)
        self.pad_dir.mkdir(exist_ok=True)
        for mid in list(self.pad_dirty):
            self.pad_dirty.discard(mid)
            if mid not in self.pads:                      # dropped with its message meanwhile
                continue
            tmp = self.pad_dir / f"{mid}.json.tmp"
            try:
                tmp.write_text(json.dumps(self.pads[mid], separators=(",", ":")))
                tmp.replace(self.pad_dir / f"{mid}.json")
            except OSError as x:                          # a full disk: tried again later, not given up on (audit)
                print(f"[pad] {mid} not saved: {x}", flush=True)
                self.pad_dirty.add(mid)                   # not empty now: pad_changed waits for this retry
                asyncio.get_running_loop().call_later(30, lambda: asyncio.ensure_future(self.pad_save_later()))
                return

    def pad_drop(self, ids) -> None:
        """the scratchpads of messages deleted or purged go with them, from the memory and the disk"""
        for i in ids:
            self.pads.pop(i, None)
            self.pad_dirty.discard(i)
            with contextlib.suppress(OSError):
                (self.pad_dir / f"{i}.json").unlink()

    def pad_changed(self, mid: int) -> None:
        if not self.pad_dirty:
            asyncio.ensure_future(self.pad_save_later())
        self.pad_dirty.add(mid)

    async def pad_draw(self, d: dict, me: dict, start: int) -> str | None:
        """a page's piece of drawing on a scratchpad (#3741): points added to its stroke (a new key starts one; `e` = an eraser's), or its own last
        stroke taken back (undo), or the whole pad wiped (clear: its author or the owner); every page that sees the message
        gets the change at once"""
        mid = _int(d.get("id"))
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or not e.get("pad") or mid in self.deleted or not self.sees(mid, start):
            return "no such scratchpad"
        name, pad = me["name"], self.pad_of(mid)
        s = pad["s"]
        if d.get("clear"):
            if me["role"] != "owner" and e.get("agent") != name:
                return "only who made the scratchpad, or the owner, wipes it"
            s.clear()
            pad["o"].clear()
            self.pad_changed(mid)
            await self.tell_seers(mid, {"type": "pad", "id": mid, "clear": 1, "n": name})
            return None
        if "obj" in d or "del" in d:
            return await self.pad_obj(mid, d, name, pad["o"])
        if isinstance(d.get("objs"), list):                 # several picked ones moved or recoloured together (#3758)
            for q in d["objs"][:200]:
                if bad := await self.pad_obj(mid, {"obj": q}, name, pad["o"]):
                    return bad
            return None
        if isinstance(d.get("sp"), list):                   # drawn lines picked and moved / turned / sized / recoloured (#3769)
            for q in d["sp"][:500]:
                if bad := await self.pad_line(mid, q, name, s):
                    return bad
            return None
        if isinstance(d.get("sdel"), dict):                 # a picked drawn line taken off, whoever drew it
            k, n = str(d["sdel"].get("k")), str(d["sdel"].get("n"))
            i = next((i for i, x in enumerate(s) if x["k"] == k and x["n"] == n), None)
            if i is not None:
                s.pop(i)
                self.pad_changed(mid)
                await self.tell_seers(mid, {"type": "pad", "id": mid, "undo": k, "n": n})
            return None
        if d.get("undo"):
            i = next((i for i in range(len(s) - 1, -1, -1) if s[i]["n"] == name), None)
            if i is not None:
                k = s.pop(i)["k"]
                self.pad_changed(mid)
                await self.tell_seers(mid, {"type": "pad", "id": mid, "undo": k, "n": name})
            return None
        k, c, p = str(d.get("k") or "")[:40], str(d.get("c") or ""), d.get("p")
        if not k or not re.fullmatch(r"#[0-9a-fA-F]{6}", c) or not isinstance(p, list) or not p or len(p) % 2 or len(p) > 800:
            return "a stroke: k, c (#rrggbb), p (x, y, ... in 0..10000)"
        try:
            p = [max(0, min(10000, int(v))) for v in p]
            w = max(1, min(40, int(d.get("w") or 5)))
        except (TypeError, ValueError, OverflowError):
            return "bad points"
        st = next((x for x in reversed(s[-60:]) if x["k"] == k and x["n"] == name), None)
        if st is None:
            if len(s) >= 5000:
                return "the scratchpad is full: wipe it, or start another"
            st = {"k": k, "n": name, "c": c.lower(), "w": w, "p": []}
            if d.get("e"):
                st["e"] = 1                               # the eraser (#3749): the stroke wipes what is under it
            s.append(st)
        if len(st["p"]) + len(p) > 20000:
            return "the stroke is too long"
        st["p"] += p
        self.pad_changed(mid)
        await self.tell_seers(mid, {"type": "pad", "id": mid, "k": k, "n": name, "c": st["c"], "w": st["w"], "e": st.get("e", 0), "p": p})
        return None

    PAD_SHAPES = ("img", "rect", "ellipse", "line", "arrow", "text")

    async def pad_line(self, mid: int, q, name: str, s: list) -> str | None:
        """a drawn line of a scratchpad changed as a whole (#3769): its points `p` all anew (moved, turned or sized on a page; they
        may lie off the pad), its colour `c` or width `w`. Anybody who draws may; one gone meanwhile is let be"""
        if not isinstance(q, dict):
            return "sp: [{k, n, p, c, w}]"
        k, n = str(q.get("k") or ""), str(q.get("n") or "")
        st = next((x for x in s if x["k"] == k and x["n"] == n and not x.get("e")), None)
        if st is None:
            return None
        out = {"k": k, "n": n}
        try:
            if q.get("p") is not None:
                p = q["p"]
                if not isinstance(p, list) or not p or len(p) % 2 or len(p) > 20000:
                    return "sp: p (x, y, ...)"
                st["p"] = out["p"] = [max(-5000, min(15000, int(v))) for v in p]
            if q.get("w") is not None:
                st["w"] = out["w"] = max(1, min(40, int(q["w"])))
        except (TypeError, ValueError, OverflowError):
            return "bad numbers"
        if re.fullmatch(r"#[0-9a-fA-F]{6}", str(q.get("c") or "")):
            st["c"] = out["c"] = q["c"].lower()
        self.pad_changed(mid)
        await self.tell_seers(mid, {"type": "pad", "id": mid, "sp": out, "n": name})
        return None

    async def pad_obj(self, mid: int, d: dict, name: str, objs: list) -> str | None:
        """a picture, a shape or a text on a scratchpad (#3751, #3758): put on (a new key, with its kind `t`; a picture names an
        uploaded file in `src`, a text holds its words in `s`), moved / turned / sized / recoloured / a text rewritten (the same
        key: x, y, w, h, r, c, sw, s - its kind and picture stay), or taken off (`del`). A text is `w` wide, its letters `h` high.
        Anybody who draws may move what is there. x, y: its centre, 0..10000 of the pad's width and height; w, h: its size in
        10000ths of the width; r: degrees"""
        if "del" in d:
            k = str(d.get("del"))
            if any(o["k"] == k for o in objs):
                objs[:] = [o for o in objs if o["k"] != k]
                self.pad_changed(mid)
                await self.tell_seers(mid, {"type": "pad", "id": mid, "del": k, "n": name})
            return None
        q = d.get("obj")
        if not isinstance(q, dict):
            return "obj: {k, t, x, y, w, h, r, c, sw, src}"
        k = str(q.get("k") or "")[:40]
        o = next((x for x in objs if x["k"] == k), None)
        if not k:
            return "obj: k"
        try:
            nums = {f: max(lo, min(hi, int(q[f]))) for f, lo, hi in
                    (("x", -5000, 15000), ("y", -5000, 15000), ("w", 0, 20000), ("h", 0, 20000), ("sw", 1, 40)) if q.get(f) is not None}
            if q.get("r") is not None:
                nums["r"] = round(max(-360.0, min(360.0, float(q["r"]))), 1)   # (a NaN comes out as 360: a number, not a crash)
        except (TypeError, ValueError, OverflowError):
            return "bad numbers"
        if o is None:
            t, c, src = q.get("t"), str(q.get("c") or "#222222"), str(q.get("src") or "")
            if t not in self.PAD_SHAPES or not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
                return f"obj: t one of {', '.join(self.PAD_SHAPES)}, c #rrggbb"
            if t == "img":
                p = Path(src[:400])
                if not (p.parent == UPLOADS and p.suffix.lower() in IMG_TYPES and p.is_file()):
                    return "obj: src, a picture uploaded to this chat"
            if len(objs) >= 200:
                return "the scratchpad has 200 pictures and shapes: take some off"
            o = {"k": k, "n": name, "t": t, "c": c.lower(), "x": 5000, "y": 5000, "w": 3000, "h": 3000, "r": 0.0, "sw": 5}
            if t == "img":
                o["src"] = str(p)
            objs.append(o)
        if o["t"] == "text" and q.get("s") is not None:
            o["s"] = str(q["s"])[:500]
        o.update(nums)
        if re.fullmatch(r"#[0-9a-fA-F]{6}", str(q.get("c") or "")):   # a shape's colour changed (a new one has it already)
            o["c"] = q["c"].lower()
        self.pad_changed(mid)
        await self.tell_seers(mid, {"type": "pad", "id": mid, "obj": o, "n": name})
        return None

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
        if not e or e["kind"] != "msg" or d not in (-1, 0, 1) or mid in self.deleted or not self.sees(mid, start):
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
            if me["role"] != "owner" and not self.sees(mid, self.start_for(me)):
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
        await self.tell_seers(mid, {"type": "trans", "id": mid, **r})
        return {"ok": True}

    def cap_langs(self) -> set:
        """the languages the pages showing live captions now read them in"""
        return {v["capdst"] for v in self.viewers.values() if v and v.get("caps") and v.get("capdst")}

    async def cap_tr(self, name: str, seg: str, text: str, src: str) -> None:
        """a finished phrase of live captions, in the languages asked for: to every page, not kept"""
        for dst in sorted(self.cap_langs() - {src})[:4]:
            out = await TR.tr(text, src, dst, live=True)
            await self.watch_all({"type": "captr", "name": name, "seg": seg, "lang": dst, **({"text": out} if out else {"err": True})})

    async def cc_tr(self, e: dict, src: str, first: str) -> None:
        """the words of a voice message made by live captions, in the language its speaker asked for and in those the
        captions are read in now: kept, and shown under the message"""
        words = e["text"].split("\n", 1)[1].strip()
        for dst in [x for x in dict.fromkeys([first, *sorted(self.cap_langs())]) if x in CC_LANGS and x != src][:4]:
            out = await TR.tr(words, src, dst)
            if not out:
                if first == dst:                         # the speaker's own page is told: its menu says the translator is silent
                    await self.watch_all({"type": "captr", "name": e["agent"], "seg": "", "lang": dst, "err": True})
                continue
            if out.casefold() == words.casefold() or e["id"] in self.deleted:
                continue
            self.cctr.setdefault(e["id"], {})[dst] = out
            with self.cctr_file.open("a") as f:
                f.write(json.dumps({"id": e["id"], "lang": dst, "text": out}, ensure_ascii=False) + "\n")
            await self.tell_seers(e["id"], {"type": "cctr", "id": e["id"], "lang": dst, "text": out})

    def cctr_drop(self, ids) -> None:
        """what purged or deleted speech said in other languages goes with it"""
        if not any(i in self.cctr for i in ids):
            return
        for i in ids:
            self.cctr.pop(i, None)
        tmp = self.cctr_file.with_name(self.cctr_file.name + ".tmp")
        tmp.write_text("".join(json.dumps({"id": i, "lang": k, "text": t}, ensure_ascii=False) + "\n" for i, d in self.cctr.items() for k, t in d.items()))
        os.replace(tmp, self.cctr_file)

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

    async def edit(self, mid: int, text: str, me: dict) -> str:
        """a person changes the text of their own message (the owner, #2930): the pages show the new text marked as edited.
        Returns what is wrong, or "" """
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or e["kind"] != "msg" or mid in self.deleted or me["role"] not in ("owner", "verified") or e["task"] != me["task"]:
            return "only your own message can be edited"
        if e.get("poll") or e.get("doc") or e.get("sh"):
            return "this message cannot be edited"
        text = text.strip()[:8000]
        if not text or text == e["text"]:
            return ""
        now = round(time.time(), 3)
        with self.edits_file.open("a") as f:
            f.write(json.dumps({"id": mid, "ts": now, "text": text, "was": e["text"]}, ensure_ascii=False) + "\n")
        e["text"], e["edited"] = text, now
        await asyncio.to_thread(self.vec.drop, {mid})            # the search by meaning takes the new text in
        await asyncio.to_thread(self.pvec.drop, {mid})
        await self.talks_forget({mid})
        self.pics_of(e)
        await self.tell_seers(mid, {"type": "edit", "id": mid, "text": text, "edited": now})
        print(f"[edit] #{mid} by {me.get('name')}", flush=True)
        return ""

    def views_key(self, mid: int) -> str:
        return f"{DATA.name}:{mid}"

    async def viewed(self, ids: list, me: dict, start: int) -> None:
        """messages that stood on this person's screen (the owner, #2930): one view per person and message"""
        rid, now, new = str(self.rater_of(me)["id"]), round(time.time(), 3), []
        want = {mid for mid in ids[:200] if isinstance(mid, int) and not isinstance(mid, bool) and self.sees(mid, start)
                and mid not in self.deleted and rid not in self.views.get(mid, ())}
        for e in self.events if want else ():             # not by a binary search: the log goes by time, not by id (#3883)
            if e["id"] in want and e["kind"] == "msg":
                self.views.setdefault(e["id"], {})[rid] = now
                new.append(e["id"])
        if not new:
            return
        with self.views_file.open("a") as f:
            f.write("".join(json.dumps({"id": mid, "v": rid, "ts": now}) + "\n" for mid in new))
        if self.rdb.uri:
            for mid in new:
                await self.to_mongo({"col": "views", "id": self.views_key(mid), "set": {"chat": DATA.name, "message": mid, "by." + rid.replace(".", "_"): now}})
        for ws in list(self.watchers):
            v = self.viewers.get(ws) or {"role": "owner", "id": ""}
            st = 0 if v["role"] == "owner" else self.start_for(v)
            cnt = {str(mid): len(self.views[mid]) for mid in new if self.sees(mid, st)}
            if cnt:
                await send(ws, json.dumps({"type": "views", "v": cnt, **({"mine": new} if str(self.rater_of(v)["id"]) == rid else {})}))

    async def delete(self, mid: int) -> None:
        """the owner takes a line out of the chat: anybody's message, his own, a line of the chat itself (joined, left, a reminder).
        Nothing is written about it (the owner, #3362): no word to the agents, and the page shows no mark where it was"""
        e = next((x for x in self.events if x["id"] == mid), None)
        if not e or mid in self.deleted:
            return
        self.deleted.add(mid)
        now = round(time.time(), 3)
        with self.deleted_file.open("a") as f:
            f.write(json.dumps({"id": mid, "ts": now, "rater": RATER}) + "\n")
        if e["kind"] == "msg" and not str(e["task"]).startswith("user"):       # the ratings' database is of the agents' messages
            msg, agent = self.rated(e)
            await self.to_mongo({"id": mid, "set": {"deleted": {"by": RATER, "ts": now}}, "insert": {"message": msg, "agent": agent}})
        await self.watch_all({"type": "del", "id": mid})
        self.cctr_drop({mid})
        self.pad_drop({mid})
        if e["kind"] == "msg":
            await self.talks_forget({mid})
        print(f"[del] #{mid} ({e['kind']} of {e.get('agent')})", flush=True)

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

    async def talks_forget(self, ids: set[int]) -> None:
        """a message purged, deleted or edited: the title and the summary written of its talk go too, and the talk is read anew"""
        gone = self.talks.drop(ids)
        if gone:
            await asyncio.to_thread(self.tvec.drop, gone)

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
        await asyncio.to_thread(self.pvec.drop, ids)
        await self.talks_forget(ids)
        for i in ids:
            self.views.pop(i, None)
            self.pic_text.pop(i, None)
            for p in self.pics.pop(i, ()):
                self.pic_ids.get(p, set()).discard(i)
        self.cctr_drop(ids)
        self.pad_drop(ids)
        await self.watch_all({"type": "purged", "ids": sorted(ids), "first": okey(self.events[0]) if self.events else 0})
        if any(p["id"] in ids for p in self.pins):
            self.pins = [p for p in self.pins if p["id"] not in ids]
            self.pins_file.write_text(json.dumps(self.pins, ensure_ascii=False))
            await self.tell_pins()
        print(f"[purge] {by}: {len(ids)} lines -> {arch}", flush=True)
        return str(arch)

    def hears(self, e: dict, task: str) -> bool:
        """an event the agent of `task` hears: not its own; a message to others only when it is one of them"""
        if e["task"] == task or e["id"] in self.deleted or e["kind"] in PEOPLE_KINDS or e.get("cc"):
            return False                                 # a person coming or going is for the page: it wakes no agent; nor does
                                                         # speech kept by live captions (#3617): a call is not a talk with the agents
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
            pass
        elif time.time() - self.started < PEOPLE_QUIET:  # the pages coming back after a restart, the first one with this code among them
            self.people_on[t] = me["name"]
        else:
            await self.post("pjoin", {"name": me["name"], "task": t}, "joined the chat")
        self.own_sync()

    # the chat's own agent (the owner, #4001): no session sits listening in an empty chat - a headless Claude Code (autoagent.py)
    # is started as superman when a person is here and told to leave when the log says everybody has left ("left the chat":
    # PEOPLE_LEAVE_DELAY after the last page went, or hidden by idleness). Set per chat: <conf>/agent.json, written from
    # chats.<slug>.agent of the config - {model, effort, mode, cwd, bin}, read at every start (a change needs no restart); no
    # model there = no agent. Another superman on the roster (a session of the owner's): none is started.
    def own_sync(self) -> None:
        p = self.own if self.own and self.own.returncode is None else None
        if not self.people_on:
            self.own_out = False
            self.own_tell(p, "leave")                  # it goes when the turn it is in is over
            return
        if p:
            self.own_tell(p, "stay")                   # somebody came back before it went
            return
        if self.own_out or time.time() < self.own_wait or not any(self.person_here(t) for t in self.people_on) \
                or any(a["name"].lower() in DEPUTY for a in self.agents.values()):
            return
        try:
            c = json.loads((auth.CONF / "agent.json").read_text())
        except (OSError, ValueError):
            return
        if isinstance(c, dict) and c.get("model"):
            self.own_wait = time.time() + AGENT_AGAIN
            asyncio.ensure_future(self.own_run(c))

    def own_tell(self, p, word: str) -> None:
        if p and self.own_word != word:
            self.own_word = word
            with contextlib.suppress(OSError, RuntimeError):
                p.stdin.write(f"{word}\n".encode())

    async def own_run(self, c: dict) -> None:
        self.own_task = str(uuid.uuid4())
        argv = [sys.executable, str(HERE / "autoagent.py"), "--url", f"ws://127.0.0.1:{PORT}", "--session", self.own_task,
                "--model", str(c["model"]), "--cwd", str(c.get("cwd") or HERE)]   # in the working copy unless told: where the owner's sessions work
        for k in ("effort", "mode", "bin"):
            if c.get(k):
                argv += [f"--{k}", str(c[k])]
        try:
            self.own = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE)
        except OSError as x:
            print(f"[agent] not started: {x}", flush=True)
            return
        self.own_word = "stay"
        print(f"[agent] started ({self.own.pid}): {' '.join(argv[6:])}", flush=True)
        code = await self.own.wait()
        if code == 0 or self.own_out:                    # it left as told, or by the owner's word: the next time one comes at once
            self.own_wait = 0.0
        print(f"[agent] ended ({code})", flush=True)
        self.own_sync()                                  # people still here: another one, when it may

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
        # a Claude Code session at work keeps its "working" through its progress notes: the end of its turn clears it (#3728)
        mid = bool(a2 and kind == "msg" and a2.get("busy") and self.listeners.get(a["task"]))
        if mid:
            a2["said"] = True
        cleared = bool(a2 and not mid and kind in ("msg", "bye") and a2.pop("working", None))
        await self.fanout(e)
        if kind in ("msg", "remind") and PUSH.subs and not e.get("cc"):
            asyncio.create_task(push_out(self, e))           # Web Push to the people whose pages are closed (the owner, #1445)
        if kind == "pleft":
            self.own_sync()                                    # the last one went: the chat's own agent goes too (#4001)
        if kind == "msg" and a["task"] not in self.agents:
            self.agent_run, self.agent_paused = 0, False         # a person spoke: the agents' count starts again (#2285)
        if kind == "msg" and a["task"] == "user":
            await self.clear_waiting(e)                          # the owner answered an agent that was waiting for him
            if (KNOCKS or GUESTS) and e.get("reply"):
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
                            **({"id": v["id"]} if with_ids and v["id"] else {}), **({"me": True} if key == mykey else {}),
                            **({"trust": self.trust[key]} if with_ids and key in self.trust else {})}
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
            if v.get("caps"):
                out[key]["caps"] = True                      # reads live captions (#2756)
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
            if isinstance(v.get("rtt"), int) and (out[key].get("rtt") is None or v["rtt"] < out[key]["rtt"]):
                out[key]["rtt"] = v["rtt"]               # the round trip of the person's best tab, ms (the owner, #2489)
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
                # by hand: away (a time, as it was kept before), or busy / sleep (the owner, #3020): {at, kind}
                m = self.away_manual[key]
                at, kind = (m.get("at", 0), m.get("kind", "away")) if isinstance(m, dict) else (m, "away")
                u.update(st=kind if kind in ("busy", "sleep") else "away", stby="manual", age=max(0.0, round(time.time() - at, 1)))
            elif sts and all(st == "auto" for st, _ in sts):
                u.update(st="away", stby="auto", age=max(0.0, round(time.time() - max(at for _, at in sts), 1)))
        return sorted(out.values(), key=lambda u: (u["role"] != "owner", u["name"].lower()))

    async def send_presence(self) -> None:
        people = access_people()
        for ws in list(self.watchers):
            me = self.viewers.get(ws)
            if me:
                await send(ws, json.dumps({"type": "presence", "users": self.presence(me["role"] == "owner", me),
                                           "people": people}, ensure_ascii=False))

    async def watch_all(self, m: dict) -> None:
        s = json.dumps(m, ensure_ascii=False)
        for ws in list(self.watchers):
            me = self.viewers.get(ws)
            if me and me["role"] != "owner" and m.get("type") in ("ack", "del"):
                if not self.sees(m.get("id", 0), self.start_for(me)):
                    continue
            await send(ws, s)

    async def agent_ctl(self, a: dict, act: str) -> None:
        """the owner's buttons on an agent's card (#2124): stop - its listener exits with code 3 and a line telling it not to
        start one again; leave - the same, and it is off the roster (a bye). The line is what a Claude Code Monitor shows it"""
        lines = {"stop": "[agentchat] the owner stopped your listening from the page: do not start `agentchat listen` again until he asks you to",
                 "leave": "[agentchat] the owner sent you out of the chat from the page: you are off the roster; do not hello or listen again until he asks you to"}
        print(f"[agent_ctl] {act} {a['name']} ({a['task'][:8]})", flush=True)
        if a["task"] == self.own_task:
            self.own_out = True                    # the chat's own agent: it goes, and is not started again until everybody has left (#4001)
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
            self.own_sync()                      # the chat's own agent ended by itself, or the other superman left: another one
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


EFFORTS = ("low", "medium", "high", "xhigh", "max")          # a session's effort, shown on its card (the owner, #3809)


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
                        effort=q.get("effort") if q.get("effort") in EFFORTS else None,
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
        magic = {**UP_MAGIC, ".mp4": None, ".m4v": None, ".mov": None, ".webm": b"\x1a\x45\xdf\xa3", ".pdf": b"%PDF-"}
        if ext not in magic:
            return {"ok": False, "error": "upload: png, jpeg, gif, webp, mp4, m4v, mov, webm or pdf only"}
        if len(data) > MAX_AGENT_UPLOAD:
            return {"ok": False, "error": f"upload: over {MAX_AGENT_UPLOAD >> 20} MB"}
        good = data[4:8] == b"ftyp" if magic[ext] is None else data.startswith(magic[ext]) and (ext != ".webp" or data[8:12] == b"WEBP")
        if not good:
            return {"ok": False, "error": f"upload: {name} is not a {ext[1:]} file"}
        UPLOADS.mkdir(parents=True, exist_ok=True)
        # 128 random bits in the name (the owner, #2538): the folder is one for all the chats of the machine, a name of the
        # second and 24 bits could be guessed by a person of another chat and named in a message there to be shown
        f = UPLOADS / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(16)}{ext}"
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
            allow_save(a)
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
    if kind == "pr_guide":
        # superman's guide for reviewing a pull request in the page's viewer (the owner, #3591): the order of the files and the
        # crucial changes; with no guide given: the one that is kept, for anybody
        key = str(q.get("pr") or "")
        if not PR_KEY_RE.fullmatch(key):
            return {"ok": False, "error": "which pull request: <owner>/<repo>#<number>"}
        if q.get("guide") is None and not q.get("remove"):
            return {"ok": True, "guide": PR_GUIDES.get(key)}
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "a pull request's review guide is superman's to give"}
        g, bad = (None, "") if q.get("remove") else pr_guide_clean(q.get("guide"))
        if bad:
            return {"ok": False, "error": bad}
        await pr_guide_set(key, g, a["name"])
        return {"ok": True, "guide": PR_GUIDES.get(key)}
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
        tr = {k: " ".join(str(v).split())[:60] for k, v in (q.get("tr") or {}).items() if k in ("en", "ru", "uk", "be", "cs", "de", "es", "sv", "fr", "it", "pl", "pt", "zh") and str(v).strip()}
        if not t and not tr:
            return {"ok": True, "title": chat.title, "tr": chat.title_tr}
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "the chat's name is superman's (the owner's deputy) to change"}
        await chat.set_title(t, tr, a["name"])
        return {"ok": True, "title": chat.title, "tr": chat.title_tr}
    if kind == "surveys":
        # the questionnaires with every answer (the owner, #3498): superman's only - he answers what he is asked with them in mind
        if a["name"].lower() not in DEPUTY:
            return {"ok": False, "error": "the questionnaires are superman's (the owner's deputy) to read"}
        ppl = {"owner": USER["name"], **access_people()}
        return {"ok": True, "surveys": [{"id": s["id"], "q": s["q"], "made": s["made"], "open": bool(s.get("open", True)), "who": _sv_who(s),
                                         "ans": {n: {"text": x["text"], "at": x["at"]} for n, x in s["ans"].items()}}
                                        for s in sorted(chat.surveys, key=lambda s: s["made"])], "people": ppl}
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
        return {"ok": True, "events": evs, "first": okey(chat.events[0]) if chat.events else 0}
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
        # With "busy" (#3728): true - a turn started that long ago, the card shows the session at work till it ends; false - it
        # ended. A turn that ended after a message of it takes its "working" along (its messages in the turn did not)
        try:
            at = round(time.time() - max(0.0, float(q.get("ago") or 0)), 3)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad ago"}
        if q.get("busy") is True:
            a["busy"] = at
        else:
            if "busy" not in q:
                a["turn_end"] = at
            a.pop("busy", None)
            if a.pop("said", None):
                a.pop("working", None)
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True}
    if kind == "meta":
        # the session's model (/model) or colour (/color) changed: the card and the name's colour on the page follow
        m = {}
        if q.get("model"):
            m["model"] = str(q["model"])[:60]
        if q.get("effort") in EFFORTS:                  # the session's effort (the owner, #3809)
            m["effort"] = q["effort"]
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
    if kind == "confirm":
        # a critical action waits for the owner's own yes (the owner, #3057): his page shows Confirm / Refuse; the pick is taken only
        # from a page whose sign-in is proven at the moment of the click (confirm_pick), and the answer carries `confirmed`, a field
        # no typed message can have. The agent asks the server itself what was picked: agentchat confirmed <id>
        if not text.strip():
            return {"ok": False, "error": 'agentchat confirm "<what exactly will be done>"'}
        e = await chat.post("msg", a, text[:1200], [USER["name"]], confirm=True)
        a["waiting"] = {"text": " ".join(text.split())[:140], "id": e["id"], "since": e["ts"]}
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True, "id": e["id"], "ttl": CONFIRM_TTL}
    if kind == "confirmed":
        try:
            cid = int(q.get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        qe = next((x for x in chat.events if x["id"] == cid and x.get("confirm")), None)
        if not qe or qe["task"] != a["task"]:
            return {"ok": False, "error": "no such question of yours"}
        st, ans = confirm_state(qe)
        return {"ok": True, "state": st, "text": qe["text"], "asked": qe["ts"], "at": ans["ts"] if ans else None, "answer": ans["id"] if ans else None}
    if kind == "card":
        # what a link's card says when the site tells a fetch nothing (the owner, #3068): a Claude artifact shows its title and
        # first paragraph only to the signed-in, so an agent that can read it gives them; an empty title takes the card's words away
        m = CA_RE.search(str(q.get("url") or ""))
        if not m:
            return {"ok": False, "error": 'agentchat card <https://claude.ai/artifact/...> "<title>" ["<the first paragraph>"]'}
        title, desc = " ".join(text.split())[:120], " ".join(str(q.get("desc") or "").split())[:400]
        if title:
            CARDS["ca:" + m.group(1)] = {"title": title, "desc": desc, "by": a["name"], "ts": round(time.time())}
        else:
            CARDS.pop("ca:" + m.group(1), None)
        while len(CARDS) > CARDS_MAX:                # every page gets them all: the oldest go
            CARDS.pop(min(CARDS, key=lambda k: CARDS[k].get("ts", 0)))
        CARDS_FILE.write_text(json.dumps(CARDS, ensure_ascii=False))
        await chat.watch_all({"type": "cards", "cards": CARDS})
        return {"ok": True, "n": len(CARDS)}
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
        a["status"], a["prog"] = s or None, status_prog(a.get("prog"), s, int(time.time()))
        chat.meta.setdefault(task, {}).update(status=a["status"], prog=a["prog"])
        chat.meta_file.write_text(json.dumps(chat.meta, ensure_ascii=False))
        await chat.watch_all({"type": "agents", "agents": chat.roster()})
        return {"ok": True, "status": s}
    if kind == "working":
        # what the agent is on now, short: the page shows it on the agent's card; its next message or bye clears it (a Claude Code
        # session's: the end of the turn in which it said something, #3728)
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
CA_RE = re.compile(r"https?://claude\.ai/(?:public/artifacts|artifact)/([\w-]{6,64})")
CARDS_FILE = DATA / "cards.json"           # {"ca:<artifact id>": {title, desc, by, ts}}: the words agents gave the cards
CARDS_MAX = 500
try:
    CARDS = {str(k): v for k, v in json.loads(CARDS_FILE.read_text()).items() if isinstance(v, dict)}
except (OSError, ValueError, AttributeError):
    CARDS = {}
CONFIRM_TTL = 900                          # a question for the owner's yes is good for 15 minutes


def confirm_state(qe: dict) -> tuple[str, dict | None]:
    """a confirm question's state: yes / no (the owner's pick, taken by confirm_pick only), expired, wait"""
    ans = next((x for x in chat.events if isinstance(x.get("confirmed"), dict) and x["confirmed"].get("id") == qe["id"]
                and x["task"] == USER["task"] and x["id"] not in chat.deleted), None)
    if ans:
        return ("yes" if ans["confirmed"].get("ok") is True else "no"), ans
    if qe["id"] in chat.deleted:
        return "no", None
    return ("expired" if time.time() - qe["ts"] > CONFIRM_TTL else "wait"), None


def proven_owner(v: dict) -> bool:
    """the owner by proof, not by default: sign-in is enforced, and Cloudflare Access signed this very email (a chat with the
    sign-in off makes everybody the owner: such a page confirms nothing)"""
    em = auth.owner_email()
    return bool(em and auth.enforced() and v.get("role") == "owner" and v.get("signed") and v.get("em") == em
                and v.get("id") and v["id"] == auth.owner_sid())


async def confirm_pick(req: web.Request, me: dict, cid, ok) -> str:
    """the owner's Confirm / Refuse on a question: who is at the page is asked again now (the token read anew, not the page's
    role from when it opened); one answer per question, in time. Returns what is wrong, or "" """
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return "no such question"
    if not isinstance(ok, bool):
        return "no such question"
    _CF_SEEN.pop(cf_token(req) or "", None)                  # not the answer of a minute ago
    if not proven_owner(await _viewer_of(req)):
        return "only the owner's signed-in page confirms"
    qe = next((x for x in chat.events if x["id"] == cid and x.get("confirm") and x["kind"] == "msg"), None)
    if not qe or cid in chat.deleted:
        return "no such question"
    st, _ = confirm_state(qe)
    if st != "wait":
        return "this question has expired" if st == "expired" else "this question is answered already"
    who = (chat.agents.get(qe["task"]) or {}).get("name") or qe["agent"]
    await chat.post("msg", {"name": me["name"], "task": USER["task"]}, "Confirmed" if ok else "Refused", [who], reply=chat.reply_ref(cid),
                    confirmed={"id": cid, "ok": ok})
    print(f"[confirm] #{cid} of {who}: {'yes' if ok else 'no'} by the owner@{me.get('ip')} via {me.get('via')}", flush=True)
    return ""


REMOTE_OPS = {"who", "remind", "log", "hello", "say", "doc", "upload", "turn", "meta", "bye", "inbox", "git", "usage", "ask", "confirm", "confirmed", "card", "poll", "pvote", "react",
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
    if e["text"] not in KNOCK_OPTS:
        return
    k = next((k for k, v in KNOCKS.items() if v["ask"] == rid and v["state"] == "wait"), None)
    if k:
        KNOCKS[k]["state"] = "in" if e["text"] == KNOCK_OPTS[0] else "out"
    em = next((k for k, v in GUESTS.items() if v["ask"] == rid and v["state"] == "wait"), None)
    if em:
        g = GUESTS[em]
        g["state"] = "in" if e["text"] == KNOCK_OPTS[0] else "out"
        if g["state"] == "in":
            a = auth.allow_list()
            a["google_emails"] = sorted({*a["google_emails"], em})
            allow_save(a)
            _ROLE_CACHE.clear()
            print(f"[allow] add email {em} (let in from the page)", flush=True)
            for ws in list(g["ws"]):                 # their waiting pages open the chat
                try:
                    await ws.send_str(json.dumps({"type": "gate", "state": "in"}))
                except (ConnectionError, RuntimeError):
                    pass
        g["ws"].clear()


def allow_save(a: dict) -> None:
    """the chat's list of who may in, and the same into the manager's config: it writes allow.json out of it at every save"""
    auth.save_allow(a)
    try:
        import lagomcfg
        c = lagomcfg.load()
        ch = next((x for x in c["chats"].values() if x.get("conf") and Path(x["conf"]).expanduser().resolve() == auth.CONF.resolve()), None)
        if ch is not None:
            ch["guests"] = {**(ch.get("guests") or {}), **a}
            lagomcfg.save(c)
    except (OSError, ValueError, KeyError) as x:
        print(f"[allow] not written to the manager's config: {x!r}", flush=True)


# A person who signed in and is not on the chat's list (the owner, #3341): the same question on the owner's page, once per email;
# "Let in" puts the email on the list (allow.json), so nobody is added at Cloudflare by hand. A refused one is not asked about again
GUESTS: dict[str, dict] = {}                      # email -> {ask, state: wait | in | out, at, ip, ws: the waiting pages}
GUEST_TTL = 7 * 86400


async def guest_knock(req: web.Request, me: dict, ws: web.WebSocketResponse) -> None:
    em = str(me.get("em") or "").lower()
    if not em or not auth.owner_email():
        return
    for k, v in list(GUESTS.items()):
        if time.time() - v["at"] > GUEST_TTL:
            del GUESTS[k]
    ip = where_from(req)[0]
    g = GUESTS.get(em)
    if not g:
        if sum(v["state"] == "wait" for v in GUESTS.values()) >= KNOCK_MAX or any(v["ip"] == ip and v["state"] != "in" for v in GUESTS.values()):
            return                                # many wait already, or this address asked under another email: no new question
        access_log(req, "guest-knock", em=em)
        g = GUESTS[em] = {"ask": 0, "state": "wait", "at": time.time(), "ip": ip, "ws": set()}
        e = await chat.post("msg", {"name": "LagomChat", "task": "knock"},
                            f"A person asks in: {me.get('acct_name') or em.split('@')[0]} <{em}>. Let them in? They will see the chat from the moment they come in.",
                            [USER["name"]], ask=KNOCK_OPTS)
        g["ask"] = e["id"]
    if g["state"] == "wait":
        g["ws"].add(ws)


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


def client_ip(req: web.Request) -> str:
    """the address a request came from. A proxy on this machine (cloudflared, ngrok, Caddy) adds the address it saw last to
    X-Forwarded-For: that one, not the ones before it and not CF-Connecting-IP, which anybody can send. A connection from
    elsewhere is its own address, whatever its headers say"""
    peer = req.remote or "?"
    fwd = (req.headers.get("X-Forwarded-For") or "").rsplit(",", 1)[-1].strip()
    return fwd[:64] if fwd and peer in ("127.0.0.1", "::1") else peer


def where_from(req: web.Request) -> tuple[str, str]:
    """the address and, when Cloudflare's visitor location headers are on, the city; no outside service gets the address"""
    ip = client_ip(req)
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
    out: set[str] = set()
    for e in chat.events:
        if e["id"] >= tok["since"] and e.get("task") != tok["task"]:
            out.update(MEDIA_PATH_RE.findall(e.get("text") or ""))
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


def _name_of(raw: str) -> str:
    return re.sub(r"[^\w.-]+", "_", raw or "").strip("_")[:28]


def human_name(sess: dict) -> str:
    n = _name_of(sess.get("name")) or "user" + sess["sid"][-6:]
    # two accounts let in whose names are one (anna@ of two mail services): each shows with the end of its own hash, anna#f2d4
    # (the owner, #2897); a name nobody shares stays as it is
    em = str(sess.get("em") or "").lower()
    if em and any(o != em and _name_of(o.split("@")[0]).lower() == n.lower() for o in auth.allow_list()["google_emails"]):
        n += "#" + hashlib.sha256(em.encode()).hexdigest()[-4:]
    return n if n.lower() not in USER_NAMES and n not in {a["name"] for a in chat.agents.values()} else n + "_h"


def access_people() -> dict[str, str]:
    """everyone let into the chat by email, on its page now or not: the name they go by here -> the display name they picked
    ('' with none). They are tagged and put into an event like the ones present (the owner, #2914). A whole domain let in has
    no list of its people; the owner is not in it: he is 'owner'"""
    out, own = {}, auth.owner_email()
    for em in auth.allow_list()["google_emails"]:
        if em == own:
            continue
        sess = {"sid": auth.email_sid(em), "name": em.split("@")[0], "em": em}
        out[human_name(sess)] = str(chat.dnames.get("user:" + sess["sid"], {}).get("name") or "")
    return out


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
    owner_em = auth.owner_email()
    mode = auth.mode()                                   # one source of the email per chat: another one's cookie or header counts for nothing
    sess = await email_session(req) if mode == "access" else auth.header_identity(req.remote or "", req.headers) if mode == "header" else session_of(req)
    role = "owner" if owner_em and sess and sess.get("em") == owner_em else await role_of(sess["sid"]) if sess else ""
    if role == "pending" and auth.allowed(sess):
        role = "verified"                                # on the allow-list (a Google domain or email; #1497)
    if role == "owner" or (not enforced and role in ("", "pending")):
        return {"role": "owner", "name": USER["name"], "id": auth.owner_sid() if role == "owner" else "", "em": (sess or {}).get("em", ""), "task": USER["task"],
                "avatar": (sess or {}).get("avatar", ""), "signed": bool(sess), "enforced": enforced, "acct_name": (sess or {}).get("name", "")}
    if role == "verified":
        return {"role": "verified", "name": human_name(sess), "id": sess["sid"], "em": sess.get("em", ""), "task": "user:" + sess["sid"], "avatar": sess.get("avatar", ""),
                "signed": True, "enforced": enforced, "acct_name": sess.get("name", "")}
    return {"role": role or "none", "name": (sess or {}).get("name", ""), "id": (sess or {}).get("sid", ""), "em": (sess or {}).get("em", ""), "task": "", "avatar": (sess or {}).get("avatar", ""),
            "signed": bool(sess), "enforced": enforced, "acct_name": (sess or {}).get("name", "")}


def viewer_key(v: dict) -> str:
    return str(v.get("id") or v.get("task") or "")


async def api_alias(req: web.Request) -> web.Response:
    """POST {target, name, av}: the viewer's own name / picture for somebody, seen on her pages only; both empty - gone"""
    v = await need_viewer(req)
    if _other_site(req):
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


# the owner's word for his role and his @tag (#2871), one for every chat of this machine (#2873): {"role": "", "tag": ""}
OWNER_FILE = Path(os.environ.get("LAGOMCHAT_SHARED_OWNER", Path.home() / ".config/agentchat-chats/owner.json"))
_owner_seen = [0.0]


def _people_names(but: str) -> set[str]:
    """every name a person other than the task `but` goes by here, lower case: on the page now, and the display names saved"""
    out = set()
    for o in chat.viewers.values():
        if o.get("task") != but:
            out |= {str(o.get("name") or "").lower(), str(o.get("acct_name") or "").lower()}
    for t, d in chat.dnames.items():
        if t != but:
            out |= {str(d.get("name") or "").lower(), str(d.get("who") or "").lower()}
    return out - {""}


def owner_sync() -> bool:
    """take the role and the tag from the shared file when it changed; True: this chat's names changed"""
    try:
        mt = OWNER_FILE.stat().st_mtime
        if mt == _owner_seen[0]:
            return False
        d = json.loads(OWNER_FILE.read_text())
        _owner_seen[0] = mt
    except (OSError, ValueError):
        return False
    role, tag = str(d.get("role") or "")[:24], str(d.get("tag") or "")
    tag = tag if NAME.match(tag) and tag.lower() not in {a["name"].lower() for a in chat.agents.values()} | _people_names(USER["task"]) else ""
    cur = dict(chat.dnames.get(USER["task"], {}))
    if (cur.get("role") or "") == role and (cur.get("tag") or "") == tag:
        return False
    old = str(cur.get("tag") or "").lower()
    if old and old not in USER_NAMES_CONF:
        USER_NAMES.discard(old)
    cur.update(role=role) if role else cur.pop("role", None)
    cur.update(tag=tag) if tag else cur.pop("tag", None)
    if tag:
        USER_NAMES.add(tag.lower())
    if cur:
        chat.dnames[USER["task"]] = cur
    else:
        chat.dnames.pop(USER["task"], None)
    chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
    return True


async def owner_loop():
    while True:
        try:
            if owner_sync():
                await chat.watch_all({"type": "names", "names": chat.dnames})
        except Exception as x:                                  # the loop outlives a bad file
            print(f"[owner] sync: {x}", flush=True)
        await asyncio.sleep(3)


async def api_display_name(req: web.Request) -> web.Response:
    """the person's display name on the page (the owner, 2026-11-02, #1235); empty = back to the account's own name"""
    v = await need_viewer(req)
    # only the page of this very host: its Origin, or (a browser that sends none) Sec-Fetch-Site same-origin; the host as the
    # browser used it - through a proxy X-Forwarded-Host (the owner, #1252: Save did nothing behind a proxy)
    org, host = req.headers.get("Origin"), req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    same = _same_page(req)
    print(f"[name] {v.get('task')}@{client_ip(req)}: origin={org} host={host} -> {'ok' if same else 'refused'}", flush=True)
    if not same:
        return web.json_response({"ok": False, "error": f"refused: the page's origin {org or '-'} is not {host}"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    if not isinstance(q, dict):
        q = {}
    cur = dict(chat.dnames.get(v["task"], {}))
    if "role" in q or "tag" in q:
        # the owner's own word for his role and for his @tag (the owner, #2871): "owner" on his card and "@owner" in the texts
        # show as he likes; the tag is one more name the agents and the people may call him by (--to <tag>, @<tag>)
        if v.get("role") != "owner" or v["task"] != USER["task"]:
            return web.json_response({"ok": False, "error": "the owner's setting"}, status=403)
        if "role" in q:
            r = " ".join(str(q.get("role") or "").split())[:24]
            if any(ord(c) < 32 for c in r):
                return web.json_response({"ok": False, "error": "no control characters"})
            cur.update(role=r) if r else cur.pop("role", None)
        if "tag" in q:
            t = str(q.get("tag") or "").strip().lstrip("@")
            if t and not NAME.match(t):
                return web.json_response({"ok": False, "error": "1-32 letters, digits, '.', '_', '-'"})
            if t and (t.lower() in {a["name"].lower() for a in chat.agents.values()} | DEPUTY | {"all"} or
                      t.lower() in _people_names(v["task"])):
                return web.json_response({"ok": False, "error": "that is somebody's name here"})
            old = str(cur.get("tag") or "").lower()
            if old and old not in USER_NAMES_CONF:
                USER_NAMES.discard(old)
            if t:
                cur.update(tag=t)
                USER_NAMES.add(t.lower())
            else:
                cur.pop("tag", None)
        if cur:
            chat.dnames[v["task"]] = {**cur, "who": v["name"]}
        else:
            chat.dnames.pop(v["task"], None)
        chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
        # one for every chat of this machine, the ones made later too (the owner, #2873): the others pick it up from the shared file
        OWNER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OWNER_FILE.with_name(f"{OWNER_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"role": cur.get("role", ""), "tag": cur.get("tag", "")}, ensure_ascii=False))
        tmp.replace(OWNER_FILE)
        await chat.watch_all({"type": "names", "names": chat.dnames})
        print(f"[owner] role={cur.get('role') or '(owner)'} tag={cur.get('tag') or '(owner)'}", flush=True)
        return web.json_response({"ok": True, "names": chat.dnames})
    if "yt" in q and "name" not in q and "color" not in q and "mono" not in q:
        # a YouTube video shown on my card while I am deafened (the owner, 2026-11-03, #1450): its id from a link or as it is
        m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", str(q.get("yt") or "")) or re.fullmatch(r"\s*([\w-]{11})\s*", str(q.get("yt") or ""))
        if m:
            cur.update(yt=m.group(1), who=v["name"], acct=v.get("acct_name") or cur.get("acct", ""))
        elif str(q.get("yt") or "").strip():
            return web.json_response({"ok": False, "error": "not a YouTube link"})
        else:
            cur.pop("yt", None)
        if any(cur.get(k) for k in ("name", "color", "mono", "yt", "role", "tag")):
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
        if any(cur.get(k) for k in ("name", "color", "mono", "yt", "role", "tag")):
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
    if n and v["task"] != USER["task"] and n.lower() in USER_NAMES:      # "owner", his @tag (#2871): nobody else shows under them
        return web.json_response({"ok": False, "error": "that is the owner's name"})
    if n and any(o.get("task") != v["task"] and n.lower() in (str(o.get("name") or "").lower(), str(o.get("acct_name") or "").lower())
                 for o in chat.viewers.values()):               # nor another person's own name (the owner, #2538: everyone picks one now)
        return web.json_response({"ok": False, "error": "somebody else shows under that name"})
    if n and any(t != v["task"] and str(d.get("name") or "").lower() == n.lower() for t, d in chat.dnames.items()):
        return web.json_response({"ok": False, "error": "somebody else shows under that name"})
    if n:
        chat.dnames[v["task"]] = {**cur, "name": n, "acct": v.get("acct_name") or "", "who": v["name"]}
    elif any(cur.get(k) for k in ("color", "mono", "yt", "role", "tag")):
        cur.pop("name", None)
        chat.dnames[v["task"]] = cur                        # the colour and the monogram stay
    else:
        chat.dnames.pop(v["task"], None)
    chat.dnames_file.write_text(json.dumps(chat.dnames, ensure_ascii=False))
    await chat.watch_all({"type": "names", "names": chat.dnames})
    print(f"[name] {v['task']}: {n or '(account name)'}", flush=True)
    return web.json_response({"ok": True, "names": chat.dnames})


_PR_CACHE: dict[str, tuple[float, dict]] = {}
_X_CACHE: dict[str, tuple[float, dict]] = {}
_OG_CACHE: dict[str, tuple[float, dict]] = {}
OG_UA = "Mozilla/5.0 (compatible; LagomChat link preview)"


async def _og_public(host: str) -> bool:
    """a link's host may be fetched only when every address it has is out on the internet: never this machine, its LAN, its tunnels"""
    import ipaddress
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    ips = {i[4][0].split("%")[0] for i in infos}
    return bool(ips) and all(_og_ip_ok(a) for a in ips)


def _og_ip_ok(a: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return False
    return ip.is_global and not (ip.version == 6 and ip in ipaddress.ip_network("64:ff9b::/96"))   # NAT64 reaches IPv4 inside


class _OgResolver(ThreadedResolver):
    """the addresses a link's fetch connects to are checked themselves (the audit before 0.2.0): a name that answered a public
    address to the check and this machine or the LAN to the connection (DNS rebinding) gets nothing"""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        res = await super().resolve(host, port, family)
        if not res or not all(_og_ip_ok(r["host"]) for r in res):
            raise OSError(f"not a public address: {host}")
        return res


_OG_SEM = asyncio.Semaphore(4)       # at most four fetches for cards at once, the chats' machine is no one's fetcher


async def _og_get(url: str, limit: int = 600_000) -> tuple[str, str] | None:
    """(final address, text) of a public page, redirects followed by hand so each hop is checked; None when it cannot be had"""
    from urllib.parse import urljoin, urlsplit
    async with _OG_SEM, ClientSession(timeout=ClientTimeout(total=10), connector=TCPConnector(resolver=_OgResolver(), use_dns_cache=False),
                                      headers={"User-Agent": OG_UA, "Accept-Language": "en,ru;q=0.8"}) as s:
        for _ in range(5):
            u = urlsplit(url)
            if u.scheme not in ("http", "https") or not u.hostname or not await _og_public(u.hostname):
                return None
            async with s.get(url, allow_redirects=False) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"]); continue
                if r.status != 200:
                    return None
                ct = r.headers.get("Content-Type", "")
                if "html" not in ct and "json" not in ct:
                    return None
                buf = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > limit:
                        break
                try:
                    return url, bytes(buf).decode(r.charset or "utf-8", "replace")
                except LookupError:                                  # a charset Python does not know
                    return url, bytes(buf).decode("utf-8", "replace")
    return None


# A picture of the page itself for a card whose page names none (the owner, #2923): made by a headless Chrome of the chat's
# machine. Chrome reaches the net only through a proxy of this process that lets a connection out to public addresses alone,
# as the cards' own fetch does: no page, redirect or script of it gets at this machine, its LAN or its tunnels.
SHOTS = DATA / "shots"
SHOT_KEEP = 7 * 86400
_SHOT_SEM = asyncio.Semaphore(1)
_SHOT_PROXY: dict = {}


def _chrome() -> str:
    for p in (os.environ.get("LAGOMCHAT_CHROME", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              *(shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "chrome-headless-shell"))):
        if p and os.access(p, os.X_OK):
            return p
    return ""


async def _shot_pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while chunk := await r.read(65536):
            w.write(chunk)
            await w.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        w.close()


async def _shot_proxy_conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    """one connection of Chrome: CONNECT host:port, or a plain http request; the host's addresses are checked, and the
    connection goes to the address that was checked"""
    up = None
    try:
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
        line = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(line) < 2:
            return
        if line[0] == "CONNECT":
            host, _, port = line[1].rpartition(":")
        else:
            u = urlparse(line[1])
            host, port = u.hostname or "", str(u.port or 80)
        host = host.strip("[]")
        infos = await asyncio.get_running_loop().getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
        ips = [i[4][0].split("%")[0] for i in infos]
        if not ips or not all(_og_ip_ok(a) for a in ips) or int(port) not in (80, 443):
            w.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await w.drain()
            return
        ur, uw = await asyncio.wait_for(asyncio.open_connection(ips[0], int(port)), 10)
        up = uw
        if line[0] == "CONNECT":
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await w.drain()
        else:
            uw.write(head)
            await uw.drain()
        await asyncio.wait([asyncio.ensure_future(_shot_pipe(r, uw)), asyncio.ensure_future(_shot_pipe(ur, w))], timeout=40)
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        for x in (w, up):
            if x:
                x.close()


async def _og_shot(url: str) -> str:
    """the name of the page's picture in SHOTS ('' when it cannot be made): 1200 x 750 as a browser shows it, kept a week"""
    name = hashlib.sha256(url.encode()).hexdigest()[:24] + ".jpg"
    out = SHOTS / name
    if out.exists() and time.time() - out.stat().st_mtime < SHOT_KEEP:
        return name
    exe = _chrome()
    if not exe or _SHOT_PROXY.get("wait", 0) >= 4:           # a few wait for the one Chrome at most: the rest get the card without a picture
        return ""
    _SHOT_PROXY["wait"] = _SHOT_PROXY.get("wait", 0) + 1
    try:
        return await _og_shot_make(url, exe, name, out)
    finally:
        _SHOT_PROXY["wait"] -= 1


async def _og_shot_make(url: str, exe: str, name: str, out: Path) -> str:
    async with _SHOT_SEM:
        if "port" not in _SHOT_PROXY:
            srv = await asyncio.start_server(_shot_proxy_conn, "127.0.0.1", 0)
            _SHOT_PROXY["port"] = srv.sockets[0].getsockname()[1]
            _SHOT_PROXY["srv"] = srv
        SHOTS.mkdir(parents=True, exist_ok=True)
        if time.time() - _SHOT_PROXY.get("swept", 0) > 86400:    # once a day: the pictures older than a week go
            _SHOT_PROXY["swept"] = time.time()
            for f in SHOTS.glob("*.jpg"):
                try:
                    if time.time() - f.stat().st_mtime > SHOT_KEEP:
                        f.unlink()
                except OSError:
                    pass
        tmp = tempfile.mkdtemp(prefix="lagomshot-")
        png, pr = Path(tmp) / "s.png", None
        try:
            pr = await asyncio.create_subprocess_exec(
                exe, "--headless=new", f"--user-data-dir={tmp}/p", "--no-first-run", "--disable-extensions",
                "--disable-gpu", "--mute-audio", "--hide-scrollbars", "--window-size=1200,750", "--virtual-time-budget=4000",
                f"--proxy-server=http://127.0.0.1:{_SHOT_PROXY['port']}", "--proxy-bypass-list=<-loopback>", "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-background-networking",
                f"--screenshot={png}", url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(pr.wait(), 30)
            if png.exists() and png.stat().st_size > 2000:
                def conv() -> bool:
                    from PIL import Image
                    im = Image.open(png).convert("RGB")
                    if len(set(im.resize((16, 10)).getdata())) < 3:      # a blank page says nothing
                        return False
                    im.resize((800, 500)).save(out, "JPEG", quality=82)
                    return True
                if await asyncio.get_running_loop().run_in_executor(None, conv):
                    return name
        except (OSError, asyncio.TimeoutError, ValueError) as x:
            print(f"[shot] {type(x).__name__}", flush=True)
        finally:
            if pr and pr.returncode is None:
                try:
                    pr.kill()
                except ProcessLookupError:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
    return ""


async def api_shot(req: web.Request) -> web.Response:
    await need_viewer(req)
    name = req.match_info["name"]
    if not re.fullmatch(r"[0-9a-f]{24}\.jpg", name) or not (SHOTS / name).exists():
        raise web.HTTPNotFound()
    return web.FileResponse(SHOTS / name, headers={"Cache-Control": "private, max-age=86400"})


def _og_parse(page: str, base: str) -> dict:
    """the card's words and picture from a page's own tags: og:, twitter:, then <title> and the description"""
    import html as _h
    from urllib.parse import urljoin
    meta = {}
    page = page[:200_000]                # bounded patterns on a bounded page: a page made to stall a regex cannot (the audit)
    for tag in re.findall(r"<meta\b[^<>]{0,2000}>", page, re.I):
        k = re.search(r"""(?:property|name)\s*=\s*["']([^"']+)["']""", tag, re.I)
        v = re.search(r"""content\s*=\s*["']([^"']*)["']""", tag, re.I) or re.search(r"""content\s*=\s*([^\s>]+)""", tag, re.I)
        if k and v:
            meta.setdefault(k.group(1).lower(), _h.unescape(v.group(1)).strip())
    t = re.search(r"<title[^<>]{0,200}>([^<]{0,500})</title>", page, re.I)
    pick = lambda *ks: next((meta[k] for k in ks if meta.get(k)), "")
    img = pick("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    return {"title": (pick("og:title", "twitter:title") or (_h.unescape(t.group(1)).strip() if t else ""))[:200],
            "desc": pick("og:description", "twitter:description", "description")[:400],
            "image": urljoin(base, img)[:1000] if img and not img.startswith("data:") else "",
            "site": pick("og:site_name")[:80]}


GH_REPO = "Crazy-IT-party/lagomchat"
_REL_FILE = Path.home() / ".cache" / "lagomchat" / "release.json"      # one for all the chats of the machine
_REL_LOCK = asyncio.Lock()


def _rel_read() -> dict:
    try:
        d = json.loads(_REL_FILE.read_text())
        return d if isinstance(d, dict) and isinstance(d.get("at"), (int, float)) else {}
    except (OSError, ValueError):
        return {}


async def api_release(req: web.Request) -> web.Response:
    """GitHub's latest release for the page's 'a new version' mark (the owner, #2568): asked by the server, once in five
    minutes for the whole machine - the answer lies in a file every chat reads - so no browser goes to GitHub (it lets one
    address ask 60 times an hour, ten open chats asked 120) and GitHub sees nobody's address but the machine's"""
    await need_viewer(req)
    d = _rel_read()
    if time.time() - d.get("at", 0) > 300:
        async with _REL_LOCK:
            d = _rel_read()
            if time.time() - d.get("at", 0) > 300:
                d = {"at": time.time(), "tag": str(d.get("tag") or ""), "url": str(d.get("url") or ""), "body": str(d.get("body") or "")}   # no answer: the last one stays, asked again in five minutes
                try:
                    async with ClientSession(timeout=ClientTimeout(total=10)) as s, s.get(
                            f"https://api.github.com/repos/{GH_REPO}/releases/latest",
                            headers={"Accept": "application/vnd.github+json", "User-Agent": "LagomChat"}) as r:
                        if r.status == 200:
                            j = json.loads((await _read_body(r, 400_000) or b"").decode("utf-8", "replace"))   # the whole body: one read() gives only what has come so far
                            url = str(j.get("html_url") or "")
                            d.update(tag=str(j.get("tag_name") or "")[:40], body=str(j.get("body") or "")[:700],
                                     url=url if url.startswith(f"https://github.com/{GH_REPO}/") and len(url) < 300 else f"https://github.com/{GH_REPO}/releases")
                        else:
                            print(f"[release] GitHub said {r.status}", flush=True)
                except Exception as x:                  # noqa: BLE001 - no network, a broken answer: the mark is no reason to fail
                    print(f"[release] {type(x).__name__}: {x}", flush=True)
                try:
                    _REL_FILE.parent.mkdir(parents=True, exist_ok=True)
                    tmp = _REL_FILE.with_name(f"release.{os.getpid()}.tmp")
                    tmp.write_text(json.dumps(d)); os.replace(tmp, _REL_FILE)
                except OSError as x:
                    print(f"[release] {x}", flush=True)
    return web.json_response({"ok": bool(d.get("tag")), "tag": d.get("tag") or "", "url": d.get("url") or "", "body": d.get("body") or ""})


async def api_og(req: web.Request) -> web.Response:
    """a link's preview card (the owner, #2473): the page's own title, words and picture, fetched by the chat's machine for the
    signed-in ones; TikTok and Reddit through their oEmbed (their pages say nothing to a fetch); kept 6 hours"""
    await need_viewer(req)
    url = req.query.get("u", "")
    if not re.fullmatch(r"https?://[^\s]{4,2000}", url):
        return web.json_response({"ok": False, "error": "bad_request"})
    hit = _OG_CACHE.get(url)
    if hit and time.time() - hit[0] < (21600 if hit[1].get("ok") else 1800):
        return web.json_response(hit[1])
    out: dict = {"ok": False}
    try:
        from urllib.parse import quote
        host = (re.match(r"https?://([^/:?#]+)", url) or [None, ""])[1].lower()
        oe = ("https://www.tiktok.com/oembed?url=" if host.endswith("tiktok.com") else
              "https://www.reddit.com/oembed?url=" if host.endswith("reddit.com") or host == "redd.it" else "")
        if oe:
            got = await _og_get(oe + quote(url, safe=""))
            if got:
                d = json.loads(got[1])
                h = str(d.get("html") or "")
                a = re.search(r"<a [^>]*>([^<]{2,300})</a>", h)
                out = {"ok": True, "title": str(d.get("title") or (a.group(1) if a else ""))[:200], "desc": str(d.get("author_name") or "")[:120],
                       "image": str(d.get("thumbnail_url") or "")[:1000], "site": str(d.get("provider_name") or "")[:80]}
        else:
            got = await _og_get(url)
            if got:
                c = await asyncio.get_running_loop().run_in_executor(None, _og_parse, got[1], got[0])
                if c["title"] or c["desc"] or c["image"]:
                    out = {"ok": True, **c}
            if not out.get("image") and await _og_public(host):           # no picture of its own: the page as a browser shows it
                shot = await _og_shot(got[0] if got else url)
                if shot:
                    out = {"title": "", "desc": "", "site": "", **out, "ok": True, "image": "", "shot": shot}
    except (ClientError, asyncio.TimeoutError, OSError, ValueError, LookupError, AttributeError, TypeError) as x:
        print(f"[og] {type(x).__name__}", flush=True)
    if len(_OG_CACHE) > 3000:
        _OG_CACHE.clear()
    _OG_CACHE[url] = (time.time(), out)
    return web.json_response(out)


def _x_text(h: str) -> str:
    """the post's own words out of X's oEmbed html: lines kept, tags and entities gone; nothing of their markup reaches the page"""
    import html as _h
    m = re.search(r"<p[^>]*>(.*?)</p>", h, re.S)
    t = re.sub(r"<br\s*/?>", "\n", m.group(1) if m else "")
    return _h.unescape(re.sub(r"<[^>]+>", "", t)).strip()[:2000]


async def api_x(req: web.Request) -> web.Response:
    """an X / Twitter post named in a message (the owner, #2453, #2455): its author, words and date from X's oEmbed, shown
    in the log at once with nothing loaded from X by the page; kept a day"""
    await need_viewer(req)
    pid = req.query.get("id", "")
    if not re.fullmatch(r"\d{5,25}", pid):
        return web.json_response({"ok": False, "error": "bad_request"})
    hit = _X_CACHE.get(pid)
    if hit and time.time() - hit[0] < (86400 if hit[1].get("ok") else 600):
        return web.json_response(hit[1])
    url = f"https://publish.twitter.com/oembed?url=https%3A%2F%2Fx.com%2Fi%2Fstatus%2F{pid}&omit_script=1&dnt=true"
    out: dict = {"ok": False, "error": "unavailable"}
    try:
        async with _OG_SEM, ClientSession(timeout=ClientTimeout(total=10)) as s, s.get(url) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                h = str(d.get("html") or "")
                date = re.findall(r"<a [^>]*>([^<]{4,40})</a>\s*</blockquote>", h)
                out = {"ok": True, "author": str(d.get("author_name") or "")[:80],
                       "handle": "@" + str(d.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1][:20],
                       "text": _x_text(h), "date": date[0] if date else "", "url": f"https://x.com/i/status/{pid}"}
            else:
                print(f"[x] oEmbed answered {r.status} for {pid}", flush=True)
    except (ClientError, asyncio.TimeoutError, OSError, ValueError, AttributeError, TypeError) as x:
        print(f"[x] oEmbed: {type(x).__name__}", flush=True)
    if len(_X_CACHE) > 3000:
        _X_CACHE.clear()
    _X_CACHE[pid] = (time.time(), out)
    return web.json_response(out)


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


# a pull request GitHub will not hand the diff of - over 20000 lines or 300 files it answers 406, and gh pr diff fails (the owner,
# #3590) - is compared on this machine: the PR's head and its merge base (GitHub's compare names it) are fetched alone, with no
# history and no file bodies (--depth=1 --filter=blob:none), into a bare repository kept per GitHub repository for every chat of
# this machine; git then takes the bodies of the changed files only. The text is the one gh pr diff gives for a smaller PR
GIT = shutil.which("git") or next((p for p in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git") if os.path.exists(p)), "")
PR_GIT = Path(os.environ.get("LAGOMCHAT_PR_GIT", Path.home() / ".cache/lagomchat/pr-git"))
_PRD_LOCKS: dict[str, asyncio.Lock] = {}             # one loading of a pull request at a time: the others wait for its answer
PR_GIT_MAX = 2 * 2 ** 30                               # a repository grown over this is started again
PR_GIT_ALL = 6 * 2 ** 30                               # all of them together: over this the ones not used for the longest go
_PR_GIT_LOCKS: dict[str, asyncio.Lock] = {}


async def _git(d: Path, *args: str, timeout: float = 120, limit: int = 30_000_000) -> tuple[int, bytes]:
    """git in the bare repository d: only its own settings count (not the machine's: an address rewritten to ssh, a helper that
    asks), GitHub is asked with gh's sign-in - a private repository - and never with a prompt"""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    p = None

    async def run() -> tuple[int, bytes, bytes]:
        """what git prints, read as it comes: an answer over `limit` is not waited for (nor held in memory)"""
        out, err = bytearray(), bytearray()

        async def errs() -> None:
            while b := await p.stderr.read(1 << 14):
                err[:] = (err + b)[-2000:]
        et = asyncio.create_task(errs())
        try:
            while b := await p.stdout.read(1 << 16):
                out.extend(b)
                if len(out) > limit:
                    raise OverflowError
            await et
            return await p.wait(), bytes(out), bytes(err)
        finally:
            et.cancel()
    try:
        # a group of its own: the helpers git starts (the download, the unpacking) are stopped with it
        p = await asyncio.create_subprocess_exec(GIT, "-C", str(d), "-c", "credential.helper=", "-c", f"credential.helper=!{shlex.quote(GH)} auth git-credential",
                                                 *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
                                                 start_new_session=True)
        rc, out, err = await asyncio.wait_for(run(), timeout)
    except (OSError, asyncio.TimeoutError, OverflowError) as x:
        if p and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        print(f"[pr] git {args[0]} in {d.name}: {'over ' + str(limit >> 20) + ' MB' if isinstance(x, OverflowError) else type(x).__name__}", flush=True)
        return -1, b""
    if rc:
        print(f"[pr] git {args[0]} in {d.name}: {err.decode('utf-8', 'replace').strip()[-300:]}", flush=True)
    return rc, out


def _pr_git_dir(d: Path) -> bool:
    """the folder of a repository's bare copy, emptied when it has grown too big; whether it is to be set up. The copies of
    all repositories together are held to PR_GIT_ALL: the ones not used for the longest go first (not one in use now)"""
    def size(x: Path) -> int:
        n = 0
        for f in x.rglob("*"):
            try:
                n += f.stat().st_size if f.is_file() else 0
            except OSError:
                pass
        return n
    if d.exists() and size(d) > PR_GIT_MAX:
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    os.utime(d)
    rest = sorted((x for x in PR_GIT.iterdir() if x.is_dir() and x != d), key=lambda x: x.stat().st_mtime, reverse=True)
    total = size(d)
    for x in rest:
        total += size(x)
        lock = _PR_GIT_LOCKS.get(x.name)
        if total > PR_GIT_ALL and not (lock and lock.locked()):
            shutil.rmtree(x, ignore_errors=True)
    return not (d / "HEAD").exists()


async def _pr_diff_local(repo: str, base: str, head: str) -> bytes | None:
    """the diff of a pull request made here by git (see above); None when it could not be made"""
    if not GIT or not all(re.fullmatch(r"[0-9a-f]{40}", x or "") for x in (base, head)):
        return None
    rc, out = await _gh("api", f"repos/{repo}/compare/{base}...{head}?per_page=1", "--jq", ".merge_base_commit.sha", timeout=30)
    mb = out.decode("ascii", "replace").strip()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", mb):
        return None
    d = PR_GIT / (repo.lower().replace("/", "__") + ".git")
    async with _PR_GIT_LOCKS.setdefault(d.name, asyncio.Lock()):
        try:
            new = await asyncio.to_thread(_pr_git_dir, d)
        except OSError as x:
            print(f"[pr] {d}: {x}", flush=True)
            return None
        t = time.time()
        for a in ((("init", "-q", "--bare"), ("config", "remote.origin.url", f"https://github.com/{repo}.git"), ("config", "remote.origin.promisor", "true"),
                   ("config", "remote.origin.partialclonefilter", "blob:none")) if new else ()):
            if (await _git(d, *a, timeout=20))[0] != 0:
                return None
        if (await _git(d, "fetch", "-q", "--depth=1", "--filter=blob:none", "origin", mb, head, timeout=120))[0] != 0:
            return None
        rc, out = await _git(d, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", mb, head, timeout=180)
        print(f"[pr] {repo} {head[:9]}: GitHub gives no diff, made here in {time.time() - t:.1f} s ({len(out) // 1024} KB){'' if rc == 0 else ' - failed'}", flush=True)
        return out if rc == 0 else None


async def api_pr_diff(req: web.Request) -> web.Response:
    await need_viewer(req)
    ref = _pr_ref(req)
    if not ref or not GH:
        return web.json_response({"ok": False, "error": "bad_request" if GH else "no_gh"})
    return web.json_response(await _pr_diff(*ref))


async def _pr_diff(repo: str, n: int, fresh: float = 60) -> dict:
    """a pull request's changes as the viewer gets them, kept for a minute"""
    key = f"{repo}#{n}"
    if len(_PRD_LOCKS) > 300:
        for k in [k for k, lk in _PRD_LOCKS.items() if not lk.locked()]:
            del _PRD_LOCKS[k]
    async with _PRD_LOCKS.setdefault(key, asyncio.Lock()):   # asked twice at once: loaded once, the second gets the kept answer
        hit = _PRD_CACHE.get(key)
        if hit and time.time() - hit[0] < (fresh if hit[1].get("ok") else 60):
            return hit[1]
        res = await _pr_diff_load(repo, n)
        if len(_PRD_CACHE) > 100:
            _PRD_CACHE.clear()
        _PRD_CACHE[key] = (time.time(), res)                 # a failure is kept for the minute too: it is not tried again at once
        return res


def _pr_files(text: str) -> list[dict]:
    files = parse_diff(text)
    for f in files:                                       # what a "viewed" mark is good for: the file's changes as they are now (#3591)
        f["sig"] = hashlib.sha1(json.dumps([f["status"], f["old"], f["bin"], [[h["os"], h["ns"], [l[:2] for l in h["lines"]]] for h in f["hunks"]]],
                                           ensure_ascii=False).encode()).hexdigest()[:12]
    return files


async def _pr_diff_load(repo: str, n: int) -> dict:
    rc, meta = await _gh("pr", "view", str(n), "-R", repo, "--json", "title,url,headRefOid,baseRefOid,headRefName,baseRefName,state", timeout=20)
    if rc != 0:
        return {"ok": False, "error": "not_found"}
    try:
        m = json.loads(meta)
    except ValueError:
        return {"ok": False, "error": "gh_failed"}
    rc, out = await _gh("pr", "diff", str(n), "-R", repo, timeout=60)
    if rc != 0:                                           # too big for GitHub's own diff (406): git makes it here
        out = await _pr_diff_local(repo, m.get("baseRefOid", ""), m.get("headRefOid", ""))
        if out is None:
            return {"ok": False, "error": "gh_failed"}
    files = await asyncio.to_thread(_pr_files, out.decode("utf-8", "replace"))   # a big one takes seconds: not on the chat's own thread
    big = sum(len(h["lines"]) for f in files for h in f["hunks"]) > 60000
    return {"ok": True, "repo": repo, "n": n, "title": m.get("title", ""), "url": m.get("url", ""), "state": m.get("state", ""),
            "head": m.get("headRefOid", ""), "base": m.get("baseRefOid", ""), "head_ref": m.get("headRefName", ""), "base_ref": m.get("baseRefName", ""),
            "files": files if not big else [dict(f, hunks=f["hunks"] if len(files) < 2 else [], cut=True) for f in files], "big": big}


async def api_pr_file(req: web.Request) -> web.Response:
    """a file of the pull request whole, at its head (or the base for a deleted one): only the PR's own commits"""
    await need_viewer(req)
    ref, path, sha = _pr_ref(req), req.query.get("path", ""), req.query.get("sha", "")
    if not ref or not GH or not re.fullmatch(r"[0-9a-f]{40}", sha) or not path or ".." in path.split("/") or len(path) > 400:
        return web.json_response({"ok": False, "error": "bad_request"})
    d = await _pr_diff(*ref, fresh=3600)                     # the one the viewer has open (asked anew after a restart of the chat)
    if sha not in (d.get("head"), d.get("base")):
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
        if _other_site(req):
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


# ---- reviewing a pull request in the chat (the owner, #3591). The files a person has looked through leave their list: kept here
# per person, so the same on every device of theirs; a file whose changes are not the same any more (a push) comes back - the mark
# holds the fingerprint of the changes it was made on. And superman's guide for a pull request: the files in the order to review
# them in, by groups (the server first, then the pages that use it), and the changes that may break something, each with a reason.
# The page's buttons ask superman for it in the chat; he answers with `agentchat pr-guide` (the skill agents-chat-pr-guide)
PR_VIEWED_FILE, PR_GUIDES_FILE = DATA / "pr_viewed.json", DATA / "pr_guides.json"


def _json_file(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


PR_VIEWED: dict[str, dict[str, dict[str, str]]] = _json_file(PR_VIEWED_FILE)     # "repo#n" -> the person -> path -> fingerprint
PR_GUIDES: dict[str, dict] = _json_file(PR_GUIDES_FILE)                           # "repo#n" -> {groups, crucial, note, head, by, ts}
PR_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}#[0-9]{1,8}")


def pr_guide_clean(g) -> tuple[dict | None, str]:
    """superman's guide as it is kept: ({summary, about: {path: text}, groups: [{title, files}], crucial: [{path, line, side, why}],
    note, head}, '') or (None, why not). summary: what the pull request changes as a whole; about: the same of one file and how it
    hangs together with the others (#3600) - given as files: [{path, about}], or as it is kept"""
    if not isinstance(g, dict):
        return None, ("the guide is a JSON object: {summary, files: [{path, about}], groups: [{title, files: [path, ...]}], "
                      "crucial: [{path, line, side, why}], note, head}")

    def text(x, n: int) -> str:                              # the lines kept (paragraphs, a list), the spaces in them tidied
        t = "\n".join(" ".join(ln.split()) for ln in str(x or "").splitlines())
        return re.sub(r"\n{3,}", "\n\n", t).strip()[:n]
    about = {}
    fl = g.get("files") if g.get("files") is not None else g.get("about")
    if isinstance(fl, dict):
        fl = [{"path": k, "about": v} for k, v in fl.items()]
    for x in fl or []:
        if not isinstance(x, dict) or not isinstance(x.get("path"), str) or not x["path"] or not isinstance(x.get("about"), str):
            return None, "files: a list of {path, about}"
        if text(x["about"], 2000):
            about[x["path"][:400]] = text(x["about"], 2000)
    if len(about) > 5000 or sum(map(len, about.values())) > 600_000:
        return None, "too much about the files (5000 files, 600 000 letters in all): a line or two for a file"
    summary = text(g.get("summary"), 6000)
    groups, crucial, n = [], [], 0
    for x in g.get("groups") or []:
        if not isinstance(x, dict) or not isinstance(x.get("files"), list):
            return None, "groups: a list of {title, files: [path, ...]}"
        fs = list(dict.fromkeys(str(f)[:400] for f in x["files"] if isinstance(f, str) and f))
        n += len(fs)
        if fs:
            groups.append({"title": " ".join(str(x.get("title") or "").split())[:80], "files": fs})
    if len(groups) > 100 or n > 5000:
        return None, "too many groups or files (100 groups, 5000 files)"
    for x in g.get("crucial") or []:
        if not isinstance(x, dict) or not isinstance(x.get("path"), str) or not x["path"]:
            return None, "crucial: a list of {path, line, side, why}"
        line = x.get("line")
        if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
            return None, "crucial: line is a line number of the file (left out: the whole file)"
        if x.get("side") not in (None, "new", "old"):
            return None, "crucial: side is new (the file after the change, the default) or old"
        crucial.append({"path": x["path"][:400], **({"line": line} if line else {}), **({"side": "old"} if x.get("side") == "old" else {}),
                        "why": " ".join(str(x.get("why") or "").split())[:300]})
    if len(crucial) > 500:
        return None, "too many crucial changes (500): name the ones a reviewer must not miss"
    if not groups and not crucial and not summary and not about:
        return None, "an empty guide: give summary, files, groups or crucial"
    head = str(g.get("head") or "")
    return {"summary": summary, "about": about, "groups": groups, "crucial": crucial, "note": " ".join(str(g.get("note") or "").split())[:600],
            "head": head if re.fullmatch(r"[0-9a-f]{40}", head) else ""}, ""


async def pr_guide_set(key: str, g: dict | None, by: str) -> None:
    if g is None:
        PR_GUIDES.pop(key, None)
    else:
        PR_GUIDES[key] = {**g, "by": by, "ts": round(time.time(), 1)}
    PR_GUIDES_FILE.write_text(json.dumps(PR_GUIDES, ensure_ascii=False))
    print(f"[prg] {by}: {key} {'-' if g is None else '+'}", flush=True)
    await chat.watch_all({"type": "prg", "pr": key, "guide": PR_GUIDES.get(key)})


async def api_pr_review(req: web.Request) -> web.Response:
    """a person's review of a pull request: GET {viewed: {path: fingerprint}, guide}; POST {path, sig, on} marks a file viewed or
    not, {clear: true} takes every mark of theirs away"""
    v = await need_viewer(req)
    ref = _pr_ref(req)
    if not ref:
        return web.json_response({"ok": False, "error": "bad_request"})
    key, vk = f"{ref[0]}#{ref[1]}", viewer_key(v)
    mine = PR_VIEWED.get(key, {}).get(vk, {})
    if req.method == "POST":
        if _other_site(req):
            return web.json_response({"ok": False, "error": "refused: another site's page"}, status=403)
        try:
            q = await req.json()
        except ValueError:
            q = {}
        mine = dict(mine)
        if q.get("clear"):
            mine = {}
        else:
            path, sig = str(q.get("path") or ""), str(q.get("sig") or "")
            if not path or len(path) > 400 or not re.fullmatch(r"[0-9a-f]{12}", sig) or not vk or (q.get("on") and len(mine) >= 5000 and path not in mine):
                return web.json_response({"ok": False, "error": "bad_request"})
            if q.get("on") and not mine and not (_PRD_CACHE.get(key) or (0, {}))[1].get("ok"):
                return web.json_response({"ok": False, "error": "bad_request"})      # the first mark: of a pull request that was opened here
            if q.get("on") and not mine:
                for old in [k for k, per in PR_VIEWED.items() if vk in per][:-299]:   # the person's oldest ones go
                    PR_VIEWED[old].pop(vk, None)
                    if not PR_VIEWED[old]:
                        del PR_VIEWED[old]
            if q.get("on"):
                mine[path] = sig
            else:
                mine.pop(path, None)
        per = PR_VIEWED.setdefault(key, {})
        if mine:
            per[vk] = mine
        else:
            per.pop(vk, None)
        if not per:
            PR_VIEWED.pop(key, None)
        PR_VIEWED_FILE.write_text(json.dumps(PR_VIEWED, ensure_ascii=False))
        m = json.dumps({"type": "prv", "pr": key, "viewed": mine}, ensure_ascii=False)
        for ws in list(chat.watchers):                    # the person's other pages: the same list there
            me = chat.viewers.get(ws)
            if me and viewer_key(me) == vk:
                await send(ws, m)
    return web.json_response({"ok": True, "viewed": mine, "guide": PR_GUIDES.get(key)})


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
        if _other_site(req):
            return web.json_response({"ok": False, "error": "refused: another site's page"}, status=403)
        try:
            q = await req.json()
        except ValueError:
            q = {}
        op, slug = str(q.get("op") or ""), str(q.get("slug") or "")[:60]
        tmp = None
        if op == "create":
            args = ["create", str(q.get("title") or "")[:60], slug]
            if str(q.get("import") or "").strip():           # a Telegram export to bring into the new chat
                args += ["--import", str(q["import"]).strip()[:1000]]
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
                              "icons": [{"src": "appicon-192.png?v=ring", "sizes": "192x192", "type": "image/png"},
                                        {"src": "appicon-512.png?v=ring", "sizes": "512x512", "type": "image/png"}]},
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


def _seen_url(req: web.Request) -> str:
    """this chat's address as the person's browser has it: the key of what they read here"""
    host = req.headers.get("X-Forwarded-Host") or req.host
    return _chat_url(host if host.startswith(("127.", "localhost")) else host.split(":")[0])


def _seen_here(req: web.Request, v: dict) -> int:
    """the last message of this chat the person has read, on any device of theirs (by the email); 0: not known"""
    cur = _seen_load().get(str(v.get("em") or "").lower())
    try:
        return int(cur.get(_seen_url(req)) or 0) if isinstance(cur, dict) and v.get("em") else 0
    except (ValueError, TypeError):
        return 0


async def api_agent_color(req: web.Request) -> web.Response:
    """the owner sets the colour of an agent's name (#1900): {name, color: "#rrggbb" | ""}; empty = the agent's own again"""
    v = await need_viewer(req)
    if v.get("role") != "owner":
        return web.json_response({"ok": False, "error": "only the owner"}, status=403)
    if not _same_page(req):
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
    """the last message id read in this chat, by the person's email, for the other chats' circles (the owner, #1860) and for
    the person's other pages of this chat: a phone does not open at what was read on the computer (the owner, #3572)"""
    v = await viewer_of(req)
    em = str(v.get("em") or "").lower()
    if not em or v["role"] not in ("owner", "verified"):       # one who waits to be let in writes nothing
        return web.json_response({"ok": False}, status=401)
    try:
        q = await req.json()
        i = int(q.get("id") or 0)
    except (ValueError, TypeError, AttributeError):
        return web.json_response({"ok": False}, status=400)
    url = _seen_url(req)
    d = _seen_load()
    cur = d.get(em) if isinstance(d.get(em), dict) else {}
    if i > int(cur.get(url) or 0):
        cur[url] = i
        d[em] = cur
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEN_FILE.with_name(f"{SEEN_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(SEEN_FILE)
        for w, x in list(chat.viewers.items()):
            if str(x.get("em") or "").lower() == em:
                await send(w, json.dumps({"type": "seen", "id": i}))
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


_SIGNIN_HITS: dict[str, list[float]] = {}
SIGNIN_PER_10MIN = 30


def next_of(req: web.Request, prefix: str) -> str:
    """where a sign-in goes when it is done: ?next when it is a path of this chat, else the chat's page (no other site)"""
    nxt = req.query.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith(("//", "/\\")) else prefix + "/"


def signin_rate(req: web.Request) -> bool:
    """the chat's own sign-in answers before anybody is known: an address begins or ends it 30 times in ten minutes, no more"""
    ip, now = client_ip(req), time.time()
    for k in [k for k, v in _SIGNIN_HITS.items() if not v or now - v[-1] >= 600]:
        del _SIGNIN_HITS[k]
    hits = [t for t in _SIGNIN_HITS.get(ip, []) if now - t < 600]
    # all the addresses together are held to 30 times that
    if len(hits) >= SIGNIN_PER_10MIN or sum(len(v) for v in _SIGNIN_HITS.values()) >= 30 * SIGNIN_PER_10MIN:
        return False
    _SIGNIN_HITS[ip] = hits + [now]
    return True


async def oauth_login(req: web.Request):
    """/auth/<provider>/login: to the provider (Google: the owner, 2026-11-03, #1497; any: 2026-11-22)"""
    prov = req.match_info["prov"]
    if auth.mode() != "oauth" or prov not in auth.oauth_conf():
        raise web.HTTPNotFound(text=f"{prov} sign-in is not set up")
    if not signin_rate(req):
        raise web.HTTPTooManyRequests(text="Too many sign-in attempts from this address. Try again in ten minutes.")
    scheme, host, prefix = origin_of(req)
    state, st = auth.make_state(next_of(req, prefix))
    try:
        url = await asyncio.to_thread(auth.oauth_url, prov, f"{scheme}://{host}{prefix}/auth/{prov}/callback", state, st)
    except Exception as x:                               # an OpenID Connect issuer that does not answer
        print(f"[auth] {prov} sign-in: the provider is not reachable: {x!r}", flush=True)
        return web.Response(status=502, text=f"The {auth.provider_names().get(prov, prov)} sign-in is not reachable now. Try again later.", content_type="text/plain")
    r = web.HTTPFound(url)
    r.set_cookie(auth.STATE_COOKIE, st, max_age=900, path=prefix or "/", httponly=True, secure=scheme == "https", samesite="Lax")
    raise r


def cf_token(req: web.Request) -> str | None:
    return req.headers.get("Cf-Access-Jwt-Assertion") or req.cookies.get("CF_Authorization")


async def cf_login(req: web.Request):
    """/auth/cf/login: in as the email Cloudflare Access proved (the owner, #1611)"""
    scheme, host, prefix = origin_of(req)
    nxt = next_of(req, prefix)
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
    st = req.cookies.get(auth.STATE_COOKIE)
    nxt = auth.read_state(req.query.get("state"), st)
    if auth.mode() != "oauth" or prov not in auth.oauth_conf() or not nxt or not req.query.get("code"):
        return web.Response(status=403, text="The sign-in was not confirmed (an old or forged link). Go back and try again.", content_type="text/plain")
    if not signin_rate(req):
        raise web.HTTPTooManyRequests(text="Too many sign-in attempts from this address. Try again in ten minutes.")
    pname = auth.provider_names().get(prov, prov)
    who = await asyncio.to_thread(auth.oauth_identity, prov, req.query["code"], f"{scheme}://{host}{prefix}/auth/{prov}/callback", st)
    if not who:
        print(f"[auth] {prov} sign-in refused: {auth.last_error}", flush=True)
        access_log(req, "signin-refused", prov=prov)
        return web.Response(status=403, text=f"The {pname} sign-in was not confirmed. Go back and try again.", content_type="text/plain")
    await asyncio.to_thread(chat.rdb.user_touch, who["sid"], who["name"], who.get("avatar", ""))
    _ROLE_CACHE.pop(who["sid"], None)
    print(f"[auth] {prov}: {who['name']} {who['sid']} {who.get('em', '')} allowed={auth.allowed(who)}", flush=True)
    access_log(req, "signin", prov=prov, em=who["em"])
    r = web.HTTPFound(f"{scheme}://{host}{nxt}")
    r.set_cookie(auth.COOKIE, auth.make_token({k: who[k] for k in ("sid", "name", "avatar", "em") if who.get(k)}), max_age=auth.SESSION_DAYS * 86400,
                 path=prefix or "/", httponly=True, secure=scheme == "https", samesite="Lax")
    r.del_cookie(auth.STATE_COOKIE, path=prefix or "/")
    raise r


async def auth_logout(req: web.Request):
    scheme, host, prefix = origin_of(req)
    # the session of Access or of a reverse proxy is theirs: out there, or the next page signs in again by itself
    mode, out = auth.mode(), auth.header_conf()["logout"]
    there = "/cdn-cgi/access/logout" if mode == "access" else out if mode == "header" and out.startswith("/") and not out.startswith("//") else prefix + "/"
    r = web.HTTPFound(f"{scheme}://{host}{there}")
    r.del_cookie(auth.COOKIE, path=prefix or "/")
    raise r


async def api_me(req: web.Request):
    v = await viewer_of(req)
    return web.json_response({k: v[k] for k in ("role", "name", "id", "avatar", "signed", "enforced", "acct_name")},
                             headers={"Cache-Control": "no-store"})


async def tz_note(me: dict) -> None:
    """the clocks (the owner, #2710): the time zone of one who shows it is kept for when they are away; one who hides it is forgotten"""
    k = me.get("id") or me.get("name") or ""
    old = chat.tzlast.get(k)
    new = {"tz": me["tz"], "name": me["name"]} if k in chat.showtz and me.get("tz") and not me.get("aux") else None if k not in chat.showtz else old
    if not k or new == old:
        return
    if new:
        chat.tzlast[k] = new
    else:
        chat.tzlast.pop(k, None)
    chat.tzlast_file.write_text(json.dumps(chat.tzlast, ensure_ascii=False))
    await chat.watch_all({"type": "clocks"})


async def api_clocks(req: web.Request) -> web.Response:
    """whose clock the page shows: everyone who turned on "Show my time zone", here now or not - their name and zone, nothing else"""
    await need_viewer(req)
    return web.json_response({"ok": True, "clocks": [{"name": c["name"], "tz": c["tz"]} for k, c in chat.tzlast.items() if k in chat.showtz]},
                             headers={"Cache-Control": "no-store"})


# live captions (the owner, #2756): a speaker's page sends its microphone straight to ElevenLabs Scribe realtime with a single-use token made
# here - the key (the file elevenlabs.token in the config folder) never leaves the server; the text it gets back goes to every page as a caption
ELEVEN_API = os.environ.get("LAGOMCHAT_ELEVEN_API", "https://api.elevenlabs.io").rstrip("/")
_stt_last: dict[str, float] = {}


# Live captions in another language, and speech kept as voice messages (the owner, #3617, #3621, #3622). CC_LANGS: the languages
# a person may say they speak and may read captions in. The translating is done by a model server of our own, an OpenAI-compatible
# /v1/chat/completions: translate.json {"url": ..., "model": ..., "key": ..., "extra": {...}} in the chat's config folder or in
# .lagomchat of the working copy; with none, the address in vision_url (the model that describes the pictures). What people say
# goes nowhere else unless the owner puts another address there.
# Anthropic's models (the owner, #3822): translate.json {"provider": "anthropic", "key": "<API key>", "model": "claude-sonnet-5-5",
# "effort": "low"} - the Messages API; "key" may be left out when the chat's process has ANTHROPIC_API_KEY
CLAUDE_URL = "https://api.anthropic.com"
CLAUDE_FALLBACKS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")   # models with the API's own fallback on a refusal
CC_LANGS = {"en": "English", "de": "German", "ru": "Russian", "uk": "Ukrainian"}
CC_VOICE_RE = re.compile(r"^(?:/[\w.\-]+)+\.(?:m4a|wav)[ \t]*\n\s*\S", re.I)


class Translator:
    def __init__(self) -> None:
        self.memo: dict[tuple, str] = {}
        self.busy = 0
        self.slow = asyncio.Semaphore(2)                 # kept phrases: two at a time, the captions go past them
        self.down = 0.0                                  # no call before this time: the server did not answer
        self.error = ""
        self.fallback = lambda: ""
        self._c: dict = {}
        self._at = 0.0

    def conf(self) -> dict:
        if time.time() - self._at > 30:                  # the file may be written while the chat runs
            self._at, c = time.time(), {}
            for f in (auth.CONF / "translate.json", _install_file("translate.json")):
                try:
                    c = json.loads(f.read_text()) if f else {}
                except (OSError, ValueError):
                    c = {}
                if isinstance(c, dict) and (c.get("url") or c.get("provider") == "anthropic"):
                    break
                c = {}
            self._c = c
        return self._c if self._c else {"url": self.fallback()}

    def ready(self) -> bool:
        c = self.conf()
        return bool(c.get("url") or c.get("provider") == "anthropic" and (c.get("key") or os.environ.get("ANTHROPIC_API_KEY")))

    async def _claude(self, cs: ClientSession, c: dict, system: str, text: str) -> str:
        """the translation by an Anthropic model: the Messages API. A refusal gives no translation"""
        model = str(c.get("model") or "claude-sonnet-5-5")
        body = {"model": model, "max_tokens": min(1500, 120 + len(text)), "system": system, "messages": [{"role": "user", "content": text}],
                "output_config": {"effort": str(c.get("effort") or "low")}}   # a translation needs little thought; the captions want it fast
        h = {"x-api-key": str(c.get("key") or os.environ.get("ANTHROPIC_API_KEY") or ""), "anthropic-version": "2026-10-21",
             "content-type": "application/json"}
        if model in CLAUDE_FALLBACKS:
            body["fallbacks"] = "default"
            h["anthropic-beta"] = "server-side-fallback-2026-07-01"
        async with cs.post(str(c.get("url") or CLAUDE_URL).rstrip("/") + "/v1/messages", json=body, headers=h) as r:
            # an overloaded or rate-limited API is "not there" for a while, like a server down - whatever its body is (a proxy's
            # HTML page too); a bad request is this text's alone
            if r.status == 429 or r.status >= 500:
                raise ClientError(f"{r.status}")
            d = await r.json(content_type=None)
            if not isinstance(d, dict):
                raise ValueError(f"{r.status}: not an object")
            if r.status >= 400 or d.get("type") == "error":
                e = d.get("error") if isinstance(d.get("error"), dict) else {}
                raise ValueError(f"{r.status} {e.get('type', '')}: {str(e.get('message', ''))[:100]}")
        if d.get("stop_reason") == "refusal":
            return ""
        return "".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text")

    async def tr(self, text: str, src: str, dst: str, live: bool = False) -> str:
        """`text` (said in `src`, "" = whatever language) in `dst`; "" when it cannot be had. `live`: a caption - not worth a
        long wait, dropped when many wait already"""
        text = " ".join(text.split())[:2000]
        if not text or dst not in CC_LANGS or src == dst:
            return ""
        k = (src, dst, text)
        if k in self.memo:
            return self.memo[k]
        c = self.conf()
        if not self.ready() or time.time() < self.down or self.busy >= (6 if live else 16):
            return ""
        to = CC_LANGS[dst]
        sys_ = (f"You translate speech from {CC_LANGS[src]} into {to}." if src in CC_LANGS else f"You translate speech into {to}.") + \
            " The user's message is the transcript of what a person has just said aloud" + ("" if src in CC_LANGS else ", in whatever language") + \
            f". Answer with its {to} translation only: no notes, no quotes, nothing added and nothing left out. Keep the names, the numbers " \
            f"and the tone." + ("" if src in CC_LANGS else f" If it is in {to} already, answer with it unchanged.")
        body = {"messages": [{"role": "system", "content": sys_}, {"role": "user", "content": text}], "temperature": 0.2,
                "max_tokens": min(1500, 120 + len(text)), **({"model": str(c["model"])} if c.get("model") else {}),
                **(c["extra"] if isinstance(c.get("extra"), dict) else {} if c.get("key") else {"chat_template_kwargs": {"enable_thinking": False}})}
        self.busy += 1
        try:
            async with contextlib.nullcontext() if live else self.slow, ClientSession(timeout=ClientTimeout(total=8 if live else 40)) as cs:
                if c.get("provider") == "anthropic":
                    out = await self._claude(cs, c, sys_, text)
                else:
                    async with cs.post(str(c["url"]).rstrip("/") + "/v1/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {c['key']}"} if c.get("key") else {}) as r:
                        d = await r.json(content_type=None)
                        out = str(d["choices"][0]["message"]["content"] or "")
        except (ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as x:
            self.error = f"{type(x).__name__}: {str(x)[:120]}"
            if isinstance(x, (ClientError, asyncio.TimeoutError, OSError)):
                self.down = time.time() + 20             # the server is not there; an answer of the wrong shape is this text's alone
            print(f"[translate] {self.error}", flush=True)
            return ""
        finally:
            self.busy -= 1
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip().strip('"«»“”„').strip()[:3000]
        self.error = ""
        if len(self.memo) > 2000:
            self.memo.clear()
        if out:
            self.memo[k] = out
        return out


TR = Translator()
_CC_SAID: dict[str, list[float]] = {}                 # a person -> when their kept phrases came (the last minute)


def _cc_may(me: dict, text: str) -> bool:
    """a kept phrase of live captions is taken from this page: its sound is a file the page has just sent up, and the person
    has not sent 20 in the last minute from all their pages (every one of them is a call to the translator)"""
    p = Path(text.split("\n", 1)[0].strip())
    if p.parent != UPLOADS or not p.is_file():
        return False
    now, k = time.time(), str(chat.rater_of(me)["id"])
    said = _CC_SAID[k] = [t for t in _CC_SAID.get(k, []) if now - t < 60]
    if len(said) >= 20:
        return False
    said.append(now)
    return True


def eleven_key() -> str:
    """The ElevenLabs key of this chat: its own (elevenlabs.token in its config folder), else the one of the whole install -
    .lagomchat/elevenlabs.token of the working copy, else the main chat's own - so one key serves every chat (the owner, #3167)"""
    places = [auth.CONF / "elevenlabs.token"]
    try:
        import lagomcfg
        places.append(lagomcfg.CFG_DIR / "elevenlabs.token")
        places += [Path(c["conf"]) / "elevenlabs.token" for c in lagomcfg.load().get("chats", {}).values() if c.get("main") and c.get("conf")]
    except Exception:                                   # a chat run by hand, with no install around it: its own key only
        pass
    for f in places:
        try:
            k = f.read_text().strip()
        except OSError:
            continue
        if k:
            return k
    return ""


# speech to text of our own (the owner, #3847): stt.json {"url": "http://<box>:8001", "token": "<secret>"} in the chat's config
# folder or in .lagomchat of the working copy - Whisper on a box of the LAN (tools/stt_server.py), speaking the protocol of
# ElevenLabs' realtime Scribe. A page gets a ticket for one line and opens it at /api/stt_ws; the server passes it through. The
# box not answering: ElevenLabs, as before, for half a minute
_STT = {"ok": 0.0, "down": 0.0}
_stt_tickets: dict[str, tuple[str, float]] = {}         # a ticket -> (whose, valid till)
_stt_open: dict[str, int] = {}                          # a person -> their lines open now


def stt_conf() -> dict:
    for f in (auth.CONF / "stt.json", _install_file("stt.json")):
        try:
            c = json.loads(f.read_text()) if f and f.is_file() else None
        except (OSError, ValueError):
            c = None
        if isinstance(c, dict) and re.match(r"https?://", str(c.get("url") or "")):
            return c
    return {}


def stt_down(why: str) -> None:
    _STT["ok"], _STT["down"] = 0.0, time.time() + 30
    print(f"[stt] our own speech to text: {why} - ElevenLabs for now", flush=True)


async def stt_local_up() -> bool:
    c, now = stt_conf(), time.time()
    if not c or now < _STT["down"]:
        return False
    if now - _STT["ok"] < 10:
        return True
    try:
        async with ClientSession(timeout=ClientTimeout(total=2)) as cs, cs.get(c["url"].rstrip("/") + "/health") as r:
            if r.status == 200:
                _STT["ok"] = now
                return True
            stt_down(f"health {r.status}")
    except (ClientError, asyncio.TimeoutError, OSError) as x:
        stt_down(type(x).__name__)
    return False


async def ws_stt(req: web.Request) -> web.WebSocketResponse:
    """a page's line to our own speech to text, by its ticket (api/stt_token): passed through both ways. A person has 3 lines
    open at most (a page has two: captions and push to talk); a line silent for two minutes is closed"""
    tk, c = _stt_tickets.pop(req.query.get("token", ""), None), stt_conf()
    if not tk or tk[1] < time.time() or not c:
        raise web.HTTPForbidden(text="no ticket")
    who = tk[0]
    if _stt_open.get(who, 0) >= 3:
        raise web.HTTPTooManyRequests(text="too many lines")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4 << 20)
    await ws.prepare(req)
    q = {k: v for k, v in req.query.items() if k in ("audio_format", "language_code", "vad_silence_threshold_secs", "commit_strategy")}
    url = re.sub(r"^http", "ws", c["url"].rstrip("/")) + "/v1/speech-to-text/realtime"
    _stt_open[who] = _stt_open.get(who, 0) + 1
    try:
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=3)) as cs, \
                cs.ws_connect(url, params=q, headers={"Authorization": f"Bearer {c.get('token', '')}"}, heartbeat=25, max_msg_size=4 << 20) as up:
            async def back():
                with contextlib.suppress(ClientError, ConnectionResetError, RuntimeError):
                    async for m in up:
                        if m.type == WSMsgType.TEXT:
                            await ws.send_str(m.data)
                await ws.close()
            bt = asyncio.create_task(back())
            try:
                while not ws.closed:
                    m = await ws.receive(timeout=120)
                    if m.type != WSMsgType.TEXT or up.closed:
                        break
                    await up.send_str(m.data)
            finally:
                bt.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bt
    except (ClientError, OSError) as x:
        stt_down(type(x).__name__)
    except asyncio.TimeoutError:
        pass                                             # two minutes of nothing from the page
    finally:
        _stt_open[who] -= 1
        if not _stt_open[who]:
            del _stt_open[who]
        await ws.close()
    return ws


async def api_stt_token(req: web.Request) -> web.Response:
    """POST: a single-use token for one realtime transcription session of this person's microphone; one per 5 s a person.
    The answer says {local: true} when it is a ticket for our own speech to text (ws_stt), else it is ElevenLabs' token"""
    v = await need_viewer(req)
    if not _same_page(req):
        return web.json_response({"ok": False, "error": "refused: another site's page"}, status=403)
    local = await stt_local_up()
    key = "" if local else eleven_key()
    if not local and not key:
        return web.json_response({"ok": False, "error": "off"})
    k, now = viewer_key(v), time.time()
    if now - _stt_last.get(k, 0) < 5:
        return web.json_response({"ok": False, "error": "too_often"}, status=429)
    _stt_last[k] = now
    if local:
        for t in [t for t, (_, end) in _stt_tickets.items() if end < now]:
            del _stt_tickets[t]
        t = secrets.token_urlsafe(24)
        _stt_tickets[t] = (k, now + 60)
        print(f"[stt] a ticket for {v['name']} (our own)", flush=True)
        return web.json_response({"ok": True, "token": t, "local": True}, headers={"Cache-Control": "no-store"})
    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as cs, cs.post(f"{ELEVEN_API}/v1/single-use-token/realtime_scribe", headers={"xi-api-key": key}) as r:
            d = await r.json(content_type=None) if r.status == 200 else {}
            if not d.get("token"):
                print(f"[stt] ElevenLabs answered {r.status}", flush=True)
                return web.json_response({"ok": False, "error": f"ElevenLabs {r.status}"})
    except (ClientError, asyncio.TimeoutError, ValueError) as x:
        print(f"[stt] ElevenLabs: {type(x).__name__}", flush=True)
        return web.json_response({"ok": False, "error": "ElevenLabs is not reachable"})
    print(f"[stt] a token for {v['name']}", flush=True)
    return web.json_response({"ok": True, "token": d["token"]}, headers={"Cache-Control": "no-store"})


async def ws_watch(req: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    me = await viewer_of(req)
    if me["role"] not in ("owner", "verified"):
        # not let in: no chat at all; a signed-in account may type, and every message gets a refusal
        st = "banned" if me["role"] == "banned" else "pending" if me["signed"] else "signin"
        own = auth.mode() == "oauth"                     # the chat's own sign-in: its providers' buttons on the gate
        cfa = await asyncio.to_thread(auth.cf_access_email, cf_token(req)) if st == "signin" and own else None
        pn = auth.provider_names() if own else {}
        await ws.send_str(json.dumps({"type": "gate", "state": st, "providers": sorted(pn), "pnames": pn, "mode": auth.mode(), "cfa": cfa or "", "name": me["acct_name"] or me["name"],
                                      "id": me["id"], "avatar": me["avatar"],
                                      **({"strikes": chat.strikes.get(me["id"], [])} if st == "banned" else {})}, ensure_ascii=False))
        if not me["signed"] or st == "banned":
            await ws.close()
            return ws
        await guest_knock(req, me, ws)
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
    await ws.send_str(json.dumps({"type": "init", "seen": _seen_here(req, me), "peer": me["peer"], "sps": {str(k): {**{x: v[x] for x in ("uri", "pos", "paused")}, "age": round(time.time() - v["at"], 2)} for k, v in chat.sps.items() if time.time() - v["at"] < 6 * 3600}, "me": {k: me[k] for k in ("role", "name", "id", "task", "avatar", "signed", "enforced", "acct_name")},
                                  "presence": chat.presence(me["role"] == "owner", me),
                                  "agents": chat.roster(), "known": chat.known_agents(), "allow_all": chat.allow_all, "sound_presets": SOUND_PRESETS, "stt": bool(eleven_key() or stt_conf()), "rimgs": _rimgs_out(), "photo": chat.photos.get(me.get("task") or ""), "loops": {str(i): v for i, v in chat.loops.items() if i >= lo}, "events": evs, "v": page_version(), "title": chat.title, "title_tr": chat.title_tr, "aliases": chat.aliases.get(viewer_key(me), {}), "names": chat.dnames, "agent_cols": chat.agent_cols,
                                  "acks": {str(i): v for i, v in chat.acks.items() if i >= lo},
                                  "trans": {str(i): v for i, v in chat.trans.items() if chat.sees(i, start) and i not in chat.deleted},
                                  "cctr": {str(i): v for i, v in chat.cctr.items() if chat.sees(i, start)}, "cc_tr": TR.ready(),
                                  "views": {str(i): len(v) for i, v in chat.views.items() if chat.sees(i, start)},
                                  "viewed": (lambda rid: [i for i, v in chat.views.items() if chat.sees(i, start) and rid in v])(str(chat.rater_of(me)["id"])),
                                  "pins": chat.pins_view(),
                                  "tally": {str(i): t for i in chat.votes if chat.sees(i, start) and (t := chat.tally(i))},
                                  "polls": {str(i): chat.poll_view(i, me.get("name") or "owner") for i in chat.polls if chat.sees(i, start)},
                                  "cards": CARDS,
                                  "reacts": {str(i): r for i, r in chat.reacts.items() if chat.sees(i, start)},   # all they may see, not only of the n newest: older messages come later with no reactions of their own (the owner, #1033)
                                  "votes": {str(i): m[str(chat.rater_of(me)["id"])][1] for i, m in chat.votes.items() if chat.sees(i, start) and str(chat.rater_of(me)["id"]) in m}, "scores": chat.scores(),
                                  "first": chat.first_for(start)}, ensure_ascii=False))
    chat.watchers.add(ws)
    if not me["aux"]:
        await chat.person_came(me)
    me["ip"], me["city"] = where_from(req)
    access_log(req, "page", name=me.get("name") or "", task=me.get("task") or "", role=me.get("role") or "", account=me.get("id") or "",
               email=me.get("em") or "")
    me["via"] = "cf" if req.headers.get("Cf-Ray") else "direct"
    lang = req.query.get("lang") or ""                   # the language picked on this page (the owner, #2132)
    me["lang"] = lang if lang in ("en", "ru", "uk", "be", "cs", "de", "es", "sv", "fr", "it", "pl", "pt", "zh") else ""
    tz = req.query.get("tz") or ""                       # the page's time zone (IANA name), shown only if the person turns it on (#2222)
    me["tz"] = tz if re.fullmatch(r"[A-Za-z]+(?:/[A-Za-z0-9_+\-]+){0,2}|UTC", tz) else ""
    await tz_note(me)
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
            if _other_site(req):
                await send(ws, json.dumps({"type": "error", "error": "wrong origin"}))
                continue
            if q.get("confirm") is not None:                     # the owner's Confirm / Refuse on an agent's critical question (#3057)
                bad = await confirm_pick(req, me, q.get("confirm"), q.get("ok"))
                if bad:
                    print(f"[confirm] refused: {bad} ({me.get('role')}/{me.get('name')}@{me.get('ip')})", flush=True)
                    await send(ws, json.dumps({"type": "error", "error": bad}))
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
            if q.get("st_set") in ("here", "away", "busy", "sleep"):   # the status by hand, one of four (the owner, #3020); with it away_set is not read
                k, kind = me["id"] or me["name"], q["st_set"]
                cur = chat.away_manual.get(k)
                was = "here" if cur is None else cur.get("kind", "away") if isinstance(cur, dict) else "away"
                if kind != was:
                    if kind == "here":
                        chat.away_manual.pop(k, None)
                    else:
                        chat.away_manual[k] = round(time.time(), 1) if kind == "away" else {"at": round(time.time(), 1), "kind": kind}
                    chat.away_file.write_text(json.dumps(chat.away_manual, ensure_ascii=False))
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
            if q.get("caps_set") is not None:                    # this page shows live captions (#2756): speakers transcribe only while somebody reads
                if _ws_rate(me, "caps_set", 4):
                    me["capdst"] = q.get("caps_dst") if q["caps_set"] and q.get("caps_dst") in CC_LANGS else ""   # read in this language (#3621)
                    if bool(q["caps_set"]) != bool(me.get("caps")):
                        me["caps"] = bool(q["caps_set"])
                        await chat.send_presence()
                continue
            if isinstance(q.get("cap"), dict):                   # a piece of what this person says, from their own page: to every page, not kept
                c = q["cap"]
                if me["role"] in ("owner", "verified") and isinstance(c.get("text"), str) and _ws_rate(me, "cap", 12):
                    seg = str(c.get("seg") or "")[:24]
                    await chat.watch_all({"type": "cap", "name": me["name"], "text": c["text"][:600], "final": bool(c.get("final")), "seg": seg})
                    if c.get("final") and c["text"].strip() and chat.cap_langs():   # a finished phrase: in the languages read now
                        asyncio.create_task(chat.cap_tr(me["name"], seg, c["text"][:600], c.get("lang") if c.get("lang") in CC_LANGS else ""))
                continue
            if isinstance(q.get("ptt"), dict):                   # push to talk (#2773): the sound as it is spoken, to every page, not kept -
                c = q["ptt"]                                     # the voice message itself comes after, as an upload a message names
                a = c.get("a")
                if me["role"] in ("owner", "verified") and (a is None or (isinstance(a, str) and len(a) <= 90000)) and _ws_rate(me, "ptt", 40):
                    await chat.watch_all({"type": "ptt", "name": me["name"], "seg": str(c.get("seg") or "")[:24], "a": a,
                                          "end": bool(c.get("end")), "cancel": bool(c.get("cancel"))})
                continue
            if isinstance(q.get("trust_set"), dict):                # the owner's trust in a person, 0-10; null: none (#2726). Nobody else sets or sees it
                t = q["trust_set"]
                who = next((x for x in chat.viewers.values() if x.get("name") == t.get("name") and x.get("role") != "owner"), None)
                lv = t.get("level")
                if me["role"] == "owner" and who and (lv is None or (isinstance(lv, int) and not isinstance(lv, bool) and 0 <= lv <= 10)):
                    k = who["id"] or who["name"]
                    if lv is None:
                        chat.trust.pop(k, None)
                    else:
                        chat.trust[k] = lv
                    chat.trust_file.write_text(json.dumps(chat.trust, ensure_ascii=False))
                    print(f"[trust] {who['name']}: {lv}", flush=True)
                    await chat.send_presence()
                continue
            if q.get("showlang_set") is not None or q.get("showtz_set") is not None:   # "Show my language / time zone" (#2222)
                k = me["id"] or me["name"]
                for f, st, fl in (("showlang_set", chat.showlang, chat.showlang_file), ("showtz_set", chat.showtz, chat.showtz_file)):
                    if q.get(f) is not None and bool(q[f]) != (k in st):
                        (st.add if q[f] else st.discard)(k)
                        fl.write_text(json.dumps(sorted(st), ensure_ascii=False))
                await tz_note(me)
                await chat.send_presence()
                continue
            if "ping" in q and len(q) == 1:                       # the page measures its round trip to this server (the owner, #2489)
                t = q["ping"]
                await send(ws, json.dumps({"type": "pong", "t": t if isinstance(t, (int, float)) and math.isfinite(t) else 0}))
                continue
            if "rtt" in q and len(q) == 1:                        # ... and says what it got: shown on its person's card
                try:
                    rtt = max(0, min(60000, int(q["rtt"])))
                except (TypeError, ValueError, OverflowError):
                    continue
                # everyone's cards are sent again only when it changed, and at most every 10 s for a tab (the audit before 0.2.0)
                was, me["rtt"] = me.get("rtt"), rtt
                if rtt != was and time.time() - me.get("rtt_at", 0) >= 10:
                    me["rtt_at"] = time.time()
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
            if q.get("pad_new") is not None:                     # a scratchpad put into the chat (the owner, #3741), with a caption or none
                if me["role"] in ("owner", "verified") and _ws_rate(me, "pad_new", 1):
                    await chat.post("msg", {"name": me["name"], "task": me["task"]}, " ".join(str(q["pad_new"]).split())[:300], None, pad=1)
                continue
            if isinstance(q.get("pad"), dict):                   # drawing on one: what is drawn goes to every page that sees it
                if me["role"] in ("owner", "verified") and _ws_rate(me, "pad", 80):
                    bad = await chat.pad_draw(q["pad"], me, start)
                    if bad:
                        await send(ws, json.dumps({"type": "error", "error": bad}))
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
                if any(x["id"] == mid and x["kind"] == "msg" for x in chat.events[-5000:]) and chat.sees(mid, start):
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
                if not st["uri"].startswith("spotify:"):
                    continue                                 # Spotify alone: videos are not watched together any more (the owner, #3655)
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
            if q.get("edit") is not None:                        # a person changes their own message (#2930)
                try:
                    bad = await chat.edit(int(q["edit"]), str(q.get("text") or ""), me) if _ws_rate(me, "edit", 2) else ""
                except (TypeError, ValueError):
                    bad = ""
                if bad:
                    await send(ws, json.dumps({"type": "error", "error": bad}))
                continue
            if isinstance(q.get("views"), list):                 # messages that stood on this person's screen for two seconds (#2930)
                if me["role"] in ("owner", "verified") and _ws_rate(me, "views", 4):
                    await chat.viewed(q["views"], me, start)
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
            # a voice message with its words, put into other languages and kept under it: what live captions heard this person say
            # up to a pause (`cc`, the owner, #3617 - marked, so the pages show where it is from; never addressed nor a reply), or a
            # push to talk (`tr`, #3830 - an ordinary message otherwise: agents hear it, it is pushed)
            cc = isinstance(q.get("cc"), dict) and not to and not rep
            tq = q["cc"] if cc else q.get("tr")
            if not (isinstance(tq, dict) and me["role"] in ("owner", "verified") and CC_VOICE_RE.match(text) and _ws_rate(me, "ccmsg", 1)
                    and _cc_may(me, text)):
                tq = None
            tl = tq.get("l") if tq and tq.get("l") in CC_LANGS else ""
            e = await chat.post("msg", {"name": me["name"], "task": me["task"]}, text, to, reply=rep, doc=doc_of(q), all=every or None,
                                sh=shm.group(1).strip() if shm else None, cc={"l": tl} if cc and tq is not None else None)
            if tq is not None:
                asyncio.create_task(chat.cc_tr(e, tl, str(tq.get("to") or "")))
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
            # videos (the owner, 2026-11-03, #1426): up to 200 MB (#3454); avi / mkv / mov are made mp4 (H.264 + AAC) so every browser plays them
            "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/avi": ".avi", "video/x-matroska": ".mkv",
            # a voice message of the push-to-talk button (the owner, #2773): the page sends wav, kept as m4a every browser plays
            "audio/wav": ".wav", "audio/x-wav": ".wav",
            # an iPhone's photo (the owner, #3144): made a JPEG, which every browser and every agent reads
            "image/heic": ".heic", "image/heif": ".heic", "image/heic-sequence": ".heic", "image/heif-sequence": ".heic",
            # a PDF (the owner, #3295): kept as it is; the page shows its first page and opens it in a viewer
            "application/pdf": ".pdf"}
UP_PDF_MAX = 50 * 2 ** 20
UP_VOICE_MAX = 12 * 2 ** 20                         # 16 kHz mono: six minutes
UP_EXT = {".mp4": ".mp4", ".m4v": ".mp4", ".mov": ".mov", ".avi": ".avi", ".mkv": ".mkv", ".heic": ".heic", ".heif": ".heic", ".pdf": ".pdf"}   # a browser that sends no type: by the name
UP_VIDEO_MAX = 200 * 2 ** 20
# the service's PATH has no anaconda: ffmpeg next to the python that runs the server, else the system's
FFMPEG = str(Path(sys.executable).with_name("ffmpeg")) if Path(sys.executable).with_name("ffmpeg").exists() else (__import__("shutil").which("ffmpeg") or "ffmpeg")
UP_MAGIC = {".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".gif": b"GIF8", ".webp": b"RIFF"}
HEIC_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1")


def heic_to_jpeg(data: bytes) -> bytes | None:
    """An iPhone's HEIC photo as a JPEG, turned the way it was shot; None: this machine has nothing that reads HEIC.
    pillow-heif where it is installed (any system), else the Mac's own sips. Not ffmpeg: of an iPhone's file it takes the
    depth map or one tile, not the picture."""
    try:
        import io
        import pillow_heif
        from PIL import Image, ImageOps
        pillow_heif.register_heif_opener()
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue()
    except ImportError:
        pass
    except Exception as x:                              # a broken file, or a kind pillow-heif does not read: sips may
        print(f"[upload] pillow-heif: {x!r}", flush=True)
    sips = shutil.which("sips") or ("/usr/bin/sips" if Path("/usr/bin/sips").exists() else None)
    if not sips:
        return None
    import subprocess
    with tempfile.TemporaryDirectory(prefix="lagomheic-") as tmp:
        src, dst = Path(tmp) / "in.heic", Path(tmp) / "out.jpg"
        src.write_bytes(data)
        try:
            r = subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", "88", str(src), "--out", str(dst)],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as x:
            print(f"[upload] sips: {x!r}", flush=True)
            return None
        if r.returncode or not dst.exists() or not dst.stat().st_size:
            print(f"[upload] sips failed: {r.stderr.decode(errors='replace')[-300:]}", flush=True)
            return None
        return dst.read_bytes()


UP_KEEP = 14 * 86400                                # a picture is kept two weeks
IMG_ROOTS = [Path(p) for p in ("/tmp", str(UPLOADS), *auth.conf_lines("img_roots"))]   # more folders: <conf>/img_roots, a path a line
IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
VID_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}      # played inline by the page (Range requests work)
AUD_TYPES = {".m4a": "audio/mp4", ".wav": "audio/wav"}                # a voice message: the page draws it as a bubble with a play button
# a file a message names by its path: what the page shows and what a person or a token may fetch
MEDIA_PATH_RE = re.compile(r"(?:/[\w.\-]+)+\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v|m4a|wav|pdf)", re.I)
DOC_TYPES = {".pdf": "application/pdf"}             # a PDF (the owner, #3295): a card with its first page, a click opens it
PDF_PV = UPLOADS / ".pdf-pv"                        # the pages drawn, each once: in the user's own folder, not in a shared /tmp
PDF_MAX_PAGES = 500                                 # pages a card can show: every one is a 2000 px picture on the disk
_PDF_DRAW = asyncio.Semaphore(2)                    # pdftoppm at once: a person leafing fast does not take every core
PDF_PV_SIDE = 2000                                  # the longer side: a lone PDF is shown as wide as the chat (the owner, #3299)
PDF_TOOLS = [p for p in (shutil.which("pdftoppm"), "/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm", "/usr/bin/pdftoppm") if p and Path(p).exists()]


_PDF_PAGES: dict = {}


async def pdf_pages(p: Path) -> int:
    """how many pages a PDF has (the page's controls, the owner #3302): poppler's pdfinfo, else counted in the file itself
    (which misses the pages of a PDF that keeps its objects packed: then 1 and no controls)"""
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _PDF_PAGES:
        n = 0
        info = PDF_TOOLS and Path(PDF_TOOLS[0]).with_name("pdfinfo")
        if info and info.exists():
            try:
                pr = await asyncio.create_subprocess_exec(str(info), str(p), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(pr.communicate(), timeout=10)
                m = re.search(rb"^Pages:\s+(\d+)", out, re.M)
                n = int(m.group(1)) if m else 0
            except (OSError, asyncio.TimeoutError):
                pass
        if not n:
            n = len(re.findall(rb"/Type\s*/Page(?![s\w])", await asyncio.to_thread(p.read_bytes)))
        if len(_PDF_PAGES) > 500:
            _PDF_PAGES.clear()
        _PDF_PAGES[key] = max(1, min(n, PDF_MAX_PAGES))
    return _PDF_PAGES[key]


async def pdf_preview(p: Path, page: int = 1) -> Path | None:
    """a page of a PDF as a PNG (poppler's pdftoppm; the Mac's sips draws only the first), kept by the file's path, time and
    size; None when it can't be drawn - the page then shows the card without a picture"""
    st = p.stat()
    out = PDF_PV / (hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{PDF_PV_SIDE}|{page}".encode()).hexdigest()[:24] + ".png")
    if out.exists():
        return out if out.stat().st_size else None
    PDF_PV.mkdir(parents=True, exist_ok=True)
    tmp = PDF_PV / f".{secrets.token_hex(6)}"
    made = tmp.with_suffix(".png")
    if PDF_TOOLS:
        cmd = [PDF_TOOLS[0], "-png", "-f", str(page), "-l", str(page), "-scale-to", str(PDF_PV_SIDE), "-singlefile", str(p), str(tmp)]
    elif page == 1 and Path("/usr/bin/sips").exists():
        cmd = ["/usr/bin/sips", "-s", "format", "png", "-Z", str(PDF_PV_SIDE), str(p), "--out", str(made)]
    else:
        return None
    async with _PDF_DRAW:
        try:
            pr = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(pr.wait(), timeout=20)
            except asyncio.TimeoutError:
                pr.kill()
        except OSError as x:
            print(f"[pdf] preview: {x!r}", flush=True)
    if made.exists() and made.stat().st_size:
        made.replace(out)
        return out
    made.unlink(missing_ok=True)
    out.write_bytes(b"")                              # not asked again: a file that can't be drawn stays so
    return None
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


PROG_RE = re.compile(r"(.*?)\s*\[[#=\-. ]{2,}\]\s*(\d{1,3})\s*%", re.S)      # the page's ustHtml reads a bar the same way


def status_prog(was: dict | None, s: str, now: int) -> dict | None:
    """the times of a job shown as a bar in a status, for the time it has been going and the time left on the card (the owner,
    #3670): when this job's bar came first (t0) and with what percent (p0), the percent now (p) and when it last grew (t), when
    it ended (end: 100% or FAILED). The job is the text before the bar: another name or a lower percent starts anew"""
    m = PROG_RE.match(s)
    if not m:
        return None
    job, pc = m.group(1), min(100, int(m.group(2)))
    if not was or was.get("job") != job or pc < was["p"]:
        g = {"job": job, "t0": now, "p0": pc, "p": pc, "t": now}
    else:
        g = {**was, "p": pc, "t": now if pc > was["p"] else was["t"]}
    g["end"] = (g.get("end") or now) if pc == 100 or re.search(r"\bFAILED\b", s) else None
    return g


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
# {email: {"all": bool, "off": [a chat's address], "prefs": {key: value}}}; only the keys the page lists, short strings.
# Every setting of the page and the profile, for the chats picked (the owner, #3523): "off" names the chats left out (one not
# named takes part, a new one too); a value is kept even when empty, so a key that is not there was never shared
SHARED_PREFS = Path(os.environ.get("LAGOMCHAT_SHARED_PREFS", Path.home() / ".config/agentchat-chats/prefs.json"))
# (the theme, the panels, the camera and the captions are each chat's own: OWN_KEYS, never shared - audit)
PREF_KEYS = {"ac_padc", "ac_padw", "ac_lang", "ac_tsz", "ac_tbs", "ac_sbs", "ac_side", "ac_enter", "ac_hidevotes", "ac_hidesys", "ac_hidetr",
             "ac_joinsnd", "ac_awayafter", "ac_hideviews", "ac_cord", "ac_agsort", "ac_agrev", "ac_ppsort", "ac_pprev",
             "sv_showlang", "sv_showtz", "sv_nocc",                    # what a chat keeps by the person: its page sets it there
             "pf_name", "pf_color", "pf_mono", "pf_yt", "pf_status", "pf_snd_in", "pf_snd_out", "pf_photo"}
# what each chat keeps for itself, by the person and the chat's address (the owner, #3802): the theme, the side panel, the camera's
# and the microphone's settings. A chat shown in a split pane of the Mac app has a storage of its own there (WebKit keeps a
# frame's apart by the page around it), so the browser alone lost them: the page takes them from here
OWN_KEYS = {"ac_thm", "ac_aside_w", "ac_aside_off", "ac_mirror", "ac_micgain", "ac_camfold", "ac_caps", "ac_capsl", "ac_capst", "ac_capsv"}


def _own_key(k) -> bool:
    return isinstance(k, str) and (k in OWN_KEYS or re.fullmatch(r"ac_fold_[a-z0-9_]{1,24}", k) is not None)


def _shared_prefs_load() -> dict:
    try:
        d = json.loads(SHARED_PREFS.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


async def prefs_loop() -> None:
    """the shared settings changed in another chat of this machine: this chat's pages of that person are told and take them at
    once. Before, a page asked only when it came into view, so a chat window that stays on the screen never learnt of a
    picture or a monogram set in another one (the owner, #3543)"""
    def stamp() -> int:
        try:
            return SHARED_PREFS.stat().st_mtime_ns
        except OSError:
            return 0                                  # no file yet: its first writing is a change too
    seen, was = stamp(), _shared_prefs_load()
    while True:
        await asyncio.sleep(1.5)
        m = stamp()
        if m == seen:
            continue
        seen, cur = m, _shared_prefs_load()
        changed = {em for em in set(cur) | set(was) if cur.get(em) != was.get(em)}
        was = cur
        for ws, v in list(chat.viewers.items()):
            if str(v.get("em") or "").lower() in changed:
                await send(ws, '{"type": "prefs"}')


def _my_chats(em: str) -> list[dict]:
    """the chats of this machine a person is let into, by the email they signed in with: they own it, or are on its list by
    address or by domain. Asked of the install's config, never of what the person's page says: the titles of the others and
    the files in them are not theirs to see"""
    try:
        import lagomcfg
        c = lagomcfg.load()
    except Exception:
        return []
    def mine(ch: dict) -> bool:
        g, low = ch.get("guests") or {}, lambda v: [str(x).strip().lower() for x in v or []]
        return em == str(ch.get("owner_email") or c.get("owner_email") or "").strip().lower() or em in low(g.get("google_emails")) \
            or em.rsplit("@", 1)[-1] in low(g.get("google_domains"))
    return [ch for ch in c.get("chats", {}).values() if isinstance(ch, dict) and em and mine(ch)]


def _prefs_chats(req: web.Request, em: str) -> tuple[str, list | None, dict]:
    """this chat's address as the shared settings name it, the chats of this machine the person may pick among (None: no
    install around, the page shows the list it has), and their titles by address"""
    host = req.headers.get("X-Forwarded-Host") or req.headers.get("Host", "")
    here, urls, titles = _chat_url(host if host.startswith(("127.", "localhost")) else host.split(":")[0]), None, {}
    try:
        import lagomcfg
        chats = lagomcfg.load().get("chats", {})
        if chats:
            here = next((lagomcfg.chat_url(ch) for ch in chats.values() if str(ch.get("port")) == str(PORT)), here)
            urls = []
            for ch in _my_chats(em):
                u = lagomcfg.chat_url(ch)
                urls.append(u)
                try:
                    titles[u] = (Path(str(ch.get("data") or "/nonexistent")) / "title.txt").read_text().strip()
                except OSError:
                    titles[u] = ""
                titles[u] = titles[u] or str(ch.get("title") or "")
            if here not in urls:
                urls.insert(0, here)
    except Exception:
        pass
    return here, urls, {u: t for u, t in titles.items() if t}


def _take_file(em: str, to: Path, name: str) -> bool:
    """a person's own sound or picture goes with their settings: the file of this name is here, or is copied here from the
    same folder of another chat of this machine the person is let into"""
    if (to / name).is_file():
        return True
    for ch in _my_chats(em):
        p = Path(str(ch.get("data") or "/nonexistent")) / to.name / name
        try:
            if p.is_file():
                to.mkdir(parents=True, exist_ok=True)
                (to / name).write_bytes(p.read_bytes())
                return True
        except OSError:
            pass
    return False


async def api_prefs(req: web.Request) -> web.Response:
    v = await viewer_of(req)
    em = str(v.get("em") or "").lower()
    if not em or v["role"] not in ("owner", "verified"):
        return web.json_response({"ok": False, "error": "sign in first"}, status=401, headers={"Cache-Control": "no-store"})
    if req.method == "GET":
        cur = _shared_prefs_load().get(em) or {}
        here, urls, titles = _prefs_chats(req, em)
        return web.json_response({"ok": True, "all": bool(cur.get("all")), "off": cur.get("off") or [], "here": here, "urls": urls, "titles": titles,
                                  "prefs": cur.get("prefs") or {}, "own": (cur.get("own") or {}).get(here) or {}}, headers={"Cache-Control": "no-store"})
    if not _same_page(req):
        return web.json_response({"ok": False, "error": "refused: another site"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    if not isinstance(q, dict):
        q = {}
    here, urls, titles = _prefs_chats(req, em)
    # every chat of the machine writes this one file: read, change and replace under a lock, with no await between (as the rail's)
    SHARED_PREFS.parent.mkdir(parents=True, exist_ok=True)
    with open(SHARED_PREFS.with_name(SHARED_PREFS.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _shared_prefs_load()
        cur = d.get(em) if isinstance(d.get(em), dict) else {}
        if "all" in q:
            cur["all"] = bool(q["all"])
        if isinstance(q.get("off"), list):
            cur["off"] = sorted({u for u in q["off"][:200] if isinstance(u, str) and re.fullmatch(r"https?://[^\s/]{1,200}", u)})
        if isinstance(q.get("prefs"), dict) and here not in (cur.get("off") or []):   # a chat left out keeps its own and gives nothing
            p = dict(cur.get("prefs") or {})
            for k, val in q["prefs"].items():
                if k in PREF_KEYS:
                    p[k] = "" if val is None else str(val)[:300]
            cur["prefs"] = p
        if isinstance(q.get("own"), dict) and here:
            own = dict(cur.get("own") or {})
            m = dict(own.get(here) or {})
            for k, val in list(q["own"].items())[:60]:
                if not _own_key(k):
                    continue
                if val is None or val == "":
                    m.pop(k, None)                       # gone back to the default: not kept
                elif k in m or len(m) < 100:              # a page's own settings: a hundred at most (audit)
                    m[k] = str(val)[:300]
            own[here] = m
            cur["own"] = own
        d[em] = cur
        tmp = SHARED_PREFS.with_name(f"{SHARED_PREFS.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        tmp.replace(SHARED_PREFS)
    return web.json_response({"ok": True, "all": bool(cur.get("all")), "off": cur.get("off") or [], "here": here, "urls": urls, "titles": titles,
                              "prefs": cur.get("prefs") or {}, "own": (cur.get("own") or {}).get(here) or {}})


# the left rail's list of chats, its order and folders (the owner, #2777): one copy for every chat on this machine, kept here by the
# person (the machine's owner under one key, whatever address he signed in with; anybody else by the email). Before, it lived in the
# browser's cookies alone, and a chat that still held a cookie of its own showed an order of its own.
# {key: {"v": n, "lay": [host | {id, n, o, u: [host]}] | null, "chats": [{u, t}]}}
RAIL_FILE = Path(os.environ.get("LAGOMCHAT_SHARED_RAIL", SEEN_FILE.with_name("rail.json")))


def _rail_load() -> dict:
    try:
        d = json.loads(RAIL_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _ws_rate(me: dict, kind: str, per_sec: int) -> bool:
    """a page's socket may send this many frames of a kind a second, the rest are dropped: what is relayed to every page
    (the sound of push to talk, the captions) is not a way to flood them"""
    now = time.monotonic()
    t0, n = me.setdefault("_rate", {}).get(kind, (now, 0))
    if now - t0 >= 1:
        t0, n = now, 0
    me["_rate"][kind] = (t0, n + 1)
    return n < per_sec


def _int(x) -> int:
    """a number out of a request, 0 for anything that is not one"""
    try:
        return int(x or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


async def api_rail(req: web.Request) -> web.Response:
    v = await viewer_of(req)
    key = "@owner" if v.get("role") == "owner" else str(v.get("em") or "").lower() if v.get("role") == "verified" else ""
    if not key:
        return web.json_response({"ok": False, "error": "sign in first"}, status=401, headers={"Cache-Control": "no-store"})
    if req.method == "GET":
        d = _rail_load()
        cur = d.get(key) if isinstance(d.get(key), dict) else {}
        return web.json_response({"ok": True, "v": int(cur.get("v") or 0), "lay": cur.get("lay"), "chats": cur.get("chats") or []},
                                 headers={"Cache-Control": "no-store"})
    if not _same_page(req):
        return web.json_response({"ok": False, "error": "refused: another site"}, status=403)
    try:
        q = await req.json()
    except ValueError:
        q = {}
    if not isinstance(q, dict):
        q = {}
    # every chat of the machine writes this one file: read, change and replace under a lock, with no await between
    RAIL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RAIL_FILE.with_name(RAIL_FILE.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _rail_load()
        cur = d.get(key) if isinstance(d.get(key), dict) else {}
        _rail_put(q, cur)
        d[key] = cur
        tmp = RAIL_FILE.with_name(f"{RAIL_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        tmp.replace(RAIL_FILE)
    return web.json_response({"ok": True, "v": cur["v"]})


def _rail_put(q: dict, cur: dict) -> None:
    """what the page sent, checked, into the person's own entry of the rail"""
    ok_u = lambda u: isinstance(u, str) and 0 < len(u) <= 200
    if "lay" in q:
        lay = None
        if isinstance(q["lay"], list):
            lay = []
            for x in q["lay"][:120]:
                if ok_u(x):
                    lay.append(x)
                elif isinstance(x, dict) and isinstance(x.get("u"), list):
                    lay.append({"id": str(x.get("id") or "")[:12], "n": str(x.get("n") or "")[:40], "o": 1 if x.get("o") else 0,
                                "u": [u for u in x["u"][:60] if ok_u(u)]})
        cur["lay"] = lay
    if isinstance(q.get("chats"), list):
        cur["chats"] = [{"u": x["u"], "t": str(x.get("t") or "")[:80]} for x in q["chats"][:60] if isinstance(x, dict) and ok_u(x.get("u"))]
    cur["v"] = int(cur.get("v") or 0) + 1


async def api_status(req: web.Request) -> web.Response:
    v = await need_viewer(req)
    if not _same_page(req):
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
    if not _same_page(req):
        raise web.HTTPForbidden()
    task = v.get("task") or ""
    if not task:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    cur = dict(chat.photos.get(task) or {})
    old = cur.get("h")
    if req.query.get("remove"):
        cur = {}
    elif req.query.get("copy"):
        # the picture the person has in another chat of this machine, by its name there (the owner, #3523: the profile goes with
        # the settings to the chats picked); shown at once
        h = req.query["copy"]
        if not re.fullmatch(r"[0-9a-f]{16}", h) or not _take_file(str(v.get("em") or "").lower(), chat.photos_dir, f"{h}.jpg"):
            return web.json_response({"ok": False, "error": "no_photo"}, status=400)
        cur = {"h": h, "on": True}
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
    if not _same_page(req):
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
    if req.query.get("copy"):
        # the sound the person has in another chat of this machine, by its name there (the owner, #3523: the profile goes with
        # the settings to the chats picked)
        h = req.query["copy"]
        if not re.fullmatch(r"[0-9a-f]{16}", h) or not _take_file(str(v.get("em") or "").lower(), chat.sounds_dir, f"{h}.mp3"):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        store(h)
        print(f"[sound] {v.get('name')} {kind} {h}: from the shared settings", flush=True)
        await chat.send_presence()
        return web.json_response({"ok": True, "hash": h})
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


def _other_site(req: web.Request) -> bool:
    """a request another site's page sent from the person's browser: its Origin is not this host. One with no Origin (a
    command line, not a browser's POST) is not"""
    org = req.headers.get("Origin")
    return bool(org) and urlparse(org).netloc not in (req.headers.get("X-Forwarded-Host"), req.headers.get("Host"))


def _same_page(req: web.Request) -> bool:
    """only the page of this very host: its Origin, or (a browser that sends none) Sec-Fetch-Site same-origin; the host as the
    browser used it - through a proxy X-Forwarded-Host"""
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
    if _other_site(req):
        raise web.HTTPForbidden()
    ext = UP_TYPES.get(req.headers.get("Content-Type", "").split(";")[0].strip().lower()) or \
        UP_EXT.get(Path(urllib.parse.unquote(req.headers.get("X-Filename", ""))).suffix.lower())
    if not ext:
        raise web.HTTPUnsupportedMediaType(text="png, jpeg, gif, webp, heic, pdf or a video: mp4, mov, avi, mkv")
    data = await req.read()
    if ext == ".heic":                                  # an iPhone's photo: a JPEG from here on
        if data[4:8] != b"ftyp" or data[8:12] not in HEIC_BRANDS or len(data) > 25 * 2 ** 20:
            raise web.HTTPUnsupportedMediaType(text="not a heic picture (or over 25 MB)")
        data = await asyncio.to_thread(heic_to_jpeg, data)
        if not data or not data.startswith(b"\xff\xd8"):
            raise web.HTTPUnsupportedMediaType(text="this heic picture could not be read; send it as a jpeg")
        ext = ".jpg"
    video = ext in (".mp4", ".mov", ".avi", ".mkv")
    if ext == ".pdf":
        if not data.startswith(b"%PDF-") or len(data) > UP_PDF_MAX:
            raise web.HTTPUnsupportedMediaType(text="not a pdf file (or over 50 MB)")
    elif ext == ".wav":
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE" or len(data) > UP_VOICE_MAX:
            raise web.HTTPUnsupportedMediaType(text="not a wav file (or over 12 MB)")
    elif video:
        if len(data) > UP_VIDEO_MAX:
            raise web.HTTPRequestEntityTooLarge(max_size=UP_VIDEO_MAX, actual_size=len(data))
        ok = (data[4:8] in (b"ftyp", b"moov", b"wide", b"mdat", b"free", b"skip") if ext in (".mp4", ".mov") else
              data[:4] == b"RIFF" and data[8:11] == b"AVI" if ext == ".avi" else data[:4] == b"\x1aE\xdf\xa3")
        if not ok:
            raise web.HTTPUnsupportedMediaType(text="not a video of that type")
    elif not data.startswith(UP_MAGIC[ext]) or (ext == ".webp" and data[8:12] != b"WEBP") or len(data) > 25 * 2 ** 20:
        raise web.HTTPUnsupportedMediaType(text="not a picture of that type (or over 25 MB)")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(16)}"   # not to be guessed from another chat (the owner, #2538)
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
    if ext == ".wav":                                   # a voice message: m4a (AAC), a tenth of the size; no ffmpeg - the wav stays
        out = UPLOADS / f"{stem}.m4a"
        try:
            pr = await asyncio.create_subprocess_exec(
                FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", str(p), "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", str(out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(pr.communicate(), timeout=60)
            if pr.returncode == 0 and out.exists() and out.stat().st_size:
                p.unlink(missing_ok=True)
                p = out
            else:
                print(f"[upload] ffmpeg failed for {p.name}: {err.decode(errors='replace')[-300:]}", flush=True)
        except (OSError, asyncio.TimeoutError) as x:
            print(f"[upload] ffmpeg: {x!r}", flush=True)
            try:
                pr.kill()                               # one that ran out of its minute does not stay behind
            except (NameError, ProcessLookupError):
                pass
    print(f"[upload] {p.name} {p.stat().st_size // 1024} KB by {v.get('name')}", flush=True)
    # the uploader sees the file at once, before a message names it: the attachment's thumbnail in the box (#1627: a person
    # who is not the owner got a broken thumbnail, the img route showing only what visible messages name)
    chat.mine_up.setdefault(v.get("task") or "", set()).add(str(p))
    return web.json_response({"path": str(p), "bytes": p.stat().st_size})


async def api_img(req: web.Request):
    """an image a message names by its path (the page shows it): only image files, only under IMG_ROOTS, no hidden dirs"""
    if "signin" in req.query:
        # a way out of Access's "Unable to find your Access organization" (the owner, #2767): the browser still holds this chat's cookie signed
        # under the team's former name; Cloudflare reads that name from it and stops, before any sign-in. This path is not behind Access, so the
        # server can take the stale cookie away and send the browser to the chat - Access then signs it in anew
        r = web.HTTPFound("/")
        r.del_cookie("CF_Authorization", path="/", secure=True, httponly=True)
        if "CF_Authorization" in req.cookies:
            print(f"[auth] the Access cookie taken away at {str(req.headers.get('Host'))[:80]}: sign in anew", flush=True)
        raise r

    raw = req.query.get("p", "")
    tok = token_of(req) if from_proxy(req) else None
    if tok:
        if raw not in remote_paths(tok):
            raise web.HTTPNotFound()
        since = tok["since"]
    else:
        v = await need_viewer(req)
        since = None if v["role"] == "owner" else chat.start_for(v)
        if (v["role"] != "owner" and raw not in chat.mine_up.get(v.get("task") or "", ())
                and raw not in {a.get("av") for a in chat.aliases.get(viewer_key(v), {}).values()} and raw not in chat.visible_paths(chat.start_for(v))):
            raise web.HTTPNotFound()
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise web.HTTPNotFound()
    ext = p.suffix.lower()
    ok = (p.is_file() and ((ext in IMG_TYPES and p.stat().st_size < 40 * 2 ** 20) or (ext in VID_TYPES and p.stat().st_size < MAX_VIDEO)
                           or (ext in AUD_TYPES and p.stat().st_size < 2 * UP_VOICE_MAX) or (ext in DOC_TYPES and p.stat().st_size <= UP_PDF_MAX))
          and any(r in p.parents for r in IMG_ROOTS) and not any(x.startswith(".") for x in p.parts)
          and (ext not in DOC_TYPES or UPLOADS.resolve() in p.parents))    # a document: only one that was uploaded, not any PDF of a served folder
    if not ok:
        raise web.HTTPNotFound()
    # an import's files have names one can guess (the chat's and the message's numbers), and the uploads folder is one for every
    # chat: such a path counts only when a message of the import names it, not when somebody typed it
    if since is not None and any(r in p.parents for r in IMPORT_ROOTS) and not any(
            e["id"] >= since and ("tg" in e or "im" in e) and raw in MEDIA_PATH_RE.findall(e.get("text") or "") for e in chat.events):
        raise web.HTTPNotFound()
    if ext in DOC_TYPES and req.query.get("pv") == "n":  # how many pages, for the card's controls
        return web.json_response({"pages": await pdf_pages(p) if PDF_TOOLS else 1}, headers={"Cache-Control": "max-age=300"})
    if ext in DOC_TYPES and req.query.get("pv", "").isdigit():   # a page of a PDF as a picture, for its card
        n = int(req.query["pv"][:5])
        if not 1 <= n <= await pdf_pages(p):
            raise web.HTTPNotFound()
        pv = await pdf_preview(p, n)
        if not pv:
            raise web.HTTPNotFound()
        return web.FileResponse(pv, headers={"Content-Type": "image/png", "Cache-Control": "max-age=86400"})
    hd = {"Content-Type": IMG_TYPES.get(ext) or VID_TYPES.get(ext) or AUD_TYPES.get(ext) or DOC_TYPES[ext], "Cache-Control": "max-age=300"}
    if ext in DOC_TYPES:                                 # shown by the browser's own viewer, never read as anything but a PDF
        hd.update({"Content-Disposition": f"inline; filename=\"{p.name}\"", "X-Content-Type-Options": "nosniff"})
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


def app_version() -> dict:
    """the version for the settings' About (the owner, #2484): the release's number (the app's tauri.conf.json, what the
    GitHub releases are tagged with) and the commit this working copy is on"""
    v, rev = "", ""
    try:
        v = json.loads((HERE / "desktop" / "src-tauri" / "tauri.conf.json").read_text()).get("version", "")
    except (OSError, ValueError):
        pass
    try:
        head = (HERE / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            f = HERE / ".git" / head[5:]
            if f.exists():
                head = f.read_text().strip()
            else:
                for line in (HERE / ".git" / "packed-refs").read_text().splitlines():
                    if line.endswith(" " + head[5:]):
                        head = line.split()[0]
                        break
        rev = head[:7] if len(head) >= 7 and all(c in "0123456789abcdef" for c in head[:40]) else ""
    except OSError:
        pass
    return {"v": v, "rev": rev}


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


CAL_OFFS = (1440, 60, 10, 5)            # minutes before an event a participant may be told at: a day, an hour, 10 and 5 minutes
CAL_OFF_TXT = {1440: "1 day", 60: "1 hour", 10: "10 minutes", 5: "5 minutes"}
CAL_MAX, CAL_KEEP = 500, 30 * 86400     # events at most; one that is over is kept a month
CAL_SLOTS = 300                         # slots of one person in one event at most: a week of whole days cut into hours fits (#2964)
CAL_MAX_MINE = 30                       # ... and of one maker who is not the owner: a guest does not fill the calendar


def _cal_me(v: dict) -> str:
    return "owner" if v["role"] == "owner" else v["name"]


def _cal_out(v: dict) -> list[dict]:
    me, now = _cal_me(v), time.time()
    return [{"id": e["id"], "title": e["title"], "at": e["at"], "by": e["by_name"], "by_task": e["by_task"], "who": e["who"],
             "mine": v["role"] == "owner" or e["by_task"] == v["task"], "in": me in e["who"], "notify": e.get("notify", {}).get(me, []),
             **({"dur": e["dur"], "voters": e.get("voters", []), # the slots of the others are not shown while the time is looked for (the owner, #2972): only who has given some.
                 # The owner sees them all and manages them (#3047)
                 "slots": e.get("slots", {}) if v["role"] == "owner" else {me: e.get("slots", {}).get(me, [])},
                 "manage": v["role"] == "owner" and not e.get("dl_told"),
                 "gave": [n for n in e.get("voters", []) if any(t > now for t in e.get("slots", {}).get(n, []))], "vote": me in e.get("voters", []) and not e.get("dl_told"),
                 "dl": e.get("dl", 0),
                 # no time suits everyone: the slots of mine that would make it happen, and who else has such a slot (2026-11-15)
                 **({"hint": h.get(me, []), "fix": [n for n in h if n != me and h[n]]} if h else {})} if e.get("dur") else {})}
            for e in sorted(chat.cal, key=lambda e: e["at"]) for h in [_cal_hints(e, now)]]


def _cal_free(e: dict, n: str, now: float) -> list[list[int]]:
    """the free stretches of one person of an event that looks for its time: slots that touch or overlap are one stretch"""
    dur, free = e["dur"] * 60, []
    for a in sorted(x for x in e.get("slots", {}).get(n, []) if x > now):
        if free and a <= free[-1][1]:
            free[-1][1] = max(free[-1][1], a + dur)
        else:
            free.append([a, a + dur])
    return free


def _cal_meet(e: dict, now: float, skip: str = "") -> list[list[int]]:
    """the stretches in which the event's whole length fits every one who answers (but `skip`)"""
    dur, common = e["dur"] * 60, None
    for n in e.get("voters", []):
        if n == skip:
            continue
        free = _cal_free(e, n, now)
        common = free if common is None else [[max(a, c), min(b, d)] for a, b in common for c, d in free if min(b, d) - max(a, c) >= dur]
        if not common:
            return []
    return common or []


def _cal_common(e: dict, now: float) -> int:
    """an event that looks for its time (the owner, #2706): the earliest start at which the event's whole length lies inside the slots of
    every one who answers. A person's slots that touch or overlap are one free stretch, so 14:00 + 15:00 of one and 14:30 of another meet"""
    common = _cal_meet(e, now)
    return min(a for a, _ in common) if common else 0


CAL_HINTS = 3                           # slots suggested to one person of an event at most


def _cal_hint(e: dict, me: str, now: float) -> list[int]:
    """no time suits everyone, and one slot of `me` would change that (the owner, 2026-11-15): the starts of such slots, the ones next
    to the slots `me` has given first - an hour earlier or later is the gentle ask. They lie where everybody else is free, one for a
    stretch; nothing when two of the others do not meet, for then no slot of `me` helps"""
    dur, dl, mine, out = e["dur"] * 60, e.get("dl") or 0, _cal_free(e, me, now), []
    for a, b in _cal_meet(e, now, skip=me):
        b = min(b, dl) if dl else b
        if b - a < dur:
            continue
        cand = {a, b - dur} | {min(max(x, a), b - dur) for c, d in mine for x in (c - dur, c, d - dur, d)}
        # how far the slot is from what `me` has given: 0 = it touches a stretch of theirs
        out.append(min((min((max(c - t - dur, t - d, 0) for c, d in mine), default=1 << 40), t) for t in cand if t > now))
    return [t for _, t in sorted(out)[:CAL_HINTS]]


def _cal_hints(e: dict, now: float) -> dict[str, list[int]]:
    """the hints of every one who answers an event that still looks for its time; nothing for any other event"""
    if not e.get("dur") or e["at"] or e.get("dl_told") or len(e.get("voters", [])) < 2:
        return {}
    return {n: _cal_hint(e, n, now) for n in e["voters"]}


def _cal_save() -> None:
    tmp = chat.cal_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(chat.cal, ensure_ascii=False))
    tmp.replace(chat.cal_file)


async def api_calendar(req: web.Request) -> web.Response:
    """the chat's calendar (the owner, #2704). GET: the events as this viewer sees them. POST {op: add, title, at, who: [names]}: a new one,
    its maker is in it; {op: del, id}: by its maker or the owner, who also take a participant out: {op: unwho, id, name}; {op: notify, id, offs: [minutes]}: a participant's own reminders.
    {op: add, title, dur: minutes, who} is an event without a date: {op: slot / unslot, id, at} is a time that suits the one who sends it;
    {op: who, id, name}: its maker or the owner adds a person later, and a time found by slots is open again;
    {op: dl, id, dl}: they move the deadline of an event that still looks for its time; {op: slot, id, ats: [times]}: several slots at once;
    {op: slot / unslot, ..., for: name}: the owner manages the slots of another person of the event"""
    v = await need_viewer(req)
    if req.method == "GET":
        return web.json_response({"ok": True, "events": _cal_out(v), "offs": CAL_OFFS}, headers={"Cache-Control": "no-store"})
    if not _same_page(req) or not v.get("task"):
        raise web.HTTPForbidden()
    try:
        q = await req.json()
        assert isinstance(q, dict)
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    me, now, op_ = _cal_me(v), time.time(), q.get("op")
    ev = next((e for e in chat.cal if e["id"] == q.get("id")), None)
    if op_ == "add":
        title = re.sub(r"\s+", " ", str(q.get("title") or "")).strip()[:200]
        # dur, minutes: an event without a date, its time is found by the slots of its people (#2706); dl: the deadline it has
        # to be held by (#2795), no slot ends after it
        at, dur, dl = (_int(q.get(k)) for k in ("at", "dur", "dl"))
        if not title or not (now - 86400 < at < now + 5 * 366 * 86400 if not dur else
                             5 <= dur <= 1440 and not at and now + dur * 60 < dl < now + 2 * 366 * 86400):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        if len(chat.cal) >= CAL_MAX or (v["role"] != "owner" and sum(1 for e in chat.cal if e["by_task"] == v["task"]) >= CAL_MAX_MINE):
            return web.json_response({"ok": False, "error": "too_many"})
        who = [me]
        for n in q.get("who") if isinstance(q.get("who"), list) else []:
            if isinstance(n, str) and PNAME.match(n) and n not in who and len(who) < 50:
                who.append("owner" if n.lower() in USER_NAMES else n)
        who = list(dict.fromkeys(who))
        agn = {a["name"] for a in chat.agents.values()}      # an agent in the event has no slots to give: the people answer
        chat.cal.append({"id": secrets.token_hex(6), "title": title, "at": at, "by_task": v["task"], "by_name": me, "who": who, "notify": {}, "sent": {},
                         "made": round(now), **({"dur": dur, "dl": dl, "voters": [n for n in who if n not in agn], "slots": {}} if dur else {})})
    elif op_ == "del" and ev:
        if not (v["role"] == "owner" or ev["by_task"] == v["task"]):
            return web.json_response({"ok": False, "error": "not_yours"}, status=403)
        chat.cal.remove(ev)
    elif op_ in ("slot", "unslot") and ev:
        t, ats = _int(q.get("at")), q.get("ats")
        # {for: name}: the owner gives or takes a slot of another person of the event (#3047); from anybody else it is their own
        whose = str(q.get("for") or "") if v["role"] == "owner" and q.get("for") else me
        if ev["at"] or not ev.get("dur") or whose not in ev.get("voters", []):
            return web.json_response({"ok": False, "error": "not_in"}, status=403)
        mine = ev.setdefault("slots", {}).setdefault(whose, [])
        if op_ == "unslot":
            if t in mine:
                mine.remove(t)
        elif isinstance(ats, list):
            # a span of time cut into slots comes as one request (the audit before 0.6.0): one write of the file and one word to
            # the pages, not one per slot. A slot that does not fit (passed, after the deadline, over the limit) is left out
            mine[:] = [x for x in mine if x > now]           # the ones that passed are dropped: the list does not grow for ever
            for x in ats[:CAL_SLOTS]:
                x = _int(x)
                if len(mine) >= CAL_SLOTS:
                    break
                if now < x < now + 2 * 366 * 86400 and x not in mine and not (ev.get("dl") and x + ev["dur"] * 60 > ev["dl"]):
                    mine.append(x)
            mine.sort()
        elif not (now < t < now + 2 * 366 * 86400) or len([x for x in mine if x > now]) >= CAL_SLOTS:      # the ones that passed do not count (#2964)
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        elif ev.get("dl") and t + ev["dur"] * 60 > ev["dl"]:
            return web.json_response({"ok": False, "error": "after_deadline"})
        elif t not in mine:
            mine[:] = [x for x in mine if x > now]           # the ones that passed are dropped: the list does not grow for ever
            mine.append(t)
            mine.sort()
        fixed = _cal_common(ev, now)
        if fixed:                                            # a time that suits everyone: it is the event's time from now on
            ev["at"] = fixed
            _cal_save()
            await chat.post("remind", {"name": ev["by_name"], "task": ev["by_task"]}, f'{ev["title"]}: the time is set, {when_txt(fixed)}',
                            to=list(ev["who"]), cal={"id": ev["id"], "title": ev["title"], "at": fixed, "fix": True})
    elif op_ == "unwho" and ev:                              # a participant taken out of the event by its maker or the owner (#2814): with their slots
        n = q.get("name")
        if not (v["role"] == "owner" or ev["by_task"] == v["task"]):
            return web.json_response({"ok": False, "error": "not_yours"}, status=403)
        if n not in ev["who"] or n == ev["by_name"]:
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        ev["who"].remove(n)
        for k in ("slots", "notify", "sent"):
            ev.get(k, {}).pop(n, None)
        if n in ev.get("voters", []):
            ev["voters"].remove(n)
        fixed = not ev["at"] and ev.get("dur") and not ev.get("dl_told") and _cal_common(ev, now)
        if fixed:                                            # the ones left already had a time in common
            ev["at"] = fixed
            _cal_save()
            await chat.post("remind", {"name": ev["by_name"], "task": ev["by_task"]}, f'{ev["title"]}: the time is set, {when_txt(fixed)}',
                            to=list(ev["who"]), cal={"id": ev["id"], "title": ev["title"], "at": fixed, "fix": True})
    elif op_ == "dl" and ev:                                 # the deadline moved by the event's maker or the owner (#2958): a day and a time
        dl = _int(q.get("dl"))
        if not (v["role"] == "owner" or ev["by_task"] == v["task"]):
            return web.json_response({"ok": False, "error": "not_yours"}, status=403)
        if ev["at"] or not ev.get("dur") or not (now + ev["dur"] * 60 < dl < now + 2 * 366 * 86400):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        if dl != ev.get("dl"):
            ev["dl"] = dl
            ev.pop("dl_told", None)                          # a deadline that was over and is moved on: the slots are open again
            for sl in ev.get("slots", {}).values():          # a slot that ends after the new deadline is gone
                sl[:] = [t for t in sl if t + ev["dur"] * 60 <= dl]
            _cal_save()
            await chat.post("remind", {"name": ev["by_name"], "task": ev["by_task"]}, f'{ev["title"]}: the deadline is now {when_txt(dl)}',
                            to=list(ev["who"]), cal={"id": ev["id"], "title": ev["title"], "at": 0, "dl": dl})
    elif op_ == "who" and ev:                                # a person added to an event already made, by its maker or the owner (#2909)
        n = q.get("name")
        if not (v["role"] == "owner" or ev["by_task"] == v["task"]):
            return web.json_response({"ok": False, "error": "not_yours"}, status=403)
        n = "owner" if isinstance(n, str) and n.lower() in USER_NAMES else n
        if not isinstance(n, str) or not PNAME.match(n) or n in ev["who"] or len(ev["who"]) >= 50 or (ev["at"] and ev["at"] < now):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        ev["who"].append(n)
        txt = f'{ev["title"]}: {n} is added'
        if ev.get("dur") and n not in {a["name"] for a in chat.agents.values()}:
            ev.setdefault("voters", []).append(n)
            # its time was found by the slots of its people: the new one has given none, so the time is open again (the slots
            # of the others stay), unless the deadline is over: then the time found is kept
            if not ev["at"] or not ev.get("dl") or ev["dl"] > now:
                if ev["at"]:
                    ev["at"], ev["sent"] = 0, {}
                    txt += ", the time is open again: add the slots that suit you"
                else:
                    txt += ": add the slots that suit you"
        _cal_save()
        await chat.post("remind", {"name": ev["by_name"], "task": ev["by_task"]}, txt, to=list(ev["who"]),
                        cal={"id": ev["id"], "title": ev["title"], "at": ev["at"]})
    elif op_ == "notify" and ev:
        if me not in ev["who"] or not ev["at"]:
            return web.json_response({"ok": False, "error": "not_in"}, status=403)
        offs = [o for o in CAL_OFFS if o in (q.get("offs") or [])] if isinstance(q.get("offs"), list) else []
        ev.setdefault("notify", {})[me] = offs
        # a moment that has passed already is not told late: it counts as told
        ev.setdefault("sent", {})[me] = sorted({*(o for o in ev["sent"].get(me, []) if o in offs), *(o for o in offs if ev["at"] - o * 60 <= now)})
    else:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    _cal_save()
    print(f"[cal] {v.get('name')}: {op_} {(ev or chat.cal[-1])['title'][:60]!r}", flush=True)
    await chat.watch_all({"type": "cal"})
    return web.json_response({"ok": True, "events": _cal_out(v)})


SV_MAX, SV_Q, SV_A = 50, 500, 8000      # questionnaires at most; the letters of a question and of an answer


def _sv_who(s: dict, more: str = "") -> list[str]:
    """whose tabs a questionnaire has: the owner, everyone let into the chat by email, whoever has answered, and the one who looks"""
    return list(dict.fromkeys(["owner", *access_people(), *s["ans"], *([more] if more else [])]))


def _sv_out(v: dict) -> list[dict]:
    """the questionnaires as this viewer sees them, the newest first. The answers of the others: all of them for the owner and in a
    questionnaire made open; in a closed one only who has answered"""
    me, own = _cal_me(v), v["role"] == "owner"
    return [{"id": s["id"], "q": s["q"], "made": s["made"], "open": bool(s.get("open", True)), "mine": own,
             "who": (who := _sv_who(s, me if v.get("task") else "")), "gave": [n for n in who if n in s["ans"]],
             "ans": {n: {"text": a["text"], "at": a["at"]} for n, a in s["ans"].items() if own or s.get("open", True) or n == me}}
            for s in sorted(chat.surveys, key=lambda s: -s["made"])]


def _sv_save() -> None:
    tmp = chat.surveys_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(chat.surveys, ensure_ascii=False))
    tmp.replace(chat.surveys_file)


async def api_surveys(req: web.Request) -> web.Response:
    """the questionnaires (the owner, #3498). GET: as this viewer sees them. POST {op: add, q, open}: a new one, the owner's alone to
    make; {op: del, id}: his too; {op: answer, id, text}: the answer of the one who sends it, saved over the one before - an empty
    text takes it back"""
    v = await need_viewer(req)
    if req.method == "GET":
        return web.json_response({"ok": True, "surveys": _sv_out(v)}, headers={"Cache-Control": "no-store"})
    if not _same_page(req) or not v.get("task"):
        raise web.HTTPForbidden()
    try:
        q = await req.json()
        assert isinstance(q, dict)
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    me, now, op_, told = _cal_me(v), time.time(), q.get("op"), False
    sv = next((s for s in chat.surveys if s["id"] == q.get("id")), None)
    text = re.sub(r"\n{3,}", "\n\n", str(q.get("q" if op_ == "add" else "text") or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    if op_ in ("add", "del") and v["role"] != "owner":
        return web.json_response({"ok": False, "error": "only the owner"}, status=403)
    if op_ == "add":
        if not text or len(text) > SV_Q:
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        if len(chat.surveys) >= SV_MAX:
            return web.json_response({"ok": False, "error": "too_many"})
        sv = {"id": secrets.token_hex(6), "q": text, "by_task": v["task"], "by_name": me, "made": round(now), "open": q.get("open") is not False, "ans": {}}
        chat.surveys.append(sv)
        told = True
    elif op_ == "del" and sv:
        chat.surveys.remove(sv)
    elif op_ == "answer" and sv:
        if len(text) > SV_A:
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        if text:
            sv["ans"][me] = {"text": text, "at": round(now), "task": v["task"]}
        else:
            sv["ans"].pop(me, None)
    else:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    _sv_save()
    print(f"[survey] {v.get('name')}: {op_} {sv['q'][:60]!r}", flush=True)
    await chat.watch_all({"type": "survey"})
    if told:                                                 # a line in the chat for the people it asks (the owner, #3500): on a phone the widget is a page away
        await chat.post("remind", {"name": me, "task": v["task"]}, f'New questionnaire: {sv["q"]}',
                        to=[n for n in _sv_who(sv) if n != "owner"] or None, sv={"id": sv["id"], "q": sv["q"]})
    return web.json_response({"ok": True, "surveys": _sv_out(v)})


async def cal_loop() -> None:
    """every 10 s: a participant who asked is told a day / an hour / 10 / 5 minutes before the event, as a reminder for them alone"""
    while True:
        try:
            now, dirty = time.time(), False
            for e in list(chat.cal):
                if e not in chat.cal:                            # deleted while a line of this pass was being posted
                    continue
                if (e["at"] or e.get("made", 0) + 2 * CAL_KEEP) < now - CAL_KEEP:      # one that never found its time: three months
                    chat.cal.remove(e)
                    dirty = True
                    continue
                if not e["at"] and e.get("dl") and now > e["dl"] and not e.get("dl_told"):   # the deadline came and no time suited everyone (#2795)
                    e["dl_told"] = True
                    dirty = True
                    await chat.post("remind", {"name": e["by_name"], "task": e["by_task"]}, f'{e["title"]}: no time suited everyone before the deadline',
                                    to=list(e["who"]), cal={"id": e["id"], "title": e["title"], "at": 0, "late": True})
                    await chat.watch_all({"type": "cal"})
                for name, offs in list(e.get("notify", {}).items()):
                    sent = e.setdefault("sent", {}).setdefault(name, [])
                    for o in sorted(offs):                       # the nearest first: after a pause of the server one line, not four
                        if o in sent or e["at"] - o * 60 > now:
                            continue
                        late = now >= e["at"] or any(x < o and e["at"] - x * 60 <= now for x in offs)
                        sent.append(o)
                        dirty = True
                        if not late and name in e["who"]:
                            await chat.post("remind", {"name": e["by_name"], "task": e["by_task"]}, f'{e["title"]}: starts in {CAL_OFF_TXT[o]}',
                                            to=[name], cal={"id": e["id"], "title": e["title"], "at": e["at"], "off": o})
            if dirty:
                _cal_save()
        except Exception as x:
            print(f"[cal] {x!r}", flush=True)
        await asyncio.sleep(10)


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


PAGE_AT: dict = {}                                   # the page as it is served now: its tag, when it was first served


async def split_page(_req):
    """the window split into several chats (the owner, #3776): each pane a chat's own page in a frame, the layout in the
    address' hash. The page holds nothing of the chat: the frames sign in as the chats' pages do"""
    return web.FileResponse(HERE / "split.html", headers={"Cache-Control": "no-cache"})


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
    body = body.replace("<head>", "<head><script>window.AC_DOMAINS=" + json.dumps(settings()).replace("<", "\\u003c") + f";window.AC_BUILT={built_at():.0f};window.AC_PORT={PORT};window.AC_VER=" + json.dumps(app_version()) + "</script>", 1)
    # highlight.js inside the page (the owner, #564): a request of its own for the file may not get through a proxy's sign-in
    try:
        hl = (HERE / "vendor" / "highlight.min.js").read_text()
        head, tail = body.rsplit("</body>", 1)
        body = f"{head}<script>{hl}</script></body>{tail}"
    except (OSError, ValueError):
        pass                                              # no library or no </body>: plain code blocks
    # going between chats (the owner, #2440): the page is ~1 MB; the browser keeps it and asks with its tag, the same page is a
    # 304 with no body, a new one comes compressed
    # measured (#3252): through Cloudflare the tag did not come back and the whole page was sent at every visit (a proxy that
    # rewrites the HTML drops its ETag). So: no-transform, and the time this very page was first served as Last-Modified, which
    # a proxy keeps. Supports-Loading-Mode: the page of another chat of the domain may open this one ahead (the page's specChat)
    tag = '"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
    if PAGE_AT.get("tag") != tag:
        PAGE_AT.update(tag=tag, at=int(time.time()))
    h = {"ETag": tag, "Cache-Control": "no-cache, no-transform", "Last-Modified": formatdate(PAGE_AT["at"], usegmt=True),
         "Supports-Loading-Mode": "credentialed-prerender"}
    inm = _req.headers.get("If-None-Match")
    try:
        since = parsedate_to_datetime(_req.headers["If-Modified-Since"]).timestamp() if inm is None and "If-Modified-Since" in _req.headers else None
    except (TypeError, ValueError):
        since = None
    if tag.strip('"') in [x.strip().removeprefix("W/").strip('"') for x in (inm or "").split(",")] or (since is not None and since >= PAGE_AT["at"]):
        return web.Response(status=304, headers=h)
    r = web.Response(text=body, content_type="text/html", headers=h)
    r.enable_compression()
    return r


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
    start = chat.start_for(v)
    # only the messages of this kind / of talks on this topic (talks.py) / with a picture of this kind (imgdesc.py)
    kind, topic, pic = req.query.get("kind", ""), req.query.get("topic", ""), req.query.get("pic", "")
    kind, topic, pic = kind if kind in talks_mod.KINDS else "", topic if topic in talks_mod.TOPICS else "", pic if pic in PIC_KINDS else ""

    def wanted(i: int) -> bool:
        return ((not kind or chat.talks.kind.get(i) == kind) and (not topic or chat.talks.topic.get(i) == topic)
                and (not pic or any((chat.img.get(p) or {}).get("kind") == pic for p in chat.pics.get(i, ()))))

    lists = {"kinds": talks_mod.KINDS, "topics": talks_mod.TOPICS, "pkinds": PIC_KINDS, "classified": len(chat.talks.kind)}
    if len(q) < 2:
        # no text: the categories are browsed (the owner, #3216) - how many messages each holds, and with one picked, its
        # messages and talks, the newest first
        counts = {"kinds": {}, "topics": {}, "pkinds": {}}
        for i, k in chat.talks.kind.items():
            if i >= start and i not in chat.deleted:
                counts["kinds"][k] = counts["kinds"].get(k, 0) + 1
                t = chat.talks.topic.get(i, "other")
                counts["topics"][t] = counts["topics"].get(t, 0) + 1
        for i, ps in chat.pics.items():
            if i >= start and i not in chat.deleted:
                for k in {(chat.img.get(p) or {}).get("kind") for p in ps} - {None}:
                    counts["pkinds"][k] = counts["pkinds"].get(k, 0) + 1
        out, found, total = [], [], 0
        if kind or topic or pic:
            for e in reversed(chat.events):
                if e["id"] < start or e["kind"] != "msg" or e["id"] in chat.deleted or not wanted(e["id"]):
                    continue
                total += 1
                if len(out) < 200:
                    view = chat.view_for(e, start, v["role"])
                    if view and view["kind"] == "msg":
                        out.append({"event": view, "score": 1.0, "how": "browse", "pics": chat.pic_views(view["text"]),
                                    "k": chat.talks.kind.get(e["id"], ""), "tp": chat.talks.topic.get(e["id"], "")})
            if topic and not pic:
                byid = {e["id"]: e for e in chat.events}
                for a in sorted(chat.talks.by_a, reverse=True):
                    r, e = chat.talks.by_a[a], byid.get(a)
                    if (a >= start and e and a not in chat.deleted and r["topic"] == topic and r["n"] >= 3 and r["title"]
                            and (not kind or kind in r["kinds"])):
                        found.append({"a": a, "b": r["b"], "n": r["n"], "title": r["title"], "sum": r["sum"], "topic": r["topic"],
                                      "tags": r["tags"], "ts": e["ts"], "score": 1.0})
                        if len(found) >= 40:
                            break
        return web.json_response({"q": "", "results": out, "semantic": True, "exact": True, "browse": True, "total": total, "talks": found,
                                  "counts": counts, **lists}, headers={"Cache-Control": "no-store"})

    def res_of(view: dict, score: float, how: str) -> dict:
        return {"event": view, "score": score, "how": how, "pics": chat.pic_views(view["text"]),
                "k": chat.talks.kind.get(view["id"], ""), "tp": chat.talks.topic.get(view["id"], "")}

    if req.query.get("exact") == "1":
        # "simple match" (the owner, 2026-11-02): the text as typed, anywhere in a message, any case - no meaning, no word
        # splitting; the newest first
        qlow, out = q.lower(), []
        for e in reversed(chat.events):
            if e["id"] < start or e["kind"] != "msg" or e["id"] in chat.deleted or not wanted(e["id"]):
                continue
            if qlow not in (e["text"] or "").lower() and qlow not in chat.pic_text.get(e["id"], "").lower():
                continue
            view = chat.view_for(e, start, v["role"])
            if view and view["kind"] == "msg":
                out.append(res_of(view, 1.0, "exact"))
                if len(out) >= 200:
                    break
        return web.json_response({"q": q, "results": out, "semantic": True, "exact": True, **lists}, headers={"Cache-Control": "no-store"})
    qv = await asyncio.to_thread(chat.vec.qvec, q)
    sem = await asyncio.to_thread(chat.vec.near, qv, start, 300 if kind or topic or pic else 30)
    ok_sem = qv is not None or not chat.vec.error
    score: dict[int, tuple[float, str]] = {i: (s, "meaning") for i, s in sem}
    for i, s in chat.pvec.near(qv, start, 300 if kind or topic or pic else 30):               # what is on a message's pictures (imgdesc.py)
        if i in chat.pic_text and s > score.get(i, (0.0, ""))[0]:
            score[i] = (s, "picture")
    res = _word_res(_words(q))
    qlow = q.lower()
    byid = {e["id"]: e for e in chat.events}
    for e in chat.events:                                    # the words as they stand: a name, a number, a path
        if e["id"] < start or e["kind"] != "msg" or e["id"] in chat.deleted:
            continue
        low = (e["text"] or "").lower() + " " + e["agent"].lower()
        pic = chat.pic_text.get(e["id"])
        in_pic = False
        if pic:                                              # the words are on its picture, not in its text
            in_pic = not (qlow in low or (res and all(r.search(low) for r in res)))
            low += " " + pic.lower()
        got = sum(1 for r in res if r.search(low))
        base, was = score.get(e["id"], (0.0, ""))
        how = ("picture" if in_pic or was == "picture" else "words") if base < 0.40 else "picture" if was == "picture" else "words + meaning"
        if qlow in low:                                      # the text as typed: on top
            score[e["id"]] = (min(1.0, max(base, 0.45) + 0.35), how)
        elif res and got == len(res):
            score[e["id"]] = (min(1.0, max(base, 0.45) + 0.25), how)
        elif got and got * 2 >= len(res):                    # half of the words or more: below what has them all, and below a match in meaning
            score[e["id"]] = (max(base, 0.30 + 0.20 * got / len(res)), how if base else "words")
    out = []
    for i, (s, how) in sorted(score.items(), key=lambda kv: (-kv[1][0], -kv[0])):     # of equal ones the newest first
        e = byid.get(i)
        view = chat.view_for(e, start, v["role"]) if e and wanted(i) else None
        if view and view["kind"] == "msg":
            out.append(res_of(view, round(s, 3), how))
            if len(out) >= 30:
                break
    # the talks that were about it (talks.py): by meaning of their title and summary, and by the words in them
    tsc: dict[int, float] = {a: s for a, s in chat.tvec.near(qv, start, 8, 0.5)}
    for a, r in chat.talks.by_a.items():
        if a < start or r["n"] < 3 or not r["title"]:
            continue
        low = talks_mod.Talks.text(r).lower()
        if qlow in low or (res and all(x.search(low) for x in res)):
            tsc[a] = min(1.0, max(tsc.get(a, 0.0), 0.45) + 0.3)
    found = []
    for a, s in sorted(tsc.items(), key=lambda kv: (-kv[1], -kv[0])):
        r, e = chat.talks.by_a.get(a), byid.get(a)
        if r and e and a not in chat.deleted and (not topic or r["topic"] == topic):
            found.append({"a": a, "b": r["b"], "n": r["n"], "title": r["title"], "sum": r["sum"], "topic": r["topic"], "tags": r["tags"],
                          "ts": e["ts"], "score": round(s, 3)})
            if len(found) >= 5:
                break
    return web.json_response({"q": q, "results": out, "semantic": ok_sem, "talks": found, **lists}, headers={"Cache-Control": "no-store"})


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


async def api_pad(req: web.Request) -> web.Response:
    """the strokes of a scratchpad (#3741), for a page that shows it: who drew what, in which colour"""
    v = await need_viewer(req)
    mid = _int(req.query.get("id"))
    e = next((x for x in chat.events if x["id"] == mid), None)
    if not e or not e.get("pad") or mid in chat.deleted or not chat.sees(mid, chat.start_for(v)):
        return web.json_response({"ok": False, "error": "no such scratchpad"}, status=404)
    pad = chat.pad_of(mid)
    return web.json_response({"ok": True, "s": pad["s"], "o": pad["o"]})


async def api_events(req: web.Request):
    """a page of the history, by the events' order keys (okey): the `n` before the key `before`; the `n` after the key `after`;
    those from the message `to` (an id) up to `before`, all of them; `n` each side of the message `around` (an id)"""
    v = await need_viewer(req)
    start = chat.start_for(v)
    q = req.query
    try:
        n = max(1, min(int(q.get("n", "500")), 5000))
        before = float(q["before"]) if q.get("before") else math.inf    # a key may be 0 or under it: an import before everything
        after = float(q["after"]) if q.get("after") else None
        to, around = int(q.get("to") or 0), int(q.get("around") or 0)
    except ValueError:
        return web.json_response({"ok": False, "error": "before, after, to, around, n: numbers"}, status=400)
    vis = [x for x in (chat.view_for(e, start, v["role"]) for e in chat.events) if x is not None]
    keys = [okey(x) for x in vis]
    at = next((i for i, x in enumerate(vis) if x["id"] == (around or to)), None) if around or to else None
    if around:
        got = vis[max(0, at - n): at + n + 1] if at is not None else []
    elif to:
        got = vis[at: bisect.bisect_left(keys, before)][:50000] if at is not None else []
    elif after is not None:
        i = bisect.bisect_right(keys, after)
        got = vis[i: i + n]
    else:
        j = bisect.bisect_left(keys, before)
        got = vis[max(0, j - n): j]
    return web.json_response({"events": got, "first": chat.first_for(start)})


PERF_KEYS = ("type", "from", "fetch", "conn", "ttfb", "resp", "kb", "dom", "ws", "init", "initkb", "paint", "pre", "net", "rtt", "mob")


async def api_perf(req: web.Request) -> web.Response:
    """how long the page took to open, as its browser measured it (ms from the start of the navigation): one line in the log
    (the owner, #3252: going between chats is slow)"""
    v = await need_viewer(req)
    try:
        d = await req.json()
        d = d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        d = {}
    vals = " ".join(k + "=" + re.sub(r"[^\w.:-]", "", str(d.get(k, "")))[:40] for k in PERF_KEYS)
    print(f"[perf] {v.get('role')}/{v.get('name') or '-'} {vals}", flush=True)
    return web.json_response({"ok": True})


async def api_days(req: web.Request):
    """the days of a month that have messages (the owner, #3243: go to a date from the calendar): {day of the month: [the
    first message's id, how many]}, by the page's own time zone"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    v = await need_viewer(req)
    start = chat.start_for(v)
    try:
        y, m = int(req.query["y"]), int(req.query["m"])
        tz = ZoneInfo(req.query.get("tz") or "UTC")
        a = datetime(y, m, 1, tzinfo=tz).timestamp()
        b = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz).timestamp()
    except Exception:
        return web.json_response({"ok": False, "error": "y, m, tz"}, status=400)
    days: dict = {}
    for e in chat.events:
        if e["kind"] != "msg" or not chat.sees(e["id"], start) or not a <= e.get("ts", 0) < b or e["id"] in chat.deleted:
            continue
        d = days.setdefault(datetime.fromtimestamp(e["ts"], tz).day, [e["id"], 0])
        d[1] += 1
    return web.json_response({"ok": True, "y": y, "m": m, "days": days})


# ---------------------------------------------------------------- import (the owner, 2026-11-16)
# a Telegram chat brought into this one (tgimport.py): the owner gives the folder Telegram Desktop exported - on this machine -
# in Settings, or when the chat is made (chats.py create --import: the folder waits in <data>/import_pending.json and the page
# opens the import with it). The messages keep their time and come after what the chat has; the files go to the uploads' tg/
# folder, which the two weeks' sweep does not touch. A folder imported again brings only what is new.
# A conversation of iMessage the same way (imimport.py, src "im"): the folder is ~/Library/Messages of this Mac or a copy of
# it, and one of its conversations is chosen first; its files go to the uploads' im/ folder
IMPORT: dict = {"run": False}
IMPORT_DIR = UPLOADS / "tg"
IMPORT_ROOTS = tuple((UPLOADS / x).resolve() for x in ("tg", "im"))
IMPORT_PENDING = DATA / "import_pending.json"


def _imp_known(tg: str, src: str = "tg") -> tuple[Path, dict]:
    f = DATA / f'import_{src}_{re.sub(r"[^0-9A-Za-z_-]", "", tg) or "0"}.json'
    try:
        return f, json.loads(f.read_text())
    except (OSError, ValueError):
        return f, {}


def _imp_scan(path: str, src: str = "tg", conv: int = 0) -> dict:
    s = imimport.scan(path, conv) if src == "im" else tgimport.scan(path)
    known = _imp_known(s["tg"], src)[1]
    if known:
        s = imimport.scan(path, conv, known) if src == "im" else tgimport.scan(path, known)
    return dict(s, src=src)


def _imp_people(asked, scan: dict) -> dict:
    """who the Telegram accounts are here: {account id: {"as": "owner" | "person" | "skip", "name"}} -> {account id: {name, task}}"""
    out, taken = {}, {a["name"].lower() for a in chat.agents.values()}
    for p in scan["people"]:
        q = asked.get(p["id"]) if isinstance(asked, dict) and isinstance(asked.get(p["id"]), dict) else {}
        if q.get("as") == "owner":
            out[p["id"]] = {"name": USER["name"], "task": USER["task"]}
        elif q.get("as") == "person":
            n = _name_of(str(q.get("name") or p["name"]))
            if not n or n.lower() in USER_NAMES or n.lower() in taken:
                raise tgimport.Bad("name_taken")
            # a person of this chat under that name: the messages are theirs; nobody: a person of the import, by the account's id
            old = next((e for e in reversed(chat.events) if str(e.get("task", "")).startswith("user:") and e["agent"].lower() == n.lower()), None)
            out[p["id"]] = {"name": old["agent"], "task": old["task"]} if old else {"name": n, "task": (
                "user:im:" + hashlib.sha256(p["id"].encode()).hexdigest()[:12] if scan.get("src") == "im" else "user:tg:" + re.sub(r"\D", "", p["id"]))}
    if len({x["task"] for x in out.values()}) < len(out):
        raise tgimport.Bad("name_taken")                     # two accounts as one person
    if not out:
        raise tgimport.Bad("bad_request")
    return out


def _imp_view() -> dict:
    return {k: IMPORT.get(k) for k in ("run", "done", "total", "name", "added", "error")}


# the folder picker of the import (the owner, #3598): the folder is chosen by clicks, not typed. The folders of the chat's
# machine under the home folder and the disks plugged in - names of folders only, never of files, hidden ones left out
IMP_DISKS = Path("/Volumes") if sys.platform == "darwin" else Path("/media")
IMP_LS_MAX = 1000


def _imp_ls(path: str, src: str) -> dict:
    """one folder for the picker: {path, up, crumbs: [[name, path]], dirs: [{name, path, exp, t}], here, more}. `exp`, `here`:
    the folder holds an export (result.json; of iMessage, chat.db). No path: where an export is most likely"""
    home, mark = Path.home().resolve(), ("chat.db" if src == "im" else "result.json")
    if path.strip():
        p = Path(os.path.expanduser(path.strip()))
    elif src == "im":
        p = home / "Library" / "Messages"
        p = p if p.is_dir() else home
    else:
        p = next((x for x in (home / "Downloads" / "Telegram Desktop", home / "Downloads") if x.is_dir()), home)
    try:
        p, disks = p.resolve(), IMP_DISKS.resolve()
        root = home if p == home or home in p.parents else disks if disks in p.parents else None
        if root is None or not p.is_dir() or any(x.startswith(".") for x in p.relative_to(root).parts):
            raise tgimport.Bad("no_folder")
    except (OSError, RuntimeError, ValueError):
        raise tgimport.Bad("no_folder")

    def has(d: Path) -> bool:
        try:
            return (d / mark).is_file()
        except OSError:
            return False

    def sub(d: Path) -> list[Path]:
        out = []
        with os.scandir(d) as it:
            for e in it:
                try:
                    if not e.name.startswith(".") and e.is_dir():
                        out.append(Path(e.path))
                except OSError:
                    pass
        return out
    try:
        kids = sub(p)
    except PermissionError:
        raise tgimport.Bad("no_list")
    except OSError:
        raise tgimport.Bad("no_folder")
    dirs = []
    for d in kids[:IMP_LS_MAX]:
        exp = has(d)
        try:
            t = int((d / mark).stat().st_mtime) if exp else 0
        except OSError:
            t = 0
        dirs.append({"name": d.name, "path": str(d), "exp": exp, "t": t})
    dirs.sort(key=lambda x: (not x["exp"], -x["t"], x["name"].lower()))
    if p == home:                                            # the disks plugged in: at the end of the home folder's list
        try:
            for d in sorted(sub(disks), key=lambda x: x.name.lower()):
                if d.resolve().parent == disks:              # not the link to the system's own disk
                    dirs.append({"name": d.name, "path": str(d), "exp": has(d), "t": 0, "disk": True})
        except OSError:
            pass
    rel = p.relative_to(root).parts
    crumbs = [["", str(home)]] + [[n, str(root.joinpath(*rel[:i + 1]))] for i, n in enumerate(rel)]
    return {"path": str(p), "up": crumbs[-2][1] if len(crumbs) > 1 else "", "crumbs": crumbs, "dirs": dirs, "here": has(p),
            "more": len(kids) > IMP_LS_MAX}


async def _imp_run(path: str, people: dict, scan: dict) -> None:
    loop = asyncio.get_running_loop()

    def progress(i: int, n: int) -> None:
        IMPORT.update(done=i, total=n)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(chat.watch_all({"type": "import", **_imp_view()})))
    try:
        f, known = _imp_known(scan["tg"], scan["src"])
        if scan["src"] == "im":
            r = await asyncio.to_thread(imimport.build, path, scan["chat"], people, UPLOADS / "im" / scan["tg"], known, FFMPEG, progress)
        else:
            r = await asyncio.to_thread(tgimport.build, path, people, IMPORT_DIR / (re.sub(r"\D", "", scan["tg"]) or "0"), known, FFMPEG, progress)
        base = chat.take_in(r["events"], r["reacts"]) if r["events"] else chat.next_id
        known.update({k: base + i for k, i in r["map"].items()})
        f.write_text(json.dumps(known))
        IMPORT_PENDING.unlink(missing_ok=True)
        IMPORT.update(run=False, added=len(r["events"]), error="")
        print(f'[import] {scan["src"]} {scan["name"]!r}: {len(r["events"])} messages from #{base}, {r["skipped"]} left out', flush=True)
    except Exception as x:
        IMPORT.update(run=False, added=None, error=str(x) if isinstance(x, tgimport.Bad) else "import_failed")
        print(f"[import] failed: {x!r}", flush=True)
    await chat.watch_all({"type": "import", **_imp_view()})


async def api_import(req: web.Request) -> web.Response:
    """GET: the folder that waits to be imported and how a running import goes. POST {op: ls, path}: the folders in a folder of
    this machine, for the page's folder picker; {op: scan, path}: what the folder holds;
    with src: "im" it is a folder of iMessage: {op: chats, path} lists its conversations, scan and run take one as `chat`;
    {op: run, path, people}: bring it in; {op: forget}: the waiting folder is not wanted. The owner only"""
    v = await need_viewer(req)
    if v["role"] != "owner":
        raise web.HTTPForbidden()
    if req.method == "GET":
        try:
            pend = str(json.loads(IMPORT_PENDING.read_text()).get("path") or "")
        except (OSError, ValueError, AttributeError):
            pend = ""
        return web.json_response({"ok": True, "pending": pend, "state": _imp_view()}, headers={"Cache-Control": "no-store"})
    if not _same_page(req):
        raise web.HTTPForbidden()
    try:
        q = await req.json()
        assert isinstance(q, dict)
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    op_ = q.get("op")
    if op_ == "forget":
        IMPORT_PENDING.unlink(missing_ok=True)
        return web.json_response({"ok": True})
    src, path = ("im" if q.get("src") == "im" else "tg"), str(q.get("path") or "")[:1000]
    if op_ == "chats" and src == "im":                       # the conversations of a Messages folder: one of them is imported
        try:
            return web.json_response({"ok": True, **(await asyncio.to_thread(imimport.chats, path))})
        except tgimport.Bad as x:
            return web.json_response({"ok": False, "error": str(x)})
    if op_ == "ls":                                          # the folder picker: a folder of this machine and what is in it
        try:
            return web.json_response({"ok": True, **(await asyncio.to_thread(_imp_ls, path, src))})
        except tgimport.Bad as x:
            return web.json_response({"ok": False, "error": str(x)})
    if op_ not in ("scan", "run"):
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    if IMPORT.get("run"):
        return web.json_response({"ok": False, "error": "import_running"})
    try:
        scan = await asyncio.to_thread(_imp_scan, path, src, _int(q.get("chat")))
        if op_ == "scan":
            return web.json_response({"ok": True, "scan": scan})
        people = _imp_people(q.get("people"), scan)
    except tgimport.Bad as x:
        return web.json_response({"ok": False, "error": str(x)})
    if IMPORT.get("run"):
        return web.json_response({"ok": False, "error": "import_running"})
    IMPORT.clear()
    IMPORT.update(run=True, done=0, total=scan["n"], name=scan["name"], added=None, error="")
    print(f'[import] {v.get("name")}: {src} {scan["name"]!r} from {scan["path"]}', flush=True)
    asyncio.create_task(_imp_run(scan["path"], people, scan))
    return web.json_response({"ok": True, "state": _imp_view()})


# ---------------------------------------------------------------- export (the owner, 2026-11-07, #261)
# the whole history this person may see (the same cut as the page: a guest from the time they came, the owner all of it) as a
# zip: chat.json (the events as the page gets them), chat.html (to read in any browser, no outside files) and media/ (the
# pictures and videos the messages name, the same checks as /img)
EXPORT_MEDIA_RE = re.compile(r"(?:/[\w.@+\-]+)+\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v|m4a|wav)", re.I)
EXPORT_MEDIA_MAX = 400 * 2 ** 20         # all the media of one export together; what is over it stays a path
EXPORT_HTML_MAX = 150 * 2 ** 20          # the same for the one-file page (#3911): the files sit in it as text, a third bigger
EXPORT_SPAN_MAX = 86400                  # the one-file page holds a day at most (the owner, #3911)


def _media_ok(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    ext = p.suffix.lower()
    if not p.is_file() or not (ext in IMG_TYPES or ext in VID_TYPES or ext in AUD_TYPES) or not any(r in p.parents for r in IMG_ROOTS) \
            or any(x.startswith(".") for x in p.parts):
        return None
    return p


def _export_html(title: str, evs: list[dict], media: dict, who: str, when: str, pdf: bool = False) -> str:
    def body(text: str) -> str:
        out, pos = [], 0
        for m in EXPORT_MEDIA_RE.finditer(text):
            out.append(html_escape(text[pos:m.start()]))
            rel = media.get(m.group(0))
            ext = Path(m.group(0)).suffix.lower()
            if rel and ext in AUD_TYPES:
                out.append(f'<i>[voice {html_escape(Path(m.group(0)).name)}]</i>' if pdf else
                           f'<audio controls preload="metadata" src="{html_escape(rel)}"></audio>')
            elif rel and ext in VID_TYPES:
                out.append(f'<i>[video {html_escape(Path(m.group(0)).name)}]</i>' if pdf else
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


def _export_media(evs: list[dict], cap: int = EXPORT_MEDIA_MAX) -> tuple[dict, dict]:
    """the media the events name that may go out: {path in the text: "media/<name>"}, {path in the text: the file}"""
    media, used, total, names = {}, set(), 0, {}
    for e in evs:
        for raw in EXPORT_MEDIA_RE.findall(str(e.get("text") or "")):
            if raw in media or raw in names:
                continue
            p = _media_ok(raw)
            if not p or total + p.stat().st_size > cap:
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


def _page_file(u: str) -> Path | None:
    """the file behind an address of the page's own: a photo, a sticker, a reaction's picture, a GIF's preview"""
    kind, f = u.split("/", 1)
    src = {"photo": chat.photos_dir / f, "sticker": STICKERS_DIR / f, "rimg": DATA / "react_imgs" / f, "gif": UPLOADS / f"gif-{f}"}[kind]
    return src if src.is_file() else None


def _data_uri(p: Path) -> str:
    import base64
    ext = p.suffix.lower()
    mime = IMG_TYPES.get(ext) or VID_TYPES.get(ext) or AUD_TYPES.get(ext) or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def _export_one(title: str, evs: list[dict], who: str, page: str = "") -> bytes:
    """one page with everything in it (the owner, #3911): the pictures, the voice messages and the videos sit in the file itself,
    so it opens in a browser from anywhere with no folder beside it. A file the page shows many times (a photo by every message
    of its person) is written once, and a few lines of script at the end put it where it is shown"""
    _, names = _export_media(evs, EXPORT_HTML_MAX)
    if not page:
        return _export_html(title, evs, {raw: _data_uri(p) for raw, p in names.items()}, who, time.strftime("%Y-%m-%d %H:%M")).encode()
    files: dict = {}                                       # the file -> how many times the page shows it

    def find(m: re.Match) -> Path | None:
        if m.group(1):
            return names.get(urllib.parse.unquote(m.group(1).replace("&amp;", "&")))
        return _page_file(m.group(2))
    for m in PAGE_FILE_RE.finditer(page):
        f = find(m)
        if f:
            files[f] = files.get(f, 0) + 1
    many = {f: i for i, f in enumerate(f for f, n in files.items() if n > 1)}

    def one(m: re.Match) -> str:
        f = find(m)
        return m.group(0) if not f else f"lcm:{many[f]}:" if f in many else _data_uri(f)
    page = PAGE_FILE_RE.sub(one, page)
    if many:
        js = ("<script>(()=>{const M=" + json.dumps([_data_uri(f) for f in many]).replace("</", "<\\/") + ";"
              "for(const e of document.querySelectorAll('*'))for(const a of [...e.attributes])"
              "if(a.value.includes('lcm:'))e.setAttribute(a.name,a.value.replace(/lcm:(\\d+):/g,(_,i)=>M[i]));})()</script>")
        i = page.rfind("</body>")
        page = page[:i] + js + page[i:] if i >= 0 else page + js
    return page.encode()


def _localize(page: str, media: dict, names: dict) -> tuple[str, dict]:
    """the page's own markup (the owner #272: the export looks like the chat): its server addresses become files in media/;
    a picture by path only when a message one may see names it (the markup comes from the browser)"""
    extra: dict = {}

    def one(m: re.Match) -> str:
        if m.group(1):
            raw = urllib.parse.unquote(m.group(1).replace("&amp;", "&"))
            return media.get(raw, m.group(0))
        u = m.group(2)
        src = _page_file(u)
        if not src:
            return u
        rel = "media/" + u.replace("/", "-", 1)
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
    fmt, page, span = req.query.get("fmt") or "zip", "", dict(req.query)     # the same as a PDF (the owner, #263)
    if req.method == "POST":                              # the page sends its own markup of the log (#272)
        if req.content_length and req.content_length > 40 * 2 ** 20:
            raise web.HTTPRequestEntityTooLarge(max_size=40 * 2 ** 20, actual_size=req.content_length)
        if _other_site(req):
            raise web.HTTPForbidden()
        q = await req.json()
        fmt, page, span = str(q.get("fmt") or "zip"), str(q.get("html") or ""), q
    if fmt not in ("zip", "pdf", "html"):
        raise web.HTTPBadRequest()
    if fmt == "html" or "from" in span or "till" in span:   # a part of the history, by the time (#3911); the one page: a day at most
        try:
            t1 = float(span.get("till") or time.time())
            t0 = float(span["from"]) if span.get("from") not in (None, "") else time.mktime(time.localtime(t1)[:3] + (0, 0, 0, 0, 0, -1))
        except (TypeError, ValueError, OverflowError):
            raise web.HTTPBadRequest()
        if not t0 < t1 or (fmt == "html" and t1 - t0 > EXPORT_SPAN_MAX):
            return web.json_response({"ok": False, "error": "export: a day at most"}, status=400)
        evs = [e for e in evs if t0 <= (e.get("ts") or 0) <= t1]
    try:
        data = await asyncio.to_thread({"pdf": _export_pdf, "zip": _export_zip, "html": _export_one}[fmt], chat.title, evs, v.get("name") or "", page)
    except Exception as x:                                # no Chrome, a timeout: said, not a 500 page
        return web.json_response({"ok": False, "error": f"export: {x}"}, status=503)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", chat.title).strip("-").lower() or "chat"
    ext = fmt
    print(f"[export] {v.get('name')}: {ext}, {len(evs)} events, {len(data) // 1024} KB", flush=True)
    return web.Response(body=data, headers={"Content-Type": {"pdf": "application/pdf", "zip": "application/zip", "html": "text/html; charset=utf-8"}[fmt], "Cache-Control": "no-store",
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

Get it, also when `command -v lagomchat` finds one already: an older copy lacks what the chat does now (it did not send
the files a message names, the owner #3933):

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
- ALWAYS NAME A FILE BY ITS FULL PATH (the owner, #3933): a picture, a screenshot, a short video or a PDF for the others to
  see or to download is named in the message by its full path, from the root - never a name alone, never a path from the
  folder you work in. The command sends it and prints `sent <your path> -> <its path in the chat>`, and the page shows the
  picture itself; with no such line the path stays a line of text that nobody can open: say it again, the right way. A file outside the folder you work in and the temp folders
  is sent only with `lagomchat say --send ...`: say so only when you mean to show that very file.
- Every message costs its readers a turn: no thanks, no "ok". `lagomchat help` has all the rules; read it once.

## 6. The progress of a long job

A build, a download, a test run of more than a few minutes: show how far it is on your card, so that nobody has to ask and no
message is spent on it. Write it as `<name> [####------] <n>% <what now>` in your card's status; the page draws a bar of it:

    lagomchat status "Build 55 [####------] 41% cooking 1120 of 1682"
    lagomchat status                                    (alone: takes it off, when the job is dealt with)

- 120 characters at most, no emoji. It stays until changed: your messages do not clear it (they clear `lagomchat working`; a
  Claude Code session's `working` stays till the end of the turn in which it wrote).
- Send it only when the line changed, every 10 seconds at most, from a script in the background that ends with the job.
  One writer at a time: the last status wins. The session or the script ends: the bar stays as it was, so take it off.
- The card shows by itself how long the job has been going and about how long is left, by the pace of the percent: keep
  the name before the bar the same for the whole job (another name starts the clock anew) and do not write times yourself.
- A count where there is one ("1120 of 1682"); a percent that is guessed is not a time left. 100% means that the job's
  scripts have ended, not that the result is good; a failure says FAILED, and a message says why.
- For example, with a command of yours, `my-progress`, that prints one line and is done when the line starts with "done":

      last=""; while :; do line=$(my-progress); [ "$line" = "$last" ] || lagomchat status "$line"; last="$line"
        case "$line" in done*) break;; esac; sleep 10; done
"""


async def join_page(req: web.Request) -> web.Response:
    return web.Response(text=_join_text(req), content_type="text/markdown", charset="utf-8", headers={"Cache-Control": "no-store"})


async def signin_page(_req: web.Request) -> web.Response:
    """/signin: the chat's own page with the ways to sign in (the owner, 2026-11-16). Cloudflare Access lets everybody see it; each
    button leads to /in/<way>, which Access guards with that one way alone, so its own page with the choice is not shown.
    Another sign-in mode: the chat's page, whose gate has that mode's ways"""
    if auth.mode() != "access":
        scheme, host, prefix = origin_of(_req)
        raise web.HTTPFound(f"{scheme}://{host}{prefix}/")
    t = html_escape(chat.title)
    body = (HERE / "signin.html").read_text().replace(f"<title>{DEFAULT_TITLE}</title>", f"<title>{t}</title>", 1) \
        .replace(f"<h1>{DEFAULT_TITLE}</h1>", f"<h1>{t}</h1>", 1)
    return web.Response(text=body, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"})


async def signin_done(req: web.Request):
    """/in/<way>: who got here has signed in at Cloudflare Access that way; on to the chat, which Access now lets them into"""
    scheme, host, prefix = origin_of(req)
    raise web.HTTPFound(f"{scheme}://{host}{prefix}/")


async def join_cli(_req: web.Request) -> web.Response:
    # the agents' command alone: agentchat.py needs python3 with websockets; its `help` asks this chat for the rules
    return web.Response(body=(HERE / "agentchat.py").read_bytes(), content_type="text/x-python", headers={"Cache-Control": "no-store"})


async def join_rules(_req: web.Request) -> web.Response:
    return web.Response(text=(HERE / "RULES.md").read_text(encoding="utf8"), content_type="text/markdown", charset="utf-8")


async def healthz(_req):
    # when this server began: the manager shows a chat whose code on disk is newer than that (it needs a restart)
    return web.Response(text=f"ok {len(chat.agents)} agents, started {STARTED:.0f}")


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
                vmark = DATA / "mongo_views_backfilled"           # the views counted before the database was there (#2930)
                if not vmark.exists():
                    for mid, by in list(chat.views.items()):
                        await chat.to_mongo({"col": "views", "id": chat.views_key(mid),
                                             "set": {"chat": DATA.name, "message": mid, **{"by." + r.replace(".", "_"): t for r, t in by.items()}}})
                    vmark.write_text(str(len(chat.views)))
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


def pic_file(raw: str) -> bool:
    """a picture the page may show (api_img's rule): an image file under the pictures' folders, not in a hidden one"""
    try:
        p = Path(raw).resolve(strict=True)
        return (p.is_file() and p.suffix.lower() in IMG_TYPES and 0 < p.stat().st_size < 40 * 2 ** 20
                and any(r in p.parents for r in IMG_ROOTS) and not any(x.startswith(".") for x in p.parts))
    except (OSError, RuntimeError, ValueError):
        return False


def pics_since(events: list[dict], seen: int, queued: set) -> list[str]:
    """the pictures of the messages that came since the last look (ids over `seen`), the newest message's first. By the id, not
    by the place in the log: an import of older messages stands among the old ones, by its time (#3883), under new ids"""
    new: list[str] = []
    for e in reversed(events):
        if e["id"] > seen:
            new += [p for p in reversed(chat.pics_of(e)) if p not in queued]
    return new


async def img_loop() -> None:
    """the model looks at the chat's pictures, one at a time, the newest messages' first, and what it says joins the search.
    No vision server set, or it is down (the GPU does something else): waits and asks again, nothing else changes"""
    seen, todo, queued, was_up, said = -1, [], set(), None, 0
    await asyncio.sleep(6)
    while True:
        try:
            new = pics_since(chat.events, seen, queued) if chat.next_id - 1 > seen else []
            seen = max(seen, chat.next_id - 1)
            queued.update(new)
            todo += reversed(new)                                      # the end of the list is taken first: the newest
            idx = [(i, t) for i, t in chat.pic_text.items() if i not in chat.pvec.have][:16]
            if idx and await asyncio.to_thread(chat.pvec.add, idx):
                continue
            if not todo and chat.img.url():                            # all looked at: the ones whose answer was cut off, once more
                todo += [p for p in chat.img.cut_off() if p in chat.pic_ids]   # with more room for it (the owner, #3224)
            if not todo or not chat.img.url():
                await asyncio.sleep(20 if not todo else 60)
                continue
            p = todo[-1]
            again = (chat.img.d.get(p) or {}).get("err") == "no answer" and not chat.img.d[p].get("again")
            if (p in chat.img.d and not again) or not await asyncio.to_thread(pic_file, p):   # gone (the two weeks' sweep) or not ours to read: skipped
                todo.pop()
                continue
            rec = await asyncio.to_thread(chat.img.describe, p, again)
            todo.pop()
            await asyncio.to_thread(chat.img.put, p, rec)
            ids = set(chat.pic_ids.get(p, ()))
            for i in ids:
                chat.pic_said(i)
            await asyncio.to_thread(chat.pvec.drop, ids)               # a message with two pictures: indexed again with both
            if was_up is not True:
                print(f"[pics] the vision server answers: {len(todo)} pictures to look at", flush=True)
            was_up, said = True, said + 1
            if said % 100 == 0 or not todo:
                print(f"[pics] {said} looked at, {len(todo)} to go", flush=True)
        except VisionDown as x:
            if was_up is not False:
                print(f"[pics] waiting for the vision server ({x}); {len(todo)} pictures to look at", flush=True)
            was_up = False
            await asyncio.sleep(120)
        except Exception as x:                                         # never takes the chat down
            print(f"[pics] {type(x).__name__}: {str(x)[:160]}", flush=True)
            await asyncio.sleep(60)


async def talk_loop() -> None:
    """the model reads the chat talk by talk, the newest first (talks.py): a title, a summary, a topic, a kind for every
    message. The server is not set or down: waits and asks again"""
    last, todo, was_up, said = None, [], None, 0
    await asyncio.sleep(9)
    while True:
        try:
            now = time.time()
            mark = (chat.next_id, int(now // 600))                     # a new message, or ten minutes on (the last talk closes by time)
            if mark != last:
                last = mark
                msgs = [e for e in chat.events if e["kind"] == "msg" and e["id"] not in chat.deleted and (e["text"] or "").strip()]
                todo = [seg for seg, closed in talks_mod.cut(msgs, now)
                        if closed and (chat.talks.by_a.get(seg[0]["id"]) or {}).get("b") != seg[-1]["id"]]
            idx = [(a, talks_mod.Talks.text(r)) for a, r in chat.talks.by_a.items() if a not in chat.tvec.have and r["n"] >= 3 and r["title"]][:16]
            if idx and await asyncio.to_thread(chat.tvec.add, idx):
                continue
            if not todo or not chat.img.url():
                await asyncio.sleep(30)
                continue
            seg = todo[-1]

            def said_in(e: dict) -> str:                               # a picture's path says nothing: what is on it does
                return PIC_RE.sub(lambda m: f"[picture: {(chat.img.get(m.group(0)) or {}).get('en', '')}]", e["text"])
            rec = await asyncio.to_thread(chat.talks.classify, [(e["id"], e["agent"], said_in(e)) for e in seg])
            todo.pop()
            if rec["a"] in chat.tvec.have:                             # the talk read again (it grew, messages were taken out)
                await asyncio.to_thread(chat.tvec.drop, {rec["a"]})
            await asyncio.to_thread(chat.talks.put, rec)
            if was_up is not True:
                print(f"[talks] the model answers: {len(todo) + 1} talks to read", flush=True)
            was_up, said = True, said + 1
            if said % 100 == 0 or not todo:
                print(f"[talks] {said} read, {len(todo)} to go", flush=True)
        except VisionDown as x:
            if was_up is not False:
                print(f"[talks] waiting for the model server ({x}); {len(todo)} talks to read", flush=True)
            was_up = False
            await asyncio.sleep(120)
        except Exception as x:                                         # never takes the chat down
            print(f"[talks] {type(x).__name__}: {str(x)[:160]}", flush=True)
            await asyncio.sleep(60)


async def on_start(app):
    asyncio.ensure_future(chat.people_after_restart())
    app["vec"] = asyncio.create_task(vec_loop())
    app["pics"] = asyncio.create_task(img_loop())
    app["talks"] = asyncio.create_task(talk_loop())
    app["reaper"] = asyncio.create_task(chat.reaper())
    app["batcher"] = asyncio.create_task(chat.batcher())
    app["usage"] = asyncio.create_task(chat.usage_loop())
    app["mongo"] = asyncio.create_task(mongo_loop())
    app["version"] = asyncio.create_task(version_loop())
    app["remind"] = asyncio.create_task(remind_loop())
    app["cal"] = asyncio.create_task(cal_loop())
    app["owner"] = asyncio.create_task(owner_loop())
    app["prefs"] = asyncio.create_task(prefs_loop())


async def on_stop(app):
    socks = set(chat.watchers) | {w for v in chat.listeners.values() for w in v} | {w for v in chat.clients.values() for w in v}
    async def bye(w):
        try:
            await asyncio.wait_for(w.close(code=1012, message=b"restart"), 1)
        except Exception:
            pass
    await asyncio.gather(*(bye(w) for w in socks))
    if chat.own and chat.own.returncode is None:
        chat.own.terminate()                       # the chat's own agent goes with its server (it says its bye by itself)


class _Stamped:
    """stdout/stderr of the server with the local date and time in front of every line: an audit of who came when needs it
    (the owner #20, chat Myself)."""

    def __init__(self, out):
        self.out, self.bol = out, True

    def write(self, s):
        if not s:
            return 0
        parts = s.split("\n")
        buf = []
        for i, p in enumerate(parts):
            if p and self.bol:
                buf.append(time.strftime("%Y-%m-%d %H:%M:%S "))
            buf.append(p)
            if i < len(parts) - 1:
                buf.append("\n")
                self.bol = True
            elif p:
                self.bol = False
        self.out.write("".join(buf))
        return len(s)

    def __getattr__(self, name):
        return getattr(self.out, name)


def main():
    global chat
    sys.stdout, sys.stderr = _Stamped(sys.stdout), _Stamped(sys.stderr)
    chat = Chat()
    app = web.Application(client_max_size=205 * 2 ** 20)         # a video up to 200 MB (#1426, #3454)
    if UPLOADS.is_dir():                              # two weeks, then gone
        for f in (*UPLOADS.iterdir(), *(PDF_PV.iterdir() if PDF_PV.is_dir() else ())):
            if f.is_file() and time.time() - f.stat().st_mtime > UP_KEEP:
                f.unlink(missing_ok=True)
    app.router.add_get("/", page)
    app.router.add_get("/split", split_page)
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
    app.router.add_get("/signin", signin_page)
    app.router.add_get("/in/{way:google|github|code|passkey}", signin_done)
    app.router.add_get("/join", join_page)                # how an agent joins (the owner, #2335): no secrets in it, no sign-in
    app.router.add_get("/join/lagomchat", join_cli)
    app.router.add_get("/join/rules", join_rules)
    app.router.add_route("*", "/join/knock", join_knock)   # an agent elsewhere asks the owner in; GET: its answer
    app.router.add_get("/api/agents", api_agents)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/pad", api_pad)
    app.router.add_get("/api/days", api_days)
    app.router.add_post("/api/perf", api_perf)
    app.router.add_get("/api/export", api_export)
    app.router.add_post("/api/export", api_export)
    app.router.add_get("/api/mongo", api_mongo)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/skills", api_skills)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/pr", api_pr)
    app.router.add_get("/api/x", api_x)
    app.router.add_get("/api/og", api_og)
    app.router.add_get("/api/shot/{name}", api_shot)
    app.router.add_get("/api/release", api_release)
    app.router.add_route("*", "/api/calendar", api_calendar)
    app.router.add_route("*", "/api/surveys", api_surveys)
    app.router.add_route("*", "/api/import", api_import)
    app.router.add_get("/api/clocks", api_clocks)
    app.router.add_post("/api/stt_token", api_stt_token)
    app.router.add_get("/api/stt_ws", ws_stt)
    app.router.add_get("/api/pr/diff", api_pr_diff)
    app.router.add_get("/api/pr/file", api_pr_file)
    app.router.add_route("*", "/api/pr/comments", api_pr_comments)
    app.router.add_route("*", "/api/pr/review", api_pr_review)
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
    app.router.add_get("/auth/cf/login", cf_login)
    app.router.add_get("/auth/{prov:[a-z][a-z0-9_-]{0,23}}/login", oauth_login)
    app.router.add_get("/auth/{prov:[a-z][a-z0-9_-]{0,23}}/callback", oauth_callback)
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
    app.router.add_route("*", "/api/rail", api_rail)
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
