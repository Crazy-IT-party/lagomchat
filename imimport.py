"""agentchat: a conversation of iMessage brought into a chat (the owner, 2026-11-16), the way tgimport.py brings a Telegram one.

The Messages app of macOS keeps everything in one folder, ~/Library/Messages: chat.db (SQLite) and Attachments/. The server
reads that folder itself when macOS lets it (Full Disk Access of the program that runs it), or a copy of it put anywhere
else. A folder holds many conversations: `chats` lists them, `scan` says what one of them holds, `build` turns it into the
chat's own events (kind msg, the time of the message as it was sent), with

    the people      "me" (the messages sent from this Mac's account) and every other address go to one of the chat: the owner,
                    or a person under a name. Messages knows the others by their phone or email only: the name is given here
    the media       copied into the uploads under names a message can carry; HEIC becomes jpg (sips), a voice message m4a
    the replies     point to the imported message they answered
    the tapbacks    are the chat's reactions
    the rest        (a group renamed, a person added, a tapback taken back) is left out

A conversation imported again brings only what is new since: `known` is what was brought before (the message's guid -> the
chat's id).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from tgimport import AUD, AUD_IN, IMG, VID, Bad

EPOCH = 978307200                         # Messages counts from 2001-01-01, in nanoseconds (in seconds before macOS 10.13)
TAPBACK = {2000: "\u2764\ufe0f", 2001: "\U0001F44D", 2002: "\U0001F44E", 2003: "\U0001F602", 2004: "\u203c\ufe0f", 2005: "\u2753"}
AUD_MORE = AUD_IN | {".caf", ".amr"}
PIC_MORE = {".heic", ".heif", ".tiff", ".tif", ".bmp"}        # made jpg: no browser but Safari shows them


def _open(path: str) -> tuple[Path, sqlite3.Connection]:
    root = Path(os.path.expanduser(str(path or "").strip() or "~/Library/Messages")).resolve()
    f = root / "chat.db" if root.is_dir() or not root.name.endswith(".db") else root
    root = f.parent
    try:
        try:
            with open(f, "rb") as fh:                          # macOS shows the file and refuses to open it: no Full Disk Access
                fh.read(16)
        except PermissionError:
            raise Bad("no_access")
        except OSError:
            raise Bad("no_messages_here")
        db = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("select count(*) from message").fetchone()
    except PermissionError:
        raise Bad("no_access")
    except (sqlite3.Error, OSError) as x:
        raise Bad("no_access" if "authoriz" in str(x).lower() or "permitted" in str(x).lower() else "no_messages_here")
    return root, db


def _ts(v) -> int:
    v = int(v or 0)
    return (v // 1_000_000_000 if v > 100_000_000_000 else v) + EPOCH if v else 0


def _cols(db, table: str) -> set:
    return {r[1] for r in db.execute(f"pragma table_info({table})")}


def chats(path: str) -> dict:
    """the conversations of the folder, the ones with the most messages first"""
    root, db = _open(path)
    try:
        rows = db.execute("""select c.ROWID id, c.guid, c.chat_identifier ident, c.display_name name, count(j.message_id) n, min(m.date) d0, max(m.date) d1
                             from chat c join chat_message_join j on j.chat_id = c.ROWID join message m on m.ROWID = j.message_id
                             group by c.ROWID having n > 0 order by n desc""").fetchall()
        out = []
        for r in rows:
            who = [x[0] for x in db.execute("select h.id from chat_handle_join j join handle h on h.ROWID = j.handle_id where j.chat_id = ?", (r["id"],))]
            out.append({"id": r["id"], "name": r["name"] or ", ".join(who[:4]) or r["ident"] or str(r["id"]), "n": r["n"], "from": _ts(r["d0"]), "to": _ts(r["d1"])})
        return {"path": str(root), "chats": out}
    finally:
        db.close()


def _rows(db, chat: int) -> list:
    have = _cols(db, "message")
    opt = [c for c in ("attributedBody", "thread_originator_guid", "date_edited", "associated_message_emoji", "item_type") if c in have]
    return db.execute(f"""select m.ROWID id, m.guid, m.text, m.date, m.is_from_me, m.associated_message_guid amg, m.associated_message_type amt,
                                 h.id handle{"".join(", m." + c for c in opt)}
                          from chat_message_join j join message m on m.ROWID = j.message_id left join handle h on h.ROWID = m.handle_id
                          where j.chat_id = ? order by m.date, m.ROWID""", (chat,)).fetchall()


def _uid(r) -> str:
    return "me" if r["is_from_me"] else str(r["handle"] or "?")


def _key(db, chat: int) -> str:
    """a name for the conversation's own files here: its guid is a phone or an email, not for a file's name"""
    g = db.execute("select guid from chat where ROWID = ?", (chat,)).fetchone()
    if not g:
        raise Bad("bad_request")
    return hashlib.sha256(str(g[0]).encode()).hexdigest()[:16]


def scan(path: str, chat: int, known: dict | None = None) -> dict:
    """what one conversation holds: its name, how many messages (and how many of them are new), the days, the people"""
    root, db = _open(path)
    try:
        key = _key(db, chat)
        known, ppl, n, new, t0, t1 = known or {}, {}, 0, 0, 0, 0
        for r in _rows(db, chat):
            if r["amt"] or ("item_type" in r.keys() and r["item_type"]):
                continue
            u = _uid(r)
            ppl.setdefault(u, {"id": u, "name": u, "n": 0})["n"] += 1
            n += 1
            new += r["guid"] not in known
            t = _ts(r["date"])
            t0, t1 = (t if not t0 else min(t0, t)), max(t1, t)
        media = db.execute("select count(distinct a.message_id) from message_attachment_join a join chat_message_join j on j.message_id = a.message_id where j.chat_id = ?", (chat,)).fetchone()[0]
        name = next((c["name"] for c in chats(path)["chats"] if c["id"] == chat), str(chat))
        return {"path": str(root), "name": name, "tg": key, "chat": chat, "type": "imessage", "n": n, "new": new, "media": media, "from": t0, "to": t1,
                "me": "me" if "me" in ppl else "", "people": sorted(ppl.values(), key=lambda p: -p["n"])}
    finally:
        db.close()


def _body(blob) -> str:
    """the text of a message that has none in its text column (macOS 13 and later): it is inside attributedBody, an archive of
    an NSAttributedString; the string follows the class name NSString, its length before it in one byte or, after 0x81, in two"""
    if not blob:
        return ""
    b = bytes(blob)
    i = b.find(b"NSString")
    if i < 0:
        return ""
    b = b[i + 8 + 5:]
    if not b:
        return ""
    if b[0] == 0x81:
        n, b = int.from_bytes(b[1:3], "little"), b[3:]
    elif b[0] == 0x82:
        n, b = int.from_bytes(b[1:5], "little"), b[5:]
    else:
        n, b = b[0], b[1:]
    return b[:n].decode("utf-8", "replace")


def _file(root: Path, raw: str, out: Path, ffmpeg: str) -> tuple[str, str]:
    """one attachment put at `out` (its extension as the page shows it): (its path, ""), or ("", a word for what is not shown)"""
    raw = str(raw or "")
    name = Path(raw).name
    src = Path(os.path.expanduser(raw))
    m = re.search(r"Library/Messages/(.+)$", raw)
    if m and not src.is_file():                                # a copy of the folder: the same path under it
        src = root / m.group(1)
    try:
        src = src.resolve()
        if not raw or root not in src.parents or not src.is_file():   # the database names a file outside the folder: not taken
            return "", f"[file: {name or 'not in the folder'}]"
    except OSError:
        return "", f"[file: {name}]"
    ext = src.suffix.lower()
    if ext == ".pluginpayloadattachment":                      # the preview of a link: the link is in the text
        return "", ""
    oext = ".m4a" if ext in AUD_MORE else ".jpg" if ext in PIC_MORE else ext
    if oext not in IMG | VID | AUD:
        return "", f"[file: {name}]"
    out = out.with_suffix(oext)
    try:
        if out.exists() and out.stat().st_size:
            return str(out), ""
        if ext in AUD_MORE:
            r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "96k", str(out)], capture_output=True, timeout=600)
        elif ext in PIC_MORE:
            r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85", str(src), "--out", str(out)], capture_output=True, timeout=120)
        else:
            r = None
            try:
                os.link(src, out)                              # the same disk: no second copy
            except OSError:
                shutil.copy2(src, out)
        if r is not None and (r.returncode or not out.exists()):
            out.unlink(missing_ok=True)
            return "", f"[file: {name}]"
    except (OSError, subprocess.SubprocessError):
        return "", f"[file: {name}]"
    return str(out), ""


def build(path: str, chat: int, people: dict, dest: Path, known: dict | None = None, ffmpeg: str = "ffmpeg", progress=None) -> dict:
    """one conversation as the chat's events; the same answer as tgimport.build (the ids count from 0)"""
    root, db = _open(path)
    try:
        known, new, rows = known or {}, {}, _rows(db, chat)
        att: dict = {}
        for a in db.execute("""select a.message_id mid, t.ROWID aid, t.filename from message_attachment_join a join attachment t on t.ROWID = a.attachment_id
                               join chat_message_join j on j.message_id = a.message_id where j.chat_id = ? order by a.message_id, t.ROWID""", (chat,)):
            att.setdefault(a["mid"], []).append((a["aid"], a["filename"]))
    finally:
        db.close()
    dest.mkdir(parents=True, exist_ok=True)
    evs, reacts, by_guid, nid, skipped, total, taps = [], {}, {}, 0, 0, len(rows), []
    for i, r in enumerate(rows):
        if progress and i % 500 == 0:
            progress(i, total)
        keys = r.keys()
        if r["amt"]:
            taps.append(r)
            continue
        if ("item_type" in keys and r["item_type"]) or r["guid"] in known:
            continue
        who = people.get(_uid(r))
        if not who:
            skipped += 1
            continue
        text = (r["text"] or (_body(r["attributedBody"]) if "attributedBody" in keys else "") or "").replace("\ufffc", "").strip()
        for aid, fn in att.get(r["id"], []):
            p, note = _file(root, fn, dest / f'{r["id"]}-{aid}', ffmpeg)
            if p or note:
                text = (text + "\n" if text else "") + (p or note)
        if not text:
            continue
        e = {"id": nid, "ts": float(_ts(r["date"])), "kind": "msg", "agent": who["name"], "task": who["task"], "text": text, "im": r["id"]}
        if "date_edited" in keys and r["date_edited"]:
            e["edited"] = float(_ts(r["date_edited"]))
        o = by_guid.get(r["thread_originator_guid"]) if "thread_originator_guid" in keys and r["thread_originator_guid"] else None
        if o:
            e["reply"] = {"id": o["id"], "agent": o["agent"], "text": " ".join(o["text"].split())[:140]}
        evs.append(e)
        by_guid[r["guid"]] = e
        new[r["guid"]] = nid
        nid += 1
    for r in taps:                                             # in the order they were made: one taken back (3000-3005) takes its own away
        who, g = people.get(_uid(r)), re.sub(r"^(?:p:\d+/|bp:)", "", str(r["amg"] or ""))
        e, t = by_guid.get(g), int(r["amt"])
        if not who or not e:
            continue
        emo = TAPBACK.get(t if t < 3000 else t - 1000) or (r["associated_message_emoji"] if "associated_message_emoji" in r.keys() and t in (2006, 3006) else "")
        if not emo:
            continue
        cur = reacts.setdefault(e["id"], {}).setdefault(emo, [])
        if t < 3000 and who["name"] not in cur:
            cur.append(who["name"])
        elif t >= 3000 and who["name"] in cur:
            cur.remove(who["name"])
    reacts = {i: {k: v for k, v in r.items() if v} for i, r in reacts.items()}
    if progress:
        progress(total, total)
    return {"events": evs, "reacts": {i: r for i, r in reacts.items() if r}, "map": new, "skipped": skipped}
