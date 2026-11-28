"""LagomChat: the one config of this machine's chats (the owner, 2026-11-08, #2009) - <working copy>/.lagomchat/config.json.

Everything the chats used to keep in files of their own is here: the domains, Cloudflare, the owner's email, and every
chat with its port, folders, sign-in, guests, tokens and timeouts. The manager (manager.py, started by ./lagom) and
chats.py change it; apply() writes it out to what the servers read (each chat's conf folder, its service with the
environment, settings.json and chats.json for the older readers) and says which chats must restart to take it.

The first run imports what is there already (chats.json, settings.json, cf.token, each chat's conf folder and its
LaunchAgent), so a running Mac carries on as it was. Services: launchd on a Mac, systemd --user on Linux.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# the environment's names are LAGOMCHAT_*; the older AGENTCHAT_* ones still work (the owner #2302)
for _k, _v in list(os.environ.items()):
    if _k.startswith("AGENTCHAT_"):
        os.environ.setdefault("LAGOMCHAT_" + _k[len("AGENTCHAT_"):], _v)

HOME = Path.home()
REPO = Path(__file__).resolve().parent
CFG_DIR = Path(os.environ.get("LAGOMCHAT_HOME", REPO / ".lagomchat"))   # in the working copy, git ignores it (the owner, #2013)
CFG = CFG_DIR / "config.json"
OLD = HOME / ".config/agentchat-chats"              # the older shared folder: chats.json, settings.json, cf.token, icons
MAC = sys.platform == "darwin"
LAUNCH = HOME / "Library/LaunchAgents"
SYSTEMD = HOME / ".config/systemd/user"
LOGS = HOME / ("Library/Logs" if MAC else ".local/state/lagomchat")   # the imported chats' logs; new ones: <cfg>/chats/<slug>/
# two chat setups on one machine (two working copies, the owner #2022): a new chat's service, folders and log are this
# working copy's own - the service's name carries a short id of the copy's path, the folders are in its .lagomchat/
ENV_ID = __import__("hashlib").sha1(str(REPO).encode()).hexdigest()[:6]
TRASH = HOME / ".local/share/agentchat-deleted"

# a chat's own settings, written to its conf folder: key -> (file, kind)
CONF_FILES = {
    "owner_email": ("owner_email", "line"),
    "reserved_names": ("reserved_names", "lines"),
    "score_rival": ("score_rival", "line"),
    "owner_steam": ("owner_steam", "line"),
    "giphy_token": ("giphy.token", "line"),
    "telegram_token": ("telegram_bot.token", "line"),
    "guests": ("allow.json", "json"),
    "signin": ("signin.json", "secret-json"),          # the sign-in mode and its providers (auth.py); none: the file is not there
    # the chat's own agent (the owner, #4001): {model, effort, mode, cwd, bin} - a headless Claude Code the server starts when
    # a person comes in and stops when everybody has left (autoagent.py); no model: none, the file is not there
    "agent": ("agent.json", "secret-json"),
}
# a chat's settings given to its server in the environment
ENV = {"cf_aud": "LAGOMCHAT_CF_AUD", "embed_url": "LAGOMCHAT_EMBED_URL", "leave_delay": "LAGOMCHAT_LEAVE_DELAY",
       "people_quiet": "LAGOMCHAT_PEOPLE_QUIET", "idle_hide": "LAGOMCHAT_IDLE_HIDE",
       "agent_turns": "LAGOMCHAT_AGENT_TURNS"}
CHAT_DEFAULTS = {"title": "", "host": "", "port": 0, "main": False, "auth": False, "owner_email": "", "reserved_names": [],
                 "score_rival": "", "owner_steam": "", "giphy_token": "", "telegram_token": "",
                 "guests": {"google_domains": [], "google_emails": [], "discord_ids": []},
                 "signin": {}, "agent": {},
                 "via": "",                              # how the chat is reached: '' (by its address: this machine, or Cloudflare) / ngrok / caddy
                 "ngrok_oauth": "",                      # an ngrok chat whose sign-in is ngrok's: the provider there (none: google)
                 "cf_aud": "", "embed_url": "", "leave_delay": 1800, "people_quiet": 30, "idle_hide": 1800,
                 "agent_turns": 10}
DEFAULTS = {"owner_email": "", "domain": "", "domains": [], "moved_from": [], "plugins": [],
            "cloudflare": {"token": "", "account": "", "tunnel": "", "idp": "", "team": "", "zones": {}},
            "ngrok": {"authtoken": "", "bin": ""},      # chats reached through an ngrok tunnel (one agent for them all)
            "caddy": {"run": False, "bin": "", "email": ""},   # chats on an own domain behind Caddy; run: its service is ours
            "chats": {}}
MANAGER_PORT = 8159
PORTS = range(8170, 8200)


class Fail(Exception):
    pass


# ---------------------------------------------------------------- the file
def load() -> dict:
    try:
        c = json.loads(CFG.read_text())
    except (OSError, ValueError):
        c = {}
    return normal(c)


def pruned(v):
    """a setting without its empty parts ('' / [] / {} / False / None): a form sends every field, a file holds what is set"""
    if isinstance(v, dict):
        return {k: w for k, w in ((k, pruned(w)) for k, w in v.items()) if w not in ("", None, False, [], {})}
    if isinstance(v, list):
        return [w for w in (pruned(w) for w in v) if w not in ("", None, [], {})]
    return v.strip() if isinstance(v, str) else v


def normal(c: dict) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    out.update({k: v for k, v in (c or {}).items() if k in DEFAULTS or k.startswith("x_")})
    for k in ("cloudflare", "ngrok", "caddy"):
        out[k] = {**DEFAULTS[k], **(c.get(k) or {})}
    out["chats"] = {s: {**json.loads(json.dumps(CHAT_DEFAULTS)), **v} for s, v in (c.get("chats") or {}).items()}
    for ch in out["chats"].values():
        for k in ("signin", "agent"):
            ch[k] = pruned(ch.get(k)) if isinstance(ch.get(k), dict) else {}
    return out


def save(c: dict) -> None:
    CFG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = CFG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1) + "\n")
    os.chmod(tmp, 0o600)                                  # tokens are in it
    tmp.replace(CFG)


def chat_url(ch: dict) -> str:
    h = ch.get("host") or f"127.0.0.1:{ch.get('port')}"
    return f"http://{h}" if h.startswith(("127.", "localhost")) else f"https://{h}"


# ---------------------------------------------------------------- what is there already
def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""



# ---------------------------------------------------------------- services: launchd (Mac), systemd --user (Linux)
def service_name(slug: str, ch: dict) -> str:
    return ch.get("service") or (f"game.lagom.chat-{ENV_ID}-{slug}" if MAC else f"lagomchat-{ENV_ID}-{slug}")


def service_file(slug: str, ch: dict) -> Path:
    n = service_name(slug, ch)
    return LAUNCH / f"{n}.plist" if MAC else SYSTEMD / f"{n}.service"


def service_env(c: dict, slug: str, ch: dict) -> dict:
    env = {"LAGOMCHAT_CONF": ch["conf"], "LAGOMCHAT_DATA": ch["data"], "LAGOMCHAT_HOST": "127.0.0.1",
           "LAGOMCHAT_PORT": str(ch["port"]), "HOME": str(HOME),     # the home the config was made in: its chats list, settings, gh
           "PATH": "/opt/homebrew/bin:/usr/bin:/bin" if MAC else "/usr/local/bin:/usr/bin:/bin"}
    for key, var in ENV.items():
        v = ch.get(key)
        if v not in ("", None) and v != CHAT_DEFAULTS.get(key):
            env[var] = str(v)
    if ch.get("cf_aud"):
        env["LAGOMCHAT_CF_AUD"] = ch["cf_aud"]
    if (c.get("cloudflare") or {}).get("team"):            # the Access team the sign-in's tokens come from (no default in the code)
        env["LAGOMCHAT_CF_TEAM"] = c["cloudflare"]["team"]
    return env


def log_file(slug: str, ch: dict) -> Path:
    if ch.get("service"):                                 # an imported chat: its log where it always was
        return LOGS / f"agentchat-{'mac' if ch.get('main') and MAC else slug}.log"
    return CFG_DIR / "chats" / slug / "server.log"


def service_text(c: dict, slug: str, ch: dict) -> str:
    py = ch.get("python") or sys.executable
    log = log_file(slug, ch)
    env = service_env(c, slug, ch)
    if MAC:
        return plistlib.dumps({"EnvironmentVariables": env, "KeepAlive": True, "Label": service_name(slug, ch),
                               "ProgramArguments": [py, str(REPO / "server.py")], "RunAtLoad": True,
                               "StandardErrorPath": str(log), "StandardOutPath": str(log)}).decode()
    kv = "\n".join(f'Environment="{k}={v}"' for k, v in env.items())
    return (f"[Unit]\nDescription=LagomChat {slug}\nAfter=network-online.target\n\n[Service]\n{kv}\n"
            f"ExecStart={py} {REPO / 'server.py'}\nRestart=always\nRestartSec=2\n"
            f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n")


def _run(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(a), capture_output=True, text=True)


def service(slug: str, ch: dict, action: str) -> None:
    """start / stop / restart a chat's service (installed by apply())"""
    n, f = service_name(slug, ch), service_file(slug, ch)
    if MAC:
        dom = f"gui/{os.getuid()}"
        loaded = lambda: _run("launchctl", "print", f"{dom}/{n}").returncode == 0
        if action == "stop":
            _run("launchctl", "bootout", f"{dom}/{n}")
            for _ in range(20):                          # bootout finishes in the background: a start right after fails
                if not loaded():
                    break
                time.sleep(0.25)
        elif action == "start" or not loaded():
            for _ in range(10):
                if loaded() or _run("launchctl", "bootstrap", dom, str(f)).returncode == 0:
                    break
                time.sleep(0.5)
        else:
            _run("launchctl", "kickstart", "-k", f"{dom}/{n}")
    else:
        if action == "stop":
            _run("systemctl", "--user", "disable", "--now", n)
        elif action == "start":
            _run("systemctl", "--user", "enable", "--now", n)
        else:
            _run("systemctl", "--user", "enable", n)
            _run("systemctl", "--user", "restart", n)


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port or 0))) == 0


def wait_port(port: int, up: bool, secs: float = 20) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if listening(port) == up:
            return True
        time.sleep(0.4)
    return False


def free_port(c: dict) -> int:
    used = {ch.get("port") for ch in c["chats"].values()}
    p = next((p for p in PORTS if p not in used and not listening(p)), None)
    if not p:
        raise Fail("no free port left in 8170-8199")
    return p


# ---------------------------------------------------------------- writing it out
def _write(p: Path, text: str, mode: int | None = None) -> bool:
    if _read(p) == text.strip() and p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode:
        os.chmod(p, mode)
    return True


def _rm(p: Path) -> bool:
    if p.exists():
        p.unlink()
        return True
    return False


def apply(c: dict) -> list[str]:
    """write the config out to what the servers read; the chats whose files changed (to restart)"""
    changed = []
    _write(OLD / "settings.json", json.dumps({k: c[k] for k in ("domain", "domains", "moved_from")}, indent=1) + "\n")
    reg = {}
    for slug, ch in c["chats"].items():
        reg[slug] = {k: v for k, v in ch.items() if k not in CONF_FILES and k not in ENV and k not in ("auth", "python", "service")}
        reg[slug]["aud"] = ch.get("cf_aud") or ""
    _write(OLD / "chats.json", json.dumps(reg, ensure_ascii=False, indent=1))
    if c["cloudflare"].get("token"):
        _write(OLD / "cf.token", c["cloudflare"]["token"] + "\n", 0o600)
    for slug, ch in c["chats"].items():
        conf = Path(ch["conf"])
        conf.mkdir(parents=True, exist_ok=True, mode=0o700)
        Path(ch["data"]).mkdir(parents=True, exist_ok=True)
        ch_changed = False
        vals = {**ch, "owner_email": ch.get("owner_email") or c.get("owner_email") or ""}
        for key, (f, kind) in CONF_FILES.items():
            v = vals.get(key)
            if kind == "secret-json":
                ch_changed |= _write(conf / f, json.dumps(v, ensure_ascii=False, indent=1) + "\n", 0o600) if v else _rm(conf / f)
            elif kind == "json":
                ch_changed |= _write(conf / f, json.dumps(v or {}, ensure_ascii=False, indent=1) + "\n")
            elif kind == "lines":
                ch_changed |= _write(conf / f, "".join(f"{x}\n" for x in v or [])) if v else _rm(conf / f)
            else:
                ch_changed |= _write(conf / f, f"{v}\n", 0o600 if f.endswith(".token") else None) if v else _rm(conf / f)
        if ch.get("auth"):
            ch_changed |= _write(conf / "auth_on", "") if not (conf / "auth_on").exists() else False
        else:
            ch_changed |= _rm(conf / "auth_on")
        log_file(slug, ch).parent.mkdir(parents=True, exist_ok=True)
        ch_changed |= _write(service_file(slug, ch), service_text(c, slug, ch))
        if ch_changed:
            changed.append(slug)
    if not MAC and changed:
        _run("systemctl", "--user", "daemon-reload")
    front_doors(c)
    return changed


# ---------------------------------------------------------------- the ways in that are not Cloudflare: ngrok, an own domain behind Caddy
# One ngrok agent and one Caddy serve every chat that is reached through them; their files are written out of the config
# (.lagomchat/ngrok.yml, .lagomchat/Caddyfile) and their services follow: there while a chat needs them, restarted when the file changes.
OPEN_PATHS = ("/ws", "/listen", "/img", "/join")       # the agents' doors: the server checks a token there, no sign-in in front
NGROK_OAUTH = ("google", "github", "gitlab", "microsoft", "linkedin", "twitch")   # the providers ngrok has an app of its own for
HOST_RE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def via(ch: dict) -> str:
    """how a chat is reached: local / cloudflare / ngrok / caddy"""
    if ch.get("via") in ("ngrok", "caddy"):
        return ch["via"]
    return "local" if not ch.get("host") or ch["host"].startswith(("127.", "localhost")) else "cloudflare"


def find_bin(name: str, given: str = "") -> str:
    """a program's path: the config's, or where it usually is (a service's PATH is short)"""
    for p in (given, shutil.which(name) or "", f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if p and Path(p).is_file():
            return p
    return ""


HEADER_RE = re.compile(r"[A-Za-z0-9-]{1,64}")           # a header's name as it goes into ngrok's and Caddy's files


def id_headers(ch: dict) -> dict:
    """the header mode's names of a chat: {email, name, secret_header, secret, strip: what a browser may not send by itself}"""
    h = (ch.get("signin") or {}).get("header") or {}
    name = lambda v, d: v if HEADER_RE.fullmatch(str(v or "")) else d
    out = {"email": name(h.get("email"), "X-Forwarded-Email"), "name": name(h.get("name"), ""),
           "secret_header": name(h.get("secret_header"), "X-Proxy-Secret"), "secret": str(h.get("secret") or "")}
    out["strip"] = sorted({x.lower() for x in (out["email"], out["name"], out["secret_header"], "X-Forwarded-Email", "X-Forwarded-User") if x})
    return out


def _reached(c: dict, how: str) -> list:
    """the chats reached that way whose address can go into a config file: [(slug, chat)]"""
    return [(s, ch) for s, ch in sorted(c["chats"].items()) if via(ch) == how and HOST_RE.fullmatch(ch.get("host") or "")]


def ngrok_policy(ch: dict) -> dict:
    """what ngrok does with a request before the chat sees it: a browser's own identity headers are taken off; when the chat's
    sign-in is "a reverse proxy" (header), ngrok signs the person in and says who it is - everywhere but at the agents' doors"""
    h = id_headers(ch)
    rules = [{"actions": [{"type": "remove-headers", "config": {"headers": h["strip"]}}]}]
    if (ch.get("signin") or {}).get("mode") == "header":
        prov = ch.get("ngrok_oauth") if ch.get("ngrok_oauth") in NGROK_OAUTH else "google"
        add = {h["email"]: "${actions.ngrok.oauth.identity.email}"}
        if h["name"]:
            add[h["name"]] = "${actions.ngrok.oauth.identity.name}"
        if h["secret"]:
            add[h["secret_header"]] = h["secret"]
        rules.append({"expressions": [f"!req.url.path.startsWith('{p}')" for p in OPEN_PATHS],
                      "actions": [{"type": "oauth", "config": {"provider": prov}}, {"type": "add-headers", "config": {"headers": add}}]})
    return {"on_http_request": rules}


def ngrok_text(c: dict) -> str:
    """the ngrok agent's config (version 3) for every chat reached through it; '' with none or no authtoken.
    Written as JSON, which YAML reads"""
    eps = [{"name": slug, "url": f"https://{ch['host']}", "upstream": {"url": f"http://127.0.0.1:{ch['port']}"}, "traffic_policy": ngrok_policy(ch)}
           for slug, ch in _reached(c, "ngrok")]
    tok = (c.get("ngrok") or {}).get("authtoken") or ""
    return json.dumps({"version": 3, "agent": {"authtoken": tok}, "endpoints": eps}, indent=1) + "\n" if eps and tok else ""


def caddy_text(c: dict) -> str:
    """the Caddyfile of every chat on an own domain: its sites only, so another Caddyfile can `import` it"""
    return "\n".join(f"# {slug}\n{ch['host']} {{\n\tencode zstd gzip\n\treverse_proxy 127.0.0.1:{int(ch['port'])} {{\n"
                     + "".join(f"\t\theader_up -{x}\n" for x in id_headers(ch)["strip"]) + "\t}\n}\n" for slug, ch in _reached(c, "caddy"))


def aux_name(kind: str) -> str:
    return f"game.lagom.{kind}-{ENV_ID}" if MAC else f"lagomchat-{kind}-{ENV_ID}"


def aux_text(kind: str, argv: list[str]) -> str:
    log = CFG_DIR / f"{kind}.log"
    if MAC:
        return plistlib.dumps({"KeepAlive": True, "Label": aux_name(kind), "ProgramArguments": argv, "RunAtLoad": True,
                               "EnvironmentVariables": {"HOME": str(HOME)}, "StandardErrorPath": str(log), "StandardOutPath": str(log)}).decode()
    return (f"[Unit]\nDescription=LagomChat {kind}\nAfter=network-online.target\n\n[Service]\nExecStart={' '.join(argv)}\n"
            f"Restart=always\nRestartSec=3\nStandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n")


def _aux(kind: str, files: dict, argv: list[str] | None, changed: bool = False) -> None:
    """one of the two services and its files ({path: (text, mode)}): there and current when argv is given, else gone"""
    svc = {"service": aux_name(kind)}
    sf = service_file(kind, svc)
    if argv:
        for f, (text, mode) in files.items():
            changed |= _write(f, text, mode)
        if _write(sf, aux_text(kind, argv)):
            changed = True
            if not MAC:
                _run("systemctl", "--user", "daemon-reload")
        if changed:
            service(kind, svc, "restart")
    elif sf.exists():
        service(kind, svc, "stop")
        sf.unlink(missing_ok=True)


def front_doors(c: dict) -> None:
    ng, cd = ngrok_text(c), caddy_text(c)
    nb = find_bin("ngrok", c["ngrok"].get("bin") or "")
    f = CFG_DIR / "ngrok.yml"
    _aux("ngrok", {f: (ng, 0o600)}, [nb, "start", "--all", "--config", str(f), "--log", "stdout"] if ng and nb else None)
    if not ng:
        _rm(f)
    # Caddy: the sites' file is always written (another Caddy may import it); our own service only when asked for
    site, run = CFG_DIR / "Caddyfile", CFG_DIR / "Caddyfile.run"
    site_changed = _write(site, cd) if cd else _rm(site)
    cb = find_bin("caddy", c["caddy"].get("bin") or "")
    em = c["caddy"].get("email") or c.get("owner_email") or ""
    em = em if re.fullmatch(r"[^@\s{}]+@[^@\s{}]+", em) else ""
    top = (f"{{\n\temail {em}\n}}\n" if em else "") + f"import {site}\n"
    _aux("caddy", {run: (top, None)}, [cb, "run", "--config", str(run), "--adapter", "caddyfile"] if cd and cb and c["caddy"].get("run") else None, site_changed)


def fronts(c: dict) -> dict:
    """for the manager's page: what is there of ngrok and Caddy"""
    return {"ngrok": {"bin": find_bin("ngrok", c["ngrok"].get("bin") or ""), "token": bool(c["ngrok"].get("authtoken"))},
            "caddy": {"bin": find_bin("caddy", c["caddy"].get("bin") or ""), "file": str(CFG_DIR / "Caddyfile"), "run": bool(c["caddy"].get("run"))},
            "oauth": list(NGROK_OAUTH)}


def create_remote(c: dict, title: str, slug: str, how: str, host: str) -> dict:
    """a chat reached through ngrok (host: the ngrok domain) or on an own domain behind Caddy (host: the name that points here).
    Sign-in is on from the start: ngrok signs people in (Google) and says who they are; behind Caddy the chat signs in itself"""
    host = re.sub(r"^https?://", "", (host or "").strip().lower()).strip("/")
    if how == "caddy" and host and "." not in host and c.get("domain"):
        host = f"{host}.{c['domain']}"                    # a bare name: under the machine's domain
    if not HOST_RE.fullmatch(host):
        raise Fail("the chat's address, please: " + ("your ngrok domain, e.g. name.ngrok-free.app" if how == "ngrok" else "a domain name that points to this machine, e.g. chat.example.com"))
    if any(ch.get("host") == host for ch in c["chats"].values()):
        raise Fail(f"{host} is another chat's address")
    if how == "ngrok" and not (c["ngrok"].get("authtoken") and find_bin("ngrok", c["ngrok"].get("bin") or "")):
        raise Fail("ngrok is not set up: install it and put its authtoken into Settings - ngrok")
    if not (c.get("owner_email") or ""):
        raise Fail("the owner's email first (Settings): a chat on the internet has an owner")
    signin = {"mode": "header", "header": {"email": "X-Lagom-Email", "name": "X-Lagom-Name", "secret_header": "X-Lagom-Secret",
                                           "secret": secrets.token_urlsafe(32), "logout": "/ngrok/logout"}} if how == "ngrok" else {"mode": "oauth"}
    # no name given: the address's first part
    return {**_create(c, title, slug or ("" if title.strip() else host.split(".")[0]), host=host, via=how, auth=True, signin=signin), "via": how}


# the rest of the working copy's .py is a chat server's code (autoagent.py is run anew at every start of a chat's own agent)
SERVER_CODE_NOT = {"manager.py", "chats.py", "lagomcfg.py", "people.py", "autoagent.py"}


def code_time() -> float:
    """when the chat server's code on disk last changed"""
    return max((f.stat().st_mtime for f in REPO.glob("*.py") if f.name not in SERVER_CODE_NOT), default=0.0)


def started(port: int) -> float:
    """when the chat server on that port began, by its own /healthz; 0: it does not say (a server older than this question)"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
            m = re.search(r"started (\d+)", r.read(200).decode(errors="replace"))
            return float(m.group(1)) if m else 0.0
    except (OSError, ValueError):
        return 0.0


def status(c: dict) -> list[dict]:
    out, code = [], code_time()
    for slug, ch in sorted(c["chats"].items(), key=lambda kv: (not kv[1].get("main"), kv[1].get("created") or 0)):
        out.append({"slug": slug, "title": _read(Path(ch.get("data") or "/nonexistent") / "title.txt") or ch.get("title") or slug, "url": chat_url(ch), "port": ch["port"],
                    "main": bool(ch.get("main")), "running": listening(ch["port"]), "local": via(ch) == "local", "via": via(ch)})
        # runs code older than what is on disk now: a server.py change waits for this chat's restart
        out[-1]["stale"] = out[-1]["running"] and started(ch["port"]) < code
    return out


def _create(c: dict, title: str, slug: str, **more) -> dict:
    """a new chat: its folders, its place in the config, its service started"""
    import chats as cz
    title, slug = cz.names(title, slug)
    if slug in c["chats"]:
        raise Fail(f"{slug} is taken")
    port = free_port(c)
    conf, data = CFG_DIR / "chats" / slug / "conf", CFG_DIR / "chats" / slug / "data"
    if conf.exists() or data.exists():
        raise Fail(f"{conf} or {data} exists already")
    ch = {**json.loads(json.dumps(CHAT_DEFAULTS)), "title": title, "host": f"127.0.0.1:{port}", "port": port,
          "conf": str(conf), "data": str(data), "created": round(time.time()), "main": not c["chats"], **more}
    c["chats"][slug] = ch
    save(c)
    data.mkdir(parents=True, exist_ok=True)
    (data / "title.txt").write_text(title)
    apply(c)
    service(slug, ch, "start")
    if not wait_port(port, True):
        raise Fail(f"the server did not start: see {log_file(slug, ch)}")
    return {"slug": slug, "url": chat_url(ch)}


def create_local(c: dict, title: str, slug: str) -> dict:
    """a chat on this machine only (http://127.0.0.1:<port>): no domain, no Cloudflare, no sign-in"""
    return _create(c, title, slug)


def delete_local(c: dict, slug: str) -> dict:
    ch = c["chats"].get(slug)
    if not ch:
        raise Fail(f"no chat {slug!r}")
    service(slug, ch, "stop")
    f = service_file(slug, ch)
    f.unlink(missing_ok=True)
    keep = TRASH / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
    for k in ("conf", "data"):
        if Path(ch[k]).exists():
            keep.mkdir(parents=True, exist_ok=True)
            shutil.move(ch[k], keep / k)
    c["chats"].pop(slug)
    save(c)
    apply(c)
    return {"slug": slug, "deleted": True, "kept": str(keep)}
