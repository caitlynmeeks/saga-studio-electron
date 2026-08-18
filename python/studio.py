#!/usr/bin/env python
"""Saga Studio — turn a manuscript into an audiobook, one chunk at a time.

    ./.venv/bin/python studio.py          # http://127.0.0.1:5010

Why it is built this way
------------------------
Every chunk's audio is content-addressed: the filename is a hash of
(text, voice, exaggeration, cfg, temperature, repetition_penalty). Edit one
line and exactly one hash changes, so exactly one chunk needs re-rendering.
Nothing else is touched, and "is this stale?" is a file-existence check rather
than bookkeeping that can drift.

The model is loaded once and held warm — a cold load is ~10s, which would make
per-line iteration unusable.

Layout:
    studio/<project>/doc.json        chunks, params, notes
    studio/<project>/source.md       the import, never modified
    studio/audio/<hash>.wav          shared cache; identical text is free
    studio/clips/<name>.wav          music and sound effects, shared like voices
    studio/takes/<sha>.wav           performances driving voiced cards
    studio/<project>/out/*.mp3       assembled book

Cards come in four kinds. A card with no "type" is speech — the original kind,
rendered by the model and content-addressed as above. type "audio" places an
imported clip on the timeline (music, an effect), and type "silence" is a
timed rest. Neither of those is rendered or hashed: their audio either exists
in clips/ or is nothing at all.

type "voiced" is speech-to-speech: you perform the line yourself and Chatterbox
re-speaks your recording in a character's voice, keeping your timing and
delivery. It is rendered and hashed like speech, but its input is a wav rather
than prose — which is why "does this card have words in it?" (is_speech) and
"does this card render to a wav?" (is_renderable) are two questions now, and
why every c["text"] site must ask the first one.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
# Data lives OUTSIDE the repo by default: voice clips and manuscripts are
# private, and a tool should never assume it may publish its user's material.
# Point these anywhere with SAGA_DATA / SAGA_VOICES.
ROOT = Path(os.environ.get("SAGA_DATA") or (Path.home() / ".saga-studio")).expanduser()
AUDIO = ROOT / "audio"
# Clips are global like voices: the same intro music recurs across episodes,
# and cards reference a clip by name, never by path. Nothing in the app ever
# deletes a clip file — undo can resurrect a card that points at one.
CLIPS = ROOT / "clips"
# Performances driving voiced cards. Named by the sha256 of the wav itself, not
# by the file you imported: re-importing the same recording costs nothing, and
# a second take can never overwrite the first one the way a name-based stem
# would. Global like clips, and never deleted for the same reason.
TAKES = ROOT / "takes"
# plugin output, cached: see the plugins section for what goes in the name
FX = ROOT / "fx"
VOICES = Path(os.environ.get("SAGA_VOICES") or (HERE / "voices")).expanduser()
PORT = int(os.environ.get("PORT", "5010"))
# Two engines speak text here. Chatterbox is the original and the default, and
# it is the only one that can do a voiced card, because it is the only one with
# speech-to-speech. OmniVoice is roughly 3x faster and speaks 600-odd languages,
# which is the whole reason it is here — the Spanish editions.
#
# They cannot share a virtualenv: chatterbox pins transformers==5.2.0 and
# safetensors==0.5.3 as exact equalities, and OmniVoice needs transformers 5.3+.
# So OmniVoice runs in its own interpreter as a warm worker process and is
# spoken to over localhost. See omnivoice_server.py.
ENGINES = ("chatterbox", "omnivoice")
OV_PYTHON = Path(os.environ.get("SAGA_OV_PYTHON")
                 or (Path.home() / "git/voice-studio/.venv-omnivoice/bin/python")).expanduser()
OV_PORT = int(os.environ.get("SAGA_OV_PORT", "5021"))
# Localhost by default. SAGA_HOST=0.0.0.0 exposes it to the LAN — see the
# README: there is no login, and the discuss window shells out to Claude, so
# anyone who can reach the port can read the manuscript and spend tokens.
# SAGA_TOKEN adds a shared secret if the network is not fully trusted.
HOST = os.environ.get("SAGA_HOST", "127.0.0.1")
TOKEN = os.environ.get("SAGA_TOKEN", "")
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
OPEN_CMD = "open" if sys.platform == "darwin" else "xdg-open"

ROOT.mkdir(parents=True, exist_ok=True)
AUDIO.mkdir(parents=True, exist_ok=True)
CLIPS.mkdir(parents=True, exist_ok=True)
TAKES.mkdir(parents=True, exist_ok=True)
FX.mkdir(parents=True, exist_ok=True)

# What this process actually loaded. studio_ui.html is re-read from disk on
# every page request, so a plain reload picks up front-end changes and looks
# like it picked up everything — but the Python is whatever was on disk when
# the process started. A new front end talking to an old route fails in ways
# that look like bugs in the new code, and twice now that has cost an evening.
# So the program watches its own source and says when it is out of date.
_SRC = [Path(__file__), HERE / "omnivoice_server.py"]
BUILD_MTIME = max((p.stat().st_mtime for p in _SRC if p.exists()), default=0.0)


def build_stale():
    """Has the source changed since this process loaded it?"""
    try:
        now = max((p.stat().st_mtime for p in _SRC if p.exists()), default=0.0)
    except OSError:
        return False
    return now > BUILD_MTIME + 1        # a second of slack for copy timestamps


_model = None
_vc = None
_vc_voice = [None]                # which voice the VC model is currently holding
_lock = threading.Lock()          # MPS: one generate() at a time
_bake = {"running": False, "done": 0, "total": 0, "project": "", "label": "",
         "cancel": False, "stopped": False}
_docmut = threading.Lock()        # one doc.json read-modify-write at a time

DEFAULTS = {"voice": "caitlyn2", "exag": 0.4, "cfg": 0.35,
            "temp": 0.7, "rep": 1.2}

# What a single card may override for itself, on top of its profile. The four
# chatterbox dials, the two OmniVoice ones, and the engine — so one line can be
# spoken by the other model without a profile of its own. `params_for` merges
# these last, so a card always wins over the profile it names, and chunk_hash
# reads the merged result, so an override renames that card's wav and nothing
# else's.
CARD_PARAMS = {
    "engine": (None, None),          # validated against ENGINES, not clamped
    "exag": (0.0, 2.0), "cfg": (0.0, 1.0), "temp": (0.05, 2.0), "rep": (1.0, 2.0),
    "speed": (0.0, 3.0), "duration": (0.0, 300.0), "gain": (0.0, 200.0),
}

# ❦ deliberately survives normalisation here: it marks a scene break, and
# assemble() gives those a longer rest. render() strips it before speaking,
# so it is a silent stage mark rather than a spoken character.
NORMALISE = [("⁓", ", "), ("—", ", "), ("…", "... "),
             ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")]


# ── text ────────────────────────────────────────────────────────────────
def normalise(t):
    for a, b in NORMALISE:
        t = t.replace(a, b)
    return re.sub(r"[ \t]+", " ", t)


def split_chunks(text, cap=280):
    """Sentence-first, then clauses, then words. Chatterbox degrades past ~40s
    of audio per call, so no chunk may exceed the cap."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        cur = ""
        for s in re.split(r"(?<=[.!?…])\s+", para):
            if len(cur) + len(s) + 1 <= cap:
                cur = f"{cur} {s}".strip()
            else:
                if cur:
                    out.append(cur)
                cur = s
        if cur:
            out.append(cur)
    final = []
    for c in out:
        while len(c) > cap:
            cut = c.rfind(" ", 0, cap) or cap
            final.append(c[:cut].strip())
            c = c[cut:].strip()
        if c:
            final.append(c)
    return final


def strip_markdown(md):
    md = re.sub(r"^---\n.*?\n---\n", "", md, flags=re.S)      # frontmatter
    md = re.sub(r"^#{1,6}\s*", "", md, flags=re.M)            # headings
    md = re.sub(r"\*\*\*(.*?)\*\*\*", r"\1", md)
    md = re.sub(r"\*\*(.*?)\*\*", r"\1", md)
    md = re.sub(r"(?<!\w)\*(.*?)\*(?!\w)", r"\1", md)
    md = re.sub(r"^\s*---\s*$", "❦", md, flags=re.M)          # scene break
    md = re.sub(r"`([^`]*)`", r"\1", md)
    return md


# ── voice profiles ──────────────────────────────────────────────────────
# Global, not per-project: Maddy and Anna recur across episodes, so a profile
# built once should be usable everywhere. "Default" always exists and cannot
# be deleted — every card falls back to it.
PROFILES = ROOT / "profiles.json"
# `engine` defaults to chatterbox so that adding a second engine changes nothing
# until a profile is deliberately moved across — every wav already on disk keeps
# its name and stays valid. `lang` and `speed` mean nothing to chatterbox and are
# only read when the engine is omnivoice.
BASE_PROFILE = {"voices": ["caitlyn2"], "active": 0, "exag": 0.4, "cfg": 0.35,
                "temp": 0.7, "rep": 1.2, "note": "", "gain": 100, "fx": {},
                "engine": "chatterbox", "lang": "en", "speed": 0}


def profiles():
    if PROFILES.exists():
        p = json.loads(PROFILES.read_text())
    else:
        p = {}
    if "Default" not in p:
        p["Default"] = dict(BASE_PROFILE)
        PROFILES.write_text(json.dumps(p, indent=1))
    return p


def save_profiles(p):
    p.setdefault("Default", dict(BASE_PROFILE))
    PROFILES.write_text(json.dumps(p, indent=1))


def profile_params(name, profs=None):
    """`profs` overrides what is on disk. Import needs to ask what a chunk
    hashed to under the *archive's* profiles, which are not this machine's."""
    p = profiles() if profs is None else profs
    prof = p.get(name) or p.get("Default") or BASE_PROFILE
    voices = prof.get("voices") or ["caitlyn2"]
    idx = min(prof.get("active", 0), len(voices) - 1)
    eng = prof.get("engine", "chatterbox")
    return {"voice": voices[idx],
            "exag": prof.get("exag", 0.4), "cfg": prof.get("cfg", 0.35),
            "temp": prof.get("temp", 0.7), "rep": prof.get("rep", 1.2),
            "engine": eng if eng in ENGINES else "chatterbox",
            "lang": prof.get("lang", "en") or "en",
            "speed": prof.get("speed", 0) or 0,
            # Level is applied when the timeline is mixed, never when the card
            # is rendered — so it is not in any hash, and evening out a
            # character who reads louder than the rest costs nothing and
            # re-bakes nothing. Same place, and the same percentage, as an
            # audio card's volume.
            "gain": prof.get("gain", 100),
            "fx": prof.get("fx") or {}}


# How many previous settings a profile remembers. A profile is five numbers, so
# a stack of ten costs nothing — and it is the difference between "put it back"
# and working out what the number used to be from the hashes on disk.
PROFILE_HISTORY = 10


def profile_usage(name, proposed=None):
    """Who uses this profile, and what changing it would cost.

    A profile's numbers are part of every hash its cards render to, so moving
    one by 0.05 re-points every card that uses it at a wav that has never been
    made. That is not destructive — nothing is ever deleted, and the old
    renders sit on disk under their old names — but it is invisible, and a
    library can go from finished to four hundred amber dots without a word.
    So: say the number first.

    `proposed` is the profile as it would be after the edit. Because the cache
    is content-addressed and permanent, "how many would already be rendered at
    the new settings" is answerable before anything changes — and the answer is
    often "some", because you have been at those settings before. Which also
    makes the reverse true: going back restores exactly what going forward
    took away."""
    profs = profiles()
    after = None
    if proposed is not None:
        after = json.loads(json.dumps(profs))
        after[name] = proposed
    out = {"profile": name, "cards": 0, "ready": 0, "projects": []}
    lost = gained = ready_after = 0
    for meta in projects():
        if meta.get("broken"):
            continue
        doc = load(meta["name"])
        if not doc:
            continue
        n = r = ra = 0
        for c in doc["chunks"]:
            if not is_renderable(c) or c.get("profile", "Default") != name:
                continue
            n += 1
            a = (AUDIO / f"{chunk_hash(c, doc, profs)}.wav").exists()
            z = after is not None and (AUDIO / f"{chunk_hash(c, doc, after)}.wav").exists()
            r += a
            ra += z
            lost += a and not z
            gained += z and not a
        if not n:
            continue
        out["projects"].append({"name": doc["name"],
                                "title": doc.get("title", doc["name"]),
                                "cards": n, "ready": r, "ready_after": ra})
        out["cards"] += n
        out["ready"] += r
        ready_after += ra
    out["projects"].sort(key=lambda p: -p["cards"])
    if after is not None:
        out.update(ready_after=ready_after, lost=lost, gained=gained)
    return out


def library_counts():
    """How many cards each profile speaks for, and each clip appears in.

    One pass and no hashing: the sidebar only needs to say how much a thing is
    carrying, which is cheap next to asking what is rendered."""
    profs, clips = {}, {}
    for d in sorted(ROOT.iterdir()):
        f = d / "doc.json"
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for c in doc.get("chunks", []):
            if is_renderable(c):
                k = c.get("profile", "Default")
                profs[k] = profs.get(k, 0) + 1
            elif c.get("type") == "audio" and c.get("clip"):
                clips[c["clip"]] = clips.get(c["clip"], 0) + 1
    return profs, clips


def params_for(c, doc, profs=None):
    """DEFAULTS <- doc defaults <- the card's profile <- per-card override."""
    return {**DEFAULTS, **doc.get("params", {}),
            **profile_params(c.get("profile", "Default"), profs),
            **c.get("params", {})}


def chunk_hash(c, doc, profs=None):
    p = params_for(c, doc, profs)
    if c.get("type") == "voiced":
        # Only two things decide what a voiced card sounds like: the recording
        # and the voice it is being spoken in. Chatterbox VC takes no
        # exaggeration, cfg, temperature or repetition penalty — it has no such
        # knobs — so folding them in would invalidate every voiced card the
        # moment a profile slider moved, for a re-render that produced exactly
        # the same audio. The recording is already named by its own checksum,
        # so naming it here is naming its contents.
        # A voiced card is always chatterbox: it is speech-to-speech, and
        # OmniVoice does not do that. So the engine is not part of its key.
        key = ["voiced", c.get("perf") or "", p["voice"]]
    elif p["engine"] == "chatterbox":
        key = [c["text"], p["voice"], p["exag"], p["cfg"], p["temp"], p["rep"]]
    else:
        # Which engine spoke it has to be in the name, or the same words in the
        # same voice collide on one filename whichever model made them, and a
        # chapter quietly mixes the two. The default engine stays unmarked, so
        # every wav chatterbox has already rendered keeps the name it has.
        #
        # And only what OmniVoice actually reads goes in: it has no
        # exaggeration, cfg, temperature or repetition penalty, so folding
        # those in would make a slider that does nothing to this profile
        # invalidate every card it speaks for.
        ext = {"engine": p["engine"], "lang": p["lang"],
               "speed": float(p["speed"] or 0)}
        # duration is per-card only and rarely set, so it stays out of the key
        # unless it is actually asked for — same discipline as takes and the
        # engine itself, and for the same reason: nothing already rendered
        # should go stale because a new option was added to the program.
        if p.get("duration"):
            ext["duration"] = float(p["duration"])
        key = [c["text"], p["voice"], ext]
    # Take 0 hashes exactly as it did before takes existed, so nothing already
    # rendered goes stale — only a card you have actually re-rolled gets a new
    # name, and each take keeps its own file, so stepping back to take 2 plays
    # take 2 again instead of re-rendering it.
    if c.get("seed"):
        key.append({"take": int(c["seed"])})
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:20]


def is_speech(c):
    """Does this card have prose in it? Guards every c["text"] access."""
    return c.get("type", "speech") == "speech"


def is_renderable(c):
    """Does this card produce a wav the model has to make?

    Speech and voiced cards both do, and both are content-addressed into
    audio/. Audio and silence cards do not — their audio is a file you imported
    or nothing at all. This is a different question from is_speech(): a voiced
    card renders but has no text, so anything counting work to be done (bake,
    the progress bar, what an export must carry) asks this one, and anything
    touching words asks the other."""
    return c.get("type", "speech") in ("speech", "voiced")


def _num(v, dflt, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return dflt


def paste_card(src):
    """A fresh card built from a copied one.

    The clipboard lives in the browser — it survives a reload in localStorage
    and it crosses from one project to another — so what arrives here is
    untrusted input and gets the same whitelist-and-clamp treatment /api/chunk
    gives an edit. `id` and `note` describe where a card sits rather than what
    it is, and hash/ready/effective are derived, so all of them are dropped.

    Everything the hash is made of is kept, and audio lives in one pool keyed
    by that hash: paste a card that was already rendered and its wav is still
    on disk under the same name, so the copy arrives ready without speaking a
    word of it again. The same goes for a clip, which cards name rather than
    contain — which is what makes pasting the intro music into next week's
    episode a paste and not an import."""
    kind = src.get("type") or "speech"
    if kind == "audio":
        fade = list(src.get("fade") or [])[:2] + [0, 100]
        lo = _num(fade[0], 0.0, 0.0, 100.0)
        c = {"id": 0, "type": "audio",
             "clip": re.sub(r"[^a-z0-9_-]", "", str(src.get("clip") or "")),
             "mode": "after" if src.get("mode") == "after" else "full",
             "after": _num(src.get("after"), 5.0, 0.0, 3600.0),
             "fade": [lo, max(lo, _num(fade[1], 100.0, 0.0, 100.0))],
             "gain": _num(src.get("gain"), 100.0, 0.0, 200.0),
             "note": ""}
    elif kind == "silence":
        c = {"id": 0, "type": "silence",
             "secs": _num(src.get("secs"), 1.0, 0.0, 3600.0), "note": ""}
    elif kind == "voiced":
        # `perf` is a checksum this program wrote, so it is hex and nothing
        # else; `perfname` is only what to call it on screen.
        c = {"id": 0, "type": "voiced",
             "perf": re.sub(r"[^a-z0-9]", "", str(src.get("perf") or ""))[:40],
             "perfname": str(src.get("perfname") or "")[:80],
             "note": ""}
        if src.get("profile"):
            c["profile"] = str(src["profile"])[:80]
        seed = int(_num(src.get("seed"), 0, 0, 10 ** 6))
        if seed:
            c["seed"] = seed
    else:
        c = {"id": 0, "text": normalise(str(src.get("text") or "")),
             # CARD_PARAMS, not DEFAULTS: a card's per-card delivery and its
             # engine are part of what you copied, and dropping them would make
             # the pasted card sound different from the one you pointed at.
             "params": {k: v for k, v in (src.get("params") or {}).items()
                        if k in CARD_PARAMS and v is not None},
             "note": ""}
        if src.get("profile"):
            c["profile"] = str(src["profile"])[:80]
        seed = int(_num(src.get("seed"), 0, 0, 10 ** 6))
        if seed:
            c["seed"] = seed
        height = int(_num(src.get("height"), 0, 0, 4000))
        if height:
            c["height"] = height
    if src.get("mute"):
        c["mute"] = True
    if src.get("runon"):
        c["runon"] = True
    return c


# ── clips ───────────────────────────────────────────────────────────────
# Everything in clips/ is a PCM wav this program wrote via ffmpeg, so the
# stdlib wave module can read it — no torch import just to ask a duration.
def clip_secs(p):
    try:
        with wave.open(str(p), "rb") as w:
            fr = w.getframerate()
            return round(w.getnframes() / fr, 2) if fr else 0.0
    except (OSError, wave.Error):
        return 0.0


def clip_file(name):
    p = CLIPS / f"{name}.wav"
    if not p.exists():
        raise FileNotFoundError(f"no clip '{name}'")
    return p


def clips_of(doc):
    """The clip names a project's audio cards point at."""
    return {c["clip"] for c in doc["chunks"]
            if c.get("type") == "audio" and c.get("clip")}


# ── takes ───────────────────────────────────────────────────────────────
# A take is a performance you recorded, driving a voiced card. Like a clip it
# is always a PCM wav this program wrote via ffmpeg, so wave can read its
# length without importing torch — but it is named by its checksum rather than
# by you, so importing the same recording twice is free and importing a second
# one can never quietly replace the first.
def take_path(name):
    return TAKES / f"{name}.wav"


def take_file(name):
    p = take_path(name or "")
    if not name or not p.exists():
        raise FileNotFoundError(f"no performance '{name}' in takes/")
    return p


def takes_of(doc):
    """The performances a project's voiced cards point at."""
    return {c["perf"] for c in doc["chunks"]
            if c.get("type") == "voiced" and c.get("perf")}


# ── projects ────────────────────────────────────────────────────────────
def pdir(name):
    return ROOT / re.sub(r"[^a-z0-9_-]+", "-", name.lower())[:60]


def load(name):
    f = pdir(name) / "doc.json"
    return json.loads(f.read_text()) if f.exists() else None


UNDO_DEPTH = 25


def snapshot(doc, label):
    """Push the current cards onto the undo stack before mutating them.

    Text only, so a stack of 25 costs almost nothing — and it covers every
    destructive action (remove, split, merge, duplicate, edits), not just the
    one that prompted it."""
    stack = doc.setdefault("_undo", [])
    stack.append({"label": label,
                  "at": time.strftime("%H:%M:%S"),
                  "chunks": json.loads(json.dumps(doc["chunks"]))})
    del stack[:-UNDO_DEPTH]


def save(doc):
    """Write via a temp file and rename. os.replace is atomic, so a crash or a
    kill mid-write leaves the previous doc.json intact rather than a truncated
    one that takes the whole server down on next boot.

    The temp name must be unique per write. A shared "doc.json.tmp" is not
    safe just because the rename is: two threads opening it at once each get
    their own file offset, so the second one's truncate-on-open resets the
    length while the first keeps writing past it. The rename then publishes
    the hybrid — a complete document with the tail of another stuck on the
    end, which is exactly the "Extra data" JSONDecodeError that made a
    project unreadable. fsync before the rename so the bytes are on disk and
    not merely in the page cache when the directory entry flips."""
    d = pdir(doc["name"])
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".doc.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / "doc.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def projects():
    out = []
    profs = profiles()          # read once, not once per chunk of every project
    for d in sorted(ROOT.iterdir()):
        f = d / "doc.json"
        if f.exists():
            try:
                doc = json.loads(f.read_text())
            except json.JSONDecodeError:
                out.append({"name": d.name, "title": f"{d.name} (unreadable)",
                            "chunks": 0, "ready": 0, "words": 0, "broken": True})
                continue
            # the progress bar tracks what needs rendering — speech and voiced
            # both do; an audio or silence card is never stale. Words are a
            # separate count: a voiced card carries a recording, not prose.
            rend = [c for c in doc["chunks"] if is_renderable(c)]
            ready = sum(1 for c in rend
                        if (AUDIO / f"{chunk_hash(c, doc, profs)}.wav").exists())
            out.append({"name": doc["name"], "title": doc.get("title", doc["name"]),
                        "chunks": len(rend), "ready": ready,
                        # `created` is minute-resolution and a batch import
                        # gives twenty projects the same stamp, so what the
                        # sidebar sorts on is when the document last changed —
                        # which is the question "show me recent" is really asking
                        "created": doc.get("created", ""),
                        "edited": round(f.stat().st_mtime),
                        "words": sum(len(c["text"].split())
                                     for c in doc["chunks"] if is_speech(c))})
    return out


def import_md(title, md):
    text = normalise(strip_markdown(md))
    chunks = [{"id": i, "text": t, "params": {}, "note": ""}
              for i, t in enumerate(split_chunks(text))]
    doc = {"name": re.sub(r"[^a-z0-9_-]+", "-", title.lower())[:60],
           "title": title, "params": {}, "chunks": chunks,
           "created": time.strftime("%Y-%m-%d %H:%M")}
    d = pdir(doc["name"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.md").write_text(md, encoding="utf-8")
    save(doc)
    return doc


# ── export / import ─────────────────────────────────────────────────────
# One portable file per backup: a gzipped tar, because tar is already
# everywhere, streams, and survives being emailed.
#
#   manifest.json             schema, kind, created, projects, voice checksums
#   profiles.json             only the profiles those projects actually use
#   projects/<name>/doc.json  cards, params, notes
#   projects/<name>/source.md the untouched import
#   voices/<stem>.wav         only the clips those profiles can speak with
#   takes/<sha>.wav           the performances voiced cards are driven by
#   audio/<hash>.wav          rendered chunks — optional, and nearly all the size
#
# The manifest is written first so that reading "what is in here?" does not
# mean decompressing a few hundred megabytes of audio to reach the last member.
# The assembled mp3 in out/ is deliberately left out: it is derived, it is
# large, and assemble() rebuilds it in seconds from the chunks that are here.
ARCHIVE_SCHEMA = 1

# Extraction allowlist. tar members are attacker-controlled paths in the
# general case, so nothing is unpacked unless its name matches a shape this
# program writes — which rules out absolute paths, "..", symlinks and devices
# without relying on any particular Python version's tarfile filter.
ARC_MEMBER = re.compile(
    r"^(manifest\.json|profiles\.json"
    r"|projects/[a-z0-9_-]{1,60}/(doc\.json|source\.md)"
    r"|voices/[a-z0-9_.-]{1,44}\.(wav|mp3|flac|m4a)"
    r"|clips/[a-z0-9_.-]{1,44}\.wav"
    r"|takes/[a-z0-9]{1,40}\.wav"
    r"|audio/[a-z0-9]{1,40}\.wav)$")
_EXTRACT = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def voices_and_profiles(doc, profs):
    """What this project can speak with.

    Every profile its cards name, and every clip in those profiles — not only
    the active one. Switching between a profile's clips is an ordinary edit, so
    an export carrying just the active clip would break the first time you
    changed it on the far side. "Default" is always included: it is what a card
    falls back to when its profile has gone."""
    pnames, vnames = {"Default"}, set()
    for c in doc["chunks"]:
        # voiced cards name a profile too — the voice they are converted into
        # is exactly as necessary to the backup as the one speech is read in
        if not is_renderable(c):
            continue
        pnames.add(c.get("profile", "Default"))
        v = (c.get("params") or {}).get("voice")
        if v:
            vnames.add(v)
    v = (doc.get("params") or {}).get("voice")
    if v:
        vnames.add(v)
    for n in pnames:
        vnames.update((profs.get(n) or {}).get("voices") or [])
    return vnames, pnames


def plan_export(names, with_audio):
    """Work out exactly what goes in before writing a byte.

    Two reasons. The UI can show the real size up front — a whole library with
    audio is hundreds of megabytes, which is worth knowing before you click.
    And the manifest, which needs every checksum, can then be the *first*
    member of the tar rather than the last."""
    profs = profiles()
    plan = {"schema": ARCHIVE_SCHEMA, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "with_audio": bool(with_audio), "projects": [], "voices": {},
            "clips": {}, "takes": {}, "unreadable": [],
            "missing_voices": [], "missing_clips": [], "missing_takes": [],
            "bytes": 0}
    vnames, pnames, cnames, tnames, audio = set(), set(), set(), set(), {}
    for nm in names:
        try:
            doc = load(nm)
        except json.JSONDecodeError:
            plan["unreadable"].append(nm)
            continue
        if not doc:
            continue
        d = pdir(nm)
        vs, ps = voices_and_profiles(doc, profs)
        vnames |= vs
        pnames |= ps
        cnames |= clips_of(doc)
        tnames |= takes_of(doc)
        rendered = 0
        for c in doc["chunks"]:
            if not is_renderable(c):
                continue
            f = AUDIO / f"{chunk_hash(c, doc, profs)}.wav"
            if not f.exists():
                continue
            rendered += 1
            if with_audio and f.name not in audio:
                audio[f.name] = f
                plan["bytes"] += f.stat().st_size
        src = d / "source.md"
        plan["projects"].append({"name": d.name, "title": doc.get("title", nm),
                                 "chunks": len(doc["chunks"]), "rendered": rendered,
                                 "source": src.exists()})
        plan["bytes"] += (d / "doc.json").stat().st_size
        if src.exists():
            plan["bytes"] += src.stat().st_size
    for v in sorted(vnames):
        try:
            f = voice_file(v)
        except FileNotFoundError:
            # A profile naming a clip that is no longer on disk. Say so rather
            # than packing quietly: the point of a backup is that it is whole,
            # and a missing voice is not something to discover on restore day.
            plan["missing_voices"].append(v)
            continue
        plan["voices"][v] = {"file": f.name, "sha": sha256_file(f),
                             "bytes": f.stat().st_size}
        plan["bytes"] += f.stat().st_size
    for cn in sorted(cnames):
        f = CLIPS / f"{cn}.wav"
        if not f.exists():
            plan["missing_clips"].append(cn)
            continue
        plan["clips"][cn] = {"file": f.name, "sha": sha256_file(f),
                             "bytes": f.stat().st_size}
        plan["bytes"] += f.stat().st_size
    for tn in sorted(tnames):
        # Always packed, even without audio: a take is the *source* of a voiced
        # card, not a derived artefact. Leaving it out would restore a card
        # that can never be rendered again — the rest of the archive holds a
        # recording of the performance, not the performance.
        f = take_path(tn)
        if not f.exists():
            plan["missing_takes"].append(tn)
            continue
        plan["takes"][tn] = {"file": f.name, "bytes": f.stat().st_size}
        plan["bytes"] += f.stat().st_size
    plan["kind"] = "library" if len(plan["projects"]) > 1 else "project"
    plan["_profiles"] = {n: profs[n] for n in sorted(pnames) if n in profs}
    plan["_audio"] = audio
    return plan


def _tarinfo(name, size):
    ti = tarfile.TarInfo(name)
    ti.size = size
    ti.mtime = int(time.time())
    ti.mode = 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""      # no need to tell the far side who packed it
    return ti


def _add_bytes(tar, name, data):
    tar.addfile(_tarinfo(name, len(data)), io.BytesIO(data))


def _add_file(tar, path, name):
    with open(path, "rb") as f:
        tar.addfile(_tarinfo(name, path.stat().st_size), f)


def write_archive(plan, dest):
    """Pack a plan into `dest`.

    Gzip level 1 whenever audio is included: WAV barely compresses, so the
    higher levels spend minutes of CPU to save a couple of per cent."""
    audio = plan.pop("_audio", {})
    profs = plan.pop("_profiles", {})
    manifest = dict(plan, audio=sorted(audio))
    with tarfile.open(dest, "w:gz", compresslevel=1 if plan["with_audio"] else 6) as tar:
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
        _add_bytes(tar, "profiles.json", json.dumps(profs, indent=1).encode())
        for p in plan["projects"]:
            d = ROOT / p["name"]
            _add_file(tar, d / "doc.json", f"projects/{p['name']}/doc.json")
            if p["source"]:
                _add_file(tar, d / "source.md", f"projects/{p['name']}/source.md")
        for meta in plan["voices"].values():
            _add_file(tar, VOICES / meta["file"], f"voices/{meta['file']}")
        for meta in plan["clips"].values():
            _add_file(tar, CLIPS / meta["file"], f"clips/{meta['file']}")
        for meta in plan["takes"].values():
            _add_file(tar, TAKES / meta["file"], f"takes/{meta['file']}")
        for name, f in sorted(audio.items()):
            _add_file(tar, f, f"audio/{name}")
    return dest


def _free_name(taken, base, suffix="imported"):
    if base not in taken:
        return base
    n = f"{base}-{suffix}"
    i = 2
    while n in taken:
        n, i = f"{base}-{suffix}-{i}", i + 1
    return n


def _same_profile(a, b):
    """`note` is prose about when to use the profile; it does not change how a
    single character sounds, so it is not grounds for forking a second copy."""
    return all(a.get(k, BASE_PROFILE.get(k)) == b.get(k, BASE_PROFILE.get(k))
               for k in ("voices", "active", "exag", "cfg", "temp", "rep",
                         "engine", "lang", "speed", "gain", "fx"))


def import_archive(path, mode="skip"):
    """Restore projects from a .sagaproj. `mode` decides what a name collision
    means: skip the incoming one, replace what is here, or keep both.

    Two rules the rest of this falls out of.

    **Never overwrite a voice clip or a profile.** Both are global — a clip is
    what *every* project using that name sounds like — so silently replacing
    one would change books that have nothing to do with this import. An
    incoming clip whose name matches but whose bytes differ lands beside it as
    `<name>-imported`, and only the arriving project is pointed at the copy.
    Profiles are reconciled the same way, for the same reason.

    **Renaming either one changes what a chunk hashes to**, because the voice
    name is part of the hash key — so every rendered chunk in the archive would
    look stale the moment it landed. Hashes are therefore recomputed on the way
    in, once under the archive's profiles and once under this machine's, and
    each cached WAV is filed under its new name. A plain restore onto the
    machine that made the archive renames nothing and takes the fast path."""
    rep = {"projects": [], "voices": [], "profiles": [], "clips": [],
           "audio": 0, "takes": 0, "skipped": []}
    tmp = Path(tempfile.mkdtemp(prefix=".import-", dir=ROOT))
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                if m.isfile() and ARC_MEMBER.match(m.name):
                    tar.extract(m, tmp, set_attrs=False, **_EXTRACT)
        if not (tmp / "manifest.json").exists():
            raise ValueError("not a Saga Studio archive — no manifest inside")
        man = json.loads((tmp / "manifest.json").read_text())
        if man.get("schema", 0) > ARCHIVE_SCHEMA:
            raise ValueError(f"made by a newer Saga Studio (schema {man['schema']}, "
                             f"this one reads {ARCHIVE_SCHEMA})")

        # ── voices ──
        arc_profs = ({} if not (tmp / "profiles.json").exists()
                     else json.loads((tmp / "profiles.json").read_text()))
        arc_profs.setdefault("Default", dict(BASE_PROFILE))
        VOICES.mkdir(parents=True, exist_ok=True)
        seen_voices = {p.stem for p in VOICES.iterdir() if p.is_file()}
        vmap = {}
        vdir = tmp / "voices"
        for f in sorted(vdir.iterdir()) if vdir.is_dir() else []:
            try:
                local = voice_file(f.stem)
            except FileNotFoundError:
                local = None
            if local is not None and sha256_file(local) == sha256_file(f):
                vmap[f.stem] = f.stem                     # already here, byte for byte
                continue
            new = f.stem if local is None else _free_name(seen_voices, f.stem)
            shutil.copy2(f, VOICES / f"{new}{f.suffix}")
            seen_voices.add(new)
            vmap[f.stem] = new
            if new != f.stem:
                rep["voices"].append(f"voice “{f.stem}” arrived as “{new}” — a "
                                     f"different clip already had that name")

        # ── clips ── same never-overwrite rule as voices, and for the same
        # reason: a clip is global, so replacing intro.wav would change every
        # episode that opens with it. Unlike voices, a clip name is not part
        # of any hash, so renaming one costs nothing beyond the pointer.
        CLIPS.mkdir(parents=True, exist_ok=True)
        seen_clips = {p.stem for p in CLIPS.glob("*.wav")}
        cmap = {}
        cdir = tmp / "clips"
        for f in sorted(cdir.iterdir()) if cdir.is_dir() else []:
            local = CLIPS / f.name
            if local.exists() and sha256_file(local) == sha256_file(f):
                cmap[f.stem] = f.stem
                continue
            new = f.stem if not local.exists() else _free_name(seen_clips, f.stem)
            shutil.copy2(f, CLIPS / f"{new}.wav")
            seen_clips.add(new)
            cmap[f.stem] = new
            if new != f.stem:
                rep["clips"].append(f"clip “{f.stem}” arrived as “{new}” — a "
                                    f"different clip already had that name")

        # ── takes ── a take is named by its own checksum, so a name that is
        # already here *is* the same recording, byte for byte. Nothing to
        # compare, nothing to rename, and no card pointer to rewrite — which is
        # the whole reason performances are content-addressed and clips, which
        # you name yourself, are not.
        TAKES.mkdir(parents=True, exist_ok=True)
        tdir = tmp / "takes"
        for f in sorted(tdir.iterdir()) if tdir.is_dir() else []:
            local = TAKES / f.name
            if not local.exists():
                shutil.copy2(f, local)
                rep["takes"] += 1

        # ── profiles ──
        local_profs = profiles()
        pmap = {}
        for n, pr in arc_profs.items():
            incoming = dict(pr, voices=[vmap.get(v, v) for v in (pr.get("voices") or [])])
            cur = local_profs.get(n)
            if cur is not None and _same_profile(cur, incoming):
                pmap[n] = n
                continue
            new = n if cur is None else _free_name(set(local_profs), n)
            local_profs[new] = incoming
            pmap[n] = new
            rep["profiles"].append(f"profile “{n}” added" if new == n else
                                   f"profile “{n}” arrived as “{new}” — a different "
                                   f"profile already had that name")
        save_profiles(local_profs)

        # ── projects ──
        taken = {d.name for d in ROOT.iterdir() if (d / "doc.json").exists()}
        pdirs = tmp / "projects"
        for pd in sorted(pdirs.iterdir()) if pdirs.is_dir() else []:
            if not (pd / "doc.json").exists():
                continue
            doc = json.loads((pd / "doc.json").read_text())
            target, copied = pd.name, False
            if target in taken:
                if mode == "skip":
                    rep["skipped"].append(doc.get("title", target))
                    continue
                if mode == "replace":
                    shutil.rmtree(ROOT / target, ignore_errors=True)
                else:
                    target, copied = _free_name(taken, target, "copy"), True

            # hashes as the archive knew them, before anything is repointed
            old = [chunk_hash(c, doc, arc_profs) if is_renderable(c) else None
                   for c in doc["chunks"]]
            doc["name"] = target
            if copied:
                doc["title"] = f"{doc.get('title', target)} (imported)"
            for c in doc["chunks"]:
                if c.get("type") == "audio" and c.get("clip"):
                    c["clip"] = cmap.get(c["clip"], c["clip"])
                # Only write the key back if it was there or the name actually
                # moved. Adding an explicit "Default" to a card that never had
                # one would mean a plain restore did not return the document it
                # was handed, and a backup that quietly rewrites is a backup you
                # cannot check.
                was = c.get("profile", "Default")
                if pmap.get(was, was) != was or "profile" in c:
                    c["profile"] = pmap.get(was, was)
                cp = c.get("params") or {}
                if cp.get("voice"):
                    cp["voice"] = vmap.get(cp["voice"], cp["voice"])
            dp = doc.get("params") or {}
            if dp.get("voice"):
                dp["voice"] = vmap.get(dp["voice"], dp["voice"])
            new = [chunk_hash(c, doc, local_profs) if is_renderable(c) else None
                   for c in doc["chunks"]]

            for o, n in zip(old, new):
                if not o:
                    continue
                src, dst = tmp / "audio" / f"{o}.wav", AUDIO / f"{n}.wav"
                # copy, not move: two projects can share a chunk hash, and the
                # second one still needs the file the first one took
                if src.exists() and not dst.exists():
                    shutil.copy2(src, dst)
                    rep["audio"] += 1

            (ROOT / target).mkdir(parents=True, exist_ok=True)
            if (pd / "source.md").exists():
                shutil.copy2(pd / "source.md", ROOT / target / "source.md")
            save(doc)
            taken.add(target)
            rep["projects"].append({"name": target, "title": doc.get("title", target),
                                    "chunks": len(doc["chunks"])})
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── audio ───────────────────────────────────────────────────────────────
def get_model():
    global _model
    if _model is None:
        from chatterbox.tts import ChatterboxTTS
        import torch
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"loading Chatterbox on {dev} …", flush=True)
        _model = ChatterboxTTS.from_pretrained(device=dev)
        print("model warm", flush=True)
    return _model


def voice_file(name):
    for ext in (".wav", ".mp3", ".flac", ".m4a"):
        p = VOICES / f"{name}{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(f"no voice '{name}'")


# ── plugins ─────────────────────────────────────────────────────────────
# A profile can put an audio plugin after the model — pitch, formant, a little
# drive — which is the one lever neither engine offers, and the one that makes
# a second character out of a single reference clip.
#
# Like level it runs when the timeline is mixed, never when a card is rendered,
# so it is in no hash and changing it re-bakes nothing. Unlike level it is not a
# multiply, so the result is cached under a name made of the wav that went in
# and the settings that were on it — the same bargain audio/ makes, one layer
# further down. Measured at ~125x realtime, so the first mix after a change is
# seconds and every one after it is a file read.
PLUGIN_DIRS = [Path("/Library/Audio/Plug-Ins/VST3"),
               Path("/Library/Audio/Plug-Ins/Components"),
               Path.home() / "Library/Audio/Plug-Ins/VST3",
               Path.home() / "Library/Audio/Plug-Ins/Components"]
_plugins = {}
_fxlock = threading.Lock()      # one plugin instance, and it is stateful


def plugin_ok(path):
    """A plugin is a native binary this program is about to load and run, so the
    path had better be one of the folders the system keeps them in rather than
    anything a request felt like naming."""
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return any(str(p).startswith(str(d.resolve()) + os.sep)
               for d in PLUGIN_DIRS if d.is_dir())


def plugins():
    """Every VST3 and Audio Unit installed. A plugin usually ships as both, so
    the second copy of a name is dropped — they are the same processor."""
    out, seen = [], set()
    for d in PLUGIN_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.vst3")) + sorted(d.rglob("*.component")):
            if p.stem in seen:
                continue
            seen.add(p.stem)
            out.append({"name": p.stem, "path": str(p), "kind": p.suffix.lstrip(".")})
    return sorted(out, key=lambda x: x["name"].lower())


def get_plugin(path):
    if path not in _plugins:
        from pedalboard import load_plugin
        _plugins[path] = load_plugin(path)
    return _plugins[path]


def plugin_params(path):
    """What the inspector draws its sliders from — the plugin's own parameter
    list, so no plugin needs any code of its own in here."""
    fx = get_plugin(path)
    out = []
    for k, meta in fx.parameters.items():
        d = {"name": k, "label": k.replace("_", " ")}
        try:
            d.update(kind="range", min=float(meta.min_value), max=float(meta.max_value),
                     step=float(getattr(meta, "step_size", 0) or 0),
                     units=(meta.units or "").strip(), value=float(getattr(fx, k)))
        except (TypeError, ValueError, AttributeError):
            # a mode switch rather than a dial: it has choices, not a range
            vals = [str(v) for v in (getattr(meta, "valid_values", None) or [])]
            d.update(kind="choice", values=vals, value=str(getattr(fx, k, "")))
        out.append(d)
    return out


def fx_of(p):
    """The plugin settings a card's profile asks for, or None."""
    f = p.get("fx") or {}
    if not f.get("enabled") or not f.get("plugin"):
        return None
    return f if plugin_ok(f["plugin"]) and Path(f["plugin"]).exists() else None


def fx_render(src, f):
    """Run one rendered card through its profile's plugin, cached."""
    import numpy as np
    import soundfile as sf
    key = hashlib.sha256(json.dumps([src.stem, f.get("plugin"),
                                     f.get("params") or {}],
                                    sort_keys=True).encode()).hexdigest()[:20]
    dest = FX / f"{key}.wav"
    if dest.exists():
        return dest
    audio, sr = sf.read(str(src), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    with _fxlock:
        fx = get_plugin(f["plugin"])
        fx.reset()
        for k, v in (f.get("params") or {}).items():
            try:
                setattr(fx, k, v)
            except Exception:
                pass            # a parameter this build of the plugin has not got
        out = np.asarray(fx(audio, sr)).reshape(-1)
    FX.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    # float32, like every render in audio/ — soundfile's wav default is 16-bit,
    # and the fx cache feeds the assemble, not just the ear
    sf.write(str(tmp), out, sr, subtype="FLOAT")
    tmp.rename(dest)
    return dest


def rename_voice(old, new):
    """Rename a voice clip, and take its renders with it.

    A voice's name is part of every chunk hash, so renaming one re-points every
    card that uses it at a wav that has never been made — nine thousand of them,
    for the voice most of this library is read in. That is not a rename, it is a
    silent invalidation, and the only sign would be a library that had gone
    amber overnight.

    So the audio moves too. Every card is hashed once under the old name and
    once under the new, and each cached wav is filed under the name it now
    answers to. That is the same manoeuvre import_archive performs when an
    incoming voice has to be renamed around a collision, for the same reason.

    Pointers are rewritten first and the hashes recomputed after, so what gets
    moved is decided by the document as it will be, not as it was."""
    old = str(old or "")
    new = re.sub(r"[^a-z0-9_-]+", "-", str(new or "").lower()).strip("-")[:40]
    if not new:
        raise ValueError("a name is needed")
    src = voice_file(old)                        # raises if there is no such voice
    if new == old:
        return {"voice": old, "cards": 0, "audio": 0, "profiles": 0, "overrides": 0}
    try:
        voice_file(new)
        raise ValueError(f"a voice called “{new}” is already here")
    except FileNotFoundError:
        pass

    profs = profiles()
    docs, before = {}, {}
    for meta in projects():
        if meta.get("broken"):
            continue
        doc = load(meta["name"])
        if not doc:
            continue
        docs[meta["name"]] = doc
        before[meta["name"]] = [chunk_hash(c, doc, profs) if is_renderable(c) else None
                                for c in doc["chunks"]]

    nprof = 0
    for pr in profs.values():
        if old in (pr.get("voices") or []):
            pr["voices"] = [new if v == old else v for v in pr["voices"]]
            nprof += 1
        # the stack of previous settings names clips too; leaving the old name
        # in there would make "put it back" restore a voice that is not there
        for h in (pr.get("_history") or []):
            if old in (h.get("voices") or []):
                h["voices"] = [new if v == old else v for v in h["voices"]]
    if nprof:
        save_profiles(profs)

    overrides = 0
    for doc in docs.values():
        touched = False
        dp = doc.get("params") or {}
        if dp.get("voice") == old:
            dp["voice"] = new
            touched = True
        for c in doc["chunks"]:
            cp = c.get("params") or {}
            if cp.get("voice") == old:
                cp["voice"] = new
                overrides += 1
                touched = True
        if touched:
            save(doc)

    src.rename(VOICES / f"{new}{src.suffix}")

    cards = moved = 0
    for nm, doc in docs.items():
        for c, o in zip(doc["chunks"], before[nm]):
            if not o:
                continue
            n = chunk_hash(c, doc, profs)
            if n == o:
                continue
            cards += 1
            a, b = AUDIO / f"{o}.wav", AUDIO / f"{n}.wav"
            # two cards with the same words in the same voice share one wav, so
            # the second finds the first has already moved it
            if a.exists() and not b.exists():
                a.rename(b)
                moved += 1
    return {"voice": new, "cards": cards, "audio": moved,
            "profiles": nprof, "overrides": overrides}


def seed_take(n):
    """Pin the sampler for take n, or leave it running free for take 0.

    Chatterbox samples with temperature and never seeds, so two generate()
    calls on the same words come out differently — that is what makes re-render
    a fresh reading, and it is why take 0 stays unseeded and keeps behaving as
    it always has. A numbered take pins the global RNG so the same take is the
    same performance every time, which is what makes stepping back to an
    earlier one mean anything. Reproducible on this machine and this build of
    torch — a seed is not a portable description of a voice."""
    if n:
        import torch
        torch.manual_seed(int(n))


# ── voice conversion ────────────────────────────────────────────────────
VC_SR = 16000             # chatterbox VC consumes 16k mono, whatever you give it
VC_CHUNK_SECS = 25        # convert at most this much performance per model call
VC_TOP_DB = 40            # silence threshold when splitting a long take


def get_vc():
    """The voice-conversion model — which is s3gen, and nothing else.

    VC renders speech tokens through the same decoder the TTS model already
    holds, so when that model is warm this borrows its s3gen rather than
    loading a second gigabyte of identical weights. Cold, it loads only s3gen
    (~1 GB) and never the ~2 GB language model, which has no part in converting
    a recording: the tokens come from your performance, not from text."""
    global _vc
    if _vc is None:
        from chatterbox.vc import ChatterboxVC
        import torch
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        if _model is not None:
            print("voice conversion: borrowing the warm s3gen", flush=True)
            _vc = ChatterboxVC(s3gen=_model.s3gen, device=dev)
        else:
            print(f"loading Chatterbox VC on {dev} …", flush=True)
            _vc = ChatterboxVC.from_pretrained(device=dev)
        _vc_voice[0] = None
        print("voice conversion warm", flush=True)
    return _vc


def speech_spans(y):
    """(start, end) sample spans of at most VC_CHUNK_SECS, cut only inside
    silences so a word is never bisected.

    Chatterbox VC has no length cap and no chunking of its own, but a long take
    costs memory linearly and drifts, so a whole scene is converted a breath at
    a time and laid back onto the original timeline — which is also what keeps
    your pauses yours."""
    max_len = VC_CHUNK_SECS * VC_SR
    if len(y) <= max_len:
        return [(0, len(y))]
    import librosa
    iv = librosa.effects.split(y, top_db=VC_TOP_DB)
    if len(iv) == 0:
        return [(0, len(y))]
    spans, start, prev_end = [], iv[0][0], iv[0][1]
    for s, e in iv[1:]:
        if e - start > max_len:
            spans.append((start, prev_end))
            start = s
        prev_end = e
    spans.append((start, prev_end))
    out = []
    for s, e in spans:            # continuous speech with no silence to cut at
        while e - s > max_len:
            out.append((s, s + max_len))
            s += max_len
        out.append((s, e))
    return out


def render_voiced(c, doc, force=False):
    """Re-speak a recorded performance in a character's voice.

    VC tokenises the take into speech tokens — which carry the words, the
    timing and the delivery — then renders those through the decoder
    conditioned on the target voice. The performance stays yours; the timbre
    becomes the character's. That is the whole card.

    There are no parameters, because VC has none: it takes two audio files and
    nothing else. A profile contributes its voice here and only its voice —
    exaggeration, cfg, temperature and repetition penalty have no meaning for
    conversion, which is why chunk_hash leaves them out."""
    import torch, torchaudio as ta, librosa
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
    src = take_file(c.get("perf"))
    voice = str(voice_file(p["voice"]))
    m = get_vc()
    y, _ = librosa.load(str(src), sr=VC_SR, mono=True)
    if not len(y):
        raise ValueError("that performance has no audio in it")
    spans = speech_spans(y)
    ratio = m.sr / VC_SR
    pieces = []
    with _lock:
        # embed_ref is the costly half and the voice rarely changes from one
        # card to the next, so hold it and re-embed only when it actually moves
        if _vc_voice[0] != voice:
            m.set_target_voice(voice)
            _vc_voice[0] = voice
        seed_take(c.get("seed"))
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".vc-") as tmpd:
            piece = Path(tmpd) / "piece.wav"
            for s, e in spans:
                ta.save(str(piece), torch.from_numpy(y[s:e]).unsqueeze(0), VC_SR)
                pieces.append((int(s * ratio), m.generate(str(piece)).cpu()))
    # Size to whichever is longer, the original timeline or the last converted
    # span — a conversion can run past its source, and clipping it would eat
    # the end of the final word.
    total = max(int(len(y) * ratio), max(pos + w.shape[-1] for pos, w in pieces))
    out = torch.zeros(1, total)
    for pos, w in pieces:
        out[:, pos:pos + w.shape[-1]] = w
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    ta.save(str(tmp), out, m.sr)
    tmp.rename(dest)                       # atomic, as render()
    return h, False


# ── the omnivoice worker ────────────────────────────────────────────────
_ov = {"proc": None}
OV_LOG = ROOT / "omnivoice.log"


_langs = None


def languages():
    """The 646 languages OmniVoice speaks, as [{name, code}, …].

    Read straight off disk rather than imported. studio.py runs in the
    chatterbox virtualenv, which has no omnivoice in it, and lang_map.py is
    pure data with no imports of its own — so ast can lift the table out
    without executing anything, and without loading three gigabytes of model to
    answer a question about spelling. Empty when OmniVoice is not installed,
    which is the UI's cue to leave the picker as a plain box."""
    global _langs
    if _langs is not None:
        return _langs
    _langs = []
    base = OV_PYTHON.parent.parent / "lib"
    src = next(base.glob("python*/site-packages/omnivoice/utils/lang_map.py"), None)
    if src is None:
        return _langs
    try:
        import ast
        for node in ast.parse(src.read_text()).body:
            if (isinstance(node, ast.Assign) and node.targets
                    and getattr(node.targets[0], "id", "") == "LANG_NAME_TO_ID"):
                m = ast.literal_eval(node.value)
                _langs = sorted(({"name": n, "code": c} for n, c in m.items()),
                                key=lambda x: x["name"])
                break
    except (SyntaxError, ValueError, OSError, TypeError):
        _langs = []
    return _langs


def ov_available():
    return OV_PYTHON.exists() and (HERE / "omnivoice_server.py").exists()


def _ov_health(timeout=1.0):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{OV_PORT}/health",
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_ov(wait=300):
    """Start the OmniVoice worker if it is not up, and wait until it is warm.

    Lazy, like get_model(): a library that never uses the second engine should
    never pay its load time or its memory. The child's output goes to a log
    file rather than a pipe — nobody drains a pipe here, and a full one would
    wedge the worker mid-render."""
    if not ov_available():
        raise RuntimeError(f"OmniVoice is not installed — no interpreter at "
                           f"{OV_PYTHON}. Set SAGA_OV_PYTHON, or keep this "
                           f"profile on chatterbox.")
    h = _ov_health()
    if h and h.get("ready"):
        return OV_PORT
    if h is None and (_ov["proc"] is None or _ov["proc"].poll() is not None):
        print("starting the OmniVoice worker …", flush=True)
        log = open(OV_LOG, "ab", buffering=0)
        _ov["proc"] = subprocess.Popen(
            [str(OV_PYTHON), str(HERE / "omnivoice_server.py"), "--port", str(OV_PORT)],
            stdout=log, stderr=log)
    t0 = time.time()
    while time.time() - t0 < wait:
        h = _ov_health()
        if h and h.get("ready"):
            print("OmniVoice worker warm", flush=True)
            return OV_PORT
        if h and h.get("error"):
            raise RuntimeError(f"OmniVoice failed to load: {h['error']}")
        pr = _ov["proc"]
        if pr is not None and pr.poll() is not None:
            tail = ""
            if OV_LOG.exists():
                tail = OV_LOG.read_text(errors="replace").strip()[-300:]
            raise RuntimeError(f"the OmniVoice worker exited. {tail}")
        time.sleep(0.5)
    raise RuntimeError("the OmniVoice worker did not come up in time")


def _ov_gen(spoken, p, dest):
    """One line through the worker. It writes the wav itself — both processes
    are on this machine, so there is nothing to gain by copying the audio back
    over a socket."""
    import urllib.request, urllib.error
    port = get_ov()
    body = json.dumps({"text": spoken, "ref_audio": str(voice_file(p["voice"])),
                       "language": p["lang"], "speed": p["speed"],
                       "duration": p.get("duration") or 0,
                       "out": str(dest)}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/gen", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as ex:
        try:
            msg = json.loads(ex.read()).get("error") or str(ex)
        except Exception:
            msg = str(ex)
        raise RuntimeError(f"omnivoice: {msg}") from None


def render_omnivoice(c, doc, force=False):
    """Speak one card with OmniVoice.

    No seed, because the model has none — so a take still gets its own file and
    stepping back to it still plays that file, but re-rendering the same take
    will not reproduce it the way a seeded chatterbox take does."""
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
    spoken = c["text"].replace("❦", " ").strip()      # scene mark: silent
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    try:
        _ov_gen(spoken, p, tmp)
        tmp.rename(dest)                   # atomic, as everywhere else here
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return h, False


def render_any(c, doc, force=False):
    """Render whichever kind of card this is, with whichever engine it names.

    A voiced card is always chatterbox — it is speech-to-speech, which is the
    one thing OmniVoice cannot do."""
    if c.get("type") == "voiced":
        return render_voiced(c, doc, force)
    if params_for(c, doc)["engine"] == "omnivoice":
        return render_omnivoice(c, doc, force)
    return render(c, doc, force)


def render(c, doc, force=False):
    """Render one chunk. With force=True the cache is bypassed and overwritten —
    the render/preview buttons always generate, so a press always means work."""
    import torchaudio as ta
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
    m = get_model()
    spoken = c["text"].replace("❦", " ").strip()      # scene mark: silent
    with _lock:
        seed_take(c.get("seed"))
        wav = m.generate(spoken, audio_prompt_path=str(voice_file(p["voice"])),
                         exaggeration=p["exag"], cfg_weight=p["cfg"],
                         temperature=p["temp"], repetition_penalty=p["rep"])
    # keep the .wav extension: torchaudio picks the encoder from it, and a
    # ".part" suffix raises "Unsupported format".
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    ta.save(str(tmp), wav, m.sr)
    tmp.rename(dest)                       # atomic: no half-written cache entries
    return h, False


def render_preview(c, doc, force=False, text=None):
    """Speak just the selected words.

    Chatterbox has no low-quality mode — sampling cost is per token, so the
    only real speedup is less text. Same voice and parameters as the full
    render, so what you hear is exactly what the bake will say."""
    import torchaudio as ta
    p = params_for(c, doc)
    spoken = (text if text is not None else c["text"]).replace("❦", " ").strip()
    if p["engine"] == "chatterbox":         # as chunk_hash: default stays unmarked
        k = [spoken, p["voice"], p["exag"], p["cfg"], p["temp"], p["rep"],
             "prev", int(c.get("seed") or 0)]
    else:
        k = [spoken, p["voice"], "prev", int(c.get("seed") or 0),
             {"engine": p["engine"], "lang": p["lang"],
              "speed": float(p["speed"] or 0)}]
    h = "p" + hashlib.sha256(json.dumps(k, sort_keys=True).encode()).hexdigest()[:19]
    dest = AUDIO / f"{h}.wav"
    if dest.exists() and not force:
        return h, True, spoken
    if p["engine"] == "omnivoice":
        tmp = dest.with_name(dest.stem + ".tmp.wav")
        try:
            _ov_gen(spoken, p, tmp)
            tmp.rename(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return h, False, spoken
    m = get_model()
    with _lock:
        seed_take(c.get("seed"))
        wav = m.generate(spoken, audio_prompt_path=str(voice_file(p["voice"])),
                         exaggeration=p["exag"], cfg_weight=p["cfg"],
                         temperature=p["temp"], repetition_penalty=p["rep"])
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    ta.save(str(tmp), wav, m.sr)
    tmp.rename(dest)
    return h, False, spoken


# ── job queue ───────────────────────────────────────────────────────────
# Renders run on a worker thread, so clicking away, switching documents or
# closing the tab does not stop them. One worker: MPS wants a single
# generate() at a time anyway, and a queue makes that explicit rather than
# leaving requests to pile up on a lock.
_jobs = {}
_queue = __import__("collections").deque()
_qlock = threading.Condition()
_seq = [0]


def enqueue(kind, project, cid, text=None):
    with _qlock:
        _seq[0] += 1
        jid = f"j{_seq[0]}"
        _jobs[jid] = {"id": jid, "kind": kind, "project": project, "chunk": cid,
                      "status": "queued", "text": text, "queued_at": time.time()}
        _queue.append(jid)
        _qlock.notify()
    return jid


def worker():
    while True:
        with _qlock:
            while not _queue:
                _qlock.wait()
            jid = _queue.popleft()
        j = _jobs[jid]
        j["status"] = "running"
        t0 = time.time()
        try:
            doc = load(j["project"])
            c = next(x for x in doc["chunks"] if x["id"] == j["chunk"])
            if j["kind"] == "preview":
                h, cached, spoken = render_preview(c, doc, force=True, text=j["text"])
                j.update(hash=h, chars=len(spoken), of=len(c["text"]))
            else:
                h, cached = render_any(c, doc, force=True)
                j.update(hash=h)
            j.update(status="done", seconds=round(time.time() - t0, 1))
        except Exception as ex:
            j.update(status="error", error=f"{type(ex).__name__}: {ex}")
        # keep the table small; finished jobs are only needed until the UI polls
        if len(_jobs) > 400:
            for k in sorted(_jobs, key=lambda k: _jobs[k]["queued_at"])[:200]:
                if _jobs[k]["status"] in ("done", "error"):
                    _jobs.pop(k, None)


threading.Thread(target=worker, daemon=True).start()


def bake(name):
    """Render every stale card. Stoppable between cards, not mid-card: a
    generate() call on MPS cannot be interrupted without losing the model, so
    /api/bake_stop lets the current card finish and skips the rest. Everything
    already rendered stays on disk, so a later bake resumes where this left
    off rather than starting over."""
    doc = load(name)
    todo = [c for c in doc["chunks"] if is_renderable(c) and not c.get("mute")
            and not (AUDIO / f"{chunk_hash(c, doc)}.wav").exists()]
    _bake.update(running=True, done=0, total=len(todo), project=name, label="",
                 cancel=False, stopped=False)
    try:
        for c in todo:
            if _bake["cancel"]:
                _bake["stopped"] = True
                break
            _bake["label"] = (c["text"][:60] if is_speech(c)
                              else "◎ " + (c.get("perfname") or "performance"))
            render_any(c, doc)
            _bake["done"] += 1
    finally:
        _bake.update(running=False, label="", cancel=False)


# A card that is not rendered contributes silence, and silence is
# indistinguishable from a pause you put there on purpose — so an unrendered
# card in the middle of a chapter slips past unnoticed. In a preview it gets a
# soft chime instead, and the gap announces itself. Never in assemble(): the
# deliverable must not contain studio noises.
CHIME_SECS = 0.45


def chime_wave(sr):
    """A brief two-note bell — a fifth, decaying fast. Quiet enough to sit under
    narration without startling, distinct enough not to be mistaken for it."""
    import torch, math
    n = int(sr * CHIME_SECS)
    t = torch.arange(n, dtype=torch.float32) / sr
    env = torch.exp(-t * 7.0)
    a = int(sr * 0.006)                    # a few ms of attack, or it clicks
    if a:
        env[:a] = env[:a] * torch.linspace(0.0, 1.0, a)
    tone = (torch.sin(2 * math.pi * 784.0 * t)
            + 0.45 * torch.sin(2 * math.pi * 1176.0 * t))
    return (tone * env * 0.13).unsqueeze(0)


def mixdown(doc, gap=0.35, frm=None, chime=False):
    """Mix every card onto one timeline; (audio, sample_rate, missing, marks) back.

    `marks` is where each card starts, in seconds — [{"id", "at"}, …]. The
    browser follows it during preview to show which card is speaking, which is
    also what makes "stop, fix that one" possible without hunting for it.

    `chime` marks unready cards with a tone instead of dropping them. Preview
    only; assemble() leaves them out, as it always has.

    `frm` is a card id to start at: the timeline then begins with that card at
    zero and runs to the end, which is how you hear the rest of the book after
    fixing something in the middle without sitting through what came before.
    A music bed opened earlier is simply not in that mix — the cards before the
    start are not on the timeline at all, so there is nothing to carry over.

    A cursor walks the cards in order. Speech is placed at the cursor and
    advances it by its own length plus a rest (scene breaks get a longer one,
    as before); a voiced card behaves identically, since it is rendered into
    the same content-addressed pool. A silence card just advances it. An audio card is placed at
    the cursor and advances it either past the whole clip (mode "full") or by
    its "after" seconds — in which case the rest of the clip keeps playing
    *under* whatever the cursor reaches next, which is how music fades out
    beneath the first line of narration. Overlaps are summed and clamped.

    Fades are percentages of the clip: fade [10, 90] ramps up over the first
    10% and down over the last 10%. Gain is applied after the fade.

    No model here — the sample rate comes from the first speech chunk on disk
    (they are all rendered at the model's rate), so mixing never costs a
    ten-second model load. Both assemble() and the in-browser preview sit on
    this one function, so what you hear is what ships, by construction."""
    import torch, torchaudio as ta
    profs = profiles()          # once for the whole mix, not once per card
    cards = doc["chunks"]
    if frm is not None:
        i = next((k for k, c in enumerate(cards) if c["id"] == frm), None)
        if i is not None:
            cards = cards[i:]
    events, cursor, missing, marks = [], 0.0, 0, []
    # The rest between cards is added *after* the card that earns it, so a card
    # that runs on has to reach back and take it off again. Hence remembering
    # how big the last one was rather than looking ahead.
    last_gap = 0.0

    def note(c):                              # where this card begins
        marks.append({"id": c["id"], "at": round(cursor, 3)})

    def unready(c):
        """Not in the book. In a preview, at least make it audible."""
        nonlocal cursor, missing, last_gap
        missing += 1
        if not chime:
            return
        note(c)
        events.append((cursor, "chime", None, None, c))
        cursor += CHIME_SECS + gap
        last_gap = gap

    for c in cards:
        if c.get("mute"):                     # muted cards are simply not in the book
            continue
        kind = c.get("type", "speech")
        # "runs on": no rest before this card, so a sentence split across two
        # cards is still one sentence. Splitting mid-sentence to give a phrase
        # its own delivery is what this exists for — without it the phrase
        # arrives after a beat of silence that was never in the writing.
        if c.get("runon"):
            cursor = max(0.0, cursor - last_gap)
            last_gap = 0.0
        if kind == "silence":
            note(c)
            cursor += max(0.0, float(c.get("secs", 1.0)))
            last_gap = 0.0                    # the silence *is* the rest
            continue
        if kind == "audio":
            f = CLIPS / f"{c.get('clip', '')}.wav"
            if not c.get("clip") or not f.exists():
                unready(c)
                continue
            w, wsr = ta.load(str(f))
            note(c)
            events.append((cursor, kind, w, wsr, c))
            cursor += (max(0.0, float(c.get("after", 0.0)))
                       if c.get("mode") == "after" else w.shape[-1] / wsr)
            last_gap = 0.0                    # a clip is not followed by a rest
            continue
        f = AUDIO / f"{chunk_hash(c, doc, profs)}.wav"
        if not f.exists():
            unready(c)
            continue
        eff = fx_of(params_for(c, doc, profs))
        if eff:
            try:
                f = fx_render(f, eff)
            except Exception as ex:
                # a plugin that will not load must not take the whole mix with
                # it; the card is still there, just dry
                print(f"plugin failed on card {c['id']}: "
                      f"{type(ex).__name__}: {ex}", flush=True)
        w, wsr = ta.load(str(f))
        note(c)
        events.append((cursor, kind, w, wsr, c))
        # a voiced card lands here too — rendered and content-addressed exactly
        # as speech is — but it has no text to carry a scene mark
        g = 1.1 if is_speech(c) and c["text"].strip().startswith("❦") else gap
        cursor += w.shape[-1] / wsr + g
        last_gap = g
    if not events:
        return None, 0, missing, []
    # every speech chunk is at the model's rate; only clips ever need resampling.
    # A preview of a chapter nobody has rendered yet is all chimes and carries no
    # rate of its own, so fall back to a clip's, then to the model's.
    sr = (next((e[3] for e in events if e[1] in ("speech", "voiced")), None)
          or next((e[3] for e in events if e[3]), None) or 24000)
    pieces = []
    for start, kind, w, wsr, c in events:
        if kind == "chime":
            pieces.append((int(start * sr), chime_wave(sr)))
            continue
        if w.shape[0] > 1:                    # the book is mono; beds follow it
            w = w.mean(0, keepdim=True)
        if wsr != sr:
            w = ta.functional.resample(w, wsr, sr)
        if kind == "audio":
            n = w.shape[-1]
            lo, hi = (list(c.get("fade") or []) + [0, 100])[:2]
            fi, fo = int(n * lo / 100), int(n * (100 - hi) / 100)
            if fi > 0:
                w[..., :fi] = w[..., :fi] * torch.linspace(0.0, 1.0, fi)
            if fo > 0:
                w[..., n - fo:] = w[..., n - fo:] * torch.linspace(1.0, 0.0, fo)
            w = w * (float(c.get("gain", 100)) / 100.0)
        else:
            # a spoken card's level comes from its profile, so a character who
            # reads louder than the rest can be evened out without re-rendering
            # a word of them
            g = float(params_for(c, doc, profs).get("gain", 100))
            if g != 100:
                w = w * (g / 100.0)
        pieces.append((int(start * sr), w))
    # a trailing silence card pads the end, so the total honours the cursor too
    total = max(int(cursor * sr), max(s + w.shape[-1] for s, w in pieces))
    full = torch.zeros(1, total)
    for s, w in pieces:
        full[..., s:s + w.shape[-1]] += w
    full.clamp_(-1.0, 1.0)
    return full, sr, missing, marks


def assemble(name, gap=0.35):
    """Mixdown to out/<name>.mp3 — the deliverable."""
    import torchaudio as ta
    full, sr, missing, _ = mixdown(load(name), gap)
    if full is None:
        return None, missing
    out = pdir(name) / "out"
    out.mkdir(exist_ok=True)
    wav = out / f"{name}.wav"
    ta.save(str(wav), full, sr)
    mp3 = out / f"{name}.mp3"
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(wav), "-codec:a", "libmp3lame", "-b:a", "64k",
                        "-ac", "1", str(mp3)], check=True)
        wav.unlink()
        return mp3, missing
    return wav, missing


def preview_book(name, frm=None):
    """The same mixdown assemble() ships, parked in a dotfile the browser can
    stream — hearing the whole book should not overwrite the mp3 in out/.
    16-bit is plenty for ears and halves what goes over the wire; the Finder
    never shows the file, and export never packs out/.

    `frm` starts the mix at a card instead of at the top, so an edit in chapter
    nine costs nine seconds to hear rather than the eight minutes before it."""
    import torchaudio as ta
    full, sr, missing, marks = mixdown(load(name), frm=frm, chime=True)
    if full is None:
        return None, 0, missing, []
    out = pdir(name) / "out"
    out.mkdir(exist_ok=True)
    f = out / ".preview.wav"
    ta.save(str(f), full, sr, encoding="PCM_S", bits_per_sample=16)
    return f, round(full.shape[-1] / sr, 1), missing, marks


# ── the discuss window ──────────────────────────────────────────────────
def ask_claude(project, question, chunk_ids=None):
    """Shell out to Claude Code headless, with the relevant text as context."""
    doc = load(project) if project else None
    ctx = ""
    if doc:
        sel = [c for c in doc["chunks"]
               if is_speech(c) and (not chunk_ids or c["id"] in chunk_ids)]
        sel = sel[:40]
        ctx = (f"Working on an audiobook of \"{doc['title']}\".\n"
               f"{len(doc['chunks'])} chunks total. "
               f"{'Selected' if chunk_ids else 'First'} passages:\n\n"
               + "\n\n".join(f"[chunk {c['id']}] {c['text']}" for c in sel)
               + "\n\n")
    prompt = (ctx + "Question from the author: " + question +
              "\n\nAnswer briefly and concretely. If suggesting a text change, "
              "give the exact replacement text and the chunk number.")
    try:
        r = subprocess.run([CLAUDE, "-p", prompt], capture_output=True,
                           text=True, timeout=180, cwd=str(HERE))
        return (r.stdout or r.stderr or "no reply").strip()
    except FileNotFoundError:
        return "Claude Code CLI not found — set CLAUDE or install `claude`."
    except subprocess.TimeoutExpired:
        return "Timed out after 3 minutes."


# ── http ────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        if not TOKEN:
            return True
        if TOKEN in (self.headers.get("Cookie") or ""):
            return True
        if parse_qs(urlparse(self.path).query).get("k", [""])[0] == TOKEN:
            return True
        return False

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_file(self, path, ctype, filename=None):
        """Copy straight from disk. An archive of the whole library runs to
        hundreds of megabytes; _send would hold every byte of it in memory."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed():
            return self._send(401, b"Add ?k=<SAGA_TOKEN> to the URL.", "text/plain")
        if TOKEN and q.get("k") and u.path == "/":   # only the page sets the cookie
            self.send_response(200)
            self.send_header("Set-Cookie", f"{TOKEN}; Path=/; Max-Age=604800")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = (HERE / "studio_ui.html").read_bytes()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if u.path == "/":
            return self._send(200, (HERE / "studio_ui.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            pcounts, ccounts = library_counts()
            return self._send(200, {
                "projects": projects(),
                # with durations: the editor shows them, and a reference clip
                # past ten seconds is worth seeing, since chatterbox reads only
                # the first ten and the rest has never been heard
                "voices": sorted(({"name": p.stem, "secs": clip_secs(p)}
                                  for p in VOICES.glob("*.wav")),
                                 key=lambda v: v["name"]),
                # `added` is the file's mtime — rename preserves it, so a clip
                # keeps the date it arrived rather than the date you retitled it
                "clips": sorted(({"name": p.stem, "secs": clip_secs(p),
                                  "added": round(p.stat().st_mtime)}
                                 for p in CLIPS.glob("*.wav")),
                                key=lambda c: c["name"]),
                "profiles": profiles(), "profile_counts": pcounts,
                "clip_counts": ccounts,
                "defaults": DEFAULTS, "bake": _bake,
                "engines": list(ENGINES), "omnivoice": ov_available(),
                "stale_build": build_stale(),
                "model": "warm" if _model else "cold"})
        if u.path == "/api/plugins":
            try:
                import pedalboard          # noqa: F401
            except ImportError:
                return self._send(200, {"plugins": [], "host": False})
            return self._send(200, {"plugins": plugins(), "host": True})
        if u.path == "/api/plugin":
            # loading one takes about a second the first time, then it is held
            path = q.get("path", [""])[0]
            if not plugin_ok(path):
                return self._send(400, {"error": "not a plugin folder this program reads"})
            try:
                return self._send(200, {"params": plugin_params(path)})
            except Exception as ex:
                return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        if u.path == "/api/languages":
            # fetched once and cached in the page: 646 entries is too much to
            # send with every /api/state, and it never changes while we run
            return self._send(200, {"languages": languages()})
        if u.path == "/api/doc":
            doc = load(q.get("name", [""])[0])
            if not doc:
                return self._send(404, {"error": "no such project"})
            for c in doc["chunks"]:
                c.setdefault("mute", False)
                if is_speech(c):
                    h = chunk_hash(c, doc)
                    c["hash"] = h
                    c["ready"] = (AUDIO / f"{h}.wav").exists()
                    c["effective"] = params_for(c, doc)
                    c.setdefault("profile", "Default")
                    c.setdefault("height", 0)
                    c.setdefault("seed", 0)
                elif c["type"] == "voiced":
                    h = chunk_hash(c, doc)
                    c["hash"] = h
                    c["effective"] = params_for(c, doc)
                    c.setdefault("profile", "Default")
                    c.setdefault("seed", 0)
                    c.setdefault("perfname", "")
                    f = take_path(c.get("perf") or "")
                    c["haveperf"] = bool(c.get("perf")) and f.exists()
                    c["perflen"] = clip_secs(f) if c["haveperf"] else 0
                    # A card whose performance has gone is never ready, whatever
                    # is in the cache — it cannot be rendered again, and saying
                    # otherwise would hide the one thing worth knowing.
                    c["ready"] = c["haveperf"] and (AUDIO / f"{h}.wav").exists()
                elif c["type"] == "audio":
                    f = CLIPS / f"{c.get('clip', '')}.wav"
                    c["ready"] = bool(c.get("clip")) and f.exists()
                    c["cliplen"] = clip_secs(f) if c["ready"] else 0
                else:                          # silence has nothing to render
                    c["ready"] = True
            return self._send(200, doc)
        if u.path == "/api/jobs":
            nm = q.get("name", [""])[0]
            js = [j for j in _jobs.values() if not nm or j["project"] == nm]
            js.sort(key=lambda j: j["queued_at"])
            return self._send(200, {"jobs": js[-60:],
                                    "busy": sum(1 for j in js
                                                if j["status"] in ("queued", "running"))})

        if u.path == "/api/audio":
            f = AUDIO / f"{q.get('h',[''])[0]}.wav"
            if not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send(200, f.read_bytes(), "audio/wav")
        if u.path == "/api/clip":
            nm = re.sub(r"[^a-z0-9_-]", "", q.get("f", [""])[0])
            f = CLIPS / f"{nm}.wav"
            if not nm or not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "audio/wav")   # music runs to megabytes
        if u.path == "/api/voice":
            # auditioning the reference clip itself — what the model is being
            # asked to sound like, before any of it is spoken
            nm = re.sub(r"[^a-z0-9_.-]", "", q.get("f", [""])[0])
            try:
                f = voice_file(nm) if nm else None
            except FileNotFoundError:
                f = None
            if not f:
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "audio/wav")
        if u.path == "/api/card_audio":
            # One card, as it will sound in the book — which since plugins
            # means the rendered wav *through its profile's effect*. The plain
            # /api/audio hash lookup predates fx and plays the file as the
            # model made it, so a profile's plugin was audible in the book
            # preview and silent on the card's own play button, which reads as
            # "my settings did nothing". Cached by fx_render, so only the
            # first press after a change pays the ~100ms.
            doc = load(q.get("name", [""])[0])
            if not doc:
                return self._send(404, b"", "text/plain")
            cid = int(q.get("id", ["-1"])[0])
            c = next((x for x in doc["chunks"] if x["id"] == cid), None)
            if c is None or not is_renderable(c):
                return self._send(404, b"", "text/plain")
            f = AUDIO / f"{chunk_hash(c, doc)}.wav"
            if not f.exists():
                return self._send(404, b"", "text/plain")
            pp = params_for(c, doc)
            eff = fx_of(pp)
            if eff:
                try:
                    f = fx_render(f, eff)
                except Exception as ex:
                    print(f"plugin failed on card {cid}: "
                          f"{type(ex).__name__}: {ex}", flush=True)
            # level too — the one card must sound like the book will. A gain
            # is a multiply, so it happens on the way out with no cache.
            g = float(pp.get("gain", 100))
            if g != 100.0:
                import io
                import soundfile as sf
                audio, asr = sf.read(str(f), dtype="float32")
                buf = io.BytesIO()
                sf.write(buf, audio * (g / 100.0), asr, format="WAV", subtype="FLOAT")
                return self._send(200, buf.getvalue(), "audio/wav")
            return self._send_file(f, "audio/wav")
        if u.path == "/api/take":
            nm = re.sub(r"[^a-z0-9]", "", q.get("f", [""])[0])
            f = take_path(nm)
            if not nm or not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "audio/wav")
        if u.path == "/api/book_audio":
            f = pdir(q.get("name", [""])[0]) / "out" / ".preview.wav"
            if not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "audio/wav")   # a whole book; stream it
        if u.path == "/api/download":
            f = pdir(q.get("name", [""])[0]) / "out" / f"{q.get('name',[''])[0]}.mp3"
            if not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send(200, f.read_bytes(), "audio/mpeg")

        if u.path in ("/api/export", "/api/export_plan"):
            names = ([p["name"] for p in projects() if not p.get("broken")]
                     if q.get("all", [""])[0] == "1" else q.get("name", []))
            plan = plan_export(names, q.get("audio", ["1"])[0] == "1")
            if u.path == "/api/export_plan":
                plan.pop("_audio", None)
                plan.pop("_profiles", None)
                return self._send(200, plan)
            if not plan["projects"]:
                return self._send(404, {"error": "nothing to export"})
            stamp = time.strftime("%Y-%m-%d")
            fn = (f"saga-backup-{stamp}.sagaproj" if len(plan["projects"]) > 1
                  else f"{plan['projects'][0]['name']}-{stamp}.sagaproj")
            # Built to a temp file rather than streamed as it is packed: the
            # size is then known, so the browser shows real download progress
            # instead of an open-ended spinner.
            fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".export-", suffix=".tgz")
            os.close(fd)
            try:
                write_archive(plan, Path(tmp))
                return self._send_file(Path(tmp), "application/gzip", fn)
            finally:
                Path(tmp).unlink(missing_ok=True)
        return self._send(404, {"error": "?"})

    def _import_archive(self, u):
        """The body is the archive itself rather than JSON: a library backup is
        hundreds of megabytes, and base64 in a JSON field would cost a third
        again in transfer and hold the whole thing in memory at both ends."""
        mode = parse_qs(u.query).get("mode", ["skip"])[0]
        if mode not in ("skip", "replace", "copy"):
            return self._send(400, {"error": f"unknown mode {mode!r}"})
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".upload-", suffix=".tgz")
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            with _docmut:
                return self._send(200, {"ok": True, **import_archive(Path(tmp), mode)})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _read_body_to(self, f):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            raise ValueError("empty upload")
        left = n
        while left > 0:
            b = self.rfile.read(min(1 << 20, left))
            if not b:
                raise ValueError("upload cut short")
            f.write(b)
            left -= len(b)

    def _clip_upload(self, u):
        """Body is the audio file itself, like _import_archive — a song in a
        base64 JSON field would hold the whole thing in memory twice. ffmpeg
        does the reading, so anything ffmpeg can decode is fair game; what
        lands in clips/ is always a plain PCM wav this program understands."""
        fn = parse_qs(u.query).get("fn", ["clip"])[0]
        stem = re.sub(r"[^a-z0-9_-]+", "-", Path(fn).stem.lower()).strip("-")[:40] or "clip"
        if not shutil.which("ffmpeg"):
            return self._send(400, {"error": "ffmpeg is needed to import audio clips"})
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".clip-",
                                   suffix=Path(fn).suffix or ".bin")
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            CLIPS.mkdir(parents=True, exist_ok=True)
            dest = CLIPS / f"{stem}.wav"
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", tmp, str(dest)], capture_output=True, text=True)
            if r.returncode:
                dest.unlink(missing_ok=True)
                return self._send(400, {"error": "ffmpeg could not read that file: "
                                        + (r.stderr or "").strip()[-300:]})
            return self._send(200, {"ok": True, "clip": stem, "secs": clip_secs(dest)})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _take_upload(self, u):
        """A performance to drive a voiced card.

        Same raw-body, let-ffmpeg-read-it approach as _clip_upload, with two
        differences. It is stored as 16k mono, because that is exactly what
        Chatterbox VC consumes — anything more is thrown away on the way in,
        and a backup carrying whole takes should not carry it. And it is named
        by the sha256 of that wav rather than by the file you picked: importing
        the same recording twice is then free, and a second take can never
        overwrite the first, which matters because every card that has already
        rendered points at its performance by name."""
        fn = parse_qs(u.query).get("fn", ["take"])[0]
        if not shutil.which("ffmpeg"):
            return self._send(400, {"error": "ffmpeg is needed to import a performance"})
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".take-",
                                   suffix=Path(fn).suffix or ".bin")
        wav = None
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            TAKES.mkdir(parents=True, exist_ok=True)
            fd2, wav = tempfile.mkstemp(dir=TAKES, prefix=".take-", suffix=".wav")
            os.close(fd2)
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", tmp, "-ac", "1", "-ar", str(VC_SR), wav],
                               capture_output=True, text=True)
            if r.returncode:
                return self._send(400, {"error": "ffmpeg could not read that file: "
                                        + (r.stderr or "").strip()[-300:]})
            name = sha256_file(Path(wav))[:20]
            dest = take_path(name)
            if dest.exists():
                Path(wav).unlink(missing_ok=True)      # already have this one
            else:
                os.replace(wav, dest)
                os.chmod(dest, 0o644)                  # mkstemp makes it 0600
            wav = None
            return self._send(200, {"ok": True, "perf": name, "secs": clip_secs(dest),
                                    "perfname": Path(fn).stem[:80] or "performance"})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)
            if wav:
                Path(wav).unlink(missing_ok=True)

    def _voice_upload(self, u):
        """A reference clip for a profile.

        Raw body and ffmpeg, like clips and takes. The route this replaces took
        base64 inside JSON, which the browser built with
        `String.fromCharCode(...bytes)` — one argument per byte, so anything
        past about 64 KB blew the stack rather than uploading. It also wrote
        whatever arrived under a .wav name, so an mp3 dropped here became a file
        claiming to be something it was not.

        And it never overwrites. A voice name is part of every chunk hash, so
        replacing the file behind one would change what every card using it
        sounds like on its next render while every hash stayed valid — the
        renders on disk would be the old voice and the new ones the new voice,
        with nothing to mark the seam. Same rule as import: a different clip
        wanting a taken name lands beside it instead."""
        fn = parse_qs(u.query).get("fn", ["voice"])[0]
        stem = re.sub(r"[^a-z0-9_-]+", "-", Path(fn).stem.lower()).strip("-")[:40] or "voice"
        if not shutil.which("ffmpeg"):
            return self._send(400, {"error": "ffmpeg is needed to import a voice"})
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".voiceup-",
                                   suffix=Path(fn).suffix or ".bin")
        wav = None
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            VOICES.mkdir(parents=True, exist_ok=True)
            fd2, wav = tempfile.mkstemp(dir=VOICES, prefix=".voiceup-", suffix=".wav")
            os.close(fd2)
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", tmp, "-ac", "1", wav],
                               capture_output=True, text=True)
            if r.returncode:
                return self._send(400, {"error": "ffmpeg could not read that file: "
                                        + (r.stderr or "").strip()[-300:]})
            name, renamed = stem, False
            try:
                existing = voice_file(stem)
            except FileNotFoundError:
                existing = None
            if existing is not None and sha256_file(existing) != sha256_file(Path(wav)):
                taken = {p.stem for p in VOICES.iterdir() if p.is_file()}
                name, renamed = _free_name(taken, stem, "new"), True
            dest = VOICES / f"{name}.wav"
            if existing is not None and not renamed:
                Path(wav).unlink(missing_ok=True)     # byte for byte what is here
            else:
                os.replace(wav, dest)
                # mkstemp makes it 0600; every other voice on disk is readable,
                # and an export carrying one should be too
                os.chmod(dest, 0o644)
            wav = None
            secs = clip_secs(dest)
            return self._send(200, {"ok": True, "voice": name, "secs": secs,
                                    "renamed": renamed, "asked": stem,
                                    "long": secs > 10})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)
            if wav:
                Path(wav).unlink(missing_ok=True)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "unauthorised"})
        if u.path == "/api/import_archive":
            return self._import_archive(u)
        if u.path == "/api/clip/upload":       # raw body, so before the JSON parse
            return self._clip_upload(u)
        if u.path == "/api/take/upload":       # likewise
            return self._take_upload(u)
        if u.path == "/api/voice/upload":      # likewise
            return self._voice_upload(u)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or "{}")
        except ValueError as ex:
            # A non-JSON body reaching a JSON route used to raise right here,
            # outside every try below — which killed the connection instead of
            # answering it, and surfaced in the browser as ERR_EMPTY_RESPONSE:
            # no status, no message, nothing in the log to work from. The case
            # that found it was a raw upload arriving at an older build whose
            # route still expected base64 in JSON, and the silence cost more
            # than the mismatch did.
            return self._send(400, {"error": f"this route expects JSON: {ex}"})
        # Every mutating route needs a real project; without this a bad name
        # surfaces as an opaque 500 from subscripting None.
        if u.path not in ("/api/import", "/api/chat") and d.get("name") is not None:
            if load(d["name"]) is None:
                return self._send(404, {"error": f"no project {d['name']!r}. "
                                                 f"have: {[p['name'] for p in projects()]}"})
        # Mutating routes each load doc.json, change it and write it back, and
        # ThreadingHTTPServer runs every request on its own thread. Two that
        # overlap — a card's text save and its height autosave both firing, say
        # — would otherwise both read the old document and the slower one would
        # write back a copy that never saw the other's edit. Chat and assemble
        # are slow and read-only, so they stay outside and never block typing.
        # impact scans every project in the library to count cards, so it is
        # slow and read-only — exactly the shape that must not hold the lock
        lock = None if u.path in ("/api/chat", "/api/assemble", "/api/book_preview",
                                  "/api/reveal", "/api/profile/impact") else _docmut
        if lock:
            lock.acquire()
        try:
            if u.path == "/api/import":
                doc = import_md(d["title"], d["markdown"])
                return self._send(200, {"ok": True, "name": doc["name"],
                                        "chunks": len(doc["chunks"])})
            if u.path == "/api/chunk":
                doc = load(d["name"])
                snapshot(doc, "edit card")
                for c in doc["chunks"]:
                    if c["id"] == d["id"]:
                        if "text" in d:
                            c["text"] = normalise(d["text"])
                        if "note" in d:
                            c["note"] = d["note"]
                        if "profile" in d:
                            c["profile"] = d["profile"]
                        if "mute" in d:
                            c["mute"] = bool(d["mute"])
                        if "runon" in d:       # no rest before this card
                            c["runon"] = bool(d["runon"])
                        if "height" in d:          # editor height, persisted
                            c["height"] = int(d["height"])
                        if "params" in d:
                            # A card's own overrides. Clamped here because they
                            # land in the hash: a stray value would name a wav
                            # that can never be rendered, and the card would sit
                            # amber for ever with nothing to explain it.
                            got = {}
                            for k, v in (d["params"] or {}).items():
                                if v is None or k not in CARD_PARAMS:
                                    continue
                                if k == "engine":
                                    if v in ENGINES:
                                        got[k] = v
                                else:
                                    lo, hi = CARD_PARAMS[k]
                                    got[k] = _num(v, lo, lo, hi)
                            c["params"] = got
                        if "seed" in d:            # which take of this card to speak
                            c["seed"] = max(0, int(d["seed"] or 0))
                        # audio-card fields; clamp here so a stray value can
                        # never put a negative duration on the timeline
                        if "clip" in d:
                            c["clip"] = re.sub(r"[^a-z0-9_-]", "", d["clip"] or "")
                        if d.get("mode") in ("full", "after"):
                            c["mode"] = d["mode"]
                        if "after" in d:
                            c["after"] = max(0.0, float(d["after"] or 0))
                        if "gain" in d:
                            c["gain"] = max(0.0, min(200.0, float(d["gain"])))
                        if "fade" in d:
                            lo, hi = (list(d["fade"]) + [0, 100])[:2]
                            lo = max(0.0, min(100.0, float(lo)))
                            c["fade"] = [lo, max(lo, min(100.0, float(hi)))]
                        if "secs" in d:            # silence-card length
                            c["secs"] = max(0.0, float(d["secs"] or 0))
                        # voiced-card fields. `perf` is a checksum this program
                        # wrote, so hex and nothing else; `perfname` is only
                        # what to call it on screen and is never hashed.
                        if "perf" in d:
                            c["perf"] = re.sub(r"[^a-z0-9]", "", d["perf"] or "")[:40]
                        if "perfname" in d:
                            c["perfname"] = str(d["perfname"] or "")[:80]
                save(doc)
                return self._send(200, {"ok": True})

            if u.path == "/api/insert":
                doc = load(d["name"])
                snapshot(doc, f"insert {d.get('kind', 'text')} card")
                kind = d.get("kind", "speech")
                if kind == "audio":
                    c = {"id": 0, "type": "audio", "clip": "", "mode": "full",
                         "after": 5.0, "fade": [0, 100], "gain": 100, "note": ""}
                elif kind == "silence":
                    c = {"id": 0, "type": "silence", "secs": 1.0, "note": ""}
                elif kind == "voiced":
                    c = {"id": 0, "type": "voiced", "perf": "", "perfname": "",
                         "note": ""}
                else:
                    c = {"id": 0, "text": "", "params": {}, "note": ""}
                at = max(0, min(int(d.get("at", 0)), len(doc["chunks"])))
                doc["chunks"].insert(at, c)
                for i, c in enumerate(doc["chunks"]):
                    c["id"] = i
                save(doc)
                return self._send(200, {"ok": True, "id": at})

            if u.path == "/api/paste":
                doc = load(d["name"])
                card = d.get("card")
                if not isinstance(card, dict):
                    return self._send(400, {"error": "nothing to paste"})
                snapshot(doc, "paste card")
                at = max(0, min(int(d.get("at", 0)), len(doc["chunks"])))
                doc["chunks"].insert(at, paste_card(card))
                for i, c in enumerate(doc["chunks"]):
                    c["id"] = i
                save(doc)
                return self._send(200, {"ok": True, "id": at})

            if u.path == "/api/move":
                doc = load(d["name"])
                ch = doc["chunks"]
                src = next((i for i, c in enumerate(ch) if c["id"] == d["id"]), None)
                if src is None:
                    return self._send(404, {"error": f"no card {d['id']}"})
                # `to` is a slot in the list as it stands, before the card is
                # lifted out — so a move past itself shifts down by one
                to = max(0, min(int(d["to"]), len(ch)))
                if to > src:
                    to -= 1
                if to == src:
                    return self._send(200, {"ok": True, "moved": False})
                snapshot(doc, "move card")
                ch.insert(to, ch.pop(src))
                for i, c in enumerate(ch):
                    c["id"] = i
                save(doc)
                return self._send(200, {"ok": True, "moved": True})

            if u.path == "/api/duplicate":
                doc = load(d["name"])
                snapshot(doc, "duplicate")
                out = []
                for c in doc["chunks"]:
                    out.append(c)
                    if c["id"] == d["id"]:
                        out.append({**json.loads(json.dumps(c)), "note": ""})
                for i, c in enumerate(out):
                    c["id"] = i
                doc["chunks"] = out
                save(doc)
                return self._send(200, {"ok": True})

            if u.path == "/api/remove":
                doc = load(d["name"])
                snapshot(doc, "remove card")
                doc["chunks"] = [c for c in doc["chunks"] if c["id"] != d["id"]]
                for i, c in enumerate(doc["chunks"]):
                    c["id"] = i
                save(doc)
                return self._send(200, {"ok": True})

            if u.path == "/api/profile":
                p = profiles()
                nm = (d.get("profile") or "").strip()
                if not nm:
                    return self._send(400, {"error": "profile name required"})
                cur = p.get(nm)
                new = {**BASE_PROFILE, **(cur or {}), **d.get("data", {})}
                # engine and its two companions are part of the hash, so a
                # stray value here would name a wav that can never be rendered
                if new.get("engine") not in ENGINES:
                    new["engine"] = "chatterbox"
                new["lang"] = re.sub(r"[^a-zA-Z_-]", "", str(new.get("lang") or "en"))[:12] or "en"
                new["speed"] = _num(new.get("speed"), 0.0, 0.0, 3.0)
                new["gain"] = _num(new.get("gain"), 100.0, 0.0, 200.0)
                f = new.get("fx")
                if not isinstance(f, dict) or not f.get("plugin"):
                    new["fx"] = {}
                else:
                    # the plugin path is checked against the folders the system
                    # keeps plugins in: this is a binary the mixer will load
                    pl = str(f.get("plugin") or "")
                    new["fx"] = {"plugin": pl if plugin_ok(pl) else "",
                                 "enabled": bool(f.get("enabled")),
                                 "params": {str(k)[:60]: (float(v)
                                            if isinstance(v, (int, float))
                                            else str(v)[:60])
                                            for k, v in (f.get("params") or {}).items()}}
                # Remember what it was. Only when something that changes how it
                # sounds actually moved — editing the note, or saving the same
                # numbers again, should not push the real settings off the end
                # of the stack.
                if cur is not None and not _same_profile(cur, new):
                    hist = list(cur.get("_history") or [])
                    hist.append({"at": time.strftime("%Y-%m-%d %H:%M"),
                                 **{k: cur.get(k, BASE_PROFILE.get(k))
                                    for k in ("voices", "active", "exag",
                                              "cfg", "temp", "rep")}})
                    new["_history"] = hist[-PROFILE_HISTORY:]
                p[nm] = new
                save_profiles(p)
                return self._send(200, {"ok": True, "profiles": p})

            if u.path == "/api/fx_preview":
                # The profile editor's "hear it": the profile's reference clip
                # through whatever the sliders say right now — the *draft*,
                # unsaved values, because auditioning is what you do before
                # deciding to keep something. Cached like every other fx pass.
                try:
                    vf = voice_file(d.get("voice") or "")
                except FileNotFoundError as ex:
                    return self._send(404, {"error": str(ex)})
                pl = str(d.get("plugin") or "")
                out = vf
                if pl:
                    if not plugin_ok(pl):
                        return self._send(400, {"error": "not a plugin folder this program reads"})
                    try:
                        out = fx_render(vf, {"plugin": pl, "enabled": True,
                                             "params": d.get("params") or {}})
                    except Exception as ex:
                        return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
                g = _num(d.get("gain"), 100.0, 0.0, 200.0)
                if g != 100.0:
                    import io
                    import soundfile as sf
                    audio, asr = sf.read(str(out), dtype="float32")
                    buf = io.BytesIO()
                    sf.write(buf, audio * (g / 100.0), asr, format="WAV", subtype="FLOAT")
                    return self._send(200, buf.getvalue(), "audio/wav")
                return self._send_file(out, "audio/wav")

            if u.path == "/api/profile/impact":
                nm = (d.get("profile") or "").strip()
                if not nm:
                    return self._send(400, {"error": "profile name required"})
                cur = profiles().get(nm)
                prop = d.get("data")
                return self._send(200, profile_usage(
                    nm, None if prop is None else {**BASE_PROFILE, **(cur or {}), **prop}))

            if u.path == "/api/profile/revert":
                p = profiles()
                nm = d.get("profile")
                pr = p.get(nm)
                if not pr or not (pr.get("_history") or []):
                    return self._send(400, {"error": "no earlier settings to go back to"})
                hist = list(pr["_history"])
                prev = hist.pop()
                p[nm] = {**pr, **{k: v for k, v in prev.items() if k != "at"},
                         "_history": hist}
                save_profiles(p)
                return self._send(200, {"ok": True, "profiles": p, "at": prev.get("at")})

            if u.path == "/api/reprofile":
                # Move cards from one profile to another. The profiles are not
                # touched, so nothing outside this project changes — which is
                # the difference between "this character sounds different now"
                # and "these lines are that character now".
                doc = load(d["name"])
                src, dst = d.get("from"), (d.get("to") or "").strip()
                if not dst:
                    return self._send(400, {"error": "no profile to move to"})
                if dst not in profiles():
                    return self._send(404, {"error": f"no profile {dst!r}"})
                ids = d.get("ids")
                snapshot(doc, f"move cards to “{dst}”")
                moved = 0
                for c in doc["chunks"]:
                    if not is_renderable(c):
                        continue
                    if ids is not None and c["id"] not in ids:
                        continue
                    if src and c.get("profile", "Default") != src:
                        continue
                    if c.get("profile", "Default") == dst:
                        continue
                    c["profile"] = dst
                    moved += 1
                save(doc)
                return self._send(200, {"ok": True, "moved": moved})

            if u.path == "/api/replace":
                doc = load(d["name"])
                find = d.get("find") or ""
                if not find:
                    return self._send(400, {"error": "nothing to find"})
                repl = d.get("replace", "")
                flags = 0 if d.get("case") else re.IGNORECASE
                try:
                    pat = (re.compile(find, flags) if d.get("regex")
                           else re.compile((r"(?<!\w)%s(?!\w)" % re.escape(find))
                                           if d.get("whole") else re.escape(find), flags))
                except re.error as ex:
                    return self._send(400, {"error": f"bad pattern: {ex}"})

                hits, stale_now, hitcount = [], 0, 0
                for c in doc["chunks"]:
                    if not is_speech(c):
                        continue
                    new, n = pat.subn(repl, c["text"])
                    if not n:
                        continue
                    hitcount += n
                    was_ready = (AUDIO / f"{chunk_hash(c, doc)}.wav").exists()
                    if was_ready:
                        stale_now += 1
                    m = pat.search(c["text"])
                    a, b = max(0, m.start() - 40), min(len(c["text"]), m.end() + 40)
                    hits.append({"id": c["id"], "n": n, "was_ready": was_ready,
                                 "new": normalise(new),
                                 "before": c["text"][a:b], "after": new[a:b + len(repl) - (m.end()-m.start())]})

                # ~0.13s of generation per character, measured on this machine
                cost = int(sum(len(c["text"]) for c in doc["chunks"] if is_speech(c)
                               and any(h["id"] == c["id"] and h["was_ready"] for h in hits)) * 0.13)
                if not d.get("dry_run") and hits:
                    # snapshot BEFORE writing, or undo restores the replacement
                    snapshot(doc, f"replace “{find[:24]}”")
                    byid = {h["id"]: h["new"] for h in hits}
                    for c in doc["chunks"]:
                        if c["id"] in byid:
                            c["text"] = byid[c["id"]]
                    save(doc)
                for h in hits:
                    h.pop("new", None)
                return self._send(200, {"ok": True, "cards": len(hits), "matches": hitcount,
                                        "stale": stale_now, "resec": cost,
                                        "hits": hits[:40], "applied": not d.get("dry_run")})

            if u.path == "/api/undo":
                doc = load(d["name"])
                stack = doc.get("_undo") or []
                if not stack:
                    return self._send(200, {"ok": False, "error": "nothing to undo"})
                last = stack.pop()
                doc["chunks"] = last["chunks"]
                save(doc)
                return self._send(200, {"ok": True, "undone": last["label"],
                                        "left": len(stack)})


            if u.path == "/api/profile/delete":
                nm = d.get("profile")
                if nm == "Default":
                    return self._send(400, {"error": "the Default profile cannot be deleted"})
                p = profiles()
                p.pop(nm, None)
                save_profiles(p)
                # cards pointing at a deleted profile fall back to Default
                for pr in projects():
                    doc = load(pr["name"])
                    touched = False
                    for c in doc["chunks"]:
                        if c.get("profile") == nm:
                            c["profile"] = "Default"
                            touched = True
                    if touched:
                        save(doc)
                return self._send(200, {"ok": True, "profiles": p})
            if u.path == "/api/params":
                doc = load(d["name"])
                doc["params"] = d["params"]
                save(doc)
                return self._send(200, {"ok": True})
            if u.path == "/api/split":
                doc = load(d["name"])
                snapshot(doc, "split")
                out = []
                for c in doc["chunks"]:
                    if c["id"] == d["id"] and is_speech(c) and 0 < d["at"] < len(c["text"]):
                        a, b = c["text"][:d["at"]].strip(), c["text"][d["at"]:].strip()
                        out.append({**c, "text": a})
                        tail = {**c, "text": b, "note": ""}
                        # Splitting mid-sentence is how a phrase gets a delivery
                        # of its own, and the rest that normally follows a card
                        # would drop a pause into the middle of a sentence that
                        # never had one. Ending punctuation means the break was
                        # intended, so that case is left alone.
                        if not re.search(r"[.!?…:;❦]$", a):
                            tail["runon"] = True
                        out.append(tail)
                    else:
                        out.append(c)
                for i, c in enumerate(out):
                    c["id"] = i
                doc["chunks"] = out
                save(doc)
                return self._send(200, {"ok": True})
            if u.path == "/api/merge":
                doc = load(d["name"])
                snapshot(doc, "merge")
                out, skip = [], False
                for i, c in enumerate(doc["chunks"]):
                    if skip:
                        skip = False
                        continue
                    if (c["id"] == d["id"] and i + 1 < len(doc["chunks"])
                            and is_speech(c) and is_speech(doc["chunks"][i + 1])):
                        nxt = doc["chunks"][i + 1]
                        out.append({**c, "text": f'{c["text"]} {nxt["text"]}'.strip()})
                        skip = True
                    else:
                        out.append(c)
                for i, c in enumerate(out):
                    c["id"] = i
                doc["chunks"] = out
                save(doc)
                return self._send(200, {"ok": True})
            if u.path == "/api/render":
                return self._send(200, {"ok": True,
                                        "job": enqueue("render", d["name"], d["id"])})
            if u.path == "/api/preview":
                sel = (d.get("text") or "").strip()
                if not sel:
                    return self._send(400, {"error": "select some text in the card first"})
                return self._send(200, {"ok": True,
                                        "job": enqueue("preview", d["name"], d["id"], sel)})

            if u.path == "/api/bake":
                if _bake["running"]:
                    return self._send(200, {"ok": False, "error": "already baking"})
                threading.Thread(target=bake, args=(d["name"],), daemon=True).start()
                return self._send(200, {"ok": True})
            if u.path == "/api/bake_stop":
                if not _bake["running"]:
                    return self._send(200, {"ok": False, "error": "not baking"})
                _bake["cancel"] = True
                # The card in flight still finishes — see bake().
                return self._send(200, {"ok": True, "remaining":
                                        _bake["total"] - _bake["done"] - 1})
            if u.path == "/api/assemble":
                f, missing = assemble(d["name"])
                return self._send(200, {"ok": bool(f), "file": str(f) if f else None,
                                        "missing": missing})
            if u.path == "/api/book_preview":
                frm = d.get("from")
                f, secs, missing, marks = preview_book(
                    d["name"], None if frm is None else int(frm))
                return self._send(200, {"ok": bool(f), "secs": secs,
                                        "missing": missing, "from": frm,
                                        "marks": marks})
            if u.path == "/api/chat":
                return self._send(200, {"reply": ask_claude(
                    d.get("name"), d["question"], d.get("chunks"))})
            if u.path == "/api/reveal":
                # The path is built from pdir(), never from the request, so
                # there is no way to point this at something outside the data
                # directory — the name is sanitised down to [a-z0-9_-] first.
                proj = pdir(d["name"])
                if not proj.is_dir():
                    return self._send(404, {"error": "no folder for that project yet"})
                out = proj / "out"
                target = out if d.get("what") == "out" and out.is_dir() else proj
                subprocess.run([OPEN_CMD, str(target)], check=False)
                return self._send(200, {"ok": True, "path": str(target),
                                        "assembled": out.is_dir()})
            if u.path == "/api/rename":
                # The title only. `name` is the folder and every path derived
                # from it, and renaming that would move a project's audio,
                # source and out/ for the sake of a label.
                doc = load(d["name"])
                t = (d.get("title") or "").strip()
                if not t:
                    return self._send(400, {"error": "a title is needed"})
                doc["title"] = t[:120]
                save(doc)
                return self._send(200, {"ok": True, "title": doc["title"]})

            if u.path == "/api/project/duplicate":
                # Free, near enough: the copy's cards hash to exactly what the
                # original's did, so every wav already on disk serves both and
                # the duplicate is born fully rendered.
                doc = load(d["name"])
                taken = {p.name for p in ROOT.iterdir() if (p / "doc.json").exists()}
                new = _free_name(taken, doc["name"], "copy")
                src = pdir(doc["name"])
                doc = json.loads(json.dumps(doc))
                doc["name"] = new
                doc["title"] = f"{doc.get('title', new)} (copy)"[:120]
                doc.pop("_undo", None)          # the copy has nothing to undo yet
                pdir(new).mkdir(parents=True, exist_ok=True)
                if (src / "source.md").exists():
                    shutil.copy2(src / "source.md", pdir(new) / "source.md")
                save(doc)
                return self._send(200, {"ok": True, "name": new, "title": doc["title"]})

            if u.path == "/api/clip/rename":
                # Unlike a voice, a clip's name is in no hash — cards point at
                # it and nothing else does — so this is a rename and a pointer
                # sweep, with no audio to migrate. Never onto a name that is
                # taken: a clip is global, and quietly replacing intro.wav would
                # change every episode that opens with it.
                old = re.sub(r"[^a-z0-9_-]", "", str(d.get("clip") or ""))
                new = re.sub(r"[^a-z0-9_-]+", "-",
                             str(d.get("to") or "").lower()).strip("-")[:40]
                if not new:
                    return self._send(400, {"error": "a name is needed"})
                src = CLIPS / f"{old}.wav"
                if not old or not src.exists():
                    return self._send(404, {"error": f"no clip “{old}”"})
                if new == old:
                    return self._send(200, {"ok": True, "clip": old, "cards": 0})
                if (CLIPS / f"{new}.wav").exists():
                    return self._send(400, {"error": f"a clip called “{new}” is already here"})
                src.rename(CLIPS / f"{new}.wav")
                cards = 0
                for pr in projects():
                    doc = load(pr["name"])
                    touched = False
                    for c in doc["chunks"]:
                        if c.get("type") == "audio" and c.get("clip") == old:
                            c["clip"] = new
                            cards += 1
                            touched = True
                    if touched:
                        save(doc)
                return self._send(200, {"ok": True, "clip": new, "cards": cards})

            if u.path == "/api/voice/rename":
                try:
                    return self._send(200, {"ok": True,
                                            **rename_voice(d.get("voice"), d.get("to"))})
                except FileNotFoundError as ex:
                    return self._send(404, {"error": str(ex)})
                except ValueError as ex:
                    return self._send(400, {"error": str(ex)})

            if u.path == "/api/profile/rename":
                # A profile's name is not part of any hash — cards resolve
                # through it to a voice and some numbers, and those are what get
                # hashed. So this is free: every card follows the new name and
                # none of them go stale.
                p = profiles()
                old, new = d.get("profile"), (d.get("to") or "").strip()
                if old == "Default":
                    return self._send(400, {"error": "the Default profile cannot be renamed"})
                if not new:
                    return self._send(400, {"error": "a name is needed"})
                if old not in p:
                    return self._send(404, {"error": f"no profile {old!r}"})
                if new in p:
                    return self._send(400, {"error": f"“{new}” already exists"})
                p[new] = p.pop(old)
                save_profiles(p)
                cards = 0
                for pr in projects():
                    doc = load(pr["name"])
                    touched = False
                    for c in doc["chunks"]:
                        if c.get("profile") == old:
                            c["profile"] = new
                            touched = True
                            cards += 1
                    if touched:
                        save(doc)
                return self._send(200, {"ok": True, "profiles": p, "cards": cards})

            if u.path == "/api/profile/duplicate":
                p = profiles()
                src = d.get("profile")
                if src not in p:
                    return self._send(404, {"error": f"no profile {src!r}"})
                new = (d.get("to") or "").strip() or _free_name(set(p), src, "copy")
                if new in p:
                    return self._send(400, {"error": f"“{new}” already exists"})
                # the copy starts with no history of its own: what it was before
                # is a fact about the profile it came from, not about this one
                p[new] = {k: v for k, v in json.loads(json.dumps(p[src])).items()
                          if k != "_history"}
                save_profiles(p)
                return self._send(200, {"ok": True, "profiles": p, "name": new})

            if u.path == "/api/delete":
                shutil.rmtree(pdir(d["name"]), ignore_errors=True)
                return self._send(200, {"ok": True})
        except Exception as ex:
            return self._send(500, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            if lock:
                lock.release()
        return self._send(404, {"error": "?"})


@__import__("atexit").register
def _stop_ov():
    """Take the worker down with us. It holds three gigabytes and has no reason
    to outlive the studio that started it."""
    pr = _ov.get("proc")
    if pr is not None and pr.poll() is None:
        pr.terminate()
        try:
            pr.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pr.kill()


if __name__ == "__main__":
    where = "127.0.0.1" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"Saga Studio  ->  http://{where}:{PORT}" + ("?k=<token>" if TOKEN else ""))
    if HOST == "0.0.0.0":
        print("Reachable on the local network. No login unless SAGA_TOKEN is set.")
    print("(model loads on the first render, not at boot)")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
