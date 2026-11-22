"""agentchat: a Telegram chat brought into a chat (the owner, 2026-11-16).

Telegram Desktop exports a chat as a folder (Export chat history, the format JSON): result.json with the messages and, next to
it, photos/, video_files/, stickers/, voice_messages/, round_video_messages/ and files/. `scan` says what such a folder holds;
`build` turns it into the chat's own events (kind msg, the time of the message as it was sent), with

    the people      every Telegram account goes to one of the chat: the owner, or a person under a name
    the media       copied (a hard link where the disk allows) into the uploads under names a message can carry; a voice message
                    and a music file become m4a (ffmpeg), an animated sticker is its still picture
    the text        bold, italics, underline, strike and code as the chat writes them; a link behind a word is shown after it
    the replies     point to the imported message they answered
    the reactions   of the people, as the chat's own
    the service     lines: a phone call stays as a line of text, the rest (pins, themes) is left out

A folder imported again brings only what is new since: `known` is what was brought before (the Telegram id -> the chat's id).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VID = {".mp4", ".m4v", ".webm", ".mov"}
AUD = {".m4a", ".wav"}                    # what the page plays as it is; any other sound is made m4a
AUD_IN = {".ogg", ".oga", ".opus", ".mp3", ".aac", ".flac"}
MARK = {"bold": "**", "italic": "*", "underline": "__", "strikethrough": "~~", "code": "`"}


class Bad(Exception):
    pass


def _load(path: str) -> tuple[Path, dict]:
    root = Path(os.path.expanduser(str(path or "").strip())).resolve()
    f = root / "result.json"
    if not root.is_dir() or not f.is_file():
        raise Bad("no_export_here")
    try:
        txt = f.read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise Bad("bad_export")
    try:
        d = json.loads(txt)
    except ValueError:
        # an export Telegram did not finish (stopped, or the app closed): result.json ends after a message, its list and the
        # chat never closed. Read up to the last whole message - what is there is imported (the owner, #3172)
        cut = txt.rfind("\n  }")
        try:
            d = json.loads(txt[:cut + 4] + "\n ]\n}") if cut > 0 else None
        except ValueError:
            d = None
        if not isinstance(d, dict):
            raise Bad("bad_export")
        d["_cut"] = True
        print(f"[import] {f}: cut off, read up to its last whole message ({len(d.get('messages') or [])})", flush=True)
    if not isinstance(d, dict) or not isinstance(d.get("messages"), list):
        raise Bad("bad_export")                              # the export of the whole account (chats.list) is not one chat
    return root, d


def _uid(m: dict) -> str:
    return str(m.get("from_id") or m.get("actor_id") or "")


def scan(path: str, known: dict | None = None) -> dict:
    """what the folder holds: the chat's name, how many messages (and how many of them are new), the days, the people"""
    root, d = _load(path)
    known, ppl, n, new, media, t0, t1 = known or {}, {}, 0, 0, 0, 0, 0
    for m in d["messages"]:
        if m.get("type") != "message":
            continue
        u = _uid(m)
        p = ppl.setdefault(u, {"id": u, "name": str(m.get("from") or "") or u, "n": 0})
        p["n"] += 1
        n += 1
        new += str(m.get("id")) not in known
        media += bool(m.get("photo") or m.get("file"))
        t = int(m.get("date_unixtime") or 0)
        t0, t1 = (t if not t0 else min(t0, t)), max(t1, t)
    # a chat of two is named after the other one: the one who exported it is who is left
    me = next((u for u, p in ppl.items() if p["name"] != d.get("name")), "") if d.get("type") == "personal_chat" and len(ppl) == 2 else ""
    return {"path": str(root), "name": str(d.get("name") or ""), "tg": str(d.get("id") or ""), "type": str(d.get("type") or ""), "n": n, "new": new,
            "media": media, "from": t0, "to": t1, "me": me, "people": sorted(ppl.values(), key=lambda p: -p["n"])}


def _text(m: dict) -> str:
    out = []
    for e in m.get("text_entities") or []:
        t, k = str(e.get("text") or ""), e.get("type")
        if k in MARK and t.strip() and "\n" not in t:
            a = MARK[k]
            lead, body = t[:len(t) - len(t.lstrip())], t.strip()
            t = lead + a + body + a + t[len(t.rstrip()):]
        elif k == "pre" and t.strip():
            t = "```\n" + t.strip("\n") + "\n```"
        elif k == "text_link" and e.get("href") and e["href"] != t:
            t = f'{t} ({e["href"]})'
        out.append(t)
    return "".join(out) if out else (m["text"] if isinstance(m.get("text"), str) else "")


def _media(root: Path, m: dict, dest: Path, ffmpeg: str) -> tuple[str, str]:
    """the message's file put into `dest` under a name a message can carry: (its path, ""), or ("", a word for what is not shown)"""
    rel = m.get("photo") or m.get("file")
    if not rel:
        return "", ""
    kind = m.get("media_type") or ("photo" if m.get("photo") else "file")
    name = str(m.get("file_name") or Path(str(rel)).name)
    src = (root / str(rel)).resolve()
    if root not in src.parents or not src.is_file():           # "(File not included...)": the export was made without this kind
        return "", f"[{kind.replace('_', ' ')}: not in the export]"
    ext = src.suffix.lower()
    if ext == ".tgs":                                          # an animated sticker: its still picture
        th = (root / str(m.get("thumbnail") or "")).resolve()
        if not m.get("thumbnail") or root not in th.parents or not th.is_file():
            return "", "[sticker]"
        src, ext = th, th.suffix.lower()
    out_ext = ".m4a" if ext in AUD_IN else ext
    if out_ext not in IMG | VID | AUD:
        return "", f"[file: {name}]"
    out = dest / f'{int(m["id"])}{out_ext}'
    if out.exists() and out.stat().st_size:
        return str(out), ""
    try:
        if ext in AUD_IN:
            r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "96k", str(out)], capture_output=True, timeout=600)
            if r.returncode or not out.exists():
                out.unlink(missing_ok=True)
                return "", f"[{kind.replace('_', ' ')}: {name}]"
        else:
            try:
                os.link(src, out)                              # the same disk: no second copy of the gigabytes
            except OSError:
                shutil.copy2(src, out)
    except (OSError, subprocess.SubprocessError):
        return "", f"[{kind.replace('_', ' ')}: {name}]"
    return str(out), ""


def build(path: str, people: dict, dest: Path, known: dict | None = None, ffmpeg: str = "ffmpeg", progress=None) -> dict:
    """the folder as the chat's events. people: the Telegram account id -> {"name", "task"} of the chat. The ids count from 0: the
    chat adds its own next id to them (in the events, their replies, the reactions and the map) at the moment it takes them in.
    Returns {"events": [...], "reacts": {id: {emoji: [names]}}, "map": {Telegram id: id} of the new ones, "skipped": how many}"""
    root, d = _load(path)
    known, new = known or {}, {}
    dest.mkdir(parents=True, exist_ok=True)
    msgs = sorted((m for m in d["messages"] if m.get("type") in ("message", "service")), key=lambda m: (int(m.get("date_unixtime") or 0), int(m.get("id") or 0)))
    evs, reacts, by_new, nid, skipped, total = [], {}, {}, 0, 0, len(msgs)
    for i, m in enumerate(msgs):
        if progress and i % 500 == 0:
            progress(i, total)
        who = people.get(_uid(m))
        if str(m.get("id")) in known:
            continue
        if not who:
            skipped += 1
            continue
        if m["type"] == "service":
            if m.get("action") != "phone_call":
                continue
            sec, why = int(m.get("duration_seconds") or 0), str(m.get("discard_reason") or "")
            text = "[phone call" + (f": {sec // 60}:{sec % 60:02d}" if sec else f": {why}" if why else "") + "]"
        else:
            text = _text(m).strip()
            p, note = _media(root, m, dest, ffmpeg)
            if m.get("forwarded_from"):
                text = f'*Forwarded from {m["forwarded_from"]}*' + ("\n" + text if text else "")
            if p or note:
                text = (text + "\n" if text else "") + (p or note)
            if not text:
                continue
        e = {"id": nid, "ts": float(int(m.get("date_unixtime") or 0)), "kind": "msg", "agent": who["name"], "task": who["task"], "text": text, "tg": int(m["id"])}
        if m.get("edited_unixtime"):
            e["edited"] = float(int(m["edited_unixtime"]))
        r = by_new.get(m.get("reply_to_message_id"))
        if r:
            e["reply"] = {"id": r["id"], "agent": r["agent"], "text": " ".join(r["text"].split())[:140]}
        for x in m.get("reactions") or []:
            names = [people[str(y.get("from_id"))]["name"] for y in x.get("recent") or [] if str(y.get("from_id")) in people]
            if x.get("type") == "emoji" and x.get("emoji") and names:
                reacts.setdefault(nid, {})[x["emoji"]] = sorted(set(names))
        evs.append(e)
        by_new[m["id"]] = e
        new[str(m["id"])] = nid
        nid += 1
    if progress:
        progress(total, total)
    return {"events": evs, "reacts": reacts, "map": new, "skipped": skipped}
