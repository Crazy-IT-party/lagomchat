"""Candidates for dead code and files in the LagomChat repo, for a person (or an agent) to look at - not to delete blindly:

- tracked files that no other tracked file names (by their path or their bare name);
- Python functions and classes defined at the top level of a module and named nowhere else in the repo;
- JavaScript functions of index.html / manager.html / signin.html named only where they are defined;
- the i18n rows of index.html (`['English', ...]`) whose English text no T('...') or other line asks for;
- CSS ids (#name) of index.html's style that no element, query or template has.

Run from the repo's root: python3 <this skill>/unused.py. Names used by the outside world (aiohttp handlers that are
registered by name, onclick="..." attributes, keys built at run time) show up here too: check each one before removing it."""
import ast
import re
import subprocess
from pathlib import Path

files = [f for f in subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n") if f]
text = {}
for f in files:
    try:
        text[f] = Path(f).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
everything = "\n".join(text.values())


def count(word: str, src: str = everything) -> int:
    return len(re.findall(r"(?<![\w$])" + re.escape(word) + r"(?![\w$])", src))


# files: some are found by the tools themselves (a package's manifest, a licence) and are left out
own = re.compile(r"(^|/)(LICENSE|NOTICE|README\.md|SKILL\.md|package(-lock)?\.json|Cargo\.(toml|lock)|requirements\.txt|\.gitignore"
                 r"|build\.rs|tauri\.conf\.json|Info\.plist|Contents\.json|project\.yml)$|^sounds/|^desktop/src-tauri/(icons|capabilities)/")
print("== files no other file names")
for f in files:
    if own.search(f):
        continue
    name = Path(f).name
    others = "\n".join(v for k, v in text.items() if k != f)
    if f not in others and name not in others and Path(f).stem not in others:
        print("   ", f)

print("== top-level Python names used nowhere else")
for f in files:
    if not f.endswith(".py") or f not in text:
        continue
    try:
        tree = ast.parse(text[f])
    except SyntaxError:
        continue
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.name.startswith("__"):
            if count(n.name) <= 1:
                print(f"    {f}:{n.lineno} {n.name}")

print("== page functions named only where they are made")
for f in ("index.html", "manager.html", "signin.html"):
    src = text.get(f, "")
    for m in re.finditer(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:^|\n)\s*const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>", src):
        name = m.group(1) or m.group(2)
        if count(name, src) <= 1:
            print(f"    {f}:{src.count(chr(10), 0, m.start()) + 2} {name}")

src = text.get("index.html", "")
print("== i18n rows nobody asks for (index.html)")
for m in re.finditer(r"^\s*\['((?:[^'\\]|\\.)+)',\s*'", src, re.M):
    en = m.group(1)
    if src.count("'" + en + "'") + src.count('"' + en + '"') + src.count("`" + en + "`") <= 1 and "{" not in en:
        print(f"    index.html:{src.count(chr(10), 0, m.start()) + 1} {en[:80]}")

print("== CSS ids of index.html's style used by nothing else in it")
style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, re.S))
rest = src.replace(style, "")
for i in sorted(set(re.findall(r"#([A-Za-z][\w-]*)(?=[\s,.:{\[>)+~])", style))):
    if not re.search(r"""(?:id=["']|['"`#])""" + re.escape(i) + r"""(?![\w-])""", rest):
        print("    #" + i)
