#!/usr/bin/env python3
"""agentchat: the chats on this machine - start a new one at <slug>.<domain> (`domain` of .lagomchat/config.json), stop, start again, delete, list (the owner, 2026-11-07).

    chats.py list                         every chat: title, address, port, running or not
    chats.py create ["<title>"] [<slug>]  a new chat: one of the two is enough - "Book club" -> book-club,
                                          books -> the title "books". Makes the conf and data folders, the Cloudflare Access
                                          apps (the page: the owner only; the agents' sockets: bypass, the server checks the
                                          token), the tunnel's route and the DNS name, the LaunchAgent, and starts it
    chats.py stop <slug> | start <slug>   the LaunchAgent off / on (everything else stays)
    chats.py icon <slug> [--text <1-3 letters>] [--color <#rrggbb>] [--img <png/jpg/webp file> | --noimg] [--bg <#rrggbb | "">]
                                          the chat's round icon on the rail: a monogram in a colour, or a picture
    chats.py delete <slug>                stop it, take away its Cloudflare apps, route and DNS name and its LaunchAgent; its conf
                                          and data go to ~/.local/share/agentchat-deleted/<slug>-<time> (not erased)
    add --json to any: the answer as JSON (the page's sidebar uses it)

Each chat is the same code (server.py, index.html of this repo) with its own conf, data and port. The main chat
(`main` in the config) is in the list but cannot be stopped or deleted from here.

Cloudflare: an API token with Access: Apps and Policies Edit, Cloudflare Tunnel Edit (account) and DNS Edit (the zones).

The list, the domains and Cloudflare live in the one config, <working copy>/.lagomchat/config.json (lagomcfg.py, the owner, #2009);
what the servers read is written out from it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import lagomcfg

HOME = Path.home()
REG_DIR = HOME / ".config/agentchat-chats"
REG = REG_DIR / "chats.json"
REPO = Path(__file__).resolve().parent
TRASH = HOME / ".local/share/agentchat-deleted"
ICONS = REG_DIR / "icons"                              # the chats' pictures: every chat's server serves them at /chaticon/<file>
# the domain and Cloudflare's ids: the config's (the owner, #1981, #2009); a fresh machine gets them from the manager's wizard
_CFG = lagomcfg.load()
_CF = _CFG["cloudflare"]
NEW_DOMAIN = _CFG.get("domain") or ""              # new chats are made here (the owner, #1648): the config's domain
ACCOUNT, TUNNEL, IDP = _CF.get("account") or "", _CF.get("tunnel") or "", _CF.get("idp") or ""
ZONES = dict(_CF.get("zones") or {})
ZONE = ZONES.get(NEW_DOMAIN, "")
PORTS = range(8170, 8200)
RESERVED = {"chat", "www", "api", "lab", "3d", "mail", "admin", "console", "static", "cdn"}
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
UID = os.getuid()

TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіўєї",
              "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya i u e i".split()))


class Fail(Exception):
    pass


# ---------------------------------------------------------------- names
def slugify(title: str) -> str:
    s = "".join(TR.get(c, c) for c in title.lower()).replace("-", " ")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40].strip("-")


def names(title: str, slug: str) -> tuple[str, str]:
    title, slug = " ".join((title or "").split())[:60], (slug or "").strip().lower()
    slug = re.sub(r"^https?://", "", slug).split(".")[0] if slug else slugify(title)
    if not title:
        title = slug
    if not SLUG_RE.match(slug or ""):
        raise Fail(f"bad subdomain {slug!r}: a-z, 0-9 and '-', up to 40")
    return title, slug


# ---------------------------------------------------------------- the list
def load() -> dict:
    return lagomcfg.load()["chats"]


def save(reg: dict) -> None:
    c = lagomcfg.load()
    for k, ch in reg.items():
        if ch.get("aud"):
            ch["cf_aud"] = ch["aud"]
    c["chats"] = {k: {**lagomcfg.CHAT_DEFAULTS, **v} for k, v in reg.items()}
    lagomcfg.save(c)
    lagomcfg.apply(c)


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def title_of(c: dict) -> str:
    try:
        return (Path(c["data"]) / "title.txt").read_text().strip() or c["title"]
    except OSError:
        return c["title"]


def listing(reg: dict) -> list[dict]:
    return [{"slug": k, "title": title_of(c), "url": lagomcfg.chat_url(c), "port": c["port"], "main": bool(c.get("main")),
             "running": listening(c["port"]), "created": c.get("created"), "icon": c.get("icon") or {}} for k, c in sorted(reg.items(), key=lambda kv: (not kv[1].get("main"), kv[1].get("created") or 0))]


# ---------------------------------------------------------------- Cloudflare
def cf(method: str, path: str, body=None, ok404: bool = False):
    tok = (lagomcfg.load()["cloudflare"].get("token") or "").strip()
    if not tok:
        raise Fail("no Cloudflare token: set it in the manager (./lagom) - Access: Apps and Policies Edit, Cloudflare Tunnel "
                   "Edit, DNS Edit of the chats' zones")
    req = urllib.request.Request(f"https://api.cloudflare.com/client/v4{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}").get("result")
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        raise Fail(f"Cloudflare {method} {path}: {e.code} {e.read()[:300].decode(errors='replace')}")


def cf_apps(host: str, owner: str) -> dict:
    base = {"type": "self_hosted", "allowed_idps": [IDP], "auto_redirect_to_identity": True, "app_launcher_visible": True,
            "http_only_cookie_attribute": True}
    page = cf("POST", f"/accounts/{ACCOUNT}/access/apps", {
        **base, "name": f"LagomChat {host}", "domain": host, "destinations": [{"type": "public", "uri": host}],
        "session_duration": "720h",
        "policies": [{"name": f"{host}: owner", "decision": "allow", "include": [{"email": {"email": owner}}], "precedence": 1}]})
    try:
        sock = cf("POST", f"/accounts/{ACCOUNT}/access/apps", {
            **base, "name": f"LagomChat {host}: agents' sockets (token checked by the server)", "domain": f"{host}/ws",
            "destinations": [{"type": "public", "uri": f"{host}/{p}"} for p in ("ws", "listen", "img", "join")], "session_duration": "24h",
            "policies": [{"name": "Agents' sockets: bypass, the server checks the token", "decision": "bypass",
                          "include": [{"everyone": {}}], "precedence": 1}]})
    except Fail:
        cf("DELETE", f"/accounts/{ACCOUNT}/access/apps/{page['id']}", ok404=True)
        raise
    return {"page": page["id"], "sock": sock["id"], "aud": page["aud"]}


def cf_route(host: str, port: int | None) -> None:
    """the tunnel's ingress: add host -> port (port None: take it away); the catch-all 404 stays last"""
    cfg = (cf("GET", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations") or {}).get("config") or {}
    ing = [r for r in cfg.get("ingress") or [] if r.get("hostname") and r["hostname"] != host]
    if port:
        ing.append({"hostname": host, "service": f"http://127.0.0.1:{port}"})
    cfg["ingress"] = ing + [{"service": "http_status:404"}]
    cf("PUT", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations", {"config": cfg})


def cf_dns_find(host: str, zone: str = ZONE) -> list:
    return cf("GET", f"/zones/{zone}/dns_records?name={host}") or []


# ---------------------------------------------------------------- the service (launchd on a Mac, systemd --user on Linux)
def launch(slug: str, on: bool) -> None:
    ch = load().get(slug) or {}
    lagomcfg.service(slug, ch, "start" if on else "stop")


def wait_port(port: int, up: bool, secs: float = 20) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if listening(port) == up:
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- the commands
def create(title: str, slug: str) -> dict:
    title, slug = names(title, slug)
    reg = load()
    host = f"{slug}.{NEW_DOMAIN}"
    zone = ZONES.get(NEW_DOMAIN) or next((z["id"] for z in cf("GET", f"/zones?name={NEW_DOMAIN}") or []), None)
    if not zone:
        raise Fail(f"no Cloudflare zone {NEW_DOMAIN} (the domain in {lagomcfg.CFG})")
    if slug in reg or slug in RESERVED:
        raise Fail(f"{host} is taken")
    if cf_dns_find(host, zone):
        raise Fail(f"{host} already has a DNS record: another subdomain, please")
    used = {c["port"] for c in reg.values()}
    port = next((p for p in PORTS if p not in used and not listening(p)), None)
    if not port:
        raise Fail("no free port left in 8170-8199")
    cfg = lagomcfg.load()
    owner = cfg.get("owner_email") or ""
    if not owner or not (ACCOUNT and TUNNEL and IDP):
        raise Fail(f"Cloudflare or the owner's email is not set up: run ./lagom (the manager's wizard)")
    main = next((x for x in reg.values() if x.get("main")), {})
    conf, data = lagomcfg.CFG_DIR / "chats" / slug / "conf", lagomcfg.CFG_DIR / "chats" / slug / "data"   # this working copy's (#2022)
    if conf.exists() or data.exists():
        raise Fail(f"{conf} or {data} exists already")
    c = {**lagomcfg.CHAT_DEFAULTS, "title": title, "host": host, "port": port, "conf": str(conf), "data": str(data),
         "created": round(time.time()), "zone": zone, "auth": True,
         **{k: main[k] for k in ("reserved_names", "giphy_token") if main.get(k)}}   # as the main chat (was: its files copied)
    reg[slug] = c
    save(reg)                                    # in the list from the start: a half-made one can be deleted
    try:
        data.mkdir(parents=True, exist_ok=True)
        (data / "title.txt").write_text(title)
        c.update(cf_apps(host, owner))
        save(reg)
        cf_route(host, port)
        c["dns"] = cf("POST", f"/zones/{zone}/dns_records", {"type": "CNAME", "name": host, "content": f"{TUNNEL}.cfargotunnel.com",
                                                           "proxied": True, "ttl": 1, "comment": f"agentchat {slug} on the Mac :{port}"})["id"]
        save(reg)                                # the conf folder and the service are written out of the config
        launch(slug, True)
        if not wait_port(port, True):
            raise Fail(f"the server did not start: see {lagomcfg.log_file(slug, c)}")
    except Exception as e:
        raise Fail(f"{e} (what was made stays in the list: chats.py delete {slug})")
    return {"slug": slug, "title": title, "url": f"https://{host}", "port": port}


def stop(slug: str, on: bool) -> dict:
    reg = load()
    c = reg.get(slug) or {}
    if not c:
        raise Fail(f"no chat {slug!r}")
    if c.get("main") and not on:
        raise Fail("the main chat is not stopped from here")
    if not c.get("main"):
        launch(slug, on)
    wait_port(c["port"], on, 10)
    return {"slug": slug, "running": listening(c["port"])}


def delete(slug: str) -> dict:
    reg = load()
    c = reg.get(slug)
    if not c:
        raise Fail(f"no chat {slug!r}")
    if c.get("main"):
        raise Fail("the main chat is not deleted from here")
    launch(slug, False)
    lagomcfg.service_file(slug, c).unlink(missing_ok=True)
    errs = []
    for k in ("page", "sock"):
        if c.get(k):
            try:
                cf("DELETE", f"/accounts/{ACCOUNT}/access/apps/{c[k]}", ok404=True)
            except Fail as e:
                errs.append(str(e))
    for host in filter(lambda h: h and not h.startswith("127."), (c["host"], c.get("old_host"))):     # old_host: the address it moved from (#1987), served too
        zone = ZONES.get(host.split(".", 1)[1], c.get("zone", ZONE))
        try:
            cf_route(host, None)
            for r in cf_dns_find(host, zone):
                if r.get("content") == f"{TUNNEL}.cfargotunnel.com":
                    cf("DELETE", f"/zones/{zone}/dns_records/{r['id']}", ok404=True)
        except Fail as e:
            errs.append(str(e))
    keep = TRASH / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
    for k in ("conf", "data"):
        if Path(c[k]).exists():
            keep.mkdir(parents=True, exist_ok=True)
            shutil.move(c[k], keep / k)
    if errs:
        raise Fail("stopped, but Cloudflare said: " + "; ".join(errs) + f" - it stays in the list, delete {slug} again")
    if (c.get("icon") or {}).get("img"):
        (ICONS / c["icon"]["img"]).unlink(missing_ok=True)
    reg.pop(slug, None)
    save(reg)
    return {"slug": slug, "deleted": True, "kept": str(keep)}


def icon(slug: str, text: str | None, color: str | None, img: str | None, noimg: bool, bg: str | None = None) -> dict:
    reg = load()
    c = reg.get(slug)
    if not c:
        raise Fail(f"no chat {slug!r}")
    ic = dict(c.get("icon") or {})
    if text is not None:
        t = " ".join(text.split())[:8]
        if t:
            ic["t"] = t
        else:
            ic.pop("t", None)
    if color is not None:
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise Fail(f"bad colour {color!r}: #rrggbb")
        if color:
            ic["c"] = color.lower()
        else:
            ic.pop("c", None)
    if bg is not None:                                  # the chat page's background tint (the owner, #1919); "" = none
        if bg and not re.fullmatch(r"#[0-9a-fA-F]{6}", bg):
            raise Fail(f"bad background {bg!r}: #rrggbb")
        if bg:
            ic["bg"] = bg.lower()
        else:
            ic.pop("bg", None)
    old = ic.get("img")
    if img:
        data = Path(img).read_bytes()
        if len(data) > 1024 * 1024:
            raise Fail("the picture is over 1 MB")
        ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg" if data[:3] == b"\xff\xd8\xff" else \
            "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else "gif" if data[:4] == b"GIF8" else ""
        if not ext:
            raise Fail("not a png, jpeg, webp or gif picture")
        import hashlib
        ICONS.mkdir(parents=True, exist_ok=True)
        name = f"{slug}-{hashlib.sha1(data).hexdigest()[:10]}.{ext}"
        (ICONS / name).write_bytes(data)
        ic["img"] = name
    elif noimg:
        ic.pop("img", None)
    if old and old != ic.get("img"):
        (ICONS / old).unlink(missing_ok=True)
    c["icon"] = ic
    save(reg)
    return {"slug": slug, "icon": ic}


def main() -> None:
    a = [x for x in sys.argv[1:] if x != "--json"]
    js = "--json" in sys.argv
    if not a or a[0] in ("-h", "--help", "help"):
        raise SystemExit(__doc__)
    cmd, a = a[0], a[1:]
    try:
        if cmd == "list":
            r = listing(load())
        elif cmd == "create":
            title = a[0] if a else ""
            slug = a[1] if len(a) > 1 else ""
            if len(a) == 1 and SLUG_RE.match(a[0]) and " " not in a[0]:
                title, slug = "", a[0]            # one plain word: the subdomain (the title is made from it)
            r = create(title, slug)
        elif cmd in ("stop", "start") and a:
            r = stop(a[0], cmd == "start")
        elif cmd == "icon" and a:
            def opt(k):
                return a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else None
            r = icon(a[0], opt("--text"), opt("--color"), opt("--img"), "--noimg" in a, opt("--bg"))
        elif cmd == "delete" and a:
            r = delete(a[0])
        else:
            raise SystemExit(__doc__)
    except Fail as e:
        if js:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            raise SystemExit(1)
        raise SystemExit(f"chats: {e}")
    if js:
        print(json.dumps({"ok": True, "result": r}, ensure_ascii=False))
    elif cmd == "list":
        for x in r:
            print(f"{'*' if x['main'] else ' '} {x['slug']:24} {x['url']:36} :{x['port']}  {'running' if x['running'] else 'stopped'}  {x['title']}")
    else:
        print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
