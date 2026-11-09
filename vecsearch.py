"""agentchat: the search of the chat by meaning (the user, 2026-10-29).

Every message is turned into a vector by the lab's embedding server (BGE-M3, 1024 numbers, many languages: a question in Russian
finds an answer in English), the vectors live in two files beside the chat's data (vec_ids.json + vec.npy, float16), a query is
turned into a vector the same way and the nearest messages win (cosine). Words that stand in the text word for word get a bonus,
so a name or a number is found even when its meaning is vague. No embedding server: the page falls back to the word search.

The server: env LAGOMCHAT_EMBED_URL, else found in docker by the container named in LAGOMCHAT_EMBED_CONTAINER (llama.cpp --embedding).
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

# the environment's names are LAGOMCHAT_*; the older AGENTCHAT_* ones still work (the owner #2302)
for _k, _v in list(os.environ.items()):
    if _k.startswith("AGENTCHAT_"):
        os.environ.setdefault("LAGOMCHAT_" + _k[len("AGENTCHAT_"):], _v)

try:
    import numpy as np
except ImportError:                                  # no numpy: no search by meaning
    np = None

DIM = 1024
CONTAINER = os.environ.get("LAGOMCHAT_EMBED_CONTAINER", "")


class VecIndex:
    def __init__(self, data_dir: Path):
        self.ids_file, self.mat_file = data_dir / "vec_ids.json", data_dir / "vec.npy"
        self.ids: list[int] = []
        self.mat = None
        self._url = os.environ.get("LAGOMCHAT_EMBED_URL", "").rstrip("/")
        self.error = "" if np is not None else "numpy is not installed"
        if np is not None:
            try:
                self.ids = json.loads(self.ids_file.read_text())
                self.mat = np.load(self.mat_file)
                if self.mat.shape != (len(self.ids), DIM):
                    raise ValueError("shape")
            except (OSError, ValueError):
                self.ids, self.mat = [], np.zeros((0, DIM), dtype=np.float16)
        self.have = set(self.ids)

    # ---- the embedding server
    def url(self, refresh: bool = False) -> str:
        if self._url and not refresh and not os.environ.get("LAGOMCHAT_EMBED_URL"):
            return self._url
        if os.environ.get("LAGOMCHAT_EMBED_URL"):
            return self._url
        if not CONTAINER:                                 # no server named: no meaning search, the plain one stays
            return ""
        try:
            ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", CONTAINER],
                                capture_output=True, text=True, timeout=8).stdout.split()
            self._url = f"http://{ip[0]}:8080" if ip else ""
        except (OSError, subprocess.SubprocessError):
            self._url = ""
        return self._url

    def embed(self, texts: list[str]) -> list[list[float]]:
        for attempt in (0, 1):
            u = self.url(refresh=attempt == 1)
            if not u:
                continue
            try:
                req = urllib.request.Request(u + "/v1/embeddings", data=json.dumps({"input": texts, "model": "bge-m3"}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read())
                self.error = ""
                return [x["embedding"] for x in sorted(d["data"], key=lambda x: x["index"])]
            except (OSError, ValueError, KeyError) as x:
                self.error = f"embedding server: {str(x)[:120]}"
        return []

    # ---- the index
    def add(self, items: list[tuple[int, str]]) -> int:
        """embed (id, text) pairs not indexed yet and keep them; returns how many went in"""
        if np is None:
            return 0
        items = [(i, t) for i, t in items if i not in self.have]
        if not items:
            return 0
        vecs = self.embed([t[:1500] for _, t in items])
        if len(vecs) != len(items):
            return 0
        m = np.asarray(vecs, dtype=np.float32)
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
        self.mat = np.concatenate([self.mat, m.astype(np.float16)])
        self.ids += [i for i, _ in items]
        self.have.update(i for i, _ in items)
        np.save(self.mat_file, self.mat)
        self.ids_file.write_text(json.dumps(self.ids))
        return len(items)

    def drop(self, ids: set[int]) -> None:
        """take messages out of the index (the chat was cleaned)"""
        if np is None or self.mat is None or not (self.have & ids):
            return
        keep = [n for n, i in enumerate(self.ids) if i not in ids]
        self.mat = self.mat[keep]
        self.ids = [self.ids[n] for n in keep]
        self.have = set(self.ids)
        np.save(self.mat_file, self.mat)
        self.ids_file.write_text(json.dumps(self.ids))

    def query(self, text: str, min_id: int = 0, k: int = 30, floor: float = 0.48) -> list[tuple[int, float]]:
        """the ids nearest to the text in meaning (cosine >= floor), best first; only ids >= min_id"""
        if np is None or self.mat is None or not len(self.ids):
            return []
        v = self.embed([text[:600]])
        if not v:
            return []
        q = np.asarray(v[0], dtype=np.float32)
        q /= max(float(np.linalg.norm(q)), 1e-9)
        sims = self.mat.astype(np.float32) @ q
        out = [(self.ids[i], float(sims[i])) for i in np.argsort(-sims)[: k * 3] if sims[i] >= floor and self.ids[i] >= min_id]
        return out[:k]
