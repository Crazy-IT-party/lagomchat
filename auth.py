"""agentchat: sign-in for the people at the page (the user, 2026-10-29; by email since the owner's #1146, 2026-11-07).

The chat signs in by the email Cloudflare Access proved (<conf>/owner_email set; that address alone is the owner). Without it,
Google OAuth. The session is a cookie signed with a server secret (~/.config/agentchat/secret, made on the first
start): {sid, name, avatar, exp}.

    owner     the email in <conf>/owner_email
    verified  anybody on the allow-list (allow.json) or whose document in MongoDB agentchat.users has verified: true
    pending   a signed-in account without that: sees no chat, every message gets a refusal

Sign-in is enforced when the file ~/.config/agentchat/auth_on exists (or LAGOMCHAT_AUTH=1); remove it (or make auth_off) and the
page is open as before - the way out if the sign-in ever locks the owner out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

# the environment's names are LAGOMCHAT_*; the older AGENTCHAT_* ones still work (the owner #2302)
for _k, _v in list(os.environ.items()):
    if _k.startswith("AGENTCHAT_"):
        os.environ.setdefault("LAGOMCHAT_" + _k[len("AGENTCHAT_"):], _v)

CONF = Path(os.environ.get("LAGOMCHAT_CONF", Path.home() / ".config/agentchat"))


def conf_lines(name: str) -> list[str]:
    """the lines of a file in the conf dir: who the people are stays out of the code"""
    try:
        return [x.strip() for x in (CONF / name).read_text().splitlines() if x.strip()]
    except OSError:
        return []


SECRET_FILE = Path(os.environ.get("LAGOMCHAT_SECRET", CONF / "secret"))
COOKIE = "ac_s"
# a person's id: e:<a hash of the email>, or (the owner, 2026-11-03, #1497) g:<Google sub>
SID_RE = re.compile(r"g:\d{5,30}|e:[0-9a-f]{16}")
SESSION_DAYS = 30


def _secret() -> bytes:
    try:
        return bytes.fromhex(SECRET_FILE.read_text().strip())
    except (OSError, ValueError):
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        s = secrets.token_hex(32)
        SECRET_FILE.write_text(s)
        SECRET_FILE.chmod(0o600)
        return bytes.fromhex(s)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(payload: dict, days: int = SESSION_DAYS) -> str:
    body = dict(payload, exp=int(time.time() + days * 86400))
    p = _b64(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    return p + "." + _b64(hmac.new(_secret(), p.encode(), hashlib.sha256).digest())


def read_token(tok: str | None) -> dict | None:
    try:
        p, sig = (tok or "").split(".")
        if not hmac.compare_digest(sig, _b64(hmac.new(_secret(), p.encode(), hashlib.sha256).digest())):
            return None
        body = json.loads(_unb64(p))
        return body if body.get("exp", 0) > time.time() and SID_RE.fullmatch(str(body.get("sid", ""))) else None
    except (ValueError, TypeError):
        return None


def enforced() -> bool:
    if (CONF / "auth_off").exists():
        return False
    return os.environ.get("LAGOMCHAT_AUTH") == "1" or (CONF / "auth_on").exists()


last_error = ""


# ---- Google sign-in (the owner, 2026-11-03, #1497) ----------------------------------------------------------------------------
# ~/.config/agentchat/oauth.json: {"google": {"client_id": ..., "client_secret": ...}}
# - a provider without its entry is off. Who may in: ~/.config/agentchat/allow.json {"google_domains": [...], "google_emails": [...]}
# (superman edits it: agentchat allow ...), or verified: true in MongoDB.
OAUTH_FILE, ALLOW_FILE = CONF / "oauth.json", CONF / "allow.json"
PROVIDERS = {
    "google": {"auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token", "scope": "openid email profile"},
}


def oauth_conf() -> dict:
    try:
        c = json.loads(OAUTH_FILE.read_text())
        return {k: v for k, v in c.items() if k in PROVIDERS and isinstance(v, dict) and v.get("client_id") and v.get("client_secret")}
    except (OSError, ValueError):
        return {}


def allow_list() -> dict:
    try:
        a = json.loads(ALLOW_FILE.read_text())
    except (OSError, ValueError):
        a = {}
    return {k: sorted({str(x).strip().lower() for x in a.get(k, []) if str(x).strip()}) for k in ("google_domains", "google_emails")}


def save_allow(a: dict) -> None:
    CONF.mkdir(parents=True, exist_ok=True)
    ALLOW_FILE.write_text(json.dumps(a, indent=1))


def allowed(sess: dict) -> bool:
    """a Google or email person on the allow-list"""
    a, sid = allow_list(), str(sess.get("sid", ""))
    if sid.startswith(("g:", "e:")):                  # Google, or an email Cloudflare Access proved (e:)
        em = str(sess.get("em") or "").lower()
        return bool(em) and (em in a["google_emails"] or em.rsplit("@", 1)[-1] in a["google_domains"])
    return False


def oauth_url(provider: str, redirect: str, state: str) -> str:
    c, p = oauth_conf()[provider], PROVIDERS[provider]
    q = {"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": p["scope"], "state": state}
    if provider == "google":
        q["prompt"] = "select_account"
    return p["auth"] + "?" + urllib.parse.urlencode(q)


def _post_form(url: str, data: dict) -> dict:
    r = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "agentchat"})
    with urllib.request.urlopen(r, timeout=15) as f:
        return json.loads(f.read())


def oauth_identity(provider: str, code: str, redirect: str) -> dict | None:
    """the code the provider sent back -> {sid, name, avatar, em}, or None. The token is fetched from the provider directly over TLS
    with our secret, so what it says is the provider's word (no signature to check on top)"""
    c = oauth_conf().get(provider)
    if not c:
        return None
    try:
        t = _post_form(PROVIDERS[provider]["token"], {"client_id": c["client_id"], "client_secret": c["client_secret"], "code": code,
                                                       "redirect_uri": redirect, "grant_type": "authorization_code"})
        claims = json.loads(_unb64(t["id_token"].split(".")[1]))
        if not claims.get("email_verified") or claims.get("aud") != c["client_id"]:
            return None
        return {"sid": "g:" + str(claims["sub"]), "name": claims.get("name") or claims["email"].split("@")[0], "avatar": claims.get("picture", ""),
                "em": claims["email"].lower()}
    except Exception as x:                                   # a refused code, the provider down: no sign-in
        global last_error
        last_error = f"{provider}: {x!r}"
        return None


# Cloudflare Access (the owner, 2026-11-03, #1611/#1612): a person proves their email to Access
# (a one-time code) on the way to the chat's page; Access hands the page's requests a JWT it signed (the header
# Cf-Access-Jwt-Assertion, the cookie CF_Authorization). Its email, verified against Access's keys, signs in like a Google
# account: on the allow-list - in. Never the plain Cf-Access-Authenticated-User-Email header: the LAN reaches the server's port directly
# The team and the app's audience come from the chat's setup, never from the code: LAGOMCHAT_CF_TEAM / LAGOMCHAT_CF_AUD in the
# service's environment, else `cloudflare.team` of .lagomchat/config.json. None of them: no sign-in through Access
def _cfg_team() -> str:
    try:
        from pathlib import Path
        home = Path(os.environ.get("LAGOMCHAT_HOME") or Path(__file__).resolve().parent / ".lagomchat")
        return str((json.loads((home / "config.json").read_text()).get("cloudflare") or {}).get("team") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _cfg_old_teams() -> list[str]:
    """the team's former addresses (`cloudflare.old_teams`): Access renamed, the tokens signed before still name the old one"""
    try:
        from pathlib import Path
        home = Path(os.environ.get("LAGOMCHAT_HOME") or Path(__file__).resolve().parent / ".lagomchat")
        return [str(x).rstrip("/") for x in (json.loads((home / "config.json").read_text()).get("cloudflare") or {}).get("old_teams") or [] if x]
    except (OSError, ValueError, AttributeError):
        return []


CF_TEAM = (os.environ.get("LAGOMCHAT_CF_TEAM") or _cfg_team()).rstrip("/")
CF_ISSUERS = [CF_TEAM] + [t for t in _cfg_old_teams() if t != CF_TEAM]   # the same account's keys sign them all
CF_AUD = os.environ.get("LAGOMCHAT_CF_AUD", "")
_JWKS = None


def cf_access_email(token: str | None) -> str | None:
    """the email in a Cloudflare Access JWT, if Access signed it for this app and it has not expired"""
    global _JWKS, last_error
    if not token or not CF_TEAM or not CF_AUD:            # this chat is not set up for Access
        return None
    try:
        import jwt
        if _JWKS is None:
            _JWKS = jwt.PyJWKClient(CF_TEAM + "/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
        key = _JWKS.get_signing_key_from_jwt(token).key
        body = jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD, issuer=CF_ISSUERS, options={"require": ["exp", "aud", "iss"]})
        em = str(body.get("email") or "").strip().lower()
        return em if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", em) else None
    except Exception as x:                               # a bad signature, another app's, expired, the keys unreachable
        last_error = f"cloudflare access: {type(x).__name__}: {x}"
        return None


def owner_email() -> str:
    """the file <conf>/owner_email: this chat signs in by the email Access proved, and that address alone is the owner"""
    try:
        return (CONF / "owner_email").read_text().strip().lower()
    except OSError:
        return ""


def email_sid(em: str) -> str:
    return "e:" + hashlib.sha256(em.encode()).hexdigest()[:16]     # an id that does not show the email


def owner_sid() -> str:
    """the owner's id: the hash of <conf>/owner_email; no file - nobody is the owner by id"""
    em = owner_email()
    return email_sid(em) if em else "\0none"


def make_state(nxt: str) -> str:
    """the sign-in's state: where to go after it, signed and good for 15 minutes (a forged callback has none)"""
    return make_token({"n": nxt, "st": secrets.token_hex(8)}, days=15 / 1440)


def read_state(tok: str | None) -> str | None:
    try:
        p, sig = (tok or "").split(".")
        if not hmac.compare_digest(sig, _b64(hmac.new(_secret(), p.encode(), hashlib.sha256).digest())):
            return None
        body = json.loads(_unb64(p))
        return str(body["n"]) if body.get("exp", 0) > time.time() and body.get("st") else None
    except (ValueError, TypeError, KeyError):
        return None
