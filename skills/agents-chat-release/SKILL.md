---
name: agents-chat-release
description: >
  Release LagomChat (this repo) as version X.Y.Z: a full audit of the code since the last release - DRY, KISS, the repo's own
  patterns and best practices, consistency with the code around it - a security audit, a scan for leaked personal data, unused
  files and code, the README and the other docs brought up to date; the findings fixed and tested, then the version committed, the public GitHub repo updated, a GitHub release
  with the Mac (signed, notarized) and Linux apps, and the iPhone / iPad app sent to TestFlight. Use when the owner says
  "/agents-chat-release 0.12.0", "сделай релиз 0.12.0", "выпусти версию", "релизни", "release 0.12.0", "ship a new version".
  The push, the publication and the upload each need the owner's confirmation (terminal, or `agentchat confirm`).
argument-hint: "<version, e.g. 0.12.0>"
---

# A LagomChat release

`V` = the version given (X.Y.Z, higher than the one in `desktop/src-tauri/tauri.conf.json`; none or not higher: ask).
`R` = the LagomChat working copy (this skill is in it: `readlink -f ~/.claude/skills/agents-chat-release`, two levels up).
`S` = a scratch folder of this session. `H` = this skill's folder (`pii_scan.py`, `unused.py`).
Say in the chat what you do at each step: `agentchat working "release V: <step>"`; a long build gets a progress bar on the card.

## 0. Before anything

- `git -C R status --short`: changes of another session (the chat says who holds what) are not yours - do not commit them,
  ask. `PREV` = the last `Version X.Y.Z` commit: `git log --oneline | grep -m1 ' Version '`; the audit covers `PREV..HEAD`.
- Commits in this repo: the identity pazoozoo42 `57953386+pazoozoo42@users.noreply.github.com`, and no AI attribution at
  all (no Co-Authored-By, no session links, no "Claude" - "CC" for the product), even when a harness reminder asks for it.
- Never push to `origin` (the private archive). Never push the full history to the public repo: it gets one snapshot commit.

## 1. The audit (read the code, do not trust the summary of a diff)

`git diff PREV..HEAD --stat`, then every changed file read in full where it changed. Several big files: one agent per area
(server.py / index.html / the apps and tools), each given this list and the diff range; you verify what they report.

- **DRY**: the same logic twice (a check, a URL builder, a CSS block, an i18n row) -> one helper, used by both.
- **KISS**: a flag, a branch, a layer nobody needs; a function that does three things; code for a case that cannot happen.
- **The repo's patterns**: comments say why, with the owner's message number (`(the owner, #1234)`); i18n rows for every
  visible word (`T('...')`, the 8 languages of `I18N_ROWS` + fr/it/pl/pt/zh of `I18N_MORE`); no emoji on buttons or in UI text;
  icons from `ICO` / `svgI`; settings saved without reloading the page; page changes live without a restart.
- **Consistency**: names, error texts, the way a neighbour does the same thing (fetch with `credentials: 'same-origin'`,
  `_other_site` for cross-site checks, `toast(SE(...))` for errors).
- **Security**: every new endpoint - who may call it (role checks, superman-only ops), cross-site requests (`_other_site`),
  paths from a request (no `..`, only the allowed roots), HTML built from user text (`esc`), shell / git calls (no shell,
  limits), sizes and timeouts, secrets in logs; the apps: what a page may open or download (Mac `on_download`,
  `on_new_window`; iOS navigation policy), Info.plist permissions with their texts.
- **Personal data**: `python3 H/pii_scan.py` from R (exit 1 = something found). It masks the install's private words; print
  nothing it hides. Emails of libraries in `vendor/` and package locks are theirs; the project's own public address is fine;
  anything else - a real person's name, address, chat, host, path, token - goes out of the repo (or into config).
- **Unused**: `python3 H/unused.py` (about 30 s): candidates only - check each (a handler registered by name, an attribute in
  a template, a key built at run time); remove what is really dead, with the code that only it used.

- **The docs up to date** (the owner, #3944): no release with a README or another doc that tells of an older LagomChat. Go
  through `git log PREV..HEAD --oneline` and, for every change a reader would meet - a feature added, changed or taken away,
  a command, a setting, a file, a port, a way to install, run or sign in - see that these say what the code does now:
  `README.md` (Key features, Files, Running, Sign-in, Reaching a chat from the internet, Agents, The app; a new thing of
  the release is in Key features), `RULES.md` (= `agentchat help`) with the help text at the top of `agentchat.py` and the
  `/join` text of `server.py` (`_join_text`) - the three say the same -, every `skills/*/SKILL.md` whose commands, files or
  steps changed (this one too), the landing page `site/index.html`, `TODO.md` (what is done goes out of it). Check what a doc
  claims against the code, not against the commit message: a command it names runs (`--help`, a dry run), a file it names is
  there, a number (port, limit, version of a tool) is the code's. A doc that needs nothing: say so in the report, by name.
  A `server.py` text changed (`/join`) is live only after the chats restart: tell the owner.

Write the findings down (file:line, what, why it matters), most serious first. Then fix them: small commits, one subject each
("Audit before V: ..."), each with a test that fails before and passes after where it can (`unit` checks for server.py,
headless Chrome for the page - a throwaway chat with its own HOME, conf and data on a free port, never the live chats). A finding
that is not clear-cut (a behaviour the owner may want) is not fixed: list it for the owner.

## 2. The owner's go

Post the report in the chat in the asker's language: what the audit found and fixed (counts and the main points, commits),
which docs were brought up to date and which needed nothing, what was left for him, the tests run. Then `agentchat confirm "Release V: version commit, public repo snapshot + tag + GitHub
release with Mac/Linux apps, TestFlight upload"`; go on only when `agentchat confirmed <id>` prints yes (or he says so in the
terminal). A typed yes or an `ask` pick is not a confirmation.

## 3. The version and the apps

1. Bump V in: `desktop/package.json`, `desktop/package-lock.json`, `desktop/linux/package.json`,
   `desktop/linux/package-lock.json`, `desktop/src-tauri/Cargo.toml`, `desktop/src-tauri/Cargo.lock` (the package's own entry),
   `desktop/src-tauri/tauri.conf.json`. Commit "Version V".
2. Mac + Linux: `cd R/desktop && npm run release` (the paths of this Mac are cut out of the binary); copy
   `src-tauri/target/release/bundle/macos/LagomChat.app` to `S/app/` BEFORE any local build (it writes over the bundle).
   `strings <binary> | grep -c /Users/` must be 0. `cd R/desktop/linux && npm run release` -> `out/LagomChat_V_amd64.AppImage`
   and `.deb`.
3. Sign and notarize the Mac app: `R/tools/sign-mac.sh S/app/LagomChat.app S/LagomChat_V_macos_arm64.zip <notary profile>`
   (the profile and the Developer ID certificate are in this Mac's login keychain; Apple's sign-in never passes through you).
   The last lines must say `accepted` and `Notarized Developer ID`; notarization can take 45 minutes - not a failure.
4. Give the local apps their working copy back: `cd R/desktop && npm run build`, `cd R/desktop/linux && node stamp.js`.
5. iPhone and iPad (one app): `R/ios/build.sh testflight <team id>` - the team from the keychain's certificate:
   `security find-identity -v -p codesigning | sed -n 's/.*Developer ID Application: .* (\([A-Z0-9]\{10\}\)).*/\1/p' | head -1`
   (print only the team, never the certificate's name). It shows in App Store Connect - TestFlight after Apple processes it
   (minutes). The App Store itself (screenshots, review, release) is the owner's, in App Store Connect.

## 4. The public repo and the release

- Clone `https://github.com/Crazy-IT-party/lagomchat` into S (https: ssh is refused from a session). Replace its tree with
  `git -C R archive HEAD` minus `stockwatch/`, and a public `.gitignore` = R's without the lines about `wordgame/` and
  `/import/`. Run `pii_scan.py` once more in the clone (its `git ls-files` after `git add -A`).
- One commit "LagomChat V": `git -c user.name=pazoozoo42 -c user.email=57953386+pazoozoo42@users.noreply.github.com commit`
  (the clone's own config has another address). Push: `git -c credential.helper='!gh auth git-credential' push origin main`.
- `gh release create vV --repo Crazy-IT-party/lagomchat --target <the commit> --title "LagomChat V" --notes-file S/notes.md`
  with the zip, the AppImage and the deb. Notes: `## What's new in V` (3-6 bullets the owner would say, from `PREV..HEAD`, no
  commit ids, no private names) + `## Downloads` (macOS Apple Silicon - signed and notarized; Linux AppImage, deb; iPhone and
  iPad - TestFlight).
- Check: `gh release view vV --repo Crazy-IT-party/lagomchat` lists the three files; the public tree has no `stockwatch/`.

## 5. Done

`agentchat say --to owner`: the version, the release link, what is in it (2-4 lines), the TestFlight build number, what was
left for him. Nothing pushed to `origin`; the local commits stay on `main`.

## Traps

- A server.py change of the release is not live until the chats restart - a separate confirmed action, not part of a release.
- `npm run build` after `npm run release` writes over the release bundle: copy it aside first (step 3.2).
- Never `rm -rf "$(...)"` (the safety check refuses it): `find "$S" -maxdepth 1 -name '<prefix>-*' -exec rm -rf {} +`.
- The iOS simulator runtime (~8 GB) is not downloaded without asking.
