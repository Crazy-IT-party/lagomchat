"""agentchat: the owner's ratings in MongoDB (the user, 2026-10-29).

One document per rated message in the collection `ratings` of the database `agentchat`:

    _id        the message's id in the chat
    message    {id, ts, text, to, kind}                  the rated message itself
    agent      {name, task, program, model, host}        who wrote it: its name, session, type and model as it told in its hello
    current    {delta, rater: {id, name}, ts} | null     the vote now (+1 / -1), null when it was taken back
    history    [{delta, rater, ts}, ...]                 every change of the vote, oldest first (delta 0 = taken back)
    deleted    {by: {id, name}, ts}                      only when the owner deleted the message (the message stays here)
The collection `views`: one document per seen message, _id "<the chat's data folder>:<message id>", {chat, message, by: {<person's
id>: when they saw it}} - a person counts once, after the message stood on their screen for two seconds.

`rater` is who voted: the owner (id owner), other people by their id. The URI: env LAGOMCHAT_MONGO, else the first line of
~/.config/agentchat/mongo (it can hold a password: not in the repo). Without a URI or a server nothing breaks: a write
that fails waits in mongo_pending.jsonl beside the chat's data and goes in when the server answers again.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

# the environment's names are LAGOMCHAT_*; the older AGENTCHAT_* ones still work (the owner #2302)
for _k, _v in list(os.environ.items()):
    if _k.startswith("AGENTCHAT_"):
        os.environ.setdefault("LAGOMCHAT_" + _k[len("AGENTCHAT_"):], _v)

try:
    from pymongo import MongoClient
except ImportError:                                  # no pymongo: the ops wait in the pending file
    MongoClient = None

DB, COLLECTION = "agentchat", "ratings"


def mongo_uri() -> str:
    u = os.environ.get("LAGOMCHAT_MONGO", "").strip()
    if u:
        return u
    try:
        return (Path.home() / ".config/agentchat/mongo").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def conv(o):
    """the ts floats of an op become dates"""
    if isinstance(o, dict):
        return {k: (dt.datetime.fromtimestamp(v, dt.timezone.utc) if k == "ts" and isinstance(v, (int, float)) else conv(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [conv(x) for x in o]
    return o


class RatingsDB:
    def __init__(self, data_dir: Path):
        self.uri = mongo_uri()
        self.pending = data_dir / "mongo_pending.jsonl"
        self.col = None
        self.users = None
        self.error = "no uri" if not self.uri else ("pymongo is not installed" if MongoClient is None else "")
        self.last_flush = 0.0
        if self.uri and MongoClient is not None:
            try:
                db = MongoClient(self.uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)[DB]
                self.col, self.users = db[COLLECTION], db["users"]
            except Exception as x:                    # a bad uri
                self.error = str(x)[:200]

    def _apply(self, op: dict) -> None:
        """op: {id, set: {...}, insert: {...}, push: {...}} -> one upsert"""
        upd: dict = {}
        if op.get("set"):
            upd["$set"] = conv(op["set"])
        if op.get("insert"):
            upd["$setOnInsert"] = conv(op["insert"])
        if op.get("push"):
            upd["$push"] = {"history": conv(op["push"])}
        col = self.col.database[op["col"]] if op.get("col") else self.col      # the views of the messages: their own collection
        col.update_one({"_id": op["id"]}, upd, upsert=True)

    def submit(self, op: dict) -> bool:
        """write now (blocking: the server runs it in a thread); on a failure the op waits in the pending file"""
        if not self.uri:            # no database configured: the chat's own files are the history, they are replayed when it is
            return False
        if self.col is not None:
            try:
                self._apply(op)
                self.error = ""
                return True
            except Exception as x:
                self.error = str(x)[:200]
        with self.pending.open("a") as f:
            f.write(json.dumps(op, ensure_ascii=False) + "\n")
        return False

    def flush(self) -> int:
        """the waiting ops, in order; stops at the first failure"""
        if self.col is None or not self.pending.exists():
            return 0
        lines = [s for s in self.pending.read_text().splitlines() if s.strip()]
        done = 0
        for s in lines:
            try:
                self._apply(json.loads(s))
            except Exception as x:
                self.error = str(x)[:200]
                break
            done += 1
        if done == len(lines):
            self.error = ""
        rest = lines[done:]
        self.pending.write_text("".join(s + "\n" for s in rest)) if rest else self.pending.unlink(missing_ok=True)
        return done

    # ---- the people: agentchat.users, _id = the account id (e:<email hash>, g:<Google>), verified (true = may see and write in the chat; set by the owner by hand)
    def user_touch(self, sid: str, name: str, avatar: str) -> None:
        """a person signed in: made (verified false) or refreshed"""
        if self.users is None:
            return
        now = dt.datetime.now(dt.timezone.utc)
        try:
            self.users.update_one({"_id": sid}, {"$set": {"name": name, "avatar": avatar, "last_seen": now}, "$inc": {"logins": 1},
                                                 "$setOnInsert": {"verified": False, "first_seen": now}}, upsert=True)
        except Exception as x:
            self.error = str(x)[:200]

    def user_verified(self, sid: str) -> bool:
        if self.users is None:
            return False
        try:
            d = self.users.find_one({"_id": sid}, {"verified": 1})
            return bool(d and d.get("verified") is True)
        except Exception as x:
            self.error = str(x)[:200]
            return False

    def count(self) -> int:
        try:
            return self.col.count_documents({}) if self.col is not None else -1
        except Exception as x:
            self.error = str(x)[:200]
            return -1
