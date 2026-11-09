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
}
# a chat's settings given to its server in the environment
ENV = {"cf_aud": "LAGOMCHAT_CF_AUD", "embed_url": "LAGOMCHAT_EMBED_URL", "leave_delay": "LAGOMCHAT_LEAVE_DELAY",
       "people_quiet": "LAGOMCHAT_PEOPLE_QUIET", "idle_hide": "LAGOMCHAT_IDLE_HIDE",
       "agent_turns": "LAGOMCHAT_AGENT_TURNS"}
CHAT_DEFAULTS = {"title": "", "host": "", "port": 0, "main": False, "auth": False, "owner_email": "", "reserved_names": [],
                 "score_rival": "", "owner_steam": "", "giphy_token": "", "telegram_token": "",
                 "guests": {"google_domains": [], "google_emails": [], "discord_ids": []},
                 "cf_aud": "", "embed_url": "", "leave_delay": 1800, "people_quiet": 30, "idle_hide": 1800,
                 "agent_turns": 10}
DEFAULTS = {"owner_email": "", "domain": "", "domains": [], "moved_from": [], "plugins": [],
            "cloudflare": {"token": "", "account": "", "tunnel": "", "idp": "", "team": "", "zones": {}}, "chats": {}}
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


def normal(c: dict) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    out.update({k: v for k, v in (c or {}).items() if k in DEFAULTS or k.startswith("x_")})
    out["cloudflare"] = {**DEFAULTS["cloudflare"], **(c.get("cloudflare") or {})}
    out["chats"] = {s: {**json.loads(json.dumps(CHAT_DEFAULTS)), **v} for s, v in (c.get("chats") or {}).items()}
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
            if kind == "json":
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
    return changed


# ---------------------------------------------------------------- the chats
def status(c: dict) -> list[dict]:
    out = []
    for slug, ch in sorted(c["chats"].items(), key=lambda kv: (not kv[1].get("main"), kv[1].get("created") or 0)):
        out.append({"slug": slug, "title": _read(Path(ch.get("data") or "/nonexistent") / "title.txt") or ch.get("title") or slug, "url": chat_url(ch), "port": ch["port"],
                    "main": bool(ch.get("main")), "running": listening(ch["port"]), "local": not ch.get("host") or
                    ch["host"].startswith("127.")})
    return out


def create_local(c: dict, title: str, slug: str) -> dict:
    """a chat on this machine only (http://127.0.0.1:<port>): no domain, no Cloudflare, no sign-in"""
    import chats as cz
    title, slug = cz.names(title, slug)
    if slug in c["chats"]:
        raise Fail(f"{slug} is taken")
    port = free_port(c)
    conf, data = CFG_DIR / "chats" / slug / "conf", CFG_DIR / "chats" / slug / "data"
    if conf.exists() or data.exists():
        raise Fail(f"{conf} or {data} exists already")
    ch = {**json.loads(json.dumps(CHAT_DEFAULTS)), "title": title, "host": f"127.0.0.1:{port}", "port": port,
          "conf": str(conf), "data": str(data), "created": round(time.time()), "main": not c["chats"]}
    c["chats"][slug] = ch
    save(c)
    data.mkdir(parents=True, exist_ok=True)
    (data / "title.txt").write_text(title)
    apply(c)
    service(slug, ch, "start")
    if not wait_port(port, True):
        raise Fail(f"the server did not start: see {log_file(slug, ch)}")
    return {"slug": slug, "url": chat_url(ch)}


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
