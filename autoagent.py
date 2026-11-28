#!/usr/bin/env python3
"""LagomChat: the chat's own agent (the owner, #4001) - a headless Claude Code that the chat's server starts when a person
comes in and stops when everybody has left, so that no session sits listening in an empty chat.

    python3 autoagent.py --url ws://127.0.0.1:<port> --model <model> [--effort <level>] [--mode <permission mode>]
                         [--cwd <folder>] [--bin <claude>] [--session <uuid: its task code>]

The server (server.py, `own_sync`) runs it with what the chat's `agent` setting says (.lagomchat/config.json,
chats.<slug>.agent) and talks to it over its stdin: a line `leave` - nobody is here, go when the turn you are in is over;
`stay` - somebody is back; the end of the input - the server is gone, go at once.

One `claude -p` with a stream of turns in and out: the first turn has it join the chat as superman (the owner's deputy),
then this process holds the listener (`lagomchat listen`, under the session's task code) and gives what it prints to the
model as the next turn - what came during a turn waits for its end. Between the turns the model is not called: silence
costs nothing. It leaves with a bye. Exit code 0: it left as told; 3: the owner stopped it or sent it out from its card (the
server does not start it again until everybody has left); 1: it could not join or its model ended.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUIET = 0.4          # s without a new line from the listener: what it printed is one turn

NOTE = ("Everybody has left the chat: the server stops you after this turn, and starts a new superman, with no memory of "
        "this talk, when a person comes in. Something it must know (a task of the owner's still open, what was agreed, "
        "where things are): `lagomchat handoff \"<the note>\"` - it gets it in its hello. Nothing to pass on: no note. "
        "No message in the chat, no bye (it is said for you). End the turn.")


def log(s: str) -> None:
    print(f"[agent] {s}", flush=True)


def join_prompt(http: str, have_cli: bool) -> str:
    return (f"You are the own agent of a LagomChat chat: its server started you, headless, because a person came in, and "
            f"stops you when everybody has left. Join the chat now: read {http}/join and follow it, with these differences.\n"
            + ("- Step 1 is done: `lagomchat` is on your PATH, the one of this machine's chats.\n" if have_cli else "") +
            f"- Your name is superman (the owner's deputy): `lagomchat hello superman \"the chat's own agent: here while "
            f"people are\"`. The hello fails (another superman is here): say so in one line and stop, no other name.\n"
            f"- Step 4 (listen) is not yours: no Monitor, no `lagomchat listen`, `wait` or `inbox`. The chat's messages come to "
            f"you by themselves, as the next turns of this conversation, in the form the listener prints them.\n"
            f"- Read `lagomchat help` once, then end your turn: say nothing in the chat until somebody writes to you.\n"
            f"Each later turn is what was said in the chat: answer what is for you with `lagomchat say` (nobody reads what you "
            f"write outside the chat), do what the owner asks by the chat's rules, then end the turn - no waiting, no polling. "
            f"A turn with nothing for you: end it with one word.")


class Agent:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.sid = a.session or str(uuid.uuid4())
        self.q: queue.Queue = queue.Queue()
        self.listener: subprocess.Popen | None = None
        # the model's own environment: not this process's Claude Code session, when a session started the server by hand
        self.env = {k: v for k, v in os.environ.items()
                    if k == "LAGOMCHAT_HOSTNAME" or not k.startswith(("CLAUDE", "LAGOMCHAT_", "AGENTCHAT_"))}
        self.env["LAGOMCHAT_URL"] = a.url
        self.env["PATH"] = os.pathsep.join([str(Path.home() / ".local/bin"), self.env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
        # the chat's command as the model has it (the server's own python may lack the websockets it needs)
        cli = shutil.which("lagomchat", path=self.env["PATH"])
        self.cli = [cli] if cli else [sys.executable, str(HERE / "agentchat.py")]
        exe = a.bin or shutil.which("claude", path=self.env["PATH"])
        if not exe:
            raise SystemExit("autoagent: no `claude` on the PATH (the chat's agent setting may name it: bin)")
        argv = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--model", a.model,
                "--name", "superman", "--session-id", self.sid, "--permission-mode", a.mode]
        if a.effort:
            argv += ["--effort", a.effort]
        self.claude = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=self.env, cwd=a.cwd or None)
        log(f"claude {self.claude.pid} model={a.model} effort={a.effort or '-'} mode={a.mode} session={self.sid[:8]}")
        threading.Thread(target=self.read_claude, daemon=True).start()
        threading.Thread(target=self.read_ctl, daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: self.q.put(("die", "stopped")))

    def read_claude(self) -> None:
        for s in self.claude.stdout:
            try:
                m = json.loads(s)
            except ValueError:
                continue
            if m.get("type") == "result":
                self.q.put(("result", m))
        self.q.put(("die", f"claude ended ({self.claude.wait()})"))

    def read_ctl(self) -> None:
        for s in sys.stdin:
            if s.strip() in ("leave", "stay"):
                self.q.put(("ctl", s.strip()))
        self.q.put(("die", "the server is gone"))

    def turn(self, text: str) -> None:
        try:
            self.claude.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
                                               ensure_ascii=False) + "\n")
            self.claude.stdin.flush()
        except OSError:                            # the model ended: its reader says so, the loop goes on to the end
            pass

    def chat_env(self) -> dict:
        """for the chat's commands run here: the session's task code and its process, as the model's own commands have them"""
        return {**self.env, "CLAUDE_CODE_SESSION_ID": self.sid, "CLAUDE_PID": str(self.claude.pid)}

    def listen(self) -> None:
        p = self.listener = subprocess.Popen([*self.cli, "listen"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=self.chat_env())

        def read() -> None:
            for s in p.stdout:
                self.q.put(("line", s.rstrip("\n")))
            self.q.put(("listener", p.wait()))
        threading.Thread(target=read, daemon=True).start()

    def run(self) -> int:
        self.turn(join_prompt(self.a.url.replace("ws", "http", 1), len(self.cli) == 1))
        busy, leave, lines, code, why = True, False, [], 0, "nobody is here"
        talked = False                             # a turn of the chat since the last note for the next superman
        while True:
            try:
                k, v = self.q.get(timeout=QUIET if lines and not busy else None)
            except queue.Empty:
                self.turn("\n".join(lines))
                busy, lines, talked = True, [], True
                continue
            if k == "line":
                lines.append(v)
            elif k == "ctl":
                leave = v == "leave"
            elif k == "result":
                busy = False
                log(f"turn over: {v.get('subtype')}, {v.get('num_turns')} steps, ${v.get('total_cost_usd') or 0:.3f} so far")
                if not self.listener:
                    self.listen()
            elif k == "listener" and v in (2, 3):
                # 3: the owner's stop / leave on its card. 2: not on the roster - the hello failed (another superman is here)
                code, why = (3, "stopped by the owner") if v == 3 else (1, "not on the roster: the hello failed")
                break
            elif k == "listener":                  # ended by something else: another one
                self.listen()
            elif k == "die":
                code, why = 1, v
                break
            if leave and not busy and not lines:
                if not talked:
                    break
                self.turn(NOTE)                    # (somebody back meanwhile: it stays, the note is kept all the same)
                busy, talked = True, False
        log(why)
        if self.listener and self.listener.poll() is None:
            self.listener.terminate()
        # off the roster with a line in the log; after the owner's "leave" or a hello that failed there is nobody to take off
        subprocess.run([*self.cli, "bye", "--no-handoff", why], env=self.chat_env(), capture_output=True, timeout=20)
        if self.claude.poll() is None:
            try:
                self.claude.stdin.close()
                self.claude.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.claude.terminate()
        return code


def main() -> None:
    ap = argparse.ArgumentParser(description="the chat's own headless agent: started and stopped by the chat's server")
    ap.add_argument("--url", required=True, help="the chat: ws://127.0.0.1:<port>")
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="")
    ap.add_argument("--mode", default="bypassPermissions", help="claude --permission-mode")
    ap.add_argument("--cwd", default="", help="the folder it works in")
    ap.add_argument("--bin", default="", help="the claude command, when it is not on the PATH")
    ap.add_argument("--session", default="", help="the session's id (a uuid): its task code in the chat")
    sys.exit(Agent(ap.parse_args()).run())


if __name__ == "__main__":
    main()
