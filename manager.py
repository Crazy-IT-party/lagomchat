#!/usr/bin/env python3
"""LagomChat manager (the owner, 2026-11-08, #2009, #2011): this machine's chats and their settings on one local page.

Started by ./lagom in the working copy. Listens on 127.0.0.1:8159 only; the page and its API need the manager's key
(a new one each start, given in the address it opens: ?k=...), so another site open in the same browser cannot drive
it. No chat running - the page shows the wizard; else the chats (open, start, stop, restart, new, delete) and every
setting of .lagomchat/config.json, written out to the servers on Save (lagomcfg.apply) and the changed chats restarted.

    manager.py              serve, open the page in the browser (or only the page, if a manager is up already)
    manager.py --no-open    serve, do not open the browser
    manager.py --app        for the LagomChat app: a manager in the background if none is up; prints its address
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import lagomcfg as L

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("LAGOMCHAT_MANAGER_PORT", L.MANAGER_PORT))   # taken (another working copy's manager): the next free


def free_manager_port() -> int:
    import socket
    for p in range(PORT, PORT + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit(f"lagom: no free port in {PORT}-{PORT + 39}")
STATE = L.CFG_DIR / "manager.json"                  # {port, key, pid} of the running manager: a second ./lagom opens it


def _up() -> dict | None:
    try:
        st = json.loads(STATE.read_text())
        with urllib.request.urlopen(f"http://127.0.0.1:{st['port']}/healthz", timeout=1) as r:
            if r.status == 200:
                return st
    except Exception:
        pass
    return None


def _addr(st: dict) -> str:
    return f"http://127.0.0.1:{st['port']}/?k={st['key']}"


# ---------------------------------------------------------------- the server
def serve(open_browser: bool) -> None:
    from aiohttp import web

    key = secrets.token_urlsafe(24)
    lock = asyncio.Lock()

    def ok(r=None):
        return web.json_response({"ok": True, "result": r})

    def bad(msg, code=400):
        return web.json_response({"ok": False, "error": str(msg)}, status=code)

    @web.middleware
    async def guard(req, handler):
        if req.path == "/healthz":
            return await handler(req)
        host = req.headers.get("Host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):            # a DNS-rebinding page gets nothing
            return web.Response(status=403, text="local only")
        if req.path == "/" and req.query.get("k") == key:  # the address ./lagom opened: the key goes into a cookie
            r = web.HTTPFound("/")
            r.set_cookie("lagomk", key, httponly=True, samesite="Strict", max_age=86400 * 30)
            raise r
        if not secrets.compare_digest(req.cookies.get("lagomk", ""), key):
            return web.Response(status=403, content_type="text/html", text=(
                "<!doctype html><meta charset=utf-8><body style='font:15px system-ui;background:#15161c;color:#ddd;padding:40px'>"
                "This manager was started again: open it with <code>./lagom</code> in the working copy.</body>"))
        if req.method == "POST" and req.headers.get("Origin") not in (None, f"http://{req.host}"):
            return web.Response(status=403, text="bad origin")
        return await handler(req)

    async def page(_req):
        return web.Response(text=(HERE / "manager.html").read_text(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def healthz(_req):
        return web.Response(text="ok")

    def state() -> dict:
        c = L.load()
        for ch in c["chats"].values():                    # the title the chat itself shows (superman may rename it)
            t = L._read(Path(ch.get("data") or "/nonexistent") / "title.txt")
            if t:
                ch["title"] = t
        return {"config": c, "chats": L.status(c), "platform": "mac" if L.MAC else "linux", "config_file": str(L.CFG),
                "repo": str(HERE), "cloudflare_ready": bool(c["cloudflare"].get("token") and c["cloudflare"].get("account")
                                                            and c["cloudflare"].get("tunnel") and c.get("domain"))}

    async def api_state(_req):
        return ok(await asyncio.to_thread(state))

    def _save(new: dict) -> dict:
        old = L.load()
        c = L.normal(new)
        if set(c["chats"]) != set(old["chats"]):
            raise L.Fail("chats are added and deleted with their own buttons")
        ports = [ch["port"] for ch in c["chats"].values()]
        if len(set(ports)) != len(ports):
            raise L.Fail("two chats on one port")
        for s, ch in c["chats"].items():
            for k in ("conf", "data", "service", "main", "page", "sock", "dns", "zone", "created", "aud"):
                if k in old["chats"][s]:
                    ch[k] = old["chats"][s][k]           # the machine's own, not the form's
            if ch.get("title") and ch["title"] != old["chats"][s].get("title"):
                Path(ch["data"]).mkdir(parents=True, exist_ok=True)
                (Path(ch["data"]) / "title.txt").write_text(ch["title"])
            o = old["chats"][s]
            if ch["port"] != o["port"] and ch.get("host") and not ch["host"].startswith("127."):
                import chats as cz
                cz.cf_route(ch["host"], ch["port"])
            if ch.get("host", "").startswith("127."):
                ch["host"] = f"127.0.0.1:{ch['port']}"
        L.save(c)
        changed = L.apply(c)
        for s in changed:
            ch = c["chats"][s]
            if L.listening(old["chats"][s]["port"]) or L.listening(ch["port"]):
                L.service(s, ch, "restart")
        return {"restarted": changed}

    async def api_save(req):
        async with lock:
            try:
                return ok(await asyncio.to_thread(_save, (await req.json())["config"]))
            except (L.Fail, KeyError, ValueError, TypeError) as e:
                return bad(e)

    def _op(op: str, slug: str) -> dict:
        c = L.load()
        ch = c["chats"].get(slug)
        if not ch:
            raise L.Fail(f"no chat {slug!r}")
        L.apply(c)                                       # its service file is there before it starts
        if op == "start":
            L.service(slug, ch, "start")
        elif op == "stop":
            L.service(slug, ch, "stop")
        elif op == "restart":
            L.service(slug, ch, "restart")
        elif op == "delete":
            if ch.get("host") and not ch["host"].startswith("127."):
                import chats as cz
                return cz.delete(slug)
            return L.delete_local(c, slug)
        else:
            raise L.Fail(f"no such op {op!r}")
        L.wait_port(ch["port"], op != "stop", 15)
        return {"slug": slug, "running": L.listening(ch["port"])}

    async def api_op(req):
        async with lock:
            try:
                q = await req.json()
                return ok(await asyncio.to_thread(_op, q["op"], q["slug"]))
            except (L.Fail, KeyError, ValueError) as e:
                return bad(e)
            except Exception as e:                       # chats.py's Fail and the like
                return bad(e)

    def _new(q: dict) -> dict:
        c = L.load()
        if q.get("owner_email"):
            c["owner_email"] = q["owner_email"].strip().lower()
            L.save(c)
        if q.get("where") == "domain":
            if not state()["cloudflare_ready"]:
                raise L.Fail("Cloudflare is not set up yet: the domain, its token, the account and the tunnel (Settings)")
            r = subprocess.run([sys.executable, str(HERE / "chats.py"), "create", q.get("title") or "", q.get("slug") or "",
                                "--json"], capture_output=True, text=True, timeout=180)
            out = json.loads(r.stdout or "{}")
            if not out.get("ok"):
                raise L.Fail(out.get("error") or r.stderr[-300:] or "chats.py create failed")
            return out["result"]
        return L.create_local(c, q.get("title") or "", q.get("slug") or "")

    async def api_new(req):
        async with lock:
            try:
                return ok(await asyncio.to_thread(_new, await req.json()))
            except Exception as e:
                return bad(e)

    app = web.Application(middlewares=[guard])
    app.router.add_get("/", page)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/save", api_save)
    app.router.add_post("/api/op", api_op)
    app.router.add_post("/api/new", api_new)

    port = free_manager_port()
    st = {"port": port, "key": key, "pid": os.getpid()}

    async def started(_app):
        L.CFG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        STATE.write_text(json.dumps(st))
        os.chmod(STATE, 0o600)
        print(f"LagomChat manager: {_addr(st)}", flush=True)
        if open_browser:
            webbrowser.open(_addr(st))

    async def stopped(_app):
        try:
            if json.loads(STATE.read_text()).get("pid") == os.getpid():
                STATE.unlink()
        except (OSError, ValueError):
            pass

    app.on_startup.append(started)
    app.on_cleanup.append(stopped)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


def main() -> None:
    a = sys.argv[1:]
    st = _up()
    if "--app" in a:
        if not st:
            L.CFG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            log = open(L.CFG_DIR / "manager.log", "a")
            subprocess.Popen([sys.executable, __file__, "--no-open"], stdout=log, stderr=log, start_new_session=True, cwd=HERE)
            for _ in range(60):
                time.sleep(0.25)
                st = _up()
                if st:
                    break
        if not st:
            raise SystemExit("lagom: the manager did not start, see .lagomchat/manager.log")
        up = [x for x in L.status(L.load()) if x["running"]]
        # the app opens the wizard when no chat runs; else, an app that remembers no address yet opens the first chat here
        print(_addr(st), len(up), *(up[0]["url"],) if up else (), flush=True)
        return
    if st:
        print(f"LagomChat manager is running already: {_addr(st)}")
        if "--no-open" not in a:
            webbrowser.open(_addr(st))
        return
    serve("--no-open" not in a)


if __name__ == "__main__":
    main()
