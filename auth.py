"""agentchat: sign-in for the people at the page (the user, 2026-10-29; by email since the owner's #1146, 2026-11-07).

Who a person is comes from one of three places, the chat's sign-in mode (<conf>/signin.json, "mode"):

    access   the email Cloudflare Access proved (its signed token with every request)
    oauth    the chat's own sign-in: Google, GitHub, any OpenID Connect provider (Keycloak, Authentik, Entra, Okta ...)
    header   the email a reverse proxy in front of the chat proved and put into a header (oauth2-proxy, Authelia, Tailscale)

No "mode" there: access when <conf>/owner_email is set, else oauth - as it was before the modes. In every mode a person is
their proven email (the id is a hash of it), so a chat that changes its mode keeps its people. The session of the oauth
mode is a cookie signed with a server secret (~/.config/agentchat/secret, made on the first start): {sid, name, avatar, em, exp}.

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


# ---- the chat's own sign-in (Google: the owner, 2026-11-03, #1497; any provider: 2026-11-22) -------------------------------
# <conf>/signin.json (0600: it holds the providers' secrets):
#   {"mode": "oauth",
#    "providers": {
#      "google": {"client_id": ..., "client_secret": ..., "domains": ["gmail.com"]},
#      "github": {"client_id": ..., "client_secret": ...},
#      "work":   {"type": "oidc", "name": "Keycloak", "issuer": "https://id.example.com/realms/x", "client_id": ..., "client_secret": ...}},
#    "header": {"email": "X-Forwarded-Email", "name": "X-Forwarded-User", "from": ["127.0.0.1"], "secret_header": "X-Proxy-Secret", "secret": "..."}}
# A provider's key is its name in the address (/auth/<key>/login, /auth/<key>/callback: the redirect to register with it);
# "type" is google / github / oidc (the key itself for the first two). "domains": only emails of these domains sign in
# through it (google: a domain other than gmail.com must be the account's Workspace domain, its `hd`). An oidc provider that
# does not say email_verified (Entra) is taken with "trust_email": true. The older <conf>/oauth.json {"google": {...}} still works.
# Who is let into the chat is another list: allow.json {"google_domains": [...], "google_emails": [...]} (any provider's emails;
# superman edits it: agentchat allow ...), or verified: true in MongoDB.
OAUTH_FILE, ALLOW_FILE, SIGNIN_FILE = CONF / "oauth.json", CONF / "allow.json", CONF / "signin.json"
MODES = ("access", "oauth", "header")
PROVIDERS = {
    "google": {"auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token", "scope": "openid email profile",
               "issuer": "https://accounts.google.com"},
    "github": {"auth": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token", "scope": "read:user user:email",
               "api": "https://api.github.com"},
}
PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,23}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SIGNIN: tuple[float, dict] = (-1.0, {})


def signin_conf() -> dict:
    """<conf>/signin.json, read again when the file changes; none or a broken one: {}"""
    global _SIGNIN
    try:
        mt = SIGNIN_FILE.stat().st_mtime
    except OSError:
        return {}
    if _SIGNIN[0] != mt:
        try:
            c = json.loads(SIGNIN_FILE.read_text())
        except (OSError, ValueError):
            c = {}
        _SIGNIN = (mt, c if isinstance(c, dict) else {})
    return _SIGNIN[1]


def mode() -> str:
    """where a person's email comes from: access / oauth / header"""
    m = signin_conf().get("mode")
    return m if m in MODES else "access" if owner_email() else "oauth"


def oauth_conf() -> dict:
    """the providers that are set up: key -> {type, name, client_id, client_secret, domains, ...}"""
    out = {}
    try:
        old = json.loads(OAUTH_FILE.read_text())
    except (OSError, ValueError):
        old = {}
    src = {**(old if isinstance(old, dict) else {}), **(signin_conf().get("providers") or {})}
    for k, v in src.items():
        if not isinstance(v, dict) or not PROVIDER_ID.fullmatch(str(k)) or k in ("cf", "logout"):
            continue
        typ = str(v.get("type") or (k if k in PROVIDERS else ""))
        if typ not in ("google", "github", "oidc") or not v.get("client_id") or not v.get("client_secret"):
            continue
        iss = str(v.get("issuer") or "").rstrip("/")
        if typ == "oidc" and not _safe_url(iss):
            continue
        out[k] = {**v, "type": typ, "issuer": iss, "name": str(v.get("name") or {"google": "Google", "github": "GitHub"}.get(typ, k.title()))[:40],
                  "domains": sorted({str(d).strip().lower().lstrip("@") for d in v.get("domains") or [] if str(d).strip()})}
    return out


def _safe_url(u: str) -> bool:
    """a provider's word comes over TLS, or from this machine"""
    return bool(re.match(r"https://[^/]|http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", u))


def provider_names() -> dict:
    return {k: v["name"] for k, v in oauth_conf().items()}


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
    """a person on the allow-list, by the email a provider proved"""
    a, sid = allow_list(), str(sess.get("sid", ""))
    if sid.startswith(("g:", "e:")):                  # g: a Google account of before 2026-11-22; e: a proven email
        em = str(sess.get("em") or "").lower()
        return bool(em) and (em in a["google_emails"] or em.rsplit("@", 1)[-1] in a["google_domains"])
    return False


_DISCOVERY: dict[str, tuple[float, dict]] = {}


def _get_json(url: str, token: str = "") -> dict | list:
    r = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "agentchat", **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(r, timeout=15) as f:
        return json.loads(f.read(1 << 20))


def _endpoints(c: dict) -> dict:
    """a provider's addresses: the known ones, or what an OpenID Connect issuer says of itself (asked once an hour)"""
    if c["type"] != "oidc":
        return PROVIDERS[c["type"]]
    hit = _DISCOVERY.get(c["issuer"])
    if not hit or time.time() - hit[0] > 3600:
        d = _get_json(c["issuer"] + "/.well-known/openid-configuration")
        if str(d.get("issuer") or "").rstrip("/") != c["issuer"]:
            raise ValueError("the issuer's own description names another issuer")
        for k in ("authorization_endpoint", "token_endpoint"):
            if not _safe_url(str(d.get(k) or "")):
                raise ValueError(f"the issuer gave no usable {k}")
        hit = _DISCOVERY[c["issuer"]] = (time.time(), {"auth": d["authorization_endpoint"], "token": d["token_endpoint"],
                                                        "scope": str(c.get("scope") or "openid email profile"), "issuer": c["issuer"]})
    return hit[1]


def oauth_url(provider: str, redirect: str, state: str, nonce: str = "") -> str:
    c = oauth_conf()[provider]
    p = _endpoints(c)
    q = {"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": p["scope"], "state": state}
    if c["type"] != "github" and nonce:
        q["nonce"] = nonce
    if c["type"] == "google":
        q["prompt"] = "select_account"
        work = [d for d in c["domains"] if d != "gmail.com"]
        if len(c["domains"]) == 1 and work:
            q["hd"] = work[0]                                # Google shows that domain's accounts only (a hint: the answer is checked)
    return p["auth"] + ("&" if "?" in p["auth"] else "?") + urllib.parse.urlencode(q)


def _post_form(url: str, data: dict) -> dict:
    r = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "agentchat"})
    with urllib.request.urlopen(r, timeout=15) as f:
        return json.loads(f.read(1 << 20))


def masked(em: str) -> str:
    """an email for a log: m*****ke@example.com - enough to know whose it is, not the address itself"""
    name, _, dom = em.rpartition("@")
    keep = 2 if len(name) > 4 else 1 if len(name) > 2 else 0     # a short name shows less of its end
    return name[:1] + "*" * (len(name) - 1 - keep) + name[len(name) - keep:] + "@" + dom


def _domain_ok(c: dict, em: str, hd: str = "") -> bool:
    """may this email sign in through this provider at all (its "domains"; none: any)"""
    if not c["domains"]:
        return True
    dom = em.rsplit("@", 1)[-1]
    if dom not in c["domains"]:
        return False
    # a Google account may carry any address it once proved; the people of a Workspace domain are those whose account is the domain's
    return c["type"] != "google" or dom == "gmail.com" or hd.lower() == dom


def oauth_identity(provider: str, code: str, redirect: str, nonce: str = "") -> dict | None:
    """the code the provider sent back -> {sid, name, avatar, em}, or None. The token is fetched from the provider directly over TLS
    with our secret, so what it says is the provider's word (no signature to check on top)"""
    global last_error
    c = oauth_conf().get(provider)
    if not c:
        return None
    try:
        p = _endpoints(c)
        t = _post_form(p["token"], {"client_id": c["client_id"], "client_secret": c["client_secret"], "code": code,
                                    "redirect_uri": redirect, "grant_type": "authorization_code"})
        hd = ""
        if c["type"] == "github":
            tok = str(t["access_token"])
            u = _get_json(p["api"] + "/user", tok)
            mails = _get_json(p["api"] + "/user/emails", tok)
            # the addresses GitHub has verified, the primary one first; with "domains", the first of them that fits
            good = [str(m["email"]).lower() for m in sorted(mails, key=lambda m: not m.get("primary")) if m.get("verified") and m.get("email")]
            em = next((m for m in good if _domain_ok(c, m)), good[0] if good else "")
            name, avatar = str(u.get("name") or u.get("login") or ""), str(u.get("avatar_url") or "")
        else:
            claims = json.loads(_unb64(t["id_token"].split(".")[1]))
            aud = claims.get("aud")
            if c["client_id"] not in (aud if isinstance(aud, list) else [aud]) or str(claims.get("iss") or "").rstrip("/") != p["issuer"] \
                    or float(claims.get("exp") or 0) < time.time() or (nonce and claims.get("nonce") != nonce):
                last_error = f"{provider}: the token is not for this sign-in (aud, iss, exp or nonce)"
                return None
            if claims.get("email_verified") not in (True, "true") and not (c["type"] == "oidc" and c.get("trust_email") is True):
                last_error = f"{provider}: the email is not verified by the provider"
                return None
            em, hd = str(claims.get("email") or "").strip().lower(), str(claims.get("hd") or "")
            name, avatar = str(claims.get("name") or claims.get("preferred_username") or ""), str(claims.get("picture") or "")
        if not EMAIL_RE.fullmatch(em) or len(em) > 254:
            last_error = f"{provider}: no verified email came"
            return None
        if not _domain_ok(c, em, hd):
            last_error = f"{provider}: {masked(em)} is not of the domains this sign-in takes"
            return None
        return {"sid": email_sid(em), "name": (name or em.split("@")[0])[:80], "avatar": avatar if avatar.startswith("https://") else "", "em": em}
    except Exception as x:                                   # a refused code, the provider down: no sign-in
        last_error = f"{provider}: {x!r}"
        return None


def header_conf() -> dict:
    """the header mode's setup: {email: the header's name, name, from: the proxy's addresses, secret: [header, value] or None}"""
    h = signin_conf().get("header")
    h = h if isinstance(h, dict) else {}
    sec = str(h.get("secret") or "")
    return {"email": str(h.get("email") or "X-Forwarded-Email"), "name": str(h.get("name") or ""),
            "from": [str(x) for x in h.get("from") or ["127.0.0.1", "::1"]],
            "secret": [str(h.get("secret_header") or "X-Proxy-Secret"), sec] if sec else None,
            "logout": str(h.get("logout") or "")}


def header_identity(peer: str, headers) -> dict | None:
    """who the reverse proxy says this is. Its word alone: the connection must come from the proxy's address, and carry the
    shared secret when one is set - the proxy has to take these headers off what a browser sends, or anybody is anybody"""
    h = header_conf()
    if peer not in h["from"]:
        return None
    if h["secret"] and not hmac.compare_digest(str(headers.get(h["secret"][0]) or "").encode(), h["secret"][1].encode()):
        return None
    em = str(headers.get(h["email"]) or "").strip().lower()
    if not EMAIL_RE.fullmatch(em) or len(em) > 254:
        return None
    return {"sid": email_sid(em), "name": (str(headers.get(h["name"]) or "") if h["name"] else "")[:80] or em.split("@")[0], "em": em}


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


STATE_COOKIE = "ac_st"


def make_state(nxt: str) -> tuple[str, str]:
    """the sign-in's state: where to go after it, signed and good for 15 minutes (a forged callback has none) -> (state, st).
    st goes into a cookie of the browser that began the sign-in: a callback from another browser is not its end (nobody is
    signed in to somebody else's account by a link), and it is the nonce the provider puts into its token"""
    st = secrets.token_hex(16)
    return make_token({"n": nxt, "st": st}, days=15 / 1440), st


def read_state(tok: str | None, st: str | None = None) -> str | None:
    try:
        p, sig = (tok or "").split(".")
        if not hmac.compare_digest(sig, _b64(hmac.new(_secret(), p.encode(), hashlib.sha256).digest())):
            return None
        body = json.loads(_unb64(p))
        if not (body.get("exp", 0) > time.time() and body.get("st")) or not hmac.compare_digest(str(body["st"]), str(st or "")):
            return None
        return str(body["n"])
    except (ValueError, TypeError, KeyError):
        return None
