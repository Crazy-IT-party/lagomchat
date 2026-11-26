"""Speech to text of our own (the owner, #3847): Whisper on a GPU box of the LAN, speaking the protocol of ElevenLabs' realtime
Scribe, so the chat's pages need no other code for it - the chat's server opens the line here and passes it through.

  ws  /v1/speech-to-text/realtime?audio_format=pcm_16000&language_code=ru&vad_silence_threshold_secs=1.5
      in:  {"message_type": "input_audio_chunk", "audio_base_64": <16-bit mono PCM>, "sample_rate": 16000, "commit": false}
      out: {"message_type": "session_started"}, {"message_type": "partial_transcript", "text"},
           {"message_type": "committed_transcript", "text"}
  GET /health -> {"ok": true, "model": ...}

A phrase is what is said between pauses (the loudness of the sound against its quiet, 30 ms frames); while it is being said it
is written down again every ~0.8 s (partial), at the pause once more, whole (committed). One GPU job at a time: a commit goes
before partials, and a partial is skipped while the GPU is busy.

Run: STT_TOKEN_FILE=~/agentchat-stt/token python3 stt_server.py --port 8001 [--model openai/whisper-large-v3-turbo]
The token file holds the secret the chat's server sends as "Authorization: Bearer ..."; --no-auth: no check (a test)."""
import argparse, asyncio, base64, hmac, json, math, os, re, time
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType, web

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="0.0.0.0")
ap.add_argument("--port", type=int, default=8001)
ap.add_argument("--model", default="openai/whisper-large-v3-turbo")
ap.add_argument("--device", default="cuda")
ap.add_argument("--no-auth", action="store_true", help="no token asked for (a test on this machine)")
ARGS = ap.parse_args()

TOKEN = Path(os.path.expanduser(os.environ["STT_TOKEN_FILE"])).read_text().strip() if os.environ.get("STT_TOKEN_FILE") else ""
if not TOKEN and not ARGS.no_auth:
    raise SystemExit("no token: set STT_TOKEN_FILE to a file with the secret (or --no-auth for a test)")
MAX_LINES = 16                    # open at once, from every chat
MAX_WAITING = 3                   # phrases of one line waiting for the GPU: more and the line is too fast - it is closed

RATE = 16000
FRAME = 480                       # 30 ms
PRE = int(0.3 * RATE)             # kept before the first loud frame
MAX_PHRASE = 25 * RATE            # longer: committed as it stands (Whisper takes 30 s at most)
PARTIAL_EVERY = 0.8
LANGS = {"en", "de", "ru", "uk", "fr", "it", "pl", "pt", "zh", "es", "cs", "be", "sv"}
# what Whisper says to silence or noise (it learned it from subtitles): never a phrase of its own
HALLU = re.compile(r"^\W*(продолжение следует|субтитры|редактор субтитров|спасибо за просмотр|thank you( for watching)?|thanks for watching|"
                   r"you|bye|субтитры сделал|dimatorzok|untertitel|amara\.org|ох|ах)\W*$", re.I)

import torch                                                       # noqa: E402  (after the arguments: --help needs no GPU)
from transformers import WhisperForConditionalGeneration, WhisperProcessor   # noqa: E402

DT = torch.float16 if ARGS.device.startswith("cuda") else torch.float32
PROC = WhisperProcessor.from_pretrained(ARGS.model)
MODEL = WhisperForConditionalGeneration.from_pretrained(ARGS.model, torch_dtype=DT, attn_implementation="sdpa").to(ARGS.device).eval()
GPU = asyncio.Lock()
STATS = {"jobs": 0, "secs": 0.0, "conns": 0}


def _run(audio: np.ndarray, lang: str) -> str:
    feats = PROC(audio, sampling_rate=RATE, return_tensors="pt").input_features.to(ARGS.device, DT)
    kw = {"task": "transcribe", "max_new_tokens": 220}
    if lang:
        kw["language"] = lang
    with torch.inference_mode():
        ids = MODEL.generate(feats, **kw)
    return PROC.batch_decode(ids, skip_special_tokens=True)[0].strip()


async def transcribe(audio: np.ndarray, lang: str, wait: bool) -> str | None:
    """None: the GPU was busy and this was a partial (not worth waiting for)"""
    if not wait and GPU.locked():
        return None
    async with GPU:
        t = time.time()
        out = await asyncio.get_running_loop().run_in_executor(None, _run, audio, lang)
        STATS["jobs"] += 1
        STATS["secs"] += time.time() - t
    out = re.sub(r"\s+", " ", out).strip()
    return "" if HALLU.match(out) else out


def resample(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE or not len(x):
        return x
    n = int(round(len(x) * RATE / rate))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


class Line:
    """one line of a page: its sound, the phrase being said, when it was loud"""

    def __init__(self, ws, lang: str, rate: int, silence: float):
        self.ws, self.lang, self.rate, self.silence = ws, lang, rate, silence
        self.pre = np.zeros(0, np.float32)           # the quiet before a phrase (a little of it starts the phrase)
        self.buf: list[np.ndarray] = []              # the phrase
        self.n = 0
        self.rest = np.zeros(0, np.float32)          # what did not fill a frame yet
        self.floor = -60.0                           # the quiet's loudness, dBFS (follows the room)
        self.loud_n = 0                              # loud frames in the phrase
        self.quiet = 0.0                             # seconds of quiet since the last loud frame
        self.last_partial = 0.0
        self.partial_job = None
        self.commits = asyncio.Lock()

    def feed(self, x: np.ndarray) -> bool:
        """the sound in; True when a phrase has just ended (a pause long enough). What comes after that end waits in `rest`:
        the caller takes the phrase and calls again with nothing new until it says False"""
        x = np.concatenate([self.rest, x])
        k = len(x) // FRAME
        self.rest = x[k * FRAME:]
        ended = False
        for i in range(k):
            f = x[i * FRAME:(i + 1) * FRAME]
            db = 10 * math.log10(float(np.mean(f * f)) + 1e-10)
            loud = db > max(self.floor + 12, -50)
            if not loud:                             # the floor: slowly up, at once down
                self.floor = db if db < self.floor else self.floor + 0.02 * (db - self.floor)
            if self.n or loud:
                if not self.n:
                    self.buf.append(self.pre[-PRE:].copy()); self.n += len(self.buf[-1])
                self.buf.append(f); self.n += FRAME
                if loud:
                    self.loud_n += 1; self.quiet = 0.0
                else:
                    self.quiet += FRAME / RATE
                if self.quiet >= self.silence or self.n >= MAX_PHRASE:
                    ended = True
                    break
            else:
                self.pre = np.concatenate([self.pre[-PRE:], f])
        if ended:                                    # the rest of the chunk waits for the next phrase
            self.rest = np.concatenate([x[(i + 1) * FRAME:k * FRAME], self.rest])
        return ended

    def take(self) -> np.ndarray | None:
        """the phrase, if there was one worth writing down (a quarter of a second loud), and a fresh start"""
        a = np.concatenate(self.buf) if self.buf else None
        enough = self.loud_n * FRAME >= 0.25 * RATE
        tail = int(max(0.0, self.quiet - 0.3) * RATE)  # most of the closing silence off
        self.buf, self.n, self.loud_n, self.quiet = [], 0, 0, 0.0
        self.pre = np.zeros(0, np.float32)
        if a is None or not enough:
            return None
        return a[:len(a) - tail] if tail and tail < len(a) else a

    async def send(self, d: dict) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(d, ensure_ascii=False))

    async def commit(self, a: np.ndarray) -> None:
        async with self.commits:                     # in the order they were said
            text = await transcribe(a, self.lang, True)
            if text:
                await self.send({"message_type": "committed_transcript", "text": text})

    async def partial(self) -> None:
        if not self.buf:                             # committed before this began
            return
        a = np.concatenate(self.buf)
        text = await transcribe(a, self.lang, False)
        if text and self.n:                          # still the same phrase (not committed meanwhile)
            await self.send({"message_type": "partial_transcript", "text": text})


async def realtime(req: web.Request) -> web.WebSocketResponse:
    if TOKEN and not hmac.compare_digest(req.headers.get("Authorization", "").encode(), f"Bearer {TOKEN}".encode()):
        raise web.HTTPUnauthorized()
    if STATS["conns"] >= MAX_LINES:
        raise web.HTTPServiceUnavailable(text="busy")
    q = req.query
    m = re.fullmatch(r"pcm_(\d+)", q.get("audio_format", "pcm_16000"))
    rate = int(m.group(1)) if m and 8000 <= int(m.group(1)) <= 48000 else RATE
    lang = q.get("language_code", "") if q.get("language_code", "") in LANGS else ""
    try:
        silence = min(3.0, max(0.3, float(q.get("vad_silence_threshold_secs", "1.0"))))
    except ValueError:
        silence = 1.0
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4 << 20)
    await ws.prepare(req)
    ln = Line(ws, lang, rate, silence)
    STATS["conns"] += 1
    tasks: set = set()
    await ln.send({"message_type": "session_started", "session_id": f"local-{id(ln):x}", "config": {"language_code": lang}})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                if not isinstance(d, dict):
                    continue
                raw = base64.b64decode(d.get("audio_base_64") or "")
                r = int(d.get("sample_rate") or rate)
            except (ValueError, TypeError):
                continue
            x = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            ended = ln.feed(resample(x, r if 8000 <= r <= 48000 else rate))
            while ended or (d.get("commit") and ln.n):
                a = ln.take()                        # taken now: what comes next is the next phrase
                if a is not None:
                    if sum(1 for t in tasks if t is not ln.partial_job) >= MAX_WAITING:
                        await ws.close(message=b"too fast")
                        break
                    t = asyncio.create_task(ln.commit(a)); tasks.add(t); t.add_done_callback(tasks.discard)
                ended = ended and ln.feed(np.zeros(0, np.float32))   # the rest of the chunk: maybe one more phrase
                d = {}
            if ws.closed:
                break
            if ln.n and ln.loud_n and time.time() - ln.last_partial >= PARTIAL_EVERY and not (ln.partial_job and not ln.partial_job.done()):
                ln.last_partial = time.time()
                ln.partial_job = asyncio.create_task(ln.partial()); tasks.add(ln.partial_job); ln.partial_job.add_done_callback(tasks.discard)
    finally:
        STATS["conns"] -= 1
        for t in list(tasks):
            if t is not ln.partial_job:
                await asyncio.gather(t, return_exceptions=True)
    return ws


async def health(_req: web.Request) -> web.Response:
    j = STATS["jobs"]
    return web.json_response({"ok": True, "model": ARGS.model, "jobs": j, "avg_s": round(STATS["secs"] / j, 3) if j else 0, "lines": STATS["conns"]})


async def warm(_app) -> None:
    await transcribe(np.zeros(RATE, np.float32), "en", True)       # the first job builds the GPU kernels: not on a person's phrase


app = web.Application()
app.router.add_get("/v1/speech-to-text/realtime", realtime)
app.router.add_get("/health", health)
app.on_startup.append(warm)
web.run_app(app, host=ARGS.host, port=ARGS.port, print=lambda *_: print(f"[stt] {ARGS.model} on {ARGS.device}, port {ARGS.port}", flush=True))
