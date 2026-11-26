"""What a public snapshot of the LagomChat repo would carry that it should not: e-mail addresses, home paths, private network
addresses, key-like strings, and the private words of this install (its chats' names, its guests' addresses, the owner's
address and domain, taken from .lagomchat/config.json). Run from the repo's root; reads `git ls-files` minus stockwatch/.

    python3 <this skill>/pii_scan.py

The private words are never printed: only the file, a masked form (two letters and stars) and the count. Key-like strings are
cut after 14 characters. Exit code 1 when anything was found, 0 when the snapshot is clean."""
import json
import re
import subprocess
import sys

files = [f for f in subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
         if f and not f.startswith("stockwatch/")]
try:
    conf = json.load(open(".lagomchat/config.json"))
except (OSError, ValueError):
    conf = {}
priv = set()
for slug, ch in (conf.get("chats") or {}).items():
    priv.add(slug.lower())
    for g in ch.get("guests") or []:
        if isinstance(g, str):
            priv.add(g.lower())
            priv.add(g.split("@")[0].lower())
if conf.get("owner_email"):                      # the domain is the project's public one: not a leak
    priv.add(str(conf["owner_email"]).lower())
# a short word (chat, main) is in every program: only the longer ones are looked for. A common word that is also a chat's name
# shows up as a hit: the one who reads the list decides (this file names none of them, it is public)
priv = {p for p in priv if len(p) >= 5}
pats = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"),
    "home path": re.compile(r"/Users/[A-Za-z][A-Za-z0-9_.-]+|/home/[A-Za-z][A-Za-z0-9_.-]+"),
    "private ip": re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "key-like": re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{16,}|xox[abp]-[A-Za-z0-9-]{10,}"
                           r"|AIza[0-9A-Za-z_-]{30,}|\b[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY)"),
    "token assign": re.compile(r"(?i)(?:token|secret|api_?key|password)\s*[=:]\s*[\"'][A-Za-z0-9_\-]{20,}[\"']"),
}
# addresses that are examples or the project's own public ones
fine_email = re.compile(r"@(?:test\.local|example\.(?:com|org|net)|users\.noreply\.github\.com|x\.y|a\.b)$|^noreply@|^git@"
                        r"|\.(?:png|jpe?g|gif|svg|webp)$", re.I)
fine_path = re.compile(r"^/(?:Users|home)/(?:you|me|user|username|name|runner|_)$")
found = 0
for k, p in pats.items():
    hits: dict = {}
    for f in files:
        try:
            s = open(f, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for m in p.finditer(s):
            v = m.group(0)
            if k == "email" and fine_email.search(v) or k == "home path" and fine_path.match(v):
                continue
            hits.setdefault(v, set()).add(f)
    print(f"== {k}: {len(hits)}")
    for v, fs in sorted(hits.items()):
        shown = v if k not in ("key-like", "token assign") else v[:14] + "..."
        print("   ", shown, "|", ", ".join(sorted(fs))[:140])
    found += len(hits)
privhits = {}
for f in files:
    try:
        low = open(f, encoding="utf-8").read().lower()
    except (OSError, UnicodeDecodeError):
        continue
    for w in priv:
        n = len(re.findall(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low))
        if n:
            privhits.setdefault(f, []).append((w[:2] + "*" * (len(w) - 2), n))
print(f"== private words of this install: {sum(len(v) for v in privhits.values())}")
for f, hs in sorted(privhits.items()):
    print("   ", f, hs[:12])
found += len(privhits)
sys.exit(1 if found else 0)
