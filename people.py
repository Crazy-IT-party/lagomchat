#!/usr/bin/env python3
"""agentchat: the people of the owner's chat page (MongoDB agentchat.users).

    people.py list                  everybody who signed in: name, account id, verified or not, when last seen
    people.py verify <account id>  let this person into the chat (verified = true)
    people.py block <account id>   take the access back (verified = false)

A person is made (verified false) at their first sign-in; the owner needs no record. A change counts within ~20 seconds.
"""
import os
import sys

from pymongo import MongoClient

uri = open(os.path.expanduser("~/.config/agentchat/mongo")).read().strip()
users = MongoClient(uri)["agentchat"]["users"]
cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
if cmd == "list":
    for u in users.find({}).sort("last_seen", -1):
        print(f'{"VERIFIED" if u.get("verified") is True else "pending ":<9} {u["_id"]}  {u.get("name", "")}  (logins {u.get("logins", 0)}, last {u.get("last_seen"):%Y-%m-%d %H:%M})' if u.get("last_seen") else f'{u["_id"]} {u}')
    if users.count_documents({}) == 0:
        print("nobody has signed in yet")
elif cmd in ("verify", "block") and len(sys.argv) > 2:
    r = users.update_one({"_id": sys.argv[2]}, {"$set": {"verified": cmd == "verify"}})
    print("done" if r.matched_count else "no such person (they must sign in once first)")
else:
    raise SystemExit(__doc__)
