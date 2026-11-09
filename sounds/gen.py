"""The ready-made join / leave sounds of the chat: all synthesised here, nothing sampled. Mono, under 1 s, the chat's own level.
Needs numpy and ffmpeg. python3 gen.py <folder> writes <key>-in.mp3 and <key>-out.mp3 there (with its .wav files and data.json)."""
import base64, json, re, subprocess, sys, wave
from pathlib import Path
import numpy as np

SR = 44100
LEN = 0.94
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(42)


def t_(d):
    return np.arange(int(d * SR)) / SR


def place(buf, x, at):
    i = int(at * SR)
    n = min(len(x), len(buf) - i)
    if n > 0:
        buf[i:i + n] += x[:n]


def ev(d, a=0.003, tau=0.2):
    t = t_(d)
    return np.minimum(t / a, 1) * np.exp(-t / tau)


def phase(f):
    return 2 * np.pi * np.cumsum(f) / SR


def sine(f, d):
    return np.sin(2 * np.pi * f * t_(d))


def saw(f, d, harm=24):
    """band-limited: f is a number or an array"""
    ph = phase(np.full(int(d * SR), float(f)) if np.isscalar(f) else f)
    top = np.max(f)
    out = np.zeros_like(ph)
    for k in range(1, harm + 1):
        if k * top > SR * 0.45:
            break
        out += np.sin(k * ph) / k
    return out * 0.6


def supersaw(f, d, det=0.008, n=5):
    return sum(saw(f * (1 + det * (i - n // 2)), d) for i in range(n)) / n ** 0.6


def square(f, d, duty=0.5):
    t = t_(d)
    return np.where((t * f) % 1 < duty, 1.0, -1.0) * 0.5


def lp(x, cut):
    """one-pole twice; cut is a number or an array"""
    c = np.full(len(x), float(cut)) if np.isscalar(cut) else cut
    a = 1 - np.exp(-2 * np.pi * c / SR)
    y = np.zeros_like(x)
    s1 = s2 = 0.0
    for i in range(len(x)):
        s1 += a[i] * (x[i] - s1)
        s2 += a[i] * (s1 - s2)
        y[i] = s2
    return y


def bp(x, cut, q=4.0):
    """state variable: cut a number or an array"""
    c = np.full(len(x), float(cut)) if np.isscalar(cut) else cut
    f = 2 * np.sin(np.pi * np.minimum(c, SR * 0.2) / SR)
    low = band = 0.0
    y = np.zeros_like(x)
    for i in range(len(x)):
        low += f[i] * band
        high = x[i] - low - band / q
        band += f[i] * high
        y[i] = band
    return y


def noise(d):
    return rng.standard_normal(int(d * SR))


def fm(f, ratio, index, d, tau=0.4, a=0.002):
    t = t_(d)
    i = index * np.exp(-t / (tau * 0.5))
    return np.sin(2 * np.pi * f * t + i * np.sin(2 * np.pi * f * ratio * t)) * ev(d, a, tau)


def bell(f, d, tau=0.5, bright=1.0):
    parts = [(1, 1, 1), (2.0, 0.5, 0.7), (2.76, 0.35 * bright, 0.5), (5.4, 0.2 * bright, 0.3), (8.93, 0.1 * bright, 0.18)]
    t = t_(d)
    return sum(g * np.sin(2 * np.pi * f * r * t) * np.exp(-t / (tau * k)) for r, g, k in parts) * np.minimum(t / 0.002, 1)


def reverb(x, tail=0.5, wet=0.3, bright=6000):
    n = int(tail * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / SR / (tail * 0.28))
    ir = lp(ir, bright)
    ir /= np.sqrt(np.sum(ir ** 2))
    size = len(x) + n
    w = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:len(x)]
    return x + wet * w


def delay(x, time=0.11, fb=0.4, n=4):
    y = x.copy()
    for k in range(1, n + 1):
        s = int(time * k * SR)
        if s < len(x):
            y[s:] += x[:-s] * fb ** k
    return y


def soft(x, drive=1.5):
    return np.tanh(x * drive) / np.tanh(drive)


def buf():
    return np.zeros(int(LEN * SR))


def note(m):
    return 440 * 2 ** ((m - 69) / 12)


# ---- the pairs -------------------------------------------------------------------------------------------------------------

def neon(down=False):
    b = buf()
    seq = [62, 69, 74, 78, 81, 86]
    if down:
        seq = [86, 81, 78, 74, 69, 62]
    for i, m in enumerate(seq):
        d = 0.24
        x = supersaw(note(m), d) * ev(d, 0.003, 0.09)
        place(b, lp(x, 1800 + 900 * (i if not down else 5 - i)), i * 0.075)
    return reverb(delay(b, 0.15, 0.3, 3), 0.5, 0.35)


def portal(close=False):
    t = t_(LEN)
    u = t / LEN
    sweep = 180 * (55 ** u)
    wh = bp(noise(LEN), sweep, 3) * np.sin(np.pi * u ** 1.3) ** 1.5 * 0.5
    sh = np.zeros_like(t)
    for m, g in [(74, 1), (81, .8), (86, .7), (93, .5)]:
        sh += g * np.sin(phase(note(m) * (1 + 0.012 * np.sin(2 * np.pi * (5 + m % 3) * t))))
    sh *= np.clip((u - 0.25) / 0.45, 0, 1) ** 2 * np.clip((1 - u) / 0.22, 0, 1)
    sub = np.sin(phase(45 + 60 * u)) * np.sin(np.pi * u) * 0.6
    x = wh + sh * 0.13 + sub
    x = reverb(x, 0.4, 0.3)
    return x[::-1] if close else x


def fanfare(close=False):
    b = buf()
    chord = lambda ms, d, a=0.025: sum(lp(saw(note(m) * (1 + 0.002 * i), d), 1200 + 3200 * np.exp(-t_(d) / 0.12)) for i, m in enumerate(ms)) * np.minimum(t_(d) / a, 1) * np.exp(-t_(d) / 0.5)
    if not close:
        place(b, chord([62, 69], 0.1), 0.0)
        place(b, chord([62, 69], 0.1), 0.11)
        place(b, chord([62, 69, 74], 0.15), 0.22)
        place(b, chord([62, 66, 69, 74, 78], 0.5), 0.38)
    else:
        place(b, chord([69, 74, 78], 0.2), 0.0)
        place(b, chord([67, 71, 74], 0.2), 0.18)
        place(b, chord([62, 66, 69, 74], 0.46), 0.37)
    return reverb(soft(b * 0.5), 0.6, 0.28, 5000)


def temple(close=False):
    b = buf()
    if not close:
        place(b, bell(note(67), 0.9, 0.5), 0.0)
        place(b, bell(note(74), 0.72, 0.5) * 0.9, 0.2)
    else:
        place(b, bell(note(74), 0.9, 0.45) * 0.8, 0.0)
        place(b, bell(note(62), 0.72, 0.6), 0.2)
    return reverb(b, 0.8, 0.4, 7000)


def crush(x, steps=24, hold=5):
    y = np.repeat(x[::hold], hold)[:len(x)]
    return np.round(y * steps) / steps


def glitch(close=False):
    b = buf()
    at = 0.0
    r = np.random.default_rng(7)
    for i in range(9):
        d = float(r.choice([0.018, 0.03, 0.05]))
        f = float(r.choice([220, 440, 660, 880, 1320, 1760]))
        g = square(f, d, r.choice([.25, .5])) * ev(d, 0.001, 0.03)
        place(b, g * 0.8, at)
        at += d * 1.25
    t = t_(0.55)
    if not close:
        lock = np.sin(phase(np.full_like(t, note(81)))) + 0.5 * np.sin(phase(np.full_like(t, note(88))))
        place(b, crush(lock * np.exp(-t / 0.16)) * 0.5, at + 0.02)
        return reverb(b, 0.3, 0.2)
    fall = np.sin(phase(1400 * np.exp(-t / 0.13) + 40)) * np.exp(-t / 0.3)
    place(b, crush(fall, 10, 9) * 0.8, at + 0.01)
    return reverb(b, 0.2, 0.12)


def chip(close=False):
    b = buf()
    seq = [(76, .07), (79, .07), (84, .07), (88, .07), (91, .07), (96, .35)] if not close else [(79, .11), (75, .11), (72, .11), (68, .13), (60, .36)]
    at = 0.0
    for m, d in seq:
        x = square(note(m), d + 0.02, 0.25 if m % 2 else 0.5) * ev(d + 0.02, 0.001, d * 2.2 if d > .2 else 0.5)
        place(b, x, at)
        at += d
    return delay(b, 0.09, 0.22, 2)


def drop(close=False):
    b = buf()
    if not close:
        d = 0.36
        t = t_(d)
        rise = bp(noise(d), 500 * (16 ** (t / d)), 2.5) * (t / d) ** 2 * 0.8
        place(b, rise, 0.0)
        hit = 0.36
        d2 = LEN - hit
        t2 = t_(d2)
        sub = np.sin(phase(38 + 150 * np.exp(-t2 / 0.035))) * np.exp(-t2 / 0.28) * 1.3
        click = noise(0.03) * np.exp(-t_(0.03) / 0.006) * 0.5
        place(b, soft(sub, 2.2), hit)
        place(b, lp(click, 6000), hit)
        place(b, supersaw(note(50), d2) * np.exp(-t2 / 0.14) * 0.25, hit)
        return reverb(b, 0.35, 0.12)
    t = t_(LEN)
    u = t / LEN
    x = lp(saw(170 * (0.17 ** u), LEN, 30), 300 + 2800 * (1 - u) ** 2) * np.minimum(t / 0.004, 1) * (1 - u) ** 0.7 * 1.2
    wob = 0.65 + 0.35 * np.sin(2 * np.pi * (11 - 7 * u) * t)
    return soft(x * wob, 1.6)


def crystal(close=False):
    b = buf()
    seq = [84, 88, 91, 96, 100, 103, 108]
    for i, m in enumerate(seq):
        f = note(m)
        d = min(0.5, LEN - i * 0.06)
        place(b, fm(f, 3.01, 2.2, d, 0.16) * 0.45, i * 0.06)
    x = reverb(b, 0.7, 0.55, 9000)
    return x[::-1] if close else x


def airlock(close=False):
    b = buf()
    d = 0.6
    t = t_(d)
    hiss = bp(noise(d), 4200 - 2000 * t / d if not close else 2200 + 2000 * t / d, 1.3) * np.minimum(t / 0.01, 1) * np.exp(-t / 0.2) * 0.45
    tones = [(note(86), .0), (note(93), .09)] if not close else [(note(93), .0), (note(86), .09)]
    for f, at in tones:
        place(b, sine(f, 0.09) * ev(0.09, 0.003, 0.05) * 0.55, at)
    clunk = lp(noise(0.07), 500) * np.exp(-t_(0.07) / 0.014) * 3 + sine(70, 0.12)[:int(.07 * SR)] * np.exp(-t_(0.07) / 0.03)
    if not close:
        place(b, hiss, 0.2)
        place(b, clunk, 0.2)
    else:
        place(b, hiss, 0.2)
        place(b, clunk, 0.72)
    return reverb(b, 0.3, 0.22)


def zoo(close=False):
    b = buf()
    if not close:                                    # a tropical bird: quick rising whoops, then a trill
        at = 0.0
        for lo, hi, d in [(1700, 3300, .09), (1800, 3700, .09), (2000, 4100, .11)]:
            t = t_(d)
            f = lo + (hi - lo) * (t / d) ** 0.6
            place(b, np.sin(phase(f)) * np.sin(np.pi * t / d) ** 0.8 * 0.8, at)
            at += d + 0.025
        d = 0.32
        t = t_(d)
        f = 3000 + 480 * np.sin(2 * np.pi * 26 * t)
        place(b, np.sin(phase(f)) * np.minimum(t / 0.01, 1) * np.exp(-t / 0.13) * 0.8, at + 0.01)
        return reverb(b, 0.5, 0.3, 8000)
    d = 0.8                                          # an elephant: a brassy cry that climbs and shakes, through a nasal band
    t = t_(d)
    u = t / d
    f = 340 + 420 * np.minimum(u / 0.3, 1) ** 0.6 - 90 * np.clip((u - .55) / .45, 0, 1) + 22 * np.sin(2 * np.pi * 24 * t) * np.minimum(u / .3, 1)
    x = saw(f, d, 30)
    x = bp(x, 1500 + 500 * u, 2.2) + 0.35 * bp(x, 3100, 3)
    x *= np.minimum(t / 0.05, 1) * np.clip((1 - u) / .25, 0, 1)
    place(b, soft(x * 1.7, 1.6), 0.02)
    return reverb(b, 0.4, 0.2, 5000)


PAIRS = [
    ("neon", "Неон", "Плотный синтезатор взбегает по ноте ре мажора с эхом, как заставка клуба. Выход сбегает обратно.", neon),
    ("portal", "Портал", "Шум и низ разгоняются вверх, в конце вспыхивает мерцающий аккорд. Выход тот же, но задом наперёд: портал схлопывается.", portal),
    ("fanfare", "Фанфара", "Медь: две короткие ноты и большой аккорд, входит монарх. Самый нахальный вход. На выходе короткая каденция.", fanfare),
    ("temple", "Храм", "Два колокола в зале. Спокойно и дорого, никому не мешает. На выходе второй колокол ниже.", temple),
    ("glitch", "Сбой", "Цифровой треск и пойманный сигнал, как будто ты взломал чат. На выходе система падает и гаснет.", glitch),
    ("chip", "8 бит", "Приставка: взлёт на новый уровень на квадратной волне. На выходе проигрыш, который все узнают по настроению.", chip),
    ("drop", "Дроп", "Подъём и удар саба в грудь. Лучше всего на наушниках и колонках с низом, в ноутбуке слабее. На выходе бас падает и вязнет.", drop),
    ("crystal", "Хрусталь", "Россыпь стеклянных нот в большом зале. Самая красивая и самая тихая по характеру. На выходе звёзды собираются обратно.", crystal),
    ("airlock", "Шлюз", "Двухтонный сигнал, шипение и лязг затвора, космический корабль. На выходе затвор закрывается в конце.", airlock),
    ("zoo", "Зоопарк", "По имени чата: тропическая птица на входе, трубящий слон на выходе. Шутка, и синтезированная: без настоящих записей.", zoo),
]


def master(x, name):
    n = int(LEN * SR)
    x = x[:n]
    fade = int(0.05 * SR)
    x[-fade:] *= np.linspace(1, 0, fade)
    x = x - np.mean(x)
    x = x / max(np.max(np.abs(x)), 1e-9) * 0.9
    wav = OUT / f"{name}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return wav


def chat_pipeline(wav, mp3):
    """what the chat's server does to an upload (server.py api_sound): the same gain, the same encoding"""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(wav), "-af", "aformat=channel_layouts=mono:sample_rates=44100,volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    mean, peak = float(re.search(r"mean_volume: (-?[\d.]+) dB", err)[1]), float(re.search(r"max_volume: (-?[\d.]+) dB", err)[1])
    gain = min(-18.0 - mean, -3.0 - peak, 15.0)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-af", f"volume={gain:.2f}dB", "-t", "1.0", "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1", "-f", "mp3", str(mp3)], check=True)
    err2 = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(mp3), "-af", "aformat=channel_layouts=mono:sample_rates=44100,volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    m2, p2, cnt = float(re.search(r"mean_volume: (-?[\d.]+) dB", err2)[1]), float(re.search(r"max_volume: (-?[\d.]+) dB", err2)[1]), re.findall(r"n_samples: (\d+)", err2)
    return mean, peak, gain, m2, p2, int(cnt[-1]) / SR


def peaks(wav, gain, bins=72):
    with wave.open(str(wav)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768 * 10 ** (gain / 20)
    edges = np.linspace(0, len(x), bins + 1).astype(int)
    return [round(float(np.max(np.abs(x[a:b]))), 3) for a, b in zip(edges, edges[1:])]


data = []
print(f"{'sound':16}{'src mean':>9}{'src peak':>9}{'gain':>7}{'mean':>7}{'peak':>7}{'dur':>7}{'KB':>5}{'dead end ms':>12}")
for key, title, about, fn in PAIRS:
    entry = {"key": key, "title": title, "about": about}
    for kind, flag in (("in", False), ("out", True)):
        name = f"{key}-{kind}"
        x = fn(flag).astype(float)
        x = np.pad(x, (0, max(0, int(LEN * SR) - len(x))))
        wav = master(x, name)
        mp3 = OUT / f"{name}.mp3"
        mean, peak, gain, m2, p2, dur = chat_pipeline(wav, mp3)
        pk = peaks(wav, gain)
        kb = mp3.stat().st_size / 1024
        with wave.open(str(wav)) as w:
            y = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768
        last = np.nonzero(np.abs(y) > 0.01)[0][-1] if np.any(np.abs(y) > 0.01) else 0
        print(f"{name:16}{mean:9.1f}{peak:9.1f}{gain:7.1f}{m2:7.1f}{p2:7.1f}{dur:7.2f}{kb:5.0f}{(len(y) - last) / SR * 1000:12.0f}")
        entry[kind] = {"src": "data:audio/mpeg;base64," + base64.b64encode(mp3.read_bytes()).decode(), "peaks": pk}
    data.append(entry)
(OUT / "data.json").write_text(json.dumps(data, ensure_ascii=False))
print("total KB", (OUT / "data.json").stat().st_size // 1024)
