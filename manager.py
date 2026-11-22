#!/usr/bin/env python3
"""LagomChat manager (the owner, 2026-11-08, #2009, #2011): this machine's chats and their settings on one local page.

Started by ./lagom in the working copy. Listens on 127.0.0.1:8159 only; the page and its API need the manager's key
(a new one each start, given in the address it opens: ?k=...), so another site open in the same browser cannot drive
it. No chat running - the page shows the wizard; else the chats (open, start, stop, restart, new, delete) and every
setting of .lagomchat/config.json, written out to the servers on Save (lagomcfg.apply) and the changed chats restarted.

    manager.py              serve, open the page in the browser (or only the page, if a manager is up already)
    manager.py --no-open    serve, do not open the browser
    manager.py --app        for the LagomChat app: a manager in the background if none is up; prints its address

The same from the command line (./lagom <command>), no page needed:

    status                  the chats (running or not, which need a restart for newer code) and the manager's address
    stop [<chat> ...]       stop the named chats; none named: every chat, ngrok / Caddy and the manager - the whole setup
    start [<chat> ...]      start the named chats; none named: what the last `stop` stopped (or every chat) and the manager
    restart [<chat> ...]    restart the named chats; none named: every running one, the main chat last
    clone <folder>          another setup from scratch: this code in <folder> with nothing of this one's chats and settings
    help                    this text
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
        return {"config": c, "chats": L.status(c), "fronts": L.fronts(c), "platform": "mac" if L.MAC else "linux", "config_file": str(L.CFG),
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
            ch["via"] = o.get("via") or ""               # how a chat is reached is set when it is made
            if ch["port"] != o["port"] and L.via(ch) == "cloudflare":
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
            if L.via(ch) == "cloudflare":
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
        if q.get("where") in ("ngrok", "caddy"):
            return L.create_remote(c, q.get("title") or "", q.get("slug") or "", q["where"], q.get("host") or "")
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


# ---------------------------------------------------------------- the commands of ./lagom
MANAGER_JOB = "game.lagom.manager"                   # a LaunchAgent somebody made for this manager (a Mac): stopped and started as that
STOPPED = L.CFG_DIR / "stopped.json"                 # what `stop` stopped, for `start`


def _job_file() -> Path | None:
    """the manager's LaunchAgent, if there is one and it is this working copy's"""
    f = L.LAUNCH / f"{MANAGER_JOB}.plist"
    try:
        import plistlib
        args = plistlib.loads(f.read_bytes()).get("ProgramArguments") or []
        return f if L.MAC and any(Path(x).resolve().parent == HERE for x in args if "/" in str(x)) else None
    except (OSError, ValueError):
        return None


def manager_stop() -> str:
    job, st = _job_file(), _up()
    if job and L._run("launchctl", "print", f"gui/{os.getuid()}/{MANAGER_JOB}").returncode == 0:
        L._run("launchctl", "bootout", f"gui/{os.getuid()}/{MANAGER_JOB}")
        return "stopped (its LaunchAgent is unloaded until `./lagom start` or the next login)"
    if st and st.get("pid"):
        try:
            os.kill(int(st["pid"]), 15)
            return "stopped"
        except (OSError, ValueError) as e:
            return f"not stopped: {e}"
    return "was not running"


def _spawn(secs: float = 15) -> dict | None:
    """the manager as a process of its own, waited for -> its state, None: it did not come up"""
    L.CFG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = open(L.CFG_DIR / "manager.log", "a")
    subprocess.Popen([sys.executable, __file__, "--no-open"], stdout=log, stderr=log, start_new_session=True, cwd=HERE)
    return _wait_up(secs)


def _wait_up(secs: float) -> dict | None:
    end = time.time() + secs
    while time.time() < end:
        time.sleep(0.25)
        st = _up()
        if st:
            return st
    return None


def manager_start() -> str:
    if _up():
        return "runs already"
    job = _job_file()
    if job:
        L._run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(job))
    return "started" if (_wait_up(10) if job else _spawn(10)) else "did not start: see .lagomchat/manager.log"


def _aux_services() -> list[str]:
    return [k for k in ("ngrok", "caddy") if L.service_file(k, {"service": L.aux_name(k)}).exists()]


def _picked(c: dict, names: list[str]) -> list[str]:
    bad = [n for n in names if n not in c["chats"]]
    if bad:
        raise L.Fail(f"no chat {', '.join(bad)}: `./lagom status` lists them")
    return names


def command(cmd: str, names: list[str]) -> int:
    c = L.load()
    last = lambda slugs: sorted(slugs, key=lambda s: bool(c["chats"][s].get("main")))     # the main chat last
    if cmd == "status":
        st = _up()
        rows = L.status(c)
        for x in rows:
            print(f"{'*' if x['main'] else ' '} {x['slug']:<22} {'running' if x['running'] else 'stopped':<8} :{x['port']:<6}"
                  f"{'needs a restart (older code)  ' if x.get('stale') else ''}{x['url']}  {x['title']}")
        if not rows:
            print("no chats yet: ./lagom opens the wizard")
        for k in _aux_services():
            print(f"  {k}: its service is installed")
        print(f"manager: {'http://127.0.0.1:%s (./lagom opens it)' % st['port'] if st else 'not running'}")
        print(f"setup: {L.CFG_DIR}")
        return 0
    if cmd == "stop":
        whole = not names
        slugs = [s for s in (_picked(c, names) or list(c["chats"])) if L.listening(c["chats"][s]["port"])]
        if whole:                                        # for `start`: what ran
            L.CFG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            STOPPED.write_text(json.dumps(slugs))
        for s in last(slugs):
            L.service(s, c["chats"][s], "stop")
            print(f"{s}: {'stopped' if L.wait_port(c['chats'][s]['port'], False, 10) else 'STILL RUNNING'}", flush=True)
        if whole:
            for k in _aux_services():
                L.service(k, {"service": L.aux_name(k)}, "stop")
                print(f"{k}: stopped")
            print(f"manager: {manager_stop()}")
            if L.MAC:
                print("The chats' addresses stay where they point (Cloudflare, DNS): they answer with an error until `./lagom start`. "
                      "A login to this Mac starts the chats again by itself.")
        elif not slugs:
            print("nothing of that was running")
        return 0
    if cmd == "start":
        whole = not names
        if whole:
            try:
                want = [s for s in json.loads(STOPPED.read_text()) if s in c["chats"]]
            except (OSError, ValueError):
                want = list(c["chats"])
        else:
            want = _picked(c, names)
        L.apply(c)                                       # the services' files are there before they start
        bad = 0
        for k in (_aux_services() if whole else []):
            L.service(k, {"service": L.aux_name(k)}, "start")
            print(f"{k}: started")
        for s in reversed(last(want)):                   # the main chat first
            ch = c["chats"][s]
            if L.listening(ch["port"]):
                print(f"{s}: runs already")
                continue
            L.service(s, ch, "start")
            up = L.wait_port(ch["port"], True, 20)
            bad += not up
            print(f"{s}: {'started' if up else 'DID NOT START: see ' + str(L.log_file(s, ch))}", flush=True)
        if whole:
            STOPPED.unlink(missing_ok=True)
            print(f"manager: {manager_start()}")
        return 1 if bad else 0
    if cmd == "restart":
        slugs = _picked(c, names) or [s for s in c["chats"] if L.listening(c["chats"][s]["port"])]
        L.apply(c)
        bad = 0
        for s in last(slugs):
            ch = c["chats"][s]
            L.service(s, ch, "restart")
            time.sleep(1)
            up = L.wait_port(ch["port"], True, 25)
            bad += not up
            print(f"{s}: {'restarted' if up else 'DID NOT COME BACK: see ' + str(L.log_file(s, ch))}", flush=True)
        return 1 if bad else 0
    if cmd == "clone":
        if len(names) != 1:
            raise L.Fail("./lagom clone <folder>: where the other setup goes")
        dst = Path(names[0]).expanduser().resolve()
        if dst.exists() and any(dst.iterdir()):
            raise L.Fail(f"{dst} is there already and not empty")
        r = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(HERE), str(dst)], capture_output=True, text=True)
        if r.returncode:
            raise L.Fail(f"git clone failed: {r.stderr.strip()[-300:]}")
        dirty = subprocess.run(["git", "-C", str(HERE), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip()
        print(f"Another setup is in {dst}: this code as committed, none of this one's chats or settings.")
        if dirty:
            print("Changes here that are not committed did not go with it.")
        print(f"Start it:  cd {dst} && ./lagom\n"
              "Both on one machine share ~/.config/agentchat-chats (the domains, the list for the rail) and the ports 8170-8199:\n"
              "the one saved last writes that folder; give the other setup another domain only if this one is stopped.")
        return 0
    raise L.Fail(f"no command {cmd!r}: ./lagom help")


def main() -> None:
    a = sys.argv[1:]
    words = [x for x in a if not x.startswith("-")]
    if words or "-h" in a or "--help" in a:
        if not words or words[0] == "help":
            print(__doc__)
            return
        try:
            raise SystemExit(command(words[0], words[1:]))
        except L.Fail as e:
            raise SystemExit(f"lagom: {e}")
    st = _up()
    if "--app" in a:
        st = st or _spawn()
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
