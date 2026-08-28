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

Cards come in six kinds. A card with no "type" is speech — the original kind,
rendered by the model and content-addressed as above. type "audio" places an
imported clip on the timeline (music, an effect), and type "silence" is a
timed rest. Neither of those is rendered or hashed: their audio either exists
in clips/ or is nothing at all. type "visual" shows a picture or film on the
stage and takes no time at all — a mark on the timeline, not a sound. type
"choice" is where interactive playback stops and asks; the audiobook walks
straight past it.

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
import unicodedata
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

HERE = Path(__file__).resolve().parent
# ── keeping current ─────────────────────────────────────────────────────
# The studio can fetch its own newer self from GitHub, so a new version
# never costs the author a terminal. Two shapes of install, two ways:
# a git checkout fast-forwards itself; a packaged app downloads the payload
# (the files below and nothing else) into a folder that outranks the one
# inside the .app — so the bundle is never written to and no code signature
# is ever broken. Checking is automatic and quiet; INSTALLING is always a
# button, because replacing the running program is the author's call.
APP_REPO = os.environ.get("SAGA_REPO", "caitlynmeeks/saga-studio")
APP_BRANCH = os.environ.get("SAGA_BRANCH", "main")
# where a downloaded payload lands. The electron shell passes this and
# prefers what it finds there; a bare install updates itself in place.
APP_DIR = Path(os.environ.get("SAGA_APP_DIR") or HERE).expanduser()
# Exactly what an update replaces: the program, never the data. A name not
# on this list cannot be written by an update, whatever a tarball holds.
PAYLOAD = ("studio.py", "studio_ui.html", "stage_ui.html", "player.js", "desk.js",
           "export_player.html", "omnivoice_server.py", "chatterbox_server.py",
           "saga_mcp.py", "openai_agent.py", "discuss_rules.md",
           "requirements.txt")
# Data lives OUTSIDE the repo by default: voice clips and manuscripts are
# private, and a tool should never assume it may publish its user's material.
# Point these anywhere with SAGA_DATA / SAGA_VOICES.
ROOT = Path(os.environ.get("SAGA_DATA") or (Path.home() / ".saga-studio")).expanduser()
AUDIO = ROOT / "audio"
# Clips are global like voices: the same intro music recurs across episodes,
# and cards reference a clip by name, never by path. Nothing in the app ever
# deletes a clip file — undo can resurrect a card that points at one.
CLIPS = ROOT / "clips"
# Pictures and film for visual cards. Global like clips, and never deleted for
# the same reason — a card names its media, and undo can resurrect the card.
MEDIA = ROOT / "media"
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
ENGINES = ("chatterbox", "omnivoice", "kokoro")
# Downloaded engines live in one per-machine folder, never in the library: a
# venv is built for this machine's Python and this machine's silicon, and a
# library that travels between machines must not carry one machine's torch.
# The engine manager (see "installing engines") builds venvs here on demand.
ENGINES_DIR = Path(os.environ.get("SAGA_ENGINES")
                   or (Path.home() / "Library/Application Support/Saga Studio/engines"
                       if sys.platform == "darwin"
                       else Path.home() / ".saga-studio/engines")).expanduser()
# Editor settings — the author's own arrangements: which model Brenda speaks
# with, who paints the pictures, which apps a clip opens in for surgery. They
# are per-MACHINE, kept beside the engines rather than in the library, because
# a library travels (Export Everything, a copied folder) and an API key must
# never travel with it.
SETTINGS_FILE = Path(os.environ.get("SAGA_SETTINGS")
                     or ENGINES_DIR.parent / "settings.json").expanduser()
SETTINGS_DEFAULTS = {
    # llm.provider: claude = the Claude Code sign-in already on this machine;
    # anthropic = the same claude binary billed by API key; the rest speak the
    # OpenAI chat shape, which is what LM Studio and llama.cpp serve locally.
    "llm": {"provider": "claude", "model": "", "key": "", "url": ""},
    "image": {"provider": "nanobanana", "key": "", "url": ""},
    "apps": {"image": "", "audio": "", "video": ""},
    # the darkride account key: uploads wearing it land in that account
    # ("My studio" on darkride.ai hands one out). Blank = share anonymously,
    # exactly as before accounts existed.
    "darkride": {"key": ""},
}
# What a darkride studio key looks like: dk_ and then hex, nothing else. The
# check exists because the thing people actually paste into this field by
# mistake is darkride's own note ABOUT the key, which My Studio used to print
# in the same monospace the key itself wears. A wrong key is worse than an
# empty one: the upload still succeeds, it just lands in nobody's account,
# and the author finds out days later looking at an empty profile.
DK_KEY = re.compile(r"dk_[0-9a-f]{16,}")
LLM_PROVIDERS = ("claude", "anthropic", "lmstudio", "llamacpp", "openai",
                 "custom")
# where each OpenAI-shaped provider listens when the url field is left blank
LLM_URLS = {"lmstudio": "http://127.0.0.1:1234/v1",
            "llamacpp": "http://127.0.0.1:8080/v1",
            "openai": "https://api.openai.com/v1"}


def settings():
    """Read fresh on every call, like gemini_key: a key pasted into the
    Settings tab must work on the very next ask, not after a restart."""
    out = json.loads(json.dumps(SETTINGS_DEFAULTS))
    try:
        got = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for sec, vals in out.items():
        g = got.get(sec)
        if isinstance(g, dict):
            for k in vals:
                if isinstance(g.get(k), str):
                    vals[k] = g[k].strip()
    return out


def save_settings(d):
    """Merge what the page sent over what is known, never trusting shape, and
    keep the file at 0600: it holds the author's keys."""
    cur = settings()
    standing_dk = cur["darkride"]["key"]
    for sec, vals in cur.items():
        g = d.get(sec)
        if isinstance(g, dict):
            for k in vals:
                if isinstance(g.get(k), str):
                    vals[k] = g[k].strip()
    if cur["llm"]["provider"] not in LLM_PROVIDERS:
        cur["llm"]["provider"] = "claude"
    if cur["image"]["provider"] not in ("nanobanana", "drawthings"):
        cur["image"]["provider"] = "nanobanana"
    # A key that is not a key never reaches the file. The rest of the form
    # still saves: one bad paste should not cost the author the model they
    # just picked. Blank stays blank, which is how you share anonymously.
    warn = ""
    if cur["darkride"]["key"] and not DK_KEY.fullmatch(cur["darkride"]["key"]):
        cur["darkride"]["key"] = standing_dk
        warn = ("that is not a darkride key. They read dk_ and then hex. "
                "Generate one on My Studio at darkride.ai, press the copy "
                "button beside it, and paste that.")
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SETTINGS_FILE.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
        os.replace(tmp, SETTINGS_FILE)
        os.chmod(SETTINGS_FILE, 0o600)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dict(cur, warn=warn) if warn else cur


def clipboard_text():
    """Whatever is on the system clipboard, as text, or "". The Settings
    tab's paste button goes through here rather than the page's own
    navigator.clipboard: reading the clipboard from web content wants a
    permission the electron shell does not grant, and this is the author's
    own machine either way. Same shape as the 📁 app picker, which is
    osascript on this side for the same reason."""
    for cmd in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-o",
                "-selection", "clipboard"], ["xsel", "-b", "-o"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return r.stdout
    return ""


def _engine_python(env, managed, classic):
    """The interpreter an engine worker runs on: the env var wins, then the
    engine manager's venv, then the classic dev install. The managed path
    comes back even when nothing is there yet — it is where an install will
    land, and *_available() is what says whether it exists."""
    p = os.environ.get(env)
    if p:
        return Path(p).expanduser()
    m = ENGINES_DIR / managed / "bin" / "python"
    if m.exists():
        return m
    c = (Path.home() / classic).expanduser()
    return c if c.exists() else m


# Chatterbox needs torch and studio.py no longer has it: like OmniVoice it
# runs in its own interpreter as a warm worker process, spoken to over
# localhost. See chatterbox_server.py for why.
CB_PYTHON = _engine_python("SAGA_CB_PYTHON", "chatterbox",
                           "git/voice-studio/.venv/bin/python")
CB_PORT = int(os.environ.get("SAGA_CB_PORT", "5022"))
OV_PYTHON = _engine_python("SAGA_OV_PYTHON", "omnivoice",
                           "git/voice-studio/.venv-omnivoice/bin/python")
OV_PORT = int(os.environ.get("SAGA_OV_PORT", "5021"))
# Kokoro is the third voice: 82M parameters, Apache-2.0, ~50 preset voices in
# nine languages, quick enough on plain CPU to need no GPU and no separate
# worker. Presets only — it cannot clone — which is exactly what lets it ship
# in the desktop app, and someday run in a browser, without dragging the
# cloning liability along. Model files sit in the library so each machine
# fetches its own once: https://github.com/thewh1teagle/kokoro-onnx/releases
KOKORO_DIR = Path(os.environ.get("SAGA_KOKORO_DIR")
                  or (ROOT / "models" / "kokoro")).expanduser()
# a voice's first letter names its language — af_heart is American, ef_dora es
KOKORO_LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr",
                "h": "hi", "i": "it", "j": "ja", "p": "pt-br", "z": "cmn"}
# one line per language for the profile editor's preview button. No sample
# clips ship with the model; at faster-than-realtime on CPU, rendering one
# IS the sample.
KOKORO_SAMPLE = {
    "a": "The fog remembers everything it has ever touched.",
    "b": "The fog remembers everything it has ever touched.",
    "e": "La niebla recuerda todo lo que ha tocado.",
    "f": "La brume se souvient de tout ce qu'elle a touché.",
    "h": "कोहरा वह सब कुछ याद रखता है जिसे उसने छुआ है।",
    "i": "La nebbia ricorda tutto ciò che ha toccato.",
    "j": "霧は触れたものすべてを覚えています。",
    "p": "A névoa lembra-se de tudo o que já tocou.",
    "z": "雾记得它触碰过的一切。",
}


def _kokoro_model_file():
    """Whichever precision is on disk — int8 is a third the download of fp32
    and all three speak; anyone who fetched a bigger one meant to use it."""
    for n in ("kokoro-v1.0.onnx", "kokoro-v1.0.fp16.onnx", "kokoro-v1.0.int8.onnx"):
        if (KOKORO_DIR / n).exists():
            return KOKORO_DIR / n
    return None


def kokoro_available():
    import importlib.util
    return (_kokoro_model_file() is not None
            and (KOKORO_DIR / "voices-v1.0.bin").exists()
            and importlib.util.find_spec("kokoro_onnx") is not None)
# Localhost by default. SAGA_HOST=0.0.0.0 exposes it to the LAN — see the
# README: there is no login, and the discuss window shells out to Claude, so
# anyone who can reach the port can read the manuscript and spend tokens.
# SAGA_TOKEN adds a shared secret if the network is not fully trusted.
HOST = os.environ.get("SAGA_HOST", "127.0.0.1")
TOKEN = os.environ.get("SAGA_TOKEN", "")
# Where the publish tab's Share button carries web stories: darkride.ai's
# stage door, or any compatible receiver — a local darkride_server.py for
# testing — via SAGA_DARKRIDE.
DARKRIDE = os.environ.get("SAGA_DARKRIDE", "https://darkride.ai").rstrip("/")
# SAGA_CLAUDE points at an unusually-installed Claude Code; otherwise PATH,
# then the homebrew spot the packaged app cannot see PATH for. A function,
# not a constant: someone following the discuss panel's install steps has
# the studio already running, and their new claude should be found on the
# very next look rather than after a restart.
def claude_path():
    return (os.environ.get("SAGA_CLAUDE") or shutil.which("claude")
            or "/opt/homebrew/bin/claude")
OPEN_CMD = "open" if sys.platform == "darwin" else "xdg-open"

ROOT.mkdir(parents=True, exist_ok=True)
AUDIO.mkdir(parents=True, exist_ok=True)
CLIPS.mkdir(parents=True, exist_ok=True)
TAKES.mkdir(parents=True, exist_ok=True)
FX.mkdir(parents=True, exist_ok=True)
MEDIA.mkdir(parents=True, exist_ok=True)

# What this process actually loaded. studio_ui.html is re-read from disk on
# every page request, so a plain reload picks up front-end changes and looks
# like it picked up everything — but the Python is whatever was on disk when
# the process started. A new front end talking to an old route fails in ways
# that look like bugs in the new code, and twice now that has cost an evening.
# So the program watches its own source and says when it is out of date.
_SRC = [Path(__file__), HERE / "omnivoice_server.py", HERE / "chatterbox_server.py"]
BUILD_MTIME = max((p.stat().st_mtime for p in _SRC if p.exists()), default=0.0)


def build_stale():
    """Has the source changed since this process loaded it?"""
    try:
        now = max((p.stat().st_mtime for p in _SRC if p.exists()), default=0.0)
    except OSError:
        return False
    return now > BUILD_MTIME + 1        # a second of slack for copy timestamps


_kokoro = None
_kvoices = None
_lock = threading.Lock()          # espeak's phonemizer: one kokoro render at a time
_bake = {"running": False, "done": 0, "total": 0, "project": "", "label": "",
         "cancel": False, "stopped": False, "error": ""}
_docmut = threading.Lock()        # one doc.json read-modify-write at a time

DEFAULTS = {"voice": "", "exag": 0.4, "cfg": 0.35,
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


def spoken_text(t):
    """What the booth is fed. The scene mark ❦ is silent, and so is // —
    the author's manual caption break, which paces the words on the screen
    (see player.js pages()) and must never be read aloud. Spacing around it
    does not matter; only a URL's own :// is shielded. Newlines survive —
    they are phrasing — so the mark eats spaces and tabs, never the line."""
    t = t.replace("❦", " ").replace("://", ":\x01")
    t = re.sub(r"[ \t]*//+[ \t]*", " ", t)
    return t.replace("\x01", "//").strip()


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
# ── shelves ─────────────────────────────────────────────────────────────
# A series is a playlist over the library: an ordered list of project names
# kept entirely OUTSIDE the projects it names. Not one byte of a doc.json says
# which shelf a story sits on, and that is the whole point. A story stands
# alone, is shared alone, plays alone; the order it is read in belongs to the
# shelf. Which is also what retires the numbers people put in titles — the
# position in `order` IS the chapter number, so inserting an episode between
# two others renames nothing.
SERIES = ROOT / "series.json"
# ── the cast ────────────────────────────────────────────────────────────
# Characters, locations, props and styles as things the library KNOWS, not
# filenames somebody remembered (CAST.md is the spec). A cast member owns
# reference plates and may link to a voice profile; a visual card will point
# at it by name. Plates live OUTSIDE media/ on purpose: canon lives somewhere
# a shot cannot be mistaken for it, and the pool stays what it is — the place
# output lands. A plate is never a shot.
CAST_FILE = ROOT / "cast.json"
CAST = ROOT / "cast"                      # plate files: cast/<slug>/<file>
# A slug is the name a stored ref will hold, so it wears the ref alphabet.
# The slot (plate) name is the addressable half of a ref's second segment.
CAST_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,60}$")
PLATE_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
# What one item in a card's `ref` may be: a bare pool name exactly as ever,
# `@slug` for a member's key plate, or `@slug/plate` for that exact plate.
# `@maisie/suit-blue-3q` is as deep as a reference ever goes (CAST.md §3).
REF_RE = re.compile(r"^@?[a-z0-9_-]{1,60}(/[a-z0-9_-]{1,40})?$")
# `engine` defaults to chatterbox so that adding a second engine changes nothing
# until a profile is deliberately moved across — every wav already on disk keeps
# its name and stays valid. `lang` and `speed` mean nothing to chatterbox and are
# only read when the engine is omnivoice; `kvoice` names a Kokoro preset and is
# only read when the engine is kokoro (`speed` serves both).
# voices starts empty: a new profile owns no reference clip until its author
# gives it one — no name may be baked in here, it would be somebody's voice
BASE_PROFILE = {"voices": [], "active": 0, "exag": 0.4, "cfg": 0.35,
                "temp": 0.7, "rep": 1.2, "note": "", "gain": 100, "fx": {},
                "engine": "chatterbox", "lang": "en", "speed": 0,
                "kvoice": "af_heart"}


def profiles():
    if PROFILES.exists():
        p = json.loads(PROFILES.read_text())
    else:
        p = {}
    if "Default" not in p:
        p["Default"] = dict(BASE_PROFILE)
        # Born speaking. No voice clip ships with the app, so a chatterbox
        # default could only greet its author with "add a voice first" —
        # Kokoro has its presets and answers immediately, and the cloning
        # engines are one flip away once a clip exists. Only at this moment
        # of birth: an existing library keeps whatever it chose.
        p["Default"]["engine"] = "kokoro"
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
    voices = prof.get("voices") or [""]
    idx = min(prof.get("active", 0), len(voices) - 1)
    eng = prof.get("engine", "chatterbox")
    return {"voice": voices[idx],
            "exag": prof.get("exag", 0.4), "cfg": prof.get("cfg", 0.35),
            "temp": prof.get("temp", 0.7), "rep": prof.get("rep", 1.2),
            "engine": eng if eng in ENGINES else "chatterbox",
            "lang": prof.get("lang", "en") or "en",
            "speed": prof.get("speed", 0) or 0,
            "kvoice": prof.get("kvoice", "af_heart") or "af_heart",
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
    profs, clips, media, home = {}, {}, {}, {}

    def claim(name, rank, proj, title, of=""):
        # Placed beats painted-against beats generated-and-not-kept, and an
        # earlier project beats a later one on a tie. Rank first so that a
        # picture two stories know is filed under the one that SHOWS it.
        cur = home.get(name)
        if cur is None or rank < cur["rank"]:
            home[name] = {"rank": rank, "project": proj, "title": title,
                          "of": of, "how": ("placed", "ref", "variant")[rank]}

    for d in sorted(ROOT.iterdir()):
        f = d / "doc.json"
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        title = doc.get("title", d.name)
        for c in doc.get("chunks", []):
            if is_renderable(c):
                k = c.get("profile", "Default")
                profs[k] = profs.get(k, 0) + 1
            elif c.get("type") == "audio" and c.get("clip"):
                clips[c["clip"]] = clips.get(c["clip"], 0) + 1
            elif c.get("type") == "visual" and c.get("media"):
                media[c["media"]] = media.get(c["media"], 0) + 1
            # Where a picture belongs is not something anyone should have to
            # file: the documents already say it. A visual card SHOWS one,
            # was painted AGAINST others, and remembers the ones it generated
            # and did not keep.
            if c.get("type") == "visual":
                chosen = c.get("media") or ""
                if chosen:
                    claim(chosen, 0, d.name, title)
                for g in c.get("gen") or []:
                    g = re.sub(r"[^a-z0-9_-]", "", str(g or ""))
                    if g and g != chosen:
                        claim(g, 2, d.name, title, chosen)
            for r in ref_list(c.get("ref")):
                if not r.startswith("@"):     # a plate is filed, not homeless
                    claim(r, 1, d.name, title)
    return profs, clips, media, home


def params_for(c, doc, profs=None):
    """DEFAULTS <- doc defaults <- the card's profile <- per-card override."""
    return {**DEFAULTS, **doc.get("params", {}),
            **profile_params(c.get("profile", "Default"), profs),
            **c.get("params", {})}


def chunk_hash(c, doc, profs=None):
    if c.get("type") == "group":
        # a group bar makes no sound; a constant key means no caller can crash
        # on it, and no file will ever exist under it
        return "g" + hashlib.sha256(b"group").hexdigest()[:19]
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
    elif p["engine"] == "kokoro":
        # Only what Kokoro reads: a preset name and a pace. The chatterbox
        # reference voice and its four dials mean nothing to it, so none of
        # them may turn a rendered kokoro card amber.
        key = [c["text"], {"engine": "kokoro", "voice": p["kvoice"],
                           "speed": float(p["speed"] or 0)}]
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


def delivery_of(c, doc, profs=None):
    """The composed delivery a render actually spoke with — the keys
    chunk_hash reads for this card's engine, branch for branch, plus the
    voice it wore. Filed into the card's history so a reading can be
    brought back whole. The card-overridable keys restore straight into
    c["params"]; voice, kvoice and lang live only in the profile, so a
    restored reading is exact while its profile still says what it said,
    and honestly re-renders when the profile has moved on."""
    p = params_for(c, doc, profs)
    if c.get("type") == "voiced":
        return {"engine": "chatterbox", "voice": p["voice"]}
    e = p["engine"]
    if e == "chatterbox":
        return {"engine": e, "voice": p["voice"], "exag": p["exag"],
                "cfg": p["cfg"], "temp": p["temp"], "rep": p["rep"]}
    if e == "kokoro":
        return {"engine": e, "kvoice": p["kvoice"],
                "speed": float(p["speed"] or 0)}
    out = {"engine": e, "voice": p["voice"], "lang": p["lang"],
           "speed": float(p["speed"] or 0)}
    if p.get("duration"):
        out["duration"] = float(p["duration"])
    return out


def file_history(project, cid, c, doc, h):
    """Every render a card has ever made, remembered ON the card: the words
    as they were spelled for the model, the take, the profile and the
    composed delivery that produced hash h — enough to bring that exact
    sound back with one gesture, cached wav and all. This is the other axis
    from takes: a take re-rolls the same reading, history spans every
    respelling, engine switch and dial change the card has been through.
    Keyed by hash, so re-rendering the same state refreshes its entry
    rather than growing a twin. `c` and `doc` are the copies the render
    actually spoke from — the live doc may already say something newer,
    which is exactly why the snapshot is taken from these."""
    entry = {"h": h, "at": int(time.time()),
             "take": int(c.get("seed") or 0),
             "profile": c.get("profile", "Default"),
             "d": delivery_of(c, doc)}
    if is_speech(c):
        entry["text"] = c["text"]
    with _docmut:
        live = load(project)
        c2 = (next((x for x in live["chunks"] if x["id"] == cid), None)
              if live else None)
        if c2 is None or not is_renderable(c2):
            return                       # the card left while it rendered
        hist = [e for e in (c2.get("hist") or [])
                if isinstance(e, dict) and e.get("h") != h]
        hist.append(entry)
        c2["hist"] = hist[-24:]          # a shortlist, not an archive
        save(live)


def file_paint_history(project, cid, mname, prompt, ref, vary, style):
    """The visual twin of file_history: every variant a card paints keeps
    the prompt, references, vary-source and style words that painted it,
    keyed by the pool name — unique for ever, pool law. A visual card's
    `hist` holds these; a speech card's holds renders — one field, and the
    card's type says which shape lives in it. The variants menu shows the
    words beside each picture and can put them back on the card, so a
    prompt that found the right picture is never lost to the next edit of
    the note."""
    entry = {"m": mname, "at": int(time.time()),
             "prompt": str(prompt or "")[:2000],
             "ref": ref_list(ref)}
    vary = re.sub(r"[^a-z0-9_-]", "", str(vary or ""))
    if vary:
        entry["vary"] = vary
    if style and style[0]:
        entry["style"] = [str(t)[:200] for t in style[0][:4]]
    with _docmut:
        live = load(project)
        c = (next((x for x in live["chunks"] if x["id"] == cid), None)
             if live else None)
        if c is None or c.get("type") != "visual":
            return
        hist = [e for e in (c.get("hist") or [])
                if isinstance(e, dict) and e.get("m") != mname]
        hist.append(entry)
        c["hist"] = hist[-24:]
        save(live)


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


# The choice grammar, validated here and NEVER evaluated here. One evaluator
# exists — in player.js, shared by the stage and the HTML export — and Python
# only keeps garbage from reaching it. State is a flat {name: number} map;
# flags are 0/1. A set op is `flag`, `!flag`, `coins+2`, `coins-1` or
# `coins=5`; a condition is `flag`, `!flag` or `coins>=3` (any of == != >= <=
# > <). Whitespace is stripped on the way in so the evaluator parses one
# spelling. What fails the shape is dropped, never a 500 — the same posture
# an edit's params get.
SET_RE = re.compile(r"^(!?[a-z][a-z0-9_]{0,23}|[a-z][a-z0-9_]{0,23}[+\-=]\d{1,4})$")
WHEN_RE = re.compile(r"^!?[a-z][a-z0-9_]{0,23}((==|!=|>=|<=|>|<)-?\d{1,4})?$")


# A link is only ever followed by someone who is not the author, so the
# scheme is a whitelist rather than a blacklist: two schemes, no spaces, no
# quotes or angle brackets that could climb out of the attribute a player
# writes it into.
URL_RE = re.compile(r"^https?://[^\s<>\"']{1,400}$", re.I)


def clean_when(v):
    w = re.sub(r"\s+", "", str(v or ""))
    return w if WHEN_RE.match(w) else ""


def clean_url(v):
    """A link an option may open, made safe.

    http and https and nothing else. An option's URL is typed by an author but
    CLICKED by a stranger, in an exported page that may be sitting on the open
    web, so javascript:, data: and file: never get through this door — and
    anything that is not a link at all comes back empty, which is exactly what
    an ordinary option is."""
    u = str(v or "").strip()
    return u if URL_RE.match(u) else ""


def clean_wait(v):
    """Seconds a choice waits before deciding for itself. 0 waits forever,
    which is what every choice card did before this existed, so a document
    that has never heard of a timeout keeps its old behaviour by saying
    nothing."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return max(0, min(600, n))


def clean_options(v):
    """A choice card's options, made safe. `goto` is a tag, and gets exactly a
    tag's sanitising; empty goto means the story ends there.

    `url` opens a page in the listener's own browser, and `dflt` marks the one
    the card's clock takes when nobody answers. Only ONE option may be the
    default: two of them is not a stalemate the players should have to break
    at playback, so the first marked one wins here, once, and both players
    then read the same document the same way."""
    out = []
    marked = False
    for o in list(v or [])[:6]:
        if not isinstance(o, dict):
            continue
        row = {
            "label": str(o.get("label") or "")[:120],
            "goto": re.sub(r"[^a-z0-9_-]", "", str(o.get("goto") or "").lower())[:24],
            "set": [s for s in (re.sub(r"\s+", "", str(x))
                                for x in list(o.get("set") or [])[:8])
                    if SET_RE.match(s)],
            "when": clean_when(o.get("when")),
        }
        u = clean_url(o.get("url"))
        if u:
            row["url"] = u
        if o.get("dflt") and not marked:
            row["dflt"] = True
            marked = True
        out.append(row)
    return out


def clean_tags(v):
    """A card's tags, made safe. A tag is two things at once: a label you can
    read down the deck, and — for choice cards — the anchor a jump lands on.
    It is never part of any hash, because a tag changes nothing about how a
    card sounds, so tagging is always free. Slug characters only: a tag gets
    typed into option fields and compared by name, and case or whitespace
    would quietly make two spellings of one anchor."""
    out = []
    for t in list(v or [])[:8]:
        t = re.sub(r"[^a-z0-9_-]", "", str(t).lower())[:24]
        if t and t not in out:
            out.append(t)
    return out


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
    elif kind == "visual":
        c = {"id": 0, "type": "visual",
             "media": re.sub(r"[^a-z0-9_-]", "", str(src.get("media") or "")),
             # on a visual card the note is the paint prompt — content,
             # not marginalia, so it travels with the copy
             "note": str(src.get("note") or "")[:2000]}
        refs = ref_list(src.get("ref"))    # the paint panel's reference(s)
        if refs:
            c["ref"] = ref_store(refs)
        if src.get("nostyle"):             # the opt-out travels with the copy
            c["nostyle"] = True
        gen = [re.sub(r"[^a-z0-9_-]", "", str(x))
               for x in (src.get("gen") or []) if str(x or "").strip()]
        if gen:                            # its painted variants, names only
            c["gen"] = gen[:40]
    elif kind == "title":
        fade = list(src.get("fade") or [])[:2] + [0.6, 0.6]
        c = {"id": 0, "type": "title",
             "text": normalise(str(src.get("text") or "")),
             "secs": _num(src.get("secs"), 3.0, 0.0, 600.0),
             "fade": [_num(fade[0], 0.6, 0.0, 30.0),
                      _num(fade[1], 0.6, 0.0, 30.0)],
             "note": ""}
    elif kind == "choice":
        c = {"id": 0, "type": "choice", "auto": bool(src.get("auto")),
             "wait": clean_wait(src.get("wait")),
             "options": clean_options(src.get("options")), "note": ""}
    elif kind == "group":
        c = {"id": 0, "type": "group",
             "gname": re.sub(r"[\"'`\\<>&]", "",
                             str(src.get("gname") or "")).strip()[:60] or "Group",
             "note": ""}
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
        hs = [int(x) for x in (src.get("hidden_takes") or [])
              if isinstance(x, (int, float)) and 0 <= int(x) < 10 ** 6]
        if hs:
            c["hidden_takes"] = sorted(set(hs))
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
        hs = [int(x) for x in (src.get("hidden_takes") or [])
              if isinstance(x, (int, float)) and 0 <= int(x) < 10 ** 6]
        if hs:
            c["hidden_takes"] = sorted(set(hs))
        height = int(_num(src.get("height"), 0, 0, 4000))
        if height:
            c["height"] = height
    if src.get("mute"):
        c["mute"] = True
    if src.get("runon"):
        c["runon"] = True
    # tags travel with a copy — but landing in another project they may collide
    # with an anchor already there; the duplicate-tag chip is what says so
    tags = clean_tags(src.get("tags"))
    if tags:
        c["tags"] = tags
    # so does a card's condition: "plays only when" is part of what was copied
    w = clean_when(src.get("when"))
    if w:
        c["when"] = w
    # and its subtitle — the shown words belong to the card like the spoken ones
    sub = str(src.get("sub") or "")[:500]
    if sub:
        c["sub"] = sub
    for k in ("tw", "twsfx"):          # the card's typewriter word travels too
        if src.get(k) is not None:
            c[k] = 1 if src[k] else 0
    if src.get("chain"):
        c["chain"] = True
    label = str(src.get("label") or "")[:80]
    if label:
        c["label"] = label
    if src.get("locked"):
        c["locked"] = True
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


# ── media ───────────────────────────────────────────────────────────────
# Pictures and film for visual cards. Stored exactly as they arrive — no
# ffmpeg, no transcode — because the browser shows these formats natively and,
# unlike audio, no mixdown ever needs them at one common rate. A visual card
# takes no time on the audio timeline: it is a mark the stage and the exports
# read, and the sound is not one sample different for it being there.
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VID_EXT = (".mp4", ".webm", ".mov")
MEDIA_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
              ".webm": "video/webm", ".mov": "video/quicktime"}


def media_file(name):
    for ext in IMG_EXT + VID_EXT:
        p = MEDIA / f"{name}{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(f"no media '{name}'")


def media_kind(p):
    return "video" if p.suffix.lower() in VID_EXT else "image"


def media_list():
    if not MEDIA.is_dir():
        return []
    return sorted(({"name": p.stem, "kind": media_kind(p),
                    "added": round(p.stat().st_mtime),
                    "size": p.stat().st_size,
                    "ext": p.suffix.lower().lstrip(".")}
                   for p in MEDIA.iterdir()
                   if p.suffix.lower() in IMG_EXT + VID_EXT
                   and not p.name.startswith(".")),
                  key=lambda m: m["name"])


def media_of(doc):
    """The media names a project's visual cards point at.

    What the STORY needs, and no more: the web export shows exactly these,
    so it must not be widened. A whole COPY of a project needs more than a
    story does, and that is the next two functions' business."""
    return {c["media"] for c in doc["chunks"]
            if c.get("type") == "visual" and c.get("media")}


def media_refs_of(doc):
    """Pool images a card was painted AGAINST: its style references.

    Nobody ever sees one, which is exactly why they went missing from the
    archive. But a project that loses its references can no longer paint in
    its own style, so a copy that drops them is not a copy of the project.
    Only POOL names belong here: an @entity ref names a cast plate, which
    is cast_of's business (CAST.md §8), not the pool closure's. The story's
    style tier can name pool pictures too, and those are equally something
    a copy was painted against."""
    out = set()
    for c in doc["chunks"]:
        out |= {r for r in ref_list(c.get("ref")) if not r.startswith("@")}
    out |= {r for r in ref_list((doc.get("style") or {}).get("refs"))
            if not r.startswith("@")}
    return out


def cast_of(doc):
    """The cast members a project's cards reach for, and the plates they
    name. A copy that loses these can no longer paint in the story's own
    style, which is the same reason media_refs_of exists. The story's style
    tier counts too — its refs travel inside the doc. The SHELF's tier does
    not: a story stands alone, and the shelf record it sat on stays home."""
    refs = []
    for c in doc["chunks"]:
        refs += ref_list(c.get("ref"))
    refs += ref_list((doc.get("style") or {}).get("refs"))
    return {r[1:].partition("/")[0] for r in refs if r.startswith("@")}


def media_history_of(doc):
    """The variants a card generated and did not keep.

    Packed when they are on disk and not mourned when they are not: a lost
    rejected take is lost history, not a broken project. That is the whole
    difference between this and the two above, and it is why the archive
    reports a missing picture or reference and stays quiet about these."""
    out = set()
    for c in doc["chunks"]:
        for g in c.get("gen") or []:
            g = re.sub(r"[^a-z0-9_-]", "", str(g or ""))
            if g:
                out.add(g)
    return out


# Nanobanana — Gemini's image model, the studio's illustrator on demand. The
# key is the author's own (aistudio.google.com) and lives beside the library
# rather than in the environment, because both launches — classic and desktop
# app — see the library, and only one of them sees a shell. Read fresh on
# every call, so pasting a key in works without a restart.
GEMINI_KEY_FILE = ROOT / "gemini_key.txt"
NB_MODEL = os.environ.get("SAGA_IMAGE_MODEL", "gemini-2.5-flash-image")
NB_ASPECTS = ("16:9", "1:1", "9:16", "4:3", "3:4", "21:9")


def gemini_key():
    k = os.environ.get("GEMINI_API_KEY", "")
    if not k:
        try:
            k = GEMINI_KEY_FILE.read_text(encoding="utf-8")
        except OSError:
            return ""
    return k.strip()


def image_ready():
    """Whether generate_media can paint: a key for nanobanana, or Draw Things
    chosen (whether its server is actually up is only knowable by asking,
    which the first painting does)."""
    st = settings()["image"]
    if st["provider"] == "drawthings":
        return True
    return bool(st["key"] or gemini_key())


def _labelled_prompt(prompt, members):
    """The words half of CAST.md §4: each referenced member's brief goes in
    as a sentence beside the plates that show it, in a FIXED order — style,
    then cast, then setting, then the shot — because consistent ordering is
    itself a consistency lever with these models. No members, no dressing:
    the prompt goes as it always went."""
    heads = {"style": "Style", "character": "Cast", "location": "Setting"}
    rank = {"style": 0, "character": 1, "location": 2}
    lines = []
    for m in sorted(members, key=lambda m: rank.get(m.get("kind") or "", 3)):
        brief = (m.get("brief") or "").strip().rstrip(".")
        if not brief:
            continue
        kind = m.get("kind") or ""
        head = heads.get(kind, (kind or "reference").capitalize())
        title = (m.get("title") or "").strip()
        lines.append(f"{head}: {brief}." if kind == "style" or not title
                     else f"{head}: {title}, {brief}.")
    if not lines:
        return prompt
    return "\n".join(lines) + f"\n\nShot: {prompt}"


def _paint_image(text, aspect, refs):
    """One painting, whoever holds the brush: `refs` are (path, label) pairs
    already resolved upstream. Returns (bytes, ext)."""
    st = settings()["image"]
    if st["provider"] == "drawthings":
        return _paint_drawthings(text, aspect, refs, st["url"])
    return _paint_nanobanana(text, aspect, refs, st["key"] or gemini_key())


def cast_paint(slug, prompt, plate="", stem="", file=""):
    """Paint inside the board (CAST.md §7d): the member's own plates as
    references — the selected plate first, so a local painter's canvas is
    the one being varied, then the key — and its brief as words, assembled
    exactly as a card's painting is. Anything less and the drift simply
    moves up a level: a turnaround that does not match the portrait it came
    from. What comes back lands as a CANDIDATE in the member's folder,
    never a plate: canon is chosen, not accumulated (§7c). A member with no
    plates yet paints from the brief alone — that is how one is
    bootstrapped from nothing.

    `file` targets a CANDIDATE instead, and then the candidate is the only
    picture sent: pulling the key in beside it would drag the paint back
    toward the very look the candidate may be escaping. Returns the new
    candidate's file name."""
    if not prompt.strip():
        raise ValueError("an empty prompt paints nothing")
    m = cast().get(slug)
    if m is None:
        raise ValueError("no such cast member")
    plates = m.get("plates") or {}
    if plate and plate not in plates:
        raise ValueError(f'no plate "{plate}" to paint against')
    rr, refs = [], []
    if file:
        if file not in (m.get("candidates") or []):
            raise ValueError("no such candidate to paint against")
        cf = CAST / slug / file
        if not cf.is_file():
            raise ValueError("the candidate's file is missing from disk")
        kind = (m.get("kind") or "reference").capitalize()
        rr.append((cf, f'{kind} reference ({m.get("title") or slug}, '
                       'candidate)'))
    else:
        if plate:
            refs.append(f"@{slug}/{plate}")
        if plates:
            k = m.get("key") or next(iter(plates))
            if k != plate:
                refs.append(f"@{slug}/{k}")
    # §7d in full: the collection's style rides too, named by the member's
    # own scope — the board still reads no DOC. Without this, a member's
    # FIRST plate is painted with no Style line at all, comes out in the
    # model's own taste, becomes the key, and then every later plate is
    # painted against it: the style drift moves up a level and calcifies.
    # (Found by Musti the cat pirate, who came out Pixar in a flat world.)
    st = (series().get(m.get("scope") or "") or {}).get("style") or {}
    stext = str(st.get("text") or "").strip()
    for r in ref_list(st.get("refs")):
        if r not in refs:
            refs.append(r)
    rr += [resolve_ref(r) for r in refs]
    text = _labelled_prompt(prompt,
                            ([{"kind": "style", "brief": stext}] if stext
                             else []) + [m])
    img, ext = _paint_image(text, "16:9", rr)
    stem = (re.sub(r"[^a-z0-9_-]+", "-", (stem or "candidate").lower())
            .strip("-")[:40] or "candidate")
    folder = CAST / slug
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".plate-", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(img)
        w = webp_still(Path(tmp))
        if w:
            Path(tmp).unlink(missing_ok=True)
            tmp, ext = str(w), ".webp"
        # the slow painting ran lockless; only this read-modify-write of the
        # registry takes the lock, like any other json touch
        with _docmut:
            reg = cast()
            m = reg.get(slug)
            if m is None:
                raise ValueError("the member went away mid-painting")
            taken = {p.stem for p in folder.iterdir() if p.is_file()}
            fname = _free_name(taken, stem, "new") + ext
            dest = folder / fname
            os.replace(tmp, dest)
            os.chmod(dest, 0o644)
            m.setdefault("candidates", []).append(fname)
            # the board's paints remember their words too (the member-side
            # twin of file_paint_history): each candidate keeps the prompt
            # and the target it was painted against, keyed by its file name
            # — which survives acceptance into a slot, so a PLATE also
            # knows the prompt that painted it. A spelling hunt (a logo,
            # a sign) stops losing its words between rolls.
            entry = {"f": fname, "at": int(time.time()),
                     "prompt": prompt[:2000]}
            if file:
                entry["vary"] = file
            elif plate:
                entry["plate"] = plate
            hist = [e for e in (m.get("hist") or []) if isinstance(e, dict)]
            hist.append(entry)
            m["hist"] = hist[-24:]
            save_cast(reg)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return fname


def generate_media(prompt, stem="", aspect="", ref="", style=None, vary=""):
    """Ask the chosen illustrator for a picture and file it in the media pool.

    Every generation is new bytes, so the upload route's dedupe has nothing
    to say here; the never-overwrite rule holds exactly as it does there —
    media is global, and replacing a name would change every episode showing
    it. 16:9 unless asked otherwise: the stage and the animatic export are
    widescreen, and a square picture would sit pillarboxed between them.
    `ref` is one name or a list: pool pictures as ever, and `@member` or
    `@member/plate` for cast plates (CAST.md §3). Each resolves to an image
    WITH a label saying what it is for, and each @member also brings its
    brief along as words — a picture and a sentence agreeing beat either
    alone. `style` is style_of's (texts, refs) — the tiers above the card,
    composed by the caller because only the caller knows the card said no.
    `vary` names a pool picture to paint a VARIANT of: it rides FIRST —
    the one canvas Draw Things paints over, the head of nanobanana's
    gallery — behind a label saying it is the one being varied, so a
    repaint keeps the picture it starts from instead of wandering.
    Returns the name the pool filed it under."""
    if not prompt.strip():
        raise ValueError("an empty prompt paints nothing")
    aspect = aspect or "16:9"
    if aspect not in NB_ASPECTS:
        raise ValueError(f"aspect must be one of: {', '.join(NB_ASPECTS)}")
    items = ref_list(ref)
    stexts = []
    if style:
        stexts = [str(t).strip() for t in style[0] if str(t).strip()]
        # tier refs ride AFTER the card's own: a local painter's canvas
        # stays the subject, and §4's example sends the style board last
        for r in ref_list(style[1]):
            if r not in items:
                items.append(r)
    vary = re.sub(r"[^a-z0-9_-]", "", str(vary or ""))
    if vary:
        items = [r for r in items if r != vary]
    refs = [resolve_ref(r) for r in items]
    if vary:
        refs.insert(0, (_ref_image(vary),
                        "The picture being varied: keep everything "
                        "the prompt does not change"))
    reg, members, seen = cast(), [], set()
    # the tiers' words arrive as style members, broadest first, so the
    # Style lines stand where §4 fixes them — before cast, setting, shot
    members += [{"kind": "style", "brief": t} for t in stexts]
    for r in items:
        slug = r[1:].partition("/")[0] if r.startswith("@") else ""
        if slug and slug not in seen and slug in reg:
            seen.add(slug)
            members.append(reg[slug])
    text = _labelled_prompt(prompt, members)
    img, ext = _paint_image(text, aspect, refs)
    stem = re.sub(r"[^a-z0-9_-]+", "-",
                  (stem or "art").lower()).strip("-")[:40] or "art"
    MEDIA.mkdir(parents=True, exist_ok=True)
    taken = {p.stem for p in MEDIA.iterdir() if p.is_file()}
    name = _free_name(taken, stem, "new")
    fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".media-", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(img)
        dest = MEDIA / f"{name}{ext}"
        os.replace(tmp, dest)              # same filesystem: tmp lives in ROOT
        os.chmod(dest, 0o644)              # mkstemp makes it 0600
        w = webp_still(dest)               # painted stills arrive pressed
        if w:
            os.chmod(w, 0o644)
            dest.unlink()
    finally:
        Path(tmp).unlink(missing_ok=True)
    return name


# ── keeping current ─────────────────────────────────────────────────────
def _git_dir():
    """The checkout this studio runs from, or None when it is a packaged
    copy. A dev tree is updated by fast-forward, never by overwriting: the
    author may have work in it, and clobbering that would be unforgivable."""
    for d in (HERE, HERE.parent):
        if (d / ".git").exists() and shutil.which("git"):
            return HERE
    return None


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=str(cwd or HERE),
                          capture_output=True, text=True, timeout=120)


def version_now():
    """What is running: the commit it came from, however it got here. A
    checkout knows from git; a packaged payload knows from the version.json
    an update wrote beside it. Neither knowing is fine — then anything
    upstream counts as newer, which is the safe way to be wrong."""
    if _git_dir():
        r = _git("log", "-1", "--format=%H%n%ct%n%s")
        if not r.returncode:
            sha, ts, subject = (r.stdout.strip().split("\n", 2) + ["", "", ""])[:3]
            dirty = bool(_git("status", "--porcelain").stdout.strip())
            return {"sha": sha, "at": int(ts or 0), "subject": subject,
                    "how": "git", "dirty": dirty}
        return {"sha": "", "at": 0, "subject": "", "how": "git", "dirty": False}
    try:
        v = json.loads((APP_DIR / "version.json").read_text(encoding="utf-8"))
        return {"sha": str(v.get("sha") or ""), "at": int(v.get("at") or 0),
                "subject": str(v.get("subject") or ""), "how": "payload",
                "dirty": False}
    except (OSError, ValueError):
        return {"sha": "", "at": 0, "subject": "", "how": "payload",
                "dirty": False}


def version_latest():
    """What GitHub has on the branch. Public repo, so no token and no
    ceremony — one small JSON over TLS."""
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{APP_REPO}/commits/{APP_BRANCH}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "SagaStudio"})
    with urllib.request.urlopen(req, timeout=20) as r:
        c = json.loads(r.read().decode("utf-8", "replace"))
    when = c.get("commit", {}).get("committer", {}).get("date") or ""
    try:
        at = int(time.mktime(time.strptime(when, "%Y-%m-%dT%H:%M:%SZ"))
                 - time.timezone)
    except ValueError:
        at = 0
    return {"sha": c.get("sha") or "",
            "subject": (c.get("commit", {}).get("message") or "").split("\n")[0],
            "at": at}


def update_check():
    """Is there a newer studio? Never raises at the caller: an offline finca
    is a normal Tuesday, and a failed check must not colour anything red."""
    cur = version_now()
    out = {"current": cur, "how": cur["how"], "behind": False, "latest": None}
    if cur["how"] == "git" and cur["dirty"]:
        out["blocked"] = ("this checkout has uncommitted changes — commit or "
                          "stash them and the update can fast-forward")
    try:
        latest = version_latest()
    except Exception as ex:
        out["error"] = f"could not ask GitHub — {ex}"
        return out
    out["latest"] = latest
    # A different sha upstream is only NEWER if it is also later in time —
    # otherwise a machine running an unpushed commit would be told to
    # "update" backwards onto what it already improved on.
    out["behind"] = bool(latest["sha"] and latest["sha"] != cur["sha"]
                         and latest["at"] >= cur["at"])
    return out


def _safe_members(tar):
    """Only the payload, only from inside the archive's own folder. A
    tarball is a stranger's data: no absolute paths, no .., no links, no
    names this program did not ask for."""
    for m in tar.getmembers():
        if not m.isfile():
            continue
        parts = Path(m.name).parts
        if len(parts) < 2 or ".." in parts:
            continue
        rel = "/".join(parts[1:])          # drop the repo-sha top folder
        if rel in PAYLOAD:
            yield rel, m


def update_apply():
    """Fetch and install. Returns a sentence for the author to read.

    A checkout fast-forwards (--ff-only, so a diverged tree is refused
    rather than mangled). A packaged copy downloads the branch tarball and
    writes only PAYLOAD names into APP_DIR, keeping the previous copy
    beside it as .prev so a bad update is one folder-rename from undone."""
    import urllib.request
    cur = version_now()
    if cur["how"] == "git":
        if cur["dirty"]:
            raise RuntimeError("this checkout has uncommitted changes — "
                               "commit or stash them first")
        r = _git("pull", "--ff-only")
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300]
                               or "git pull failed")
        now = version_now()
        return f"updated to “{now['subject']}”", now
    latest = version_latest()
    url = f"https://github.com/{APP_REPO}/archive/{latest['sha']}.tar.gz"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=APP_DIR, prefix=".update-", suffix=".tgz")
    os.close(fd)
    stage = APP_DIR / ".update-stage"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SagaStudio"})
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        stage.mkdir(parents=True)
        got = 0
        with tarfile.open(tmp) as tar:
            for rel, m in _safe_members(tar):
                src = tar.extractfile(m)
                if src is None:
                    continue
                with src, open(stage / rel, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                got += 1
        if not got or not (stage / "studio.py").exists():
            raise RuntimeError("that download carried no studio — nothing changed")
        (stage / "version.json").write_text(json.dumps(
            {"sha": latest["sha"], "at": latest["at"],
             "subject": latest["subject"]}), encoding="utf-8")
        # keep the outgoing copy whole, then move the new one in file by file
        prev = APP_DIR / ".prev"
        shutil.rmtree(prev, ignore_errors=True)
        prev.mkdir(parents=True)
        for name in PAYLOAD + ("version.json",):
            if (APP_DIR / name).exists():
                shutil.copy2(APP_DIR / name, prev / name)
        for f in sorted(stage.iterdir()):
            os.replace(f, APP_DIR / f.name)   # same filesystem: atomic each
        return f"updated to “{latest['subject']}”", version_now()
    finally:
        Path(tmp).unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)


def webp_still(src):
    """Press one still image into WebP beside src; the .webp path comes back
    when it is genuinely smaller, else None and src stands untouched.

    ffmpeg does the pressing (quality 82 — visually clean at a fraction of a
    PNG's weight, alpha kept). GIFs are left alone: they may animate, and
    this would keep only their first frame. No ffmpeg, no pressing — the
    pool still works, it is just heavier."""
    src = Path(src)
    if src.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return None
    if not shutil.which("ffmpeg"):
        return None
    tmp = src.with_name("." + src.stem + ".press.webp")
    r = subprocess.run(["ffmpeg", "-y", "-i", str(src), "-c:v", "libwebp",
                        "-quality", "82", str(tmp)], capture_output=True)
    if r.returncode or not tmp.exists() or not tmp.stat().st_size \
            or tmp.stat().st_size >= src.stat().st_size:
        tmp.unlink(missing_ok=True)
        return None
    out = src.with_suffix(".webp")
    os.replace(tmp, out)
    return out


def _ref_image(ref):
    """A reference must be a picture already in the pool, never film."""
    try:
        rf = media_file(ref)
    except FileNotFoundError:
        raise ValueError(f'no media "{ref}" to use as a reference')
    if rf.suffix.lower() not in IMG_EXT:
        raise ValueError("only a picture can be a reference — not film")
    return rf


def resolve_ref(item):
    """A ref item to (path, label). A bare name is a pool picture and labels
    itself; an @entity is a plate and says what it is FOR, which is the half
    the model was never told. The only place that knows what an @ means."""
    if not item.startswith("@"):
        return _ref_image(item), "Reference"
    slug, _, plate = item[1:].partition("/")
    e = cast().get(slug)
    if not e:
        raise ValueError(f'no cast member "{slug}"')
    plate = plate or e.get("key") or next(iter(e.get("plates") or {}), "")
    p = e.get("plates", {}).get(plate)
    if not p:
        raise ValueError(f'"{slug}" has no plate "{plate}"')
    kind = (e.get("kind") or "reference").capitalize()
    return CAST / slug / p["file"], \
        f'{kind} reference ({e.get("title") or slug}, {plate})'


def ref_list(v):
    """The reference field in every shape it has worn: absent, one name, or
    a list of them. A bare name is a pool picture, exactly as ever; `@slug`
    is a cast member's key plate and `@slug/plate` an exact plate. Held to
    REF_RE, empties and doubles dropped, order kept — the first reference
    is the one a single-image painter gets."""
    vs = v if isinstance(v, (list, tuple)) else [v]
    out = []
    for x in vs:
        x = re.sub(r"[^a-z0-9_@/-]", "", str(x or ""))
        if x and REF_RE.match(x) and x not in out:
            out.append(x)
    return out


def ref_store(vs):
    """How the card carries its references: nothing, the bare name, or the
    list. The bare-name form is what every doc written before lists looked
    like, so old and new stay interchangeable both ways."""
    return vs if len(vs) > 1 else (vs[0] if vs else "")


# Draw Things paints on this machine for free, through the A1111-compatible
# HTTP API it serves when its "API Server" switch is on. Sizes rather than
# ratios, because that is the shape its API takes; these are the SDXL-native
# resolutions nearest each of the studio's aspects.
DT_SIZES = {"16:9": (1344, 768), "1:1": (1024, 1024), "9:16": (768, 1344),
            "4:3": (1152, 896), "3:4": (896, 1152), "21:9": (1536, 640)}


def _paint_drawthings(prompt, aspect, refs, url):
    import base64
    import urllib.request, urllib.error
    base = (url or "http://127.0.0.1:7860").rstrip("/")
    w, h = DT_SIZES[aspect]
    body = {"prompt": prompt, "width": w, "height": h}
    route = "/sdapi/v1/txt2img"
    if refs:
        # img2img with the reference underneath: it keeps the bones of the
        # picture and repaints the skin, the local cousin of a style match.
        # One canvas only — img2img paints over a single image, so of many
        # references the FIRST is the one that goes under the brush. It
        # cannot take a gallery; that is a limit of the local painter, not
        # of the design — the labelled text still rides in the prompt.
        route = "/sdapi/v1/img2img"
        body["init_images"] = [base64.b64encode(refs[0][0]
                                                .read_bytes()).decode()]
        body["denoising_strength"] = 0.65
    req = urllib.request.Request(base + route,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        # local diffusion takes what it takes; a big model on a quiet
        # machine can want minutes
        with urllib.request.urlopen(req, timeout=590) as r:
            got = json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"Draw Things said {ex.code}. Is its API server "
                           "switched on? (Draw Things settings, API Server)")
    except urllib.error.URLError as ex:
        raise RuntimeError(f"could not reach Draw Things at {base} "
                           f"({ex.reason}). Start the app and switch on its "
                           "HTTP API server, or fix the address in Settings.")
    imgs = got.get("images") or []
    if not imgs:
        raise RuntimeError("Draw Things sent no image back")
    return base64.b64decode(imgs[0].split(",")[-1]), ".png"


def _paint_nanobanana(prompt, aspect, refs, key):
    import base64
    import urllib.request, urllib.error
    if not key:
        raise RuntimeError("no Gemini API key. Paste one from "
                           "aistudio.google.com into the Settings tab.")
    parts = []
    # a described gallery, not an anonymous pile: the model takes a whole
    # gallery of references — a face from one, a palette from another — and
    # each one now arrives BEHIND a line saying what it is for. Sending four
    # references without saying which is which is how you get blending
    # (CAST.md §0 cause 4); the label is what actually kills the drift.
    for path, label in refs:
        parts.append({"text": f"{label}:"})
        parts.append({"inlineData": {
            "mimeType": MEDIA_MIME.get(path.suffix.lower(), "image/png"),
            "data": base64.b64encode(path.read_bytes()).decode()}})
    parts.append({"text": prompt})
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"imageConfig": {"aspectRatio": aspect}},
    }).encode()
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{NB_MODEL}:generateContent",
        data=body, headers={"Content-Type": "application/json",
                            "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=110) as r:
            got = json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        try:
            msg = json.loads(ex.read().decode())["error"]["message"]
        except Exception:
            msg = f"the image service said {ex.code}"
        raise RuntimeError(str(msg)[:300])
    except urllib.error.URLError as ex:
        raise RuntimeError(f"could not reach the image service: {ex.reason}")
    parts = ((got.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    data = next((p["inlineData"] for p in parts if "inlineData" in p), None)
    if data is None:
        # a refusal arrives as prose (or a bare block reason) — pass it on,
        # it says what to rephrase
        why = (next((p["text"] for p in parts if p.get("text")), "")
               or (got.get("promptFeedback") or {}).get("blockReason")
               or "no image came back")
        raise RuntimeError(str(why)[:300])
    return base64.b64decode(data["data"]), {
        "image/png": ".png", "image/jpeg": ".jpg",
        "image/webp": ".webp"}.get(data.get("mimeType"), ".png")


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


def normalize_groups(doc):
    """A group is still a contiguous run of cards sharing a name — but its bar
    is now a real card (type "group"), so it can carry anchor tags and can
    stand with no members at all. This keeps bars and runs in step whatever
    just happened: every run gets exactly one bar, seated directly above its
    first member; a bar whose members have gone keeps its place as an empty
    group; docs from before bars existed grow theirs here, which is the whole
    migration. Idempotent, and cheap enough to run on every load and save."""
    ch = doc.get("chunks") or []
    bars, order = {}, []
    for c in ch:
        if c.get("type") == "group":
            g = re.sub(r"[\"'`\\<>&]", "",
                       str(c.get("gname") or "")).strip()[:60] or "Group"
            if g in bars:                     # a paste's double: number it
                k = 2
                while f"{g} ({k})" in bars:
                    k += 1
                g = f"{g} ({k})"
            c["gname"] = g
            bars[g] = c
        order.append(c)
    member_names = {c.get("group") for c in order
                    if c.get("type") != "group" and c.get("group")}
    out, placed, prev, cur = [], set(), None, None
    for c in order:
        if c.get("type") == "group":
            # a bar with members is re-seated at its run, below; an empty
            # one keeps the place it holds itself
            if c["gname"] not in member_names:
                out.append(c)
            prev = None
            continue
        g = c.get("group") or None
        if g != prev:
            cur = None
            if g:
                gg = g
                if gg in placed:              # a second run under one name
                    k = 2
                    while f"{gg} ({k})" in placed or f"{gg} ({k})" in bars:
                        k += 1
                    gg = f"{gg} ({k})"
                bar = bars.get(gg) or {"id": 0, "type": "group",
                                       "gname": gg, "note": ""}
                bar["gname"] = gg
                out.append(bar)
                placed.add(gg)
                cur = gg
        if g:
            c["group"] = cur
        out.append(c)
        prev = g
    doc["chunks"] = out
    for i, c in enumerate(out):
        c["id"] = i


def group_exists(ch, g, skip=None):
    """Is `g` a group here — a run of members, or an empty group's bar?
    Joining an empty group must count, or its bar could never take a card."""
    return any((x.get("group") == g
                or (x.get("type") == "group" and x.get("gname") == g))
               for x in ch if x is not skip)


def load(name):
    f = pdir(name) / "doc.json"
    if not f.exists():
        return None
    doc = json.loads(f.read_text())
    normalize_groups(doc)
    return doc


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
    normalize_groups(doc)     # bars follow their runs through every mutation
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
                        # the sidebar badges drafts and the discuss agent's
                        # overview marks them; both read it from here
                        "draft": bool(doc.get("draft")),
                        "draft_of": doc.get("draft_of") or "",
                        "words": sum(len(c["text"].split())
                                     for c in doc["chunks"] if is_speech(c))})
    return out


def series():
    """Every shelf, by slug. Missing or unreadable reads as none, so a library
    with no series.json behaves exactly as one did before shelves existed."""
    if not SERIES.exists():
        return {}
    try:
        s = json.loads(SERIES.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return s if isinstance(s, dict) else {}


def save_series(s):
    SERIES.write_text(json.dumps(s, indent=1))


def series_slug(title, taken, fallback="series"):
    """Accents are folded rather than replaced, so "Cardon Poems" is what
    `Cardón Poems` becomes and not `card-n-poems`. This slug is the shelf's
    name on disk and, one day, its address on darkride, and a Canarian title
    should not have to spell itself in ASCII to get a decent one. The cast
    borrows it (fallback="member"): same alphabet, same manners."""
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9_-]+", "-", t.lower()).strip("-")[:60]
    slug, k = (base or fallback), 2
    while slug in taken:
        slug, k = f"{base or fallback}-{k}", k + 1
    return slug


def _clean(v, cap):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(v or "")).strip()[:cap]


def series_state():
    """What the sidebar draws: every shelf, and the members that answer to a
    real project, in the order the author put them.

    A name no project answers to is passed OVER rather than struck out.
    Deleting a story should not quietly rewrite a shelf, and a story restored
    from a backup then walks straight back into the place it held. A story
    sits on one shelf only, and only a hand-edited file could say otherwise —
    but if one does, the claim is settled in the ORDER THE SHELVES WERE MADE,
    never in the order they happen to be displayed. Sorting for display is a
    view; it must not be able to move a story from one shelf to another, which
    is exactly what deduping down the sorted list would have done the moment a
    shelf was renamed."""
    recs = series()
    have = {d.name for d in ROOT.iterdir() if (d / "doc.json").exists()}
    claim = {}
    for slug in recs:
        for n in recs[slug].get("order") or []:
            claim.setdefault(n, slug)
    out = []
    for slug in sorted(recs, key=lambda k: str(recs[k].get("title") or k).lower()):
        rec = recs[slug]
        mem, seen = [], set()
        for n in rec.get("order") or []:
            if n in have and n not in seen and claim.get(n) == slug:
                seen.add(n)
                mem.append(n)
        out.append({"slug": slug, "title": rec.get("title") or slug,
                    # what this shelf calls itself and what it calls its parts:
                    # Saga is a serial of episodes, the poems are a collection
                    # of poems, and darkride's pages should say so
                    "noun": rec.get("noun") or "series",
                    "member": rec.get("member") or "episode",
                    "blurb": rec.get("blurb") or "", "cover": rec.get("cover") or "",
                    # the shelf's picture style (CAST.md §5) — the editor
                    # shows it on every card it dresses, visible never silent
                    "style": rec.get("style") or None,
                    "order": mem, "created": rec.get("created") or ""})
    return out


def series_of(name):
    """Which shelf a story sits on, by the same claim rule series_state uses:
    the shelf MADE first wins. Derived, never stored, and a story still knows
    nothing about where it is shelved."""
    for slug, rec in series().items():
        if name in (rec.get("order") or []):
            return slug
    return ""


def series_new(title):
    t = _clean(title, 80)
    if not t:
        raise ValueError("a title is needed")
    recs = series()
    slug = series_slug(t, recs)
    recs[slug] = {"title": t, "noun": "series", "member": "episode",
                  "blurb": "", "cover": "", "order": [],
                  "created": time.strftime("%Y-%m-%d %H:%M")}
    save_series(recs)
    return slug


def series_assign(name, to, at=None):
    """Move a story onto a shelf, to another place on the shelf it is already
    on, or off shelves entirely (`to` None).

    One shelf per story, so joining one is also leaving the other. `at` is an
    index into the shelf WITHOUT this story: that is what makes dragging a
    story one slot down land one slot down instead of back where it started.
    Nothing here touches the story itself; a shelf move writes one file."""
    recs = series()
    if to is not None and to not in recs:
        raise KeyError(to)
    for rec in recs.values():
        rec["order"] = [n for n in (rec.get("order") or []) if n != name]
    if to is not None:
        o = recs[to]["order"]
        if at is None:
            o.append(name)
        else:
            # `at` counts the shelf AS DRAWN, and what is drawn is the
            # filtered view: a deleted story's name still holds its place in
            # the file but is not on screen. Counting into the raw list would
            # then land the story a slot or two from where it was pointed at,
            # once for every ghost above it. So translate: find the shown
            # story that is to sit below this one, and take its raw seat.
            have = {d.name for d in ROOT.iterdir() if (d / "doc.json").exists()}
            claim = {}
            for slug in recs:
                for n in recs[slug]["order"]:
                    claim.setdefault(n, slug)
            shown = [n for n in o if n in have and claim.get(n) == to]
            at = max(0, min(int(at), len(shown)))
            o.insert(len(o) if at == len(shown) else o.index(shown[at]), name)
    save_series(recs)


def cast():
    """Every cast member, by slug. Missing or unreadable reads as empty, so a
    library that has never heard of the cast behaves exactly as one did
    before it existed — same manners as series() and for the same reason."""
    if not CAST_FILE.exists():
        return {}
    try:
        c = json.loads(CAST_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return c if isinstance(c, dict) else {}


def save_cast(c):
    CAST_FILE.write_text(json.dumps(c, indent=1))


def style_of(doc):
    """The style tiers above a card (CAST.md §5), composed and never chosen:
    the shelf's style, then the story's. Returns (texts, refs) — texts
    broadest first, since that is the order the prompt states them in; refs
    in the same order, deduped. The card's own tier is its note, as ever,
    and a card opts out with `nostyle`, which is its caller's business."""
    texts, refs = [], []
    for st in ((series().get(series_of(doc.get("name") or "")) or {})
               .get("style") or {},
               doc.get("style") or {}):
        t = str(st.get("text") or "").strip()
        if t:
            texts.append(t)
        for r in ref_list(st.get("refs")):
            if r not in refs:
                refs.append(r)
    return texts, refs


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
#   cast.json                 only the cast members those projects reach for
#   cast/<slug>/<file>        their plates — reference artwork, never shown
#   takes/<sha>.wav           the performances voiced cards are driven by
#   audio/<hash>.wav          rendered chunks — optional, and nearly all the size
#
# The manifest is written first so that reading "what is in here?" does not
# mean decompressing a few hundred megabytes of audio to reach the last member.
# The assembled mp3 in out/ is deliberately left out: it is derived, it is
# large, and assemble() rebuilds it in seconds from the chunks that are here.
#
# Schema stays 1 across the cast's arrival, deliberately: an older studio's
# allowlist below simply never extracts cast members, so a new archive opens
# there whole-minus-cast instead of being refused outright.
ARCHIVE_SCHEMA = 1

# Extraction allowlist. tar members are attacker-controlled paths in the
# general case, so nothing is unpacked unless its name matches a shape this
# program writes — which rules out absolute paths, "..", symlinks and devices
# without relying on any particular Python version's tarfile filter.
ARC_MEMBER = re.compile(
    r"^(manifest\.json|profiles\.json|cast\.json"
    r"|projects/[a-z0-9_-]{1,60}/(doc\.json|source\.md)"
    r"|voices/[a-z0-9_.-]{1,44}\.(wav|mp3|flac|m4a)"
    r"|clips/[a-z0-9_.-]{1,44}\.wav"
    r"|media/[a-z0-9_.-]{1,44}\.(png|jpe?g|webp|gif|mp4|webm|mov)"
    r"|cast/[a-z0-9_-]{1,60}/[a-z0-9_.-]{1,60}\.(png|jpe?g|webp|gif)"
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
            "clips": {}, "media": {}, "takes": {}, "cast": {}, "unreadable": [],
            "missing_voices": [], "missing_clips": [], "missing_media": [],
            "missing_takes": [], "missing_cast": [], "bytes": 0}
    vnames, pnames, cnames, tnames, audio = set(), set(), set(), set(), {}
    mnames, hnames, castslugs = set(), set(), set()
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
        # what the cards show AND what they were painted against: both are
        # needed for the project to open whole somewhere else
        mnames |= media_of(doc) | media_refs_of(doc)
        hnames |= media_history_of(doc)
        castslugs |= cast_of(doc)
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
    for mn in sorted(mnames):
        # like takes, always packed: media is source material, not a render —
        # an export leaving it out would restore visual cards that show nothing
        try:
            f = media_file(mn)
        except FileNotFoundError:
            plan["missing_media"].append(mn)
            continue
        plan["media"][mn] = {"file": f.name, "sha": sha256_file(f),
                             "bytes": f.stat().st_size}
        plan["bytes"] += f.stat().st_size
    # The variant history rides along, quietly. A rejected take that is no
    # longer on disk is skipped rather than reported: the archive is whole
    # without it, and "missing" is a word that should mean something.
    for hn in sorted(hnames - mnames):
        try:
            f = media_file(hn)
        except FileNotFoundError:
            continue
        plan["media"][hn] = {"file": f.name, "sha": sha256_file(f),
                             "bytes": f.stat().st_size}
        plan["bytes"] += f.stat().st_size
    # The cast the cards reach for (CAST.md §8): each member's record, its
    # plate files reported when missing — a plate is canon, and a backup
    # short one canon picture should say so — and its candidates riding
    # quietly on the history rule: packed when on disk, never mourned.
    reg = cast()
    arc_cast = {}
    for slug in sorted(castslugs):
        m = reg.get(slug)
        if m is None:
            plan["missing_cast"].append(f"@{slug}")
            continue
        arc_cast[slug] = m
        quiet = set(m.get("candidates") or [])
        named = [p.get("file") for p in (m.get("plates") or {}).values()]
        for fn in named + sorted(quiet):
            if not fn or f"{slug}/{fn}" in plan["cast"]:
                continue
            f = CAST / slug / fn
            if not f.is_file():
                if fn not in quiet:
                    plan["missing_cast"].append(f"@{slug}/{fn}")
                continue
            plan["cast"][f"{slug}/{fn}"] = {"file": fn, "sha": sha256_file(f),
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
    plan["_cast"] = arc_cast
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
    arc_cast = plan.pop("_cast", {})
    manifest = dict(plan, audio=sorted(audio))
    with tarfile.open(dest, "w:gz", compresslevel=1 if plan["with_audio"] else 6) as tar:
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
        _add_bytes(tar, "profiles.json", json.dumps(profs, indent=1).encode())
        _add_bytes(tar, "cast.json", json.dumps(arc_cast, indent=1).encode())
        for p in plan["projects"]:
            d = ROOT / p["name"]
            _add_file(tar, d / "doc.json", f"projects/{p['name']}/doc.json")
            if p["source"]:
                _add_file(tar, d / "source.md", f"projects/{p['name']}/source.md")
        for meta in plan["voices"].values():
            _add_file(tar, VOICES / meta["file"], f"voices/{meta['file']}")
        for meta in plan["clips"].values():
            _add_file(tar, CLIPS / meta["file"], f"clips/{meta['file']}")
        for meta in plan["media"].values():
            _add_file(tar, MEDIA / meta["file"], f"media/{meta['file']}")
        for key, meta in plan["cast"].items():
            slug = key.split("/", 1)[0]
            _add_file(tar, CAST / slug / meta["file"],
                      f"cast/{slug}/{meta['file']}")
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
                         "engine", "lang", "speed", "kvoice", "gain", "fx"))


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
           "media": [], "cast": [], "audio": 0, "takes": 0, "skipped": []}
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

        # ── media ── the clips rule again: never overwrite, rename around a
        # collision, and repoint only the arriving projects at the copy. Like
        # a clip and unlike a voice, no hash names media, so a rename costs
        # nothing beyond the pointer.
        MEDIA.mkdir(parents=True, exist_ok=True)
        seen_media = {p.stem for p in MEDIA.iterdir() if p.is_file()}
        mmap = {}
        mdir = tmp / "media"
        for f in sorted(mdir.iterdir()) if mdir.is_dir() else []:
            try:
                local = media_file(f.stem)
            except FileNotFoundError:
                local = None
            if local is not None and sha256_file(local) == sha256_file(f):
                mmap[f.stem] = f.stem
                continue
            new = f.stem if local is None else _free_name(seen_media, f.stem)
            shutil.copy2(f, MEDIA / f"{new}{f.suffix}")
            seen_media.add(new)
            mmap[f.stem] = new
            if new != f.stem:
                rep["media"].append(f"media “{f.stem}” arrived as “{new}” — a "
                                    f"different file already had that name")

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

        # ── the cast ── merged by slug on the same terms as profiles: an
        # existing slug is KEPT, never overwritten (CAST.md §8). Kept means
        # kept whole — the local member's plates stay its plates, and an
        # imported @ref answers to whatever this library says the name
        # means; if that leaves a pinned plate dangling, the card's chip
        # says so in amber rather than anything being clobbered. Files are
        # copied only for slugs that are new, and every name is held to the
        # same alphabets the registry's own writers use.
        try:
            arc_cast = json.loads((tmp / "cast.json").read_text())
        except (OSError, ValueError):
            arc_cast = {}
        if isinstance(arc_cast, dict):
            local_cast = cast()
            added = []
            for slug, m in arc_cast.items():
                if (not isinstance(m, dict) or not CAST_SLUG_RE.match(str(slug))
                        or slug in local_cast):
                    continue
                fn_ok = re.compile(r"^[a-z0-9_.-]{1,60}$")
                m["plates"] = {s: p for s, p in (m.get("plates") or {}).items()
                               if PLATE_RE.match(str(s)) and isinstance(p, dict)
                               and fn_ok.match(str(p.get("file") or ""))}
                m["candidates"] = [f for f in (m.get("candidates") or [])
                                   if fn_ok.match(str(f))]
                if not m["candidates"]:
                    m.pop("candidates")
                local_cast[slug] = m
                added.append(slug)
                src_dir = tmp / "cast" / slug
                if src_dir.is_dir():
                    (CAST / slug).mkdir(parents=True, exist_ok=True)
                    for f in sorted(src_dir.iterdir()):
                        dst = CAST / slug / f.name
                        if f.is_file() and not dst.exists():
                            shutil.copy2(f, dst)
            if added:
                save_cast(local_cast)
                rep["cast"] = [f"@{s}" for s in added]

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
                if c.get("type") == "visual" and c.get("media"):
                    c["media"] = mmap.get(c["media"], c["media"])
                # A renamed picture takes its history and its style
                # references with it. Repointing only the visible one left a
                # card whose variant menu and whose reference chips named
                # files that had arrived under other names.
                if c.get("gen"):
                    c["gen"] = [mmap.get(g, g) for g in c["gen"]]
                if c.get("ref"):
                    rs = [mmap.get(r, r) for r in ref_list(c.get("ref"))]
                    c["ref"] = rs if isinstance(c["ref"], list) else (rs[0] if rs else "")
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
            # the story style's pool refs follow a renamed picture the same
            # way a card's do; its @refs pass through untouched, as ever
            st = doc.get("style") or {}
            if st.get("refs"):
                st["refs"] = [mmap.get(r, r) for r in ref_list(st["refs"])]
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
# The chatterbox worker — the twin of the omnivoice worker below, and for the
# same reason seen from the other side: studio.py itself runs on a small
# interpreter with no torch in it, so the engine that needs torch lives
# behind a process boundary. See chatterbox_server.py.
_cb = {"proc": None, "warm": False}
CB_LOG = ROOT / "chatterbox.log"


def _managed_ready(py):
    """A managed venv counts only once its install finished — the interpreter
    file appears seconds into a forty-minute download, and an engine that is
    two-thirds of a torch must not look installed."""
    if not str(py).startswith(str(ENGINES_DIR)):
        return True
    return (Path(py).parent.parent / ".ready").exists()


def cb_available():
    return (CB_PYTHON.exists() and _managed_ready(CB_PYTHON)
            and (HERE / "chatterbox_server.py").exists())


def _cb_health(timeout=1.0):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CB_PORT}/health",
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_cb(wait=300):
    """Start the Chatterbox worker if it is not up, and wait until it is warm.

    Lazy, like get_ov(): a library that speaks only Kokoro should never pay
    torch's load time or its memory. The child's output goes to a log file
    rather than a pipe — nobody drains a pipe here, and a full one would
    wedge the worker mid-render."""
    if not cb_available():
        raise RuntimeError("Chatterbox is not installed on this machine — "
                           "install it under Voice Engines on the home "
                           "screen, or switch this profile to Kokoro.")
    h = _cb_health()
    if h and h.get("ready"):
        _cb["warm"] = True
        return CB_PORT
    if h is None and (_cb["proc"] is None or _cb["proc"].poll() is not None):
        print("starting the Chatterbox worker …", flush=True)
        log = open(CB_LOG, "ab", buffering=0)
        _cb["proc"] = subprocess.Popen(
            [str(CB_PYTHON), str(HERE / "chatterbox_server.py"),
             "--port", str(CB_PORT)],
            stdout=log, stderr=log, env=_worker_env(CB_PYTHON))
    t0 = time.time()
    while time.time() - t0 < wait:
        h = _cb_health()
        if h and h.get("ready"):
            print("Chatterbox worker warm", flush=True)
            _cb["warm"] = True
            return CB_PORT
        if h and h.get("error"):
            raise RuntimeError(f"Chatterbox failed to load: {h['error']}")
        pr = _cb["proc"]
        if pr is not None and pr.poll() is not None:
            tail = ""
            if CB_LOG.exists():
                tail = CB_LOG.read_text(errors="replace").strip()[-300:]
            raise RuntimeError(f"the Chatterbox worker exited. {tail}")
        time.sleep(0.5)
    raise RuntimeError("the Chatterbox worker did not come up in time")


def _cb_call(route, body):
    """One request to the worker. It writes the wav itself — both processes
    are on this machine, so there is nothing to gain by copying the audio
    back over a socket."""
    import urllib.request, urllib.error
    port = get_cb()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as ex:
        try:
            msg = json.loads(ex.read()).get("error") or str(ex)
        except Exception:
            msg = str(ex)
        raise RuntimeError(f"chatterbox: {msg}") from None


def _cb_gen(spoken, p, seed, dest):
    """One line through the worker, onto disk at dest. The engine's absence
    is checked before the voice clip's: on a machine with neither, "install
    Chatterbox or switch to Kokoro" is the message that actually helps."""
    if not cb_available():
        get_cb()                      # raises the how-to-install message
    return _cb_call("/gen", {"text": spoken,
                             "ref_audio": str(voice_file(p["voice"])),
                             "exag": p["exag"], "cfg": p["cfg"],
                             "temp": p["temp"], "rep": p["rep"],
                             "seed": int(seed or 0), "out": str(dest)})


def _cb_vc(src, voice, seed, dest):
    """One performance through the worker's voice conversion, onto dest."""
    if not cb_available():
        get_cb()                      # raises the how-to-install message
    return _cb_call("/vc", {"src": str(src), "ref_audio": str(voice),
                            "seed": int(seed or 0), "out": str(dest)})


def _nothing_to_say(spoken):
    """True when the text strips to nothing a voice could say — blank, only
    scene marks, zero-width characters, or bare punctuation. The engines
    misbehave on such input: kokoro throws ("need at least one array to
    concatenate"), and chatterbox SPEAKS — its punc_norm substitutes "You
    need to add some text for me to talk." for empty input, and it babbles
    over punctuation alone. \\W is Unicode-aware, so any real letter in any
    script keeps a card speakable; no letters and no digits means a beat
    of silence instead of a voice explaining the blank."""
    return not re.sub(r"[\W_]+", "", spoken)


def _write_silence(dest, secs=0.3, sr=24000):
    """What a card with nothing to say renders to: a beat of silence. A lone
    scene mark or a blank line an import kept is a breath the author drew on
    purpose — or a card they will delete on sight — and neither should stop
    a bake with an engine's stack trace."""
    import numpy as np
    import soundfile as sf
    sf.write(str(dest), np.zeros(int(sr * secs), dtype=np.float32), sr)


def _render_mute(dest):
    """The silence path skips the cache on purpose, overwriting whatever wav
    stands under this hash: one from before the punctuation guard may carry
    chatterbox's spoken complaint about the empty text, and 0.3 seconds of
    zeros costs less to rewrite than that ghost costs to hear again."""
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    try:
        _write_silence(tmp)
        tmp.rename(dest)                   # atomic, as everywhere else here
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def voice_file(name):
    if not name:
        raise FileNotFoundError("this profile has no voice clip yet — add one "
                                "in the profile editor, or switch it to Kokoro")
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


# ── voice conversion ────────────────────────────────────────────────────
# Takes are stored as 16k mono because that is exactly what Chatterbox VC
# consumes — the conversion itself happens in the worker, but the format of
# what is on disk is this file's business: _take_upload writes it.
VC_SR = 16000


def render_voiced(c, doc, force=False):
    """Re-speak a recorded performance in a character's voice.

    VC tokenises the take into speech tokens — which carry the words, the
    timing and the delivery — then renders those through the decoder
    conditioned on the target voice. The performance stays yours; the timbre
    becomes the character's. That is the whole card. The conversion itself —
    the chunking, the seeding, the timeline reassembly — lives in the
    chatterbox worker now, beside the model that does it.

    There are no parameters, because VC has none: it takes two audio files and
    nothing else. A profile contributes its voice here and only its voice —
    exaggeration, cfg, temperature and repetition penalty have no meaning for
    conversion, which is why chunk_hash leaves them out."""
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
    src = take_file(c.get("perf"))
    voice = voice_file(p["voice"])
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    try:
        _cb_vc(src, voice, c.get("seed"), tmp)
        tmp.rename(dest)                   # atomic, as render()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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
    return (OV_PYTHON.exists() and _managed_ready(OV_PYTHON)
            and (HERE / "omnivoice_server.py").exists())


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

    Lazy, like get_cb(): a library that never uses the second engine should
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
            stdout=log, stderr=log, env=_worker_env(OV_PYTHON))
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


# ── installing engines ──────────────────────────────────────────────────
# Kokoro ships with the app; the torch engines are fetched on demand, one
# venv each under ENGINES_DIR, built by whatever interpreter runs this file.
# OmniVoice borrows Chatterbox's torch through a .pth file rather than
# downloading its own copy — they cannot share one venv (transformers pins),
# but they can share the 340 MB that never differs. Installs run in a thread
# and report through /api/engines; one at a time, they would only fight for
# bandwidth. Model weights land in HF_HOME if the user set one, otherwise in
# ENGINES_DIR/hf — never inside the app, never inside the library.
ENGINE_INFO = {
    # `only` names the files from_pretrained actually loads — the repo also
    # carries .pt duplicates and multilingual variants, another ~10 GB that
    # an unfiltered snapshot would pull down for nothing
    "chatterbox": {"weights": "ResembleAI/chatterbox",
                   "only": ["ve.safetensors", "t3_cfg.safetensors",
                            "s3gen.safetensors", "tokenizer.json", "conds.pt"],
                   "packages": ["chatterbox-tts"], "est_gb": 4.5},
    "omnivoice": {"weights": "k2-fsa/OmniVoice", "only": None,
                  "packages": ["omnivoice", "accelerate", "tensorboardx",
                               "webdataset", "psutil"], "est_gb": 3.2},
}
_eng = {"name": None, "stage": "", "log": [], "error": None, "proc": None,
        "cancel": False, "thread": None}


def _hf_home():
    return Path(os.environ.get("HF_HOME") or (ENGINES_DIR / "hf")).expanduser()


def _worker_env(py):
    """What an engine worker is spawned with. A managed install put its
    weights in the managed HF_HOME, so a worker from a managed venv must look
    there too; a classic install keeps whatever the shell had — pointing it
    at the managed cache would quietly re-download gigabytes it already has."""
    env = os.environ.copy()
    if str(py).startswith(str(ENGINES_DIR)):
        env.setdefault("HF_HOME", str(_hf_home()))
    return env


def _uv():
    p = os.environ.get("SAGA_UV") or shutil.which("uv")
    return p if p and Path(p).exists() else None


def _du(path):
    """Bytes under a directory, by walking it — no subprocess, no du.
    Symlinks are counted as links, not as their targets: the HF cache is a
    blobs/ directory plus a snapshots/ tree of symlinks into it, and
    following them would count every model twice."""
    total = 0
    for base, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.stat(os.path.join(base, f),
                                 follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _snapshot_dir(repo):
    return _hf_home() / "hub" / ("models--" + repo.replace("/", "--"))


def _eng_log(line):
    _eng["log"].append(str(line)[-240:])
    del _eng["log"][:-30]


def _eng_step(cmd, env=None):
    """One subprocess of an install: output into the log ring, failure raised.
    The command is what a curious user would have typed themselves — uv, pip,
    python -m venv — never anything a request names."""
    _eng_log("$ " + " ".join(Path(str(c)).name if os.sep in str(c) else str(c)
                             for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace", env=env)
    _eng["proc"] = proc
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _eng_log(line)
    proc.wait()
    _eng["proc"] = None
    if _eng["cancel"]:
        raise RuntimeError("cancelled")
    if proc.returncode:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed "
                           f"(exit {proc.returncode}) — see the log")


def _venv_site(vpy):
    r = subprocess.run([str(vpy), "-c",
                        "import site;print(site.getsitepackages()[0])"],
                       capture_output=True, text=True)
    return Path(r.stdout.strip())


def _pip_env():
    return dict(os.environ, UV_CACHE_DIR=str(ENGINES_DIR / "cache"),
                PIP_DISABLE_PIP_VERSION_CHECK="1")


def _pip_install(vpy, packages, no_deps=False):
    uv = _uv()
    cmd = ([uv, "pip", "install", "--python", str(vpy)] if uv
           else [str(vpy), "-m", "pip", "install"])
    if no_deps:
        cmd.append("--no-deps")
    _eng_step(cmd + list(packages), env=_pip_env())


def _make_venv(name, parent=None):
    """A venv under ENGINES_DIR, --copies so it survives the app moving.
    With `parent`, a .pth file makes the parent's site-packages visible —
    the child's own packages shadow them, which is the whole trick: shared
    torch, private transformers."""
    vdir = ENGINES_DIR / name
    vpy = vdir / "bin" / "python"
    if not vpy.exists():
        ENGINES_DIR.mkdir(parents=True, exist_ok=True)
        _eng_step([sys.executable, "-m", "venv", "--copies", str(vdir)])
    if parent is not None:
        psite = _venv_site(ENGINES_DIR / parent / "bin" / "python")
        (_venv_site(vpy) / "_parent_venv.pth").write_text(str(psite) + "\n")
    return vpy


def _fetch_weights(vpy, repo, label, only=None):
    _eng["stage"] = f"downloading the {label} voice model — the big one"
    env = dict(os.environ, HF_HOME=str(_hf_home()),
               HF_HUB_DISABLE_PROGRESS_BARS="1")
    kw = f", allow_patterns={only!r}" if only else ""
    _eng_step([str(vpy), "-c",
               "from huggingface_hub import snapshot_download; "
               f"snapshot_download({repo!r}{kw})"], env=env)


def _install_chatterbox():
    _eng["stage"] = "building the Chatterbox environment"
    vpy = _make_venv("chatterbox")
    _eng["stage"] = "installing Chatterbox and PyTorch"
    _pip_install(vpy, ENGINE_INFO["chatterbox"]["packages"])
    _fetch_weights(vpy, ENGINE_INFO["chatterbox"]["weights"], "Chatterbox",
                   ENGINE_INFO["chatterbox"]["only"])
    _eng["stage"] = "checking the install"
    _eng_step([str(vpy), "-c", "import chatterbox.tts"])
    (ENGINES_DIR / "chatterbox" / ".ready").write_text(
        time.strftime("%Y-%m-%d %H:%M") + "\n")


def _install_omnivoice():
    # OmniVoice leans on Chatterbox's venv for torch, so that goes in first —
    # which also happens to be the engine most people want anyway.
    if not (ENGINES_DIR / "chatterbox" / ".ready").exists():
        _install_chatterbox()
    _eng["stage"] = "building the OmniVoice environment"
    vpy = _make_venv("omnivoice", parent="chatterbox")
    _eng["stage"] = "installing OmniVoice"
    _pip_install(vpy, ENGINE_INFO["omnivoice"]["packages"], no_deps=True)
    # newer transformers than the parent's pin, shadowing it from the child
    _pip_install(vpy, ["transformers>=5.3"])
    _fetch_weights(vpy, ENGINE_INFO["omnivoice"]["weights"], "OmniVoice")
    _eng["stage"] = "checking the install"
    _eng_step([str(vpy), "-c", "import omnivoice"])
    (ENGINES_DIR / "omnivoice" / ".ready").write_text(
        time.strftime("%Y-%m-%d %H:%M") + "\n")


def install_engine(name):
    if name not in ENGINE_INFO:
        raise ValueError(f"no such engine {name!r}")
    if _eng["thread"] and _eng["thread"].is_alive():
        raise RuntimeError("an engine is already installing — one at a time")

    def run():
        try:
            (_install_chatterbox if name == "chatterbox"
             else _install_omnivoice)()
            _eng.update(stage="done", error=None)
        except Exception as ex:
            _eng["error"] = f"{type(ex).__name__}: {ex}"
            _eng["stage"] = "failed"

    _eng.update(name=name, stage="starting", log=[], error=None, cancel=False)
    _eng["thread"] = threading.Thread(target=run, daemon=True)
    _eng["thread"].start()


def cancel_install():
    _eng["cancel"] = True
    p = _eng["proc"]
    if p is not None:
        try:
            p.terminate()
        except OSError:
            pass


def remove_engine(name):
    """Take a managed engine out again — the venv, and the weights when they
    live in the managed HF_HOME. A user's own HF cache is never touched: the
    manager deletes only what the manager put there."""
    if name not in ENGINE_INFO:
        raise ValueError(f"no such engine {name!r}")
    if _eng["thread"] and _eng["thread"].is_alive():
        raise RuntimeError("an install is running — cancel it first")
    if name == "chatterbox" and (ENGINES_DIR / "omnivoice").exists():
        raise RuntimeError("OmniVoice borrows Chatterbox's torch — "
                           "remove OmniVoice first")
    for proc_state in (_cb, _ov):
        pr = proc_state.get("proc")
        if pr is not None and pr.poll() is None:
            pr.terminate()
    _cb["warm"] = False
    shutil.rmtree(ENGINES_DIR / name, ignore_errors=True)
    if "HF_HOME" not in os.environ:
        shutil.rmtree(_snapshot_dir(ENGINE_INFO[name]["weights"]),
                      ignore_errors=True)


_esize = {}                        # path -> (when, bytes): du is not free


def _sized(path):
    now = time.time()
    hit = _esize.get(str(path))
    if hit and now - hit[0] < 10:
        return hit[1]
    b = _du(path) if Path(path).exists() else 0
    _esize[str(path)] = (now, b)
    return b


def engines_status():
    installing = None
    if _eng["thread"] and _eng["thread"].is_alive():
        # a set of paths, not a list: an omnivoice install may be mid-way
        # through the chatterbox install it depends on, and the chatterbox
        # venv must not be counted twice
        grow = {ENGINES_DIR / _eng["name"], ENGINES_DIR / "chatterbox",
                _snapshot_dir(ENGINE_INFO[_eng["name"]]["weights"])}
        if _eng["name"] == "omnivoice":
            grow.add(_snapshot_dir(ENGINE_INFO["chatterbox"]["weights"]))
        installing = {"engine": _eng["name"], "stage": _eng["stage"],
                      "log": _eng["log"][-8:],
                      "mb": round(sum(_sized(p) for p in grow) / 1e6)}
    out = {"dir": str(ENGINES_DIR), "uv": bool(_uv()),
           "installing": installing,
           "failed": None if installing else _eng["error"],
           "kokoro": {"available": kokoro_available(),
                      "voices": len(kokoro_voices())}}
    for name, info in ENGINE_INFO.items():
        managed = (ENGINES_DIR / name / ".ready").exists()
        avail = cb_available() if name == "chatterbox" else ov_available()
        d = {"available": avail, "managed": managed, "est_gb": info["est_gb"]}
        if managed:
            d["gb"] = round((_sized(ENGINES_DIR / name)
                             + (_sized(_snapshot_dir(info["weights"]))
                                if "HF_HOME" not in os.environ else 0)) / 1e9, 1)
        out[name] = d
    return out


def get_kokoro():
    """Lazy, like get_cb(): a library that never speaks through Kokoro
    should never pay its load time. It is small enough (~100MB, CPU) that
    sharing the process with chatterbox costs nothing."""
    global _kokoro
    if _kokoro is None:
        mf = _kokoro_model_file()
        if mf is None:
            raise RuntimeError(
                f"Kokoro model files are missing — put kokoro-v1.0.int8.onnx and "
                f"voices-v1.0.bin in {KOKORO_DIR} (from "
                f"github.com/thewh1teagle/kokoro-onnx/releases), or keep this "
                f"profile on chatterbox.")
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(mf), str(KOKORO_DIR / "voices-v1.0.bin"))
    return _kokoro


def kokoro_voices():
    """The preset list without paying the model load: the voices file is a
    numpy archive whose keys are the names."""
    global _kvoices
    if _kvoices is None:
        try:
            import numpy as np
            z = np.load(str(KOKORO_DIR / "voices-v1.0.bin"))
            _kvoices = sorted(z.files)
        except Exception:
            return []                 # not cached: it may be mid-download
    return _kvoices


def _kokoro_gen(spoken, p, dest):
    """One line through Kokoro, onto disk. Under the model lock — espeak's
    phonemizer is not certainly reentrant, and serialising with the other
    engine's renders is the behaviour everything here already assumes."""
    import soundfile as sf
    k = get_kokoro()
    v = p["kvoice"]
    speed = min(max(float(p["speed"] or 0) or 1.0, 0.5), 2.0)
    with _lock:
        samples, sr = k.create(spoken, voice=v, speed=speed,
                               lang=KOKORO_LANGS.get(v[:1], "en-us"))
    sf.write(str(dest), samples, sr)


def kokoro_sample(v, spd):
    """The audition wav for one (preset, pace) — rendered on first ask,
    cached ever after. No sample clips ship with the model; at faster-than-
    realtime on CPU, making one is cheaper than shipping one."""
    v = re.sub(r"[^a-z0-9_]", "", str(v or "af_heart"))[:24] or "af_heart"
    line = KOKORO_SAMPLE.get(v[:1], KOKORO_SAMPLE["a"])
    key = ["kprev", v, float(spd or 0), line]
    h = "p" + hashlib.sha256(json.dumps(
        key, sort_keys=True).encode()).hexdigest()[:19]
    dest = AUDIO / f"{h}.wav"
    if not dest.exists():
        tmp = dest.with_name(dest.stem + ".tmp.wav")
        try:
            _kokoro_gen(line, {"kvoice": v, "speed": spd}, tmp)
            tmp.rename(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return dest


def render_kokoro(c, doc, force=False):
    """Speak one card with Kokoro. Deterministic — no sampling to seed — so
    re-rendering a take reproduces it exactly."""
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    spoken = spoken_text(c["text"])       # ❦ and // are for the screen
    if _nothing_to_say(spoken):
        _render_mute(dest)                 # never from cache — see the helper
        return h, False
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    try:
        _kokoro_gen(spoken, p, tmp)
        tmp.rename(dest)                   # atomic, as everywhere else here
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return h, False


def render_omnivoice(c, doc, force=False):
    """Speak one card with OmniVoice.

    No seed, because the model has none — so a take still gets its own file and
    stepping back to it still plays that file, but re-rendering the same take
    will not reproduce it the way a seeded chatterbox take does."""
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    spoken = spoken_text(c["text"])       # ❦ and // are for the screen
    if _nothing_to_say(spoken):
        _render_mute(dest)                 # never from cache — see the helper
        return h, False
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
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
    eng = params_for(c, doc)["engine"]
    if eng == "omnivoice":
        return render_omnivoice(c, doc, force)
    if eng == "kokoro":
        return render_kokoro(c, doc, force)
    return render(c, doc, force)


def render(c, doc, force=False):
    """Render one chunk. With force=True the cache is bypassed and overwritten —
    the render/preview buttons always generate, so a press always means work."""
    h = chunk_hash(c, doc)
    dest = AUDIO / f"{h}.wav"
    spoken = spoken_text(c["text"])       # ❦ and // are for the screen
    if _nothing_to_say(spoken):
        _render_mute(dest)                 # never from cache — see the helper
        return h, False
    if dest.exists() and not force:
        return h, True
    p = params_for(c, doc)
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    try:
        _cb_gen(spoken, p, c.get("seed"), tmp)
        tmp.rename(dest)                   # atomic: no half-written cache entries
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return h, False


def render_preview(c, doc, force=False, text=None):
    """Speak just the selected words.

    Chatterbox has no low-quality mode — sampling cost is per token, so the
    only real speedup is less text. Same voice and parameters as the full
    render, so what you hear is exactly what the bake will say."""
    p = params_for(c, doc)
    spoken = spoken_text(text if text is not None else c["text"])
    if p["engine"] == "chatterbox":         # as chunk_hash: default stays unmarked
        k = [spoken, p["voice"], p["exag"], p["cfg"], p["temp"], p["rep"],
             "prev", int(c.get("seed") or 0)]
    elif p["engine"] == "kokoro":
        k = [spoken, "prev", int(c.get("seed") or 0),
             {"engine": "kokoro", "voice": p["kvoice"],
              "speed": float(p["speed"] or 0)}]
    else:
        k = [spoken, p["voice"], "prev", int(c.get("seed") or 0),
             {"engine": p["engine"], "lang": p["lang"],
              "speed": float(p["speed"] or 0)}]
    h = "p" + hashlib.sha256(json.dumps(k, sort_keys=True).encode()).hexdigest()[:19]
    dest = AUDIO / f"{h}.wav"
    if _nothing_to_say(spoken):
        _render_mute(dest)                 # never from cache — see the helper
        return h, False, spoken
    if dest.exists() and not force:
        return h, True, spoken
    if p["engine"] == "kokoro":
        tmp = dest.with_name(dest.stem + ".tmp.wav")
        try:
            _kokoro_gen(spoken, p, tmp)
            tmp.rename(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return h, False, spoken
    if p["engine"] == "omnivoice":
        tmp = dest.with_name(dest.stem + ".tmp.wav")
        try:
            _ov_gen(spoken, p, tmp)
            tmp.rename(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return h, False, spoken
    tmp = dest.with_name(dest.stem + ".tmp.wav")
    try:
        _cb_gen(spoken, p, c.get("seed"), tmp)
        tmp.rename(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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
# the print queue's hold button: the worker stops POPPING, never mid-card —
# a generate() on MPS cannot be interrupted without losing the model, so
# pause is honoured between jobs, the same law bake's Stop already follows
_qstate = {"paused": False}


def enqueue(kind, project, cid, text=None, label=""):
    with _qlock:
        _seq[0] += 1
        jid = f"j{_seq[0]}"
        _jobs[jid] = {"id": jid, "kind": kind, "project": project, "chunk": cid,
                      "status": "queued", "text": text,
                      "label": str(label or "")[:80], "queued_at": time.time()}
        _queue.append(jid)
        _qlock.notify()
    return jid


def job_start(kind, project, cid, label=""):
    """File already-RUNNING work in the jobs table so the queue window sees
    it. Paints run in their request's own thread — parallel, unpausable,
    answered on the connection that asked — so they never pass through
    _queue; this is bookkeeping, not scheduling."""
    with _qlock:
        _seq[0] += 1
        jid = f"j{_seq[0]}"
        _jobs[jid] = {"id": jid, "kind": kind, "project": project, "chunk": cid,
                      "status": "running", "label": str(label or "")[:80],
                      "queued_at": time.time()}
    return jid


def job_end(jid, error=""):
    j = _jobs.get(jid)
    if j:
        j.update(status="error" if error else "done",
                 seconds=round(time.time() - j["queued_at"], 1))
        if error:
            j["error"] = error
    _prune_jobs()


def _prune_jobs():
    # keep the table small; finished jobs are only needed until the UI polls
    if len(_jobs) > 400:
        for k in sorted(_jobs, key=lambda k: _jobs[k]["queued_at"])[:200]:
            if _jobs[k]["status"] in ("done", "error", "canceled"):
                _jobs.pop(k, None)


def worker():
    while True:
        with _qlock:
            while not _queue or _qstate["paused"]:
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
                # previews speak a selection, never the whole card, so only
                # real renders join the card's history
                file_history(j["project"], j["chunk"], c, doc, h)
            j.update(status="done", seconds=round(time.time() - t0, 1))
        except Exception as ex:
            j.update(status="error", error=f"{type(ex).__name__}: {ex}")
        _prune_jobs()


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
                 cancel=False, stopped=False, error="")
    fails = []
    try:
        for c in todo:
            # the queue's Pause holds these presses too — between cards,
            # never mid-card, the same law as Stop
            while _qstate["paused"] and not _bake["cancel"]:
                time.sleep(0.3)
            if _bake["cancel"]:
                _bake["stopped"] = True
                break
            _bake["label"] = (c["text"][:60] if is_speech(c)
                              else "◎ " + (c.get("perfname") or "performance"))
            try:
                h, _ = render_any(c, doc)
                file_history(name, c["id"], c, doc, h)
            except Exception as ex:
                # A bake must never die silently — but one strange card must
                # not stop the other 139 either. A card's failure is noted
                # and the bake walks on; the SAME failure twice running is
                # systemic — a missing engine, a dead worker — and every
                # further card would only repeat it, or wait minutes to.
                msg = f"{type(ex).__name__}: {ex}"
                fails.append((c["id"], msg))
                if len(fails) >= 2 and fails[-2][1] == msg:
                    _bake["stopped"] = True
                    break
                continue
            _bake["done"] += 1
    finally:
        if fails:
            shown = "; ".join(f"card #{i}: {m}" for i, m in fails[:3])
            _bake["error"] = (f"{len(fails)} card(s) failed — {shown}"
                              + (" …" if len(fails) > 3 else ""))
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
    import numpy as np, math
    n = int(sr * CHIME_SECS)
    t = np.arange(n, dtype=np.float32) / sr
    env = np.exp(-t * 7.0)
    a = int(sr * 0.006)                    # a few ms of attack, or it clicks
    if a:
        env[:a] = env[:a] * np.linspace(0.0, 1.0, a, dtype=np.float32)
    tone = (np.sin(2 * math.pi * 784.0 * t)
            + 0.45 * np.sin(2 * math.pi * 1176.0 * t))
    return (tone * env * 0.13).astype(np.float32)


def _read_wav(path):
    """A file off disk as mono float32 — (samples, sample_rate). The book is
    mono, so everything entering the mix comes through here and beds follow
    the narration down to one channel."""
    import numpy as np
    import soundfile as sf
    w, wsr = sf.read(str(path), dtype="float32", always_2d=True)
    return np.ascontiguousarray(w.mean(axis=1)), wsr


def _resample(w, wsr, sr):
    """Only clips ever need this — every speech chunk is at the model's rate."""
    import numpy as np
    import soxr
    return soxr.resample(w, wsr, sr).astype(np.float32)


def channel_gains(doc):
    """Channel id -> linear gain, with mute folded to zero. The story's mixer
    (doc["channels"]) made number: cards name a channel or belong to main, and
    a name no channel answers to any more follows main too — the UI reassigns
    on remove, and this is the net under it. An absent mixer is no mixer:
    empty dict, and every card plays at one."""
    out = {}
    for ch in (doc.get("channels") or []):
        cid = str(ch.get("id") or "")
        if cid:
            g = _num(ch.get("gain"), 100.0, 0.0, 200.0)
            out[cid] = 0.0 if ch.get("mute") else g / 100.0
    return out


def channel_gain_of(c, gains):
    if not gains:
        return 1.0
    return gains.get(c.get("channel") or "main", gains.get("main", 1.0))


def master_gain(doc):
    """The mixer's Master bus as a linear factor: the level of the book
    itself, applied over the summed mix wherever it is heard — preview,
    exports, the stage's stretches, a lone card's play button. The monitor
    slider in the studio's top bar is a different animal: this machine's
    loudness, never in any render."""
    m = doc.get("master") or {}
    return 0.0 if m.get("mute") else _num(m.get("gain"), 100.0, 0.0, 200.0) / 100.0


def mix_events(doc, gap=0.35, frm=None, upto=None, chime=False, secs_of=None):
    """The cursor walk, factored out of mixdown so /api/mix_plan and the
    shipped sum read one score and can never drift. Returns (events, cursor,
    missing, marks): events are (start, kind, path, c) tuples, start in
    seconds, path None for a chime. Placement only — gains are nobody's
    business here.

    `secs_of(path)` supplies durations: the default reads sf.info headers,
    which is what the plan wants; mixdown passes a reader that also caches
    the arrays it is about to sum.

    A cursor walks the cards in order. Speech is placed at the cursor and
    advances it by its own length plus a rest (scene breaks get a longer one,
    as before); a voiced card behaves identically, since it is rendered into
    the same content-addressed pool. A silence card just advances it. An audio card is placed at
    the cursor and advances it either past the whole clip (mode "full") or by
    its "after" seconds — in which case the rest of the clip keeps playing
    *under* whatever the cursor reaches next, which is how music fades out
    beneath the first line of narration.

    `marks` is where each card starts, in seconds — [{"id", "at"}, …]. The
    browser follows it during playback to show which card is speaking, which
    is also what makes "stop, fix that one" possible without hunting for it.

    `chime` marks unready cards with a tone instead of dropping them. Preview
    only; assemble() leaves them out, as it always has.

    `frm` is a card id to start at: the timeline then begins with that card at
    zero and runs to the end, which is how you hear the rest of the book after
    fixing something in the middle without sitting through what came before.
    A music bed opened earlier is simply not in that mix — the cards before the
    start are not on the timeline at all, so there is nothing to carry over."""
    if secs_of is None:
        import soundfile as sf

        def secs_of(path):
            i = sf.info(str(path))
            return i.frames / i.samplerate
    profs = profiles()          # once for the whole walk, not once per card
    cards = doc["chunks"]
    if frm is not None:
        i = next((k for k, c in enumerate(cards) if c["id"] == frm), None)
        if i is not None:
            cards = cards[i:]
    # `upto` is exclusive — the timeline runs to the card before it, which is
    # how interactive playback speaks exactly one stretch between two stops.
    # A bed opened inside the stretch still plays out its tail (the total
    # honours every piece), so music carries under the chooser rather than
    # being cut off mid-bar.
    if upto is not None:
        j = next((k for k, c in enumerate(cards) if c["id"] == upto), None)
        if j is not None:
            cards = cards[:j]
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
        events.append((cursor, "chime", None, c))
        cursor += CHIME_SECS + gap
        last_gap = gap

    for c in cards:
        if c.get("mute"):                     # muted cards are simply not in the book
            continue
        kind = c.get("type", "speech")
        # A visual takes no time and makes no sound: it is a mark the stage
        # and the exports read, nothing more. A choice card likewise — it is
        # where interactive playback stops, and the audiobook does not stop.
        # Both must turn back HERE: the default branch below hashes the card,
        # and hashing starts by reading text these kinds have not got.
        if kind in ("visual", "choice", "group"):
            note(c)
            continue
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
        if kind == "title":
            # a title card holds the screen while the mix holds its breath:
            # fade in + hold + fade out, all silence on the timeline. The
            # display is the stage's business; the seconds are the mix's.
            note(c)
            fi, fo = (list(c.get("fade") or []) + [0.6, 0.6])[:2]
            cursor += (max(0.0, float(fi)) + max(0.0, float(c.get("secs", 3.0)))
                       + max(0.0, float(fo)))
            last_gap = 0.0                    # its quiet is its own rest
            continue
        if kind == "audio":
            f = CLIPS / f"{c.get('clip', '')}.wav"
            if not c.get("clip") or not f.exists():
                unready(c)
                continue
            secs = secs_of(f)
            note(c)
            events.append((cursor, kind, f, c))
            cursor += (max(0.0, float(c.get("after", 0.0)))
                       if c.get("mode") == "after" else secs)
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
        secs = secs_of(f)
        note(c)
        events.append((cursor, kind, f, c))
        # a voiced card lands here too — rendered and content-addressed exactly
        # as speech is — but it has no text to carry a scene mark
        g = 1.1 if is_speech(c) and c["text"].strip().startswith("❦") else gap
        cursor += secs + g
        last_gap = g
    return events, cursor, missing, marks


def mixdown(doc, gap=0.35, frm=None, upto=None, chime=False):
    """Mix every card onto one timeline; (audio, sample_rate, missing, marks) back.

    The walk itself lives in mix_events — written once, shared with the live
    desk's /api/mix_plan, so the desk in the browser and the book that ships
    read one score. This half is the sum: place, fade, gain, clamp. Overlaps
    are summed and clamped.

    Fades are percentages of the clip: fade [10, 90] ramps up over the first
    10% and down over the last 10%. Gain is applied after the fade.

    Plain numpy throughout: the mix is adds and multiplies on one array, and
    keeping torch out of it is what lets the studio itself run on a small
    interpreter — the engines that need torch live behind worker processes.

    No model here — the sample rate comes from the first speech chunk on disk
    (they are all rendered at the model's rate), so mixing never costs a
    ten-second model load. Both assemble() and the in-browser preview sit on
    this one function, so what you hear is what ships, by construction."""
    import numpy as np
    profs = profiles()          # once for the whole mix, not once per card
    chg = channel_gains(doc)    # the mixer's faders, resolved once too
    cache = {}                  # path -> (samples, rate): read once, sum often

    def reader(path):
        if path not in cache:
            cache[path] = _read_wav(path)
        w, wsr = cache[path]
        return w.shape[-1] / wsr

    events, cursor, missing, marks = mix_events(doc, gap, frm, upto, chime,
                                                reader)
    if not events:
        return None, 0, missing, []
    # every speech chunk is at the model's rate; only clips ever need resampling.
    # A preview of a chapter nobody has rendered yet is all chimes and carries no
    # rate of its own, so fall back to a clip's, then to the model's.
    sr = (next((cache[e[2]][1] for e in events if e[1] in ("speech", "voiced")), None)
          or next((cache[e[2]][1] for e in events if e[2] is not None), None) or 24000)
    pieces = []
    for start, kind, path, c in events:
        if kind == "chime":
            pieces.append((int(start * sr), chime_wave(sr)))
            continue
        w, wsr = cache[path]
        if wsr != sr:                         # mono already: _read_wav saw to it
            w = _resample(w, wsr, sr)
        if kind == "audio":
            # two cards can share one clip, and the cache hands them the same
            # array — the fades below write in place, so they fade a copy
            if w is cache[path][0]:
                w = w.copy()
            n = w.shape[-1]
            lo, hi = (list(c.get("fade") or []) + [0, 100])[:2]
            fi, fo = int(n * lo / 100), int(n * (100 - hi) / 100)
            if fi > 0:
                w[:fi] = w[:fi] * np.linspace(0.0, 1.0, fi, dtype=np.float32)
            if fo > 0:
                w[n - fo:] = w[n - fo:] * np.linspace(1.0, 0.0, fo, dtype=np.float32)
            w = w * (float(c.get("gain", 100)) / 100.0)
        else:
            # a spoken card's level comes from its profile, so a character who
            # reads louder than the rest can be evened out without re-rendering
            # a word of them
            g = float(params_for(c, doc, profs).get("gain", 100))
            if g != 100:
                w = w * (g / 100.0)
        # the card's channel, last: the mixer sits after every per-card and
        # per-profile level, the way a desk fader sits after the channel strip.
        # A muted channel is a fader at zero, not a dropped card — the timeline
        # keeps its shape and every mark stays where it was.
        cg = channel_gain_of(c, chg)
        if cg != 1.0:
            w = w * cg
        pieces.append((int(start * sr), w))
    # a trailing silence card pads the end, so the total honours the cursor too
    total = max(int(cursor * sr), max(s + w.shape[-1] for s, w in pieces))
    full = np.zeros(total, dtype=np.float32)
    for s, w in pieces:
        full[s:s + w.shape[-1]] += w
    # the Master bus, over the summed whole and before the clamp — so pulling
    # it down can rescue a mix that was clipping, exactly as a desk's would
    mf = master_gain(doc)
    if mf != 1.0:
        full *= mf
    np.clip(full, -1.0, 1.0, out=full)
    return full, sr, missing, marks


# what assemble() can ship. The mix is mono narration, so lossy stays mono;
# wav is the untouched master and skips ffmpeg entirely.
ASSEMBLE_FMTS = {"mp3": ["-codec:a", "libmp3lame", "-b:a", "64k", "-ac", "1"],
                 "m4a": ["-codec:a", "aac", "-b:a", "96k", "-ac", "1"],
                 "flac": ["-codec:a", "flac"],
                 "wav": None}


def assemble(name, gap=0.35, fmt="mp3"):
    """Mixdown to out/<name>.<fmt> — the deliverable."""
    import soundfile as sf
    full, sr, missing, _ = mixdown(load(name), gap)
    if full is None:
        return None, missing
    out = pdir(name) / "out"
    out.mkdir(exist_ok=True)
    wav = out / f"{name}.wav"
    # float32, like every render in audio/ — soundfile's wav default is 16-bit
    sf.write(str(wav), full, sr, subtype="FLOAT")
    codec = ASSEMBLE_FMTS.get(fmt if fmt in ASSEMBLE_FMTS else "mp3")
    if codec is None or not shutil.which("ffmpeg"):
        return wav, missing
    dst = out / f"{name}.{fmt}"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(wav), *codec, str(dst)],
                       capture_output=True, text=True)
    if r.returncode:
        # the wav master is whole — leave it, name it, and say what happened
        raise RuntimeError("ffmpeg could not encode " + fmt + ": "
                           + (r.stderr or "").strip()[-300:]
                           + f" — the wav master is at {wav}")
    wav.unlink()
    return dst, missing


def preview_book(name, frm=None, upto=None):
    """The same mixdown assemble() ships, parked in a dotfile the browser can
    stream — hearing the whole book should not overwrite the mp3 in out/.
    16-bit is plenty for ears and halves what goes over the wire; the Finder
    never shows the file, and export never packs out/.

    `frm` starts the mix at a card instead of at the top, so an edit in chapter
    nine costs nine seconds to hear rather than the eight minutes before it.
    `upto` stops it before a card — the stage plays choice-to-choice with it."""
    import soundfile as sf
    full, sr, missing, marks = mixdown(load(name), frm=frm, upto=upto, chime=True)
    if full is None:
        return None, 0, missing, []
    out = pdir(name) / "out"
    out.mkdir(exist_ok=True)
    f = out / ".preview.wav"
    sf.write(str(f), full, sr, subtype="PCM_16")
    return f, round(full.shape[-1] / sr, 1), missing, marks


# ── publishing ──────────────────────────────────────────────────────────
# Three doors out of the studio. Assemble has always been the first: the
# audiobook, cards top to bottom, choices ignored. The second is an animatic —
# the same audio under the visual cards' stills, timed by the marks mixdown
# already returns. The third is a folder of HTML that plays the story with its
# choices, offline, from file:// — the voiced visual novel.

def segment_spans(chunks):
    """Every stretch the exported player could need, as (start, end) index
    pairs — cut where the walk stops (choice cards, conditional cards) AND at
    every card a goto names, because the export's audio is premixed and a jump
    cannot land mid-file. This mirrors player.js segments() structurally; it
    derives cut points and never evaluates a condition, so the one-evaluator
    rule holds. Over-cutting is harmless — the runtime chains files — but an
    uncut jump target would be a landing with no floor, so the two err the
    same way: more cuts, never fewer."""
    targets = set()
    for c in chunks:
        if c.get("type") == "choice":
            for o in c.get("options") or []:
                if o.get("goto"):
                    targets.add(o["goto"])
    brk = {0}
    for i, c in enumerate(chunks):
        if c.get("type") == "choice" or c.get("when"):
            brk.update((i, i + 1))
        elif any(t in targets for t in c.get("tags") or []):
            brk.add(i)
    cuts = sorted(i for i in brk if i < len(chunks))
    out = []
    for k, a in enumerate(cuts):
        b = cuts[k + 1] if k + 1 < len(cuts) else len(chunks)
        if a < b and chunks[a].get("type") != "choice":
            out.append((a, b))
    return out


def story_problems(chunks):
    """What would make the exported story lie or die, said before any work.

    A goto naming a tag no card carries is a landing with no floor; a tag on
    two cards makes every jump to it ambiguous; a runon card at a jump seam
    would ship an audible pause inside a sentence, because premixed files
    cannot reach back and close the gap the way one timeline can."""
    problems = []
    counts = {}
    for c in chunks:
        for t in c.get("tags") or []:
            counts[t] = counts.get(t, 0) + 1
    for t, n in sorted(counts.items()):
        if n > 1:
            problems.append(f"the tag “{t}” is on {n} cards — a jump to it is ambiguous")
    for c in chunks:
        if c.get("type") != "choice":
            continue
        for o in c.get("options") or []:
            g = o.get("goto")
            if g and not counts.get(g):
                problems.append(f"card #{c['id']} jumps to “{g}” and no card carries that tag")
    for a, _ in segment_spans(chunks):
        if a > 0 and chunks[a].get("runon"):
            problems.append(f"card #{chunks[a]['id']} runs on, but a jump can land there — "
                            f"the seam would put a pause inside the sentence")
    return problems


STILLS = ROOT / "stills"


def still_of(src, height):
    """One visual as a video-ready frame: scaled and padded onto a 16:9 canvas,
    cached content-addressed like fx/ — the first export pays, the rest read.
    Film contributes its first frame; the animatic is a storyboard, not a cut.
    Every frame identical in geometry, because the concat demuxer breaks on
    mid-stream dimension changes."""
    w, h = height * 16 // 9, height
    key = hashlib.sha256(f"{sha256_file(src)}|{h}".encode()).hexdigest()[:20]
    dest = STILLS / f"{key}.png"
    if dest.exists():
        return dest
    STILLS.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".tmp.png")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-frames:v", "1",
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
         str(tmp)], capture_output=True, text=True)
    if r.returncode:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg could not read {src.name}: "
                           + (r.stderr or "").strip()[-200:])
    tmp.rename(dest)
    return dest


def _title_font():
    """A real font file for drawtext — fontconfig is often left out of ffmpeg
    builds, so a name is not enough and a path is probed instead."""
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arial.ttf"):
        if Path(p).exists():
            return p
    return None


def title_frame(text, height):
    """A title card as a video-ready frame: white words on black, centred,
    cached content-addressed like stills. drawtext reads the words from a
    file, so no card can break the filter with a quote or a colon. No font
    on this machine means no frame — the caller lets the wall stand rather
    than failing the whole export over typography."""
    font = _title_font()
    if font is None:
        raise RuntimeError("no font for a title frame")
    w, h = height * 16 // 9, height
    key = hashlib.sha256(f"title|{text}|{h}".encode()).hexdigest()[:20]
    dest = STILLS / f"{key}.png"
    if dest.exists():
        return dest
    STILLS.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".tmp.png")
    fd, tf = tempfile.mkstemp(dir=ROOT, suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"color=black:s={w}x{h}",
             "-vf", (f"drawtext=textfile='{tf}':fontfile='{font}'"
                     f":fontcolor=white:fontsize={h // 12}"
                     ":x=(w-text_w)/2:y=(h-text_h)/2"),
             "-frames:v", "1", str(tmp)], capture_output=True, text=True)
        if r.returncode:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg could not draw the title: "
                               + (r.stderr or "").strip()[-200:])
        tmp.rename(dest)
    finally:
        Path(tf).unlink(missing_ok=True)
    return dest


def black_frame(height):
    """The wall before the first visual."""
    w, h = height * 16 // 9, height
    dest = STILLS / f"black-{h}.png"
    if dest.exists():
        return dest
    STILLS.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".tmp.png")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=black:s={w}x{h}",
                    "-frames:v", "1", str(tmp)], check=True)
    tmp.rename(dest)
    return dest


def publish_video(name, height=1080):
    """The animatic: every still held from its mark to the next, under the
    exact audio assemble ships. Total length comes from the audio, never
    -shortest — that flag would eat the final reverb tail."""
    import soundfile as sf
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed to export video")
    doc = load(name)
    full, sr, missing, marks = mixdown(doc)
    if full is None:
        raise RuntimeError("nothing to export — render some cards first")
    total = full.shape[-1] / sr
    out = pdir(name) / "out"
    out.mkdir(exist_ok=True)
    wav = out / ".animatic.wav"
    sf.write(str(wav), full, sr, subtype="FLOAT")
    byid = {c["id"]: c for c in doc["chunks"]}
    frames = []
    wall = None                            # the still a title interrupts
    for m in marks:
        c = byid.get(m["id"])
        if not c:
            continue
        if c.get("type") == "visual" and c.get("media"):
            try:
                wall = still_of(media_file(c["media"]), height)
            except FileNotFoundError:
                continue                   # a visual with no file shows nothing
            frames.append((m["at"], wall))
        elif c.get("type") == "title":
            # the animatic states the title plain for its whole span — the
            # stage and the web story fade it; a storyboard just says it —
            # and the standing visual returns when the span ends
            fi, fo = (list(c.get("fade") or []) + [0.6, 0.6])[:2]
            dur = (max(0.0, float(fi)) + max(0.0, float(c.get("secs", 3.0)))
                   + max(0.0, float(fo)))
            try:
                frames.append((m["at"], title_frame(c.get("text") or "", height)))
            except RuntimeError as ex:
                print(f"title frame skipped: {ex}", flush=True)
                continue                   # no font: the wall stands
            frames.append((m["at"] + dur, wall or black_frame(height)))
    if not frames or frames[0][0] > 0:
        frames.insert(0, (0.0, black_frame(height)))
    lines = ["ffconcat version 1.0"]
    for k, (at, p) in enumerate(frames):
        end = frames[k + 1][0] if k + 1 < len(frames) else total
        lines.append(f"file '{p}'")
        lines.append(f"duration {max(0.04, end - at):.3f}")
    # the demuxer ignores the last duration unless the final file repeats
    lines.append(f"file '{frames[-1][1]}'")
    concat = out / ".animatic.txt"
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mp4 = out / f"{name}.mp4"
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(wav),
             "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
             str(mp4)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("ffmpeg failed: " + (r.stderr or "").strip()[-300:])
    finally:
        wav.unlink(missing_ok=True)
        concat.unlink(missing_ok=True)
    return mp4, missing, len(frames)


# What the exported page needs to know about a card — never the whole card:
# params, notes and profile names are studio business, not story business.
def _export_chunk(c):
    out = {"id": c["id"], "type": c.get("type", "speech")}
    if is_speech(c):
        out["text"] = c["text"]
    if c.get("type") == "title":
        # the words ARE the picture here, and the player needs the clock too
        out["text"] = c.get("text") or ""
        out["secs"] = float(c.get("secs", 3.0))
        out["fade"] = (list(c.get("fade") or []) + [0.6, 0.6])[:2]
    for k in ("tags", "when", "auto", "wait", "mute", "media", "mediakind",
              "sub", "chain"):
        if c.get(k):
            out[k] = c[k]
    for k in ("tw", "twsfx"):           # 0 is a real word here: explicit off
        if c.get(k) is not None:
            out[k] = c[k]
    if c.get("type") == "choice":
        out["options"] = c.get("options") or []
    return out


def publish_html(name):
    """The voiced visual novel: a folder that plays offline from file://.

    index.html carries the runtime and the whole story graph inline — fetch is
    blocked from file:// so nothing may be loaded, only referenced: media and
    per-stretch mp3s sit beside it as plain relative files. The stretches are
    segment_spans premixed through the same mixdown assemble uses, so what the
    export says is what the studio said. Hard-fails on story_problems: a
    broken jump is worth a message now, not a dead end in someone's browser."""
    import soundfile as sf
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed to export the web player")
    doc = load(name)
    chunks = doc["chunks"]
    problems = story_problems(chunks)
    if problems:
        raise RuntimeError("fix the story first: " + "; ".join(problems))
    dest = pdir(name) / "out" / "html"
    shutil.rmtree(dest, ignore_errors=True)
    (dest / "audio").mkdir(parents=True)
    (dest / "media").mkdir()
    segs, missing_total = [], 0
    for a, b in segment_spans(chunks):
        frm = chunks[a]["id"]
        upto = chunks[b]["id"] if b < len(chunks) else None
        full, sr, missing, marks = mixdown(doc, frm=frm, upto=upto)
        missing_total += missing
        seg = {"from": frm, "upto": upto, "marks": marks}
        if full is not None:
            import tempfile as _tf
            fn = f"s{frm:04d}.mp3"
            with _tf.TemporaryDirectory(dir=ROOT) as td:
                wav = Path(td) / "seg.wav"
                sf.write(str(wav), full, sr, subtype="FLOAT")
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(wav), "-codec:a", "libmp3lame", "-b:a", "64k",
                     "-ac", "1", str(dest / "audio" / fn)],
                    capture_output=True, text=True)
                if r.returncode:
                    raise RuntimeError("ffmpeg failed: " + (r.stderr or "").strip()[-300:])
            seg["file"] = "audio/" + fn
            seg["secs"] = round(full.shape[-1] / sr, 2)
        segs.append(seg)
    media_urls = {}
    for mn in sorted(media_of(doc)):
        try:
            f = media_file(mn)
        except FileNotFoundError:
            continue
        shutil.copy2(f, dest / "media" / f.name)
        media_urls[mn] = "media/" + f.name
    graph = {"title": doc.get("title", name),
             "typewriter": bool(doc.get("typewriter")),
             "typesfx": bool(doc.get("typesfx")),
             "chunks": [_export_chunk(c) for c in chunks],
             "segments": segs, "media": media_urls}
    page = (HERE / "export_player.html").read_text(encoding="utf-8")
    page = page.replace("/*SAGA_PLAYER*/", (HERE / "player.js").read_text(encoding="utf-8"))
    # "</" would close the script tag from inside a string — a card whose
    # prose mentions "</script>" must not be able to end the player early
    page = page.replace("\"/*SAGA_GRAPH*/\"",
                        json.dumps(graph, ensure_ascii=False).replace("</", "<\\/"))
    (dest / "index.html").write_text(page, encoding="utf-8")
    zip_path = pdir(name) / "out" / f"{name}-web"
    Path(str(zip_path) + ".zip").unlink(missing_ok=True)
    made = shutil.make_archive(str(zip_path), "zip", root_dir=dest.parent, base_dir="html")
    return Path(made), len(segs), missing_total


def share_web(name, unlisted=None):
    """Carry the web story to darkride.ai and come back with the link.

    The export is rebuilt when the document has changed since the zip was
    packed, so Share always means "share what I have now". share.json in
    the project folder keeps the slug and the secret token the stage door
    hands out on the first upload — which is what lets every later share
    REPLACE the story at the same URL instead of scattering a new link per
    revision. A token the server no longer recognises (the story deleted
    there, the box rebuilt) is dropped and the share retried once as new,
    because a stale secret should cost the author nothing but a fresh URL.

    `unlisted` is the "anyone with the link" mode: the story runs at its
    URL but stays off darkride's front page. It travels with every upload
    (the choice lives in share.json between shares), so re-sharing can
    flip a story either way without moving its link. None means the caller
    had no opinion; the standing choice rides again."""
    import urllib.request
    import urllib.error
    zp = pdir(name) / "out" / f"{name}-web.zip"
    docf = pdir(name) / "doc.json"
    built = False
    if not zp.exists() or (docf.exists()
                           and docf.stat().st_mtime > zp.stat().st_mtime):
        zp, _, _ = publish_html(name)
        built = True
    sf = pdir(name) / "share.json"
    try:
        share = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        share = {}
    if zp.stat().st_size > 400 * 1024 * 1024:
        raise RuntimeError("the story is over darkride's 400 MB cap — "
                           "trim the media or the audio and export again")
    if unlisted is None:
        unlisted = bool(share.get("unlisted"))
    payload = zp.read_bytes()
    dk = settings()["darkride"]["key"]
    series_slug = series_of(name)
    for retry in (False, True):
        req = urllib.request.Request(DARKRIDE + "/api/upload", data=payload,
                                     headers={"Content-Type": "application/zip"})
        req.add_header("X-Darkride-Unlisted", "1" if unlisted else "0")
        if dk:                       # the account this share belongs to
            req.add_header("X-Darkride-Key", dk)
        if series_slug:
            req.add_header("X-Darkride-Series", series_slug)
        if share.get("slug") and share.get("token"):
            req.add_header("X-Darkride-Slug", share["slug"])
            req.add_header("X-Darkride-Token", share["token"])
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                out = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as ex:
            try:
                msg = json.loads(ex.read().decode("utf-8")).get("error") or str(ex)
            except (ValueError, OSError):
                msg = str(ex)
            if ex.code == 403 and share and not retry:
                share = {}
                continue
            raise RuntimeError(msg)
        except (urllib.error.URLError, OSError) as ex:
            raise RuntimeError(f"could not reach {DARKRIDE} — "
                               f"{getattr(ex, 'reason', None) or ex}")
    share.update({"slug": out["slug"], "url": out["url"], "at": int(time.time()),
                  "unlisted": bool(unlisted)})
    if out.get("token"):
        share["token"] = out["token"]
    sf.write_text(json.dumps(share), encoding="utf-8")
    return out["url"], built, zp.stat().st_size, bool(unlisted)


# a fresh .sagaproj with the renders inside can be most of a gigabyte, so it
# is streamed off disk rather than read into memory, and the box holds the
# matching cap on its side (DARKRIDE_SRC_CAP there)
SOURCE_CAP = 1024 * 1024 * 1024


def share_source(name):
    """Carry the PROJECT ITSELF to darkride and come back with a private
    download link.

    What the web share is to an audience, this is to another machine: the
    .sagaproj packed here is the full export — doc, source, voices, clips,
    media, takes, every rendered wav — so downloading it and dropping it on
    Saga Studio opens the project whole, nothing re-rendered, nothing
    missing. The link is ALWAYS private by design: darkride files a source
    under a long unguessable slug, keeps it off the marquee, asks the
    crawlers to look away, and serves it only as a download — it never runs
    there. share.json carries a `source` section (slug, token, url) beside
    the web share's own, so sharing again REPLACES the file at the same
    link; a token the box no longer knows is dropped and the share retried
    once as new, like the web share before it."""
    import urllib.request
    import urllib.error
    plan = plan_export([name], True)
    if not plan["projects"]:
        raise RuntimeError("nothing to pack — is the project readable?")
    title = plan["projects"][0].get("title") or name
    sf = pdir(name) / "share.json"
    try:
        share = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        share = {}
    src = share.get("source") or {}
    fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".share-src-", suffix=".tgz")
    os.close(fd)
    try:
        write_archive(plan, Path(tmp))
        size = Path(tmp).stat().st_size
        if size > SOURCE_CAP:
            raise RuntimeError("the packed project is over darkride's "
                               f"{SOURCE_CAP // (1 << 30)} GB source cap — "
                               "trim the media pool or the takes")
        fn = f"{name}-{time.strftime('%Y-%m-%d')}.sagaproj"
        dk = settings()["darkride"]["key"]
        for retry in (False, True):
            # streamed, not read_bytes(): urllib sends a file object in
            # blocks as long as Content-Length is given by hand
            body = open(tmp, "rb")
            req = urllib.request.Request(
                DARKRIDE + "/api/upload_source", data=body,
                headers={"Content-Type": "application/gzip",
                         "Content-Length": str(size),
                         "X-Darkride-Filename": fn,
                         "X-Darkride-Title": title})
            if dk:                   # the account this upload belongs to
                req.add_header("X-Darkride-Key", dk)
            if src.get("slug") and src.get("token"):
                req.add_header("X-Darkride-Slug", src["slug"])
                req.add_header("X-Darkride-Token", src["token"])
            try:
                with urllib.request.urlopen(req, timeout=1800) as r:
                    out = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as ex:
                try:
                    msg = json.loads(ex.read().decode("utf-8")).get("error") or str(ex)
                except (ValueError, OSError):
                    msg = str(ex)
                if ex.code == 403 and src and not retry:
                    src = {}              # stale token: retry once as new
                    continue
                if ex.code == 404:
                    raise RuntimeError("this darkride has no source door yet "
                                       "— its server needs the update")
                raise RuntimeError(msg)
            except (urllib.error.URLError, OSError) as ex:
                raise RuntimeError(f"could not reach {DARKRIDE} — "
                                   f"{getattr(ex, 'reason', None) or ex}")
            finally:
                body.close()
    finally:
        Path(tmp).unlink(missing_ok=True)
    src.update({"slug": out["slug"], "url": out["url"], "at": int(time.time())})
    if out.get("token"):
        src["token"] = out["token"]
    share["source"] = src
    sf.write_text(json.dumps(share), encoding="utf-8")
    return out["url"], size


def fetch_source_link(url, dest):
    """Follow a darkride source link to its .sagaproj and stream it to dest.

    What people paste is the LANDING page; its one download button names the
    file, so an html answer is read just far enough to find that href. A
    direct link to the .sagaproj itself works too. http(s) only, streamed in
    blocks, never more than SOURCE_CAP bytes — the other side of the same
    cap share_source packs under."""
    import urllib.request
    import urllib.error
    from urllib.parse import urljoin
    if not re.match(r"^https?://", url or ""):
        raise RuntimeError("paste the whole link — it starts with https://")
    def _open(u_, t):
        return urllib.request.urlopen(
            urllib.request.Request(u_, headers={"User-Agent": "SagaStudio"}),
            timeout=t)
    try:
        r = _open(url, 60)
        if "text/html" in (r.headers.get("Content-Type") or "").lower():
            page = r.read(1 << 20).decode("utf-8", "replace")
            r.close()
            m = re.search(r'href="([^"]+\.sagaproj)"', page)
            if not m:
                raise RuntimeError("that page offers no .sagaproj — is it a "
                                   "darkride source link?")
            r = _open(urljoin(url, m.group(1)), 1800)
        got = 0
        try:
            with open(dest, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    got += len(b)
                    if got > SOURCE_CAP:
                        raise RuntimeError("that file is over the "
                                           f"{SOURCE_CAP // (1 << 30)} GB cap")
                    f.write(b)
        finally:
            r.close()
        if not got:
            raise RuntimeError("the link answered with nothing")
        return got
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"the link answered {ex.code} — has the source "
                           "been deleted, or the address mistyped?")
    except (urllib.error.URLError, OSError) as ex:
        raise RuntimeError(f"could not reach the link — "
                           f"{getattr(ex, 'reason', None) or ex}")


# ── Brenda, the drama manager ───────────────────────────────────────────
# This used to be three lines: shell out, wait three minutes, print whatever
# came back. Now the agent has hands and a memory. It runs headless Claude
# Code with exactly one MCP server — saga_mcp.py, which speaks this studio's
# own API over loopback — so every change it can make goes through the same
# door the editor uses: undo, locks and validation hold for it exactly as
# they hold for the author. The sandbox law (read anywhere, write only in a
# draft) is enforced in saga_mcp.py and taught in discuss_rules.md.
#
# The reply streams to the panel as it happens — text deltas and tool calls
# both, because watching the cards land is the point — and the conversation
# resumes across asks: the session id comes back in the event stream and is
# kept per project, so "now make Juan grumpier" means something.
SESSIONS_FILE = ROOT / "chat_sessions.json"
_chats = {}                     # project -> Popen, so the stop button can aim
_chats_lock = threading.Lock()
CHAT_IDLE_S = 300               # silence this long means wedged, not thinking
CHAT_MAX_S = 1800               # and no single ask runs past half an hour

# ── the agent's memory ──────────────────────────────────────────────────
# What Brenda keeps between conversations, CLAUDE.md-style: one markdown
# file in the library, because it is ABOUT these stories (a key is about a
# machine and lives in settings; the distinction is the whole filing system).
# Two kinds of entry share it: a journal the studio writes itself after any
# ask that changed something, and notes the agent chooses to keep with its
# `remember` tool. The whole file rides into every conversation's system
# prompt, so the window must roll: oldest entries fall off once the file
# outgrows NOTES_LIMIT. The author can read or prune it like any file.
NOTES_FILE = ROOT / "agent_memory.md"
NOTES_LIMIT = 12000             # characters carried; newest survive
_notes_lock = threading.Lock()


def agent_notes():
    try:
        return NOTES_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def note_append(entry):
    """Append one `## `-headed entry, trimming whole entries from the top
    once the file outgrows the window — never mid-entry."""
    with _notes_lock:
        cur = agent_notes()
        cur = (cur + "\n\n" if cur else "") + entry.strip()
        if len(cur) > NOTES_LIMIT:
            tail = cur[-NOTES_LIMIT:]
            cut = tail.find("\n## ")
            if cut >= 0:
                tail = tail[cut + 1:]
            cur = tail
        try:
            NOTES_FILE.write_text(cur + "\n", encoding="utf-8")
        except OSError:
            pass


# the tools whose landing is worth a journal line — the same set the editor
# watches to live-reload the deck, minus render_card (a solo render is a
# casting check, not a change worth remembering)
CHAT_JOURNAL = {"insert_card", "edit_card", "remove_card", "move_card",
                "rename_group", "create_profile", "draft_story",
                "create_story", "render_story", "generate_image"}


def _journal(project, question, actions):
    """The studio's own diary of an ask that changed something: which story
    was open, what was asked, what the agent's hands actually did. Written
    here from the observed tool calls rather than trusted to the model, so
    the memory of an edit can never be a hallucination of one."""
    if not actions:
        return

    def brief(name, inp):
        keep = [f"{k}={inp[k]}" for k in ("story", "title", "gname", "name",
                                          "id", "at", "to", "profile")
                if inp.get(k) not in (None, "")]
        return name + (" (" + ", ".join(keep[:4]) + ")" if keep else "")

    did = [brief(n, i) for n, i in actions[:14]]
    if len(actions) > 14:
        did.append(f"and {len(actions) - 14} more")
    note_append("## " + time.strftime("%Y-%m-%d %H:%M")
                + (f' · in "{project}"' if project else " · no story open")
                + "\nAsked: " + " ".join(question.split())[:200]
                + "\nDid: " + "; ".join(did))


def _sessions():
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}


def _remember_session(project, sid):
    s = _sessions()
    s[project or ""] = sid
    try:
        SESSIONS_FILE.write_text(json.dumps(s, indent=1))
    except OSError:
        pass


def _chat_cmd(project, question, chunk_ids, fresh):
    doc = load(project) if project else None
    ctx = ""
    if doc:
        sel = [c for c in doc["chunks"]
               if is_speech(c) and (not chunk_ids or c["id"] in chunk_ids)]
        sel = sel[:40]
        where = (f"Open in the editor: \"{doc['title']}\" — "
                 f"story name `{doc['name']}`")
        if doc.get("draft"):
            where += (f", a draft of `{doc['draft_of']}`"
                      if doc.get("draft_of") else ", a draft not yet kept")
        ctx = where + f". {len(doc['chunks'])} cards.\n"
        if sel:
            ctx += (("Selected" if chunk_ids else "First") + " passages:\n\n"
                    + "\n\n".join(f"[card {c['id']}] {c['text']}" for c in sel)
                    + "\n\n")
    st = settings()["llm"]
    rules = (HERE / "discuss_rules.md").read_text(encoding="utf-8")
    notes = agent_notes()
    if notes:
        rules += ("\n\n## Your memory of this library\n"
                  "Carried over from earlier conversations, oldest first. "
                  "The journal entries were logged by the studio itself; "
                  "when a note and the story disagree, trust the story:\n\n"
                  + notes)
    prompt = ctx + "The author says: " + question
    if st["provider"] in ("claude", "anthropic"):
        cfg = {"mcpServers": {"saga": {
            "type": "stdio", "command": sys.executable,
            "args": [str(HERE / "saga_mcp.py")],
            # the same secret the editor holds — the agent itself never sees
            # it, having no shell to read the environment with
            "env": {"SAGA_API": f"http://127.0.0.1:{PORT}",
                    "SAGA_TOKEN": TOKEN}}}}
        cmd = [claude_path(), "-p", prompt,
               "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",
               "--mcp-config", json.dumps(cfg),
               "--allowedTools", "mcp__saga__*",
               "--append-system-prompt", rules]
        if st["model"]:
            cmd += ["--model", st["model"]]
        sid = None if fresh else _sessions().get(project or "")
        if sid:
            cmd += ["--resume", sid]
        env = None
        if st["provider"] == "anthropic" and st["key"]:
            # billed to the key rather than the machine's sign-in
            env = {**os.environ, "ANTHROPIC_API_KEY": st["key"]}
        return cmd, env
    # Any OpenAI-shaped server: LM Studio, llama.cpp, OpenAI itself.
    # openai_agent.py replays the same stream-json dialect Claude Code
    # speaks, so everything downstream of the Popen is one code path — and
    # its transcript file is the local equivalent of --resume.
    url = st["url"] or LLM_URLS.get(st["provider"], "")
    if not url:
        raise RuntimeError("no server address for the model yet. "
                           "The Settings tab holds it.")
    tname = re.sub(r"[^a-z0-9_-]+", "-", (project or "library").lower())
    conf = {"url": url, "key": st["key"], "model": st["model"],
            "rules": rules, "prompt": prompt, "fresh": bool(fresh),
            "transcript": str(ROOT / "chat_local" / f"{tname}.json")}
    env = {**os.environ, "SAGA_LLM": json.dumps(conf),
           "SAGA_API": f"http://127.0.0.1:{PORT}", "SAGA_TOKEN": TOKEN}
    return [sys.executable, str(HERE / "openai_agent.py")], env


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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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

    # ── the discuss stream ──────────────────────────────────────────────
    def _sse(self, ev):
        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
        self.wfile.flush()

    def _chat(self, d):
        """Run one ask as a server-sent event stream.

        The panel reads it with a fetch reader, so the connection doubles as
        the stop signal: close the tab or press stop and the write fails,
        which kills the agent rather than letting it spend on nobody."""
        project = d.get("name") or ""
        with _chats_lock:
            if project in _chats and _chats[project].poll() is None:
                return self._send(409, {"error": "already thinking — "
                                        "stop that first"})
        try:
            cmd, cenv = _chat_cmd(d.get("name"), d["question"],
                                  d.get("chunks"), bool(d.get("fresh")))
        except FileNotFoundError as ex:
            return self._send(500, {"error": f"missing file: {ex}"})
        except RuntimeError as ex:
            return self._send(500, {"error": str(ex)})
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    cwd=str(HERE), env=cenv)
        except FileNotFoundError:
            return self._send(500, {"error": "Claude Code is not installed "
                                    "— install `claude` or set SAGA_CLAUDE"})
        with _chats_lock:
            _chats[project] = proc

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # A watchdog instead of a deadline: research takes as long as it
        # takes, but silence past CHAT_IDLE_S means wedged, not thinking.
        last = [time.time()]
        t0 = time.time()

        def watchdog():
            while proc.poll() is None:
                if (time.time() - last[0] > CHAT_IDLE_S
                        or time.time() - t0 > CHAT_MAX_S):
                    proc.kill()
                    return
                time.sleep(2)

        threading.Thread(target=watchdog, daemon=True).start()
        errtail = []

        def drain_err():
            for ln in proc.stderr:
                errtail.append(ln.strip())
                del errtail[:-4]

        errt = threading.Thread(target=drain_err, daemon=True)
        errt.start()

        streamed = 0            # deltas sent; the fallback for CLIs without
        done = False            # partial messages only fires when none came
        actions = []            # mutating tool calls seen, for the journal
        try:
            for line in proc.stdout:
                last[0] = time.time()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "system" and ev.get("subtype") == "init":
                    if ev.get("session_id"):
                        _remember_session(project, ev["session_id"])
                    self._sse({"e": "start"})
                elif t == "stream_event":
                    e2 = ev.get("event") or {}
                    if e2.get("type") == "content_block_delta":
                        dl = e2.get("delta") or {}
                        if dl.get("type") == "text_delta" and dl.get("text"):
                            streamed += len(dl["text"])
                            self._sse({"e": "t", "t": dl["text"]})
                    elif e2.get("type") == "content_block_start":
                        cb = e2.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            self._sse({"e": "tool", "name":
                                       (cb.get("name") or "").split("__")[-1]})
                elif t == "assistant":
                    for cb in ((ev.get("message") or {}).get("content") or []):
                        if cb.get("type") == "tool_use":
                            short = (cb.get("name") or "").split("__")[-1]
                            if short in CHAT_JOURNAL:
                                actions.append((short, cb.get("input") or {}))
                            self._sse({"e": "tooldone", "name": short,
                                       "brief": json.dumps(
                                           cb.get("input") or {})[:160]})
                        elif (cb.get("type") == "text" and cb.get("text")
                              and not streamed):
                            self._sse({"e": "t", "t": cb["text"]})
                elif t == "user":
                    for cb in ((ev.get("message") or {}).get("content") or []):
                        if (isinstance(cb, dict)
                                and cb.get("type") == "tool_result"
                                and cb.get("is_error")):
                            txt = cb.get("content")
                            if isinstance(txt, list):
                                txt = " ".join(x.get("text", "") for x in txt
                                               if isinstance(x, dict))
                            self._sse({"e": "toolerr", "t": str(txt)[:200]})
                elif t == "result":
                    done = True
                    if ev.get("session_id"):
                        _remember_session(project, ev["session_id"])
                    self._sse({"e": "done"})
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()         # the panel went away; stop spending
        finally:
            proc.wait()
            with _chats_lock:
                if _chats.get(project) is proc:
                    del _chats[project]
        if done and actions:
            # the ask finished and changed something: journal it, so the
            # next conversation opens already knowing what this one did
            _journal(project, str(d.get("question") or ""), actions)
        if not done:
            errt.join(timeout=2)     # stderr hits EOF once the child is gone
            tail = " · ".join(x for x in errtail if x)[-300:]
            try:
                # "Not logged in · Please run /login" is Claude Code's way of
                # saying the machine has never signed in. The panel turns
                # that into hand-holding rather than terminal jargon.
                if re.search(r"not logged in|please run /login|invalid api key",
                             tail, re.I):
                    self._sse({"e": "auth"})
                else:
                    self._sse({"e": "err", "t": "the agent stopped early"
                               + (f" — {tail}" if tail else "")})
            except OSError:
                pass

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
        if u.path == "/stage":
            # the audience's window: visuals, captions and — for stories with
            # choice cards — the chooser. Read from disk per request like the
            # editor page, so a reload picks up front-end changes.
            return self._send(200, (HERE / "stage_ui.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/player.js":
            # the choice grammar's one evaluator, shared by the stage and the
            # HTML export — see the file's own preamble for why it is alone
            return self._send(200, (HERE / "player.js").read_bytes(),
                              "text/javascript; charset=utf-8")
        if u.path == "/desk.js":
            # the live mixing desk — the Web Audio graph the stage and the
            # editor both play through. Never inlined into an export: the
            # exported player has no server and stays premixed.
            return self._send(200, (HERE / "desk.js").read_bytes(),
                              "text/javascript; charset=utf-8")
        if u.path == "/api/state":
            pcounts, ccounts, mcounts, mhome = library_counts()
            return self._send(200, {
                "projects": projects(),
                # the shelves, and who is on them — the sidebar draws the
                # tree from this and calls everything else Unfiled
                "series": series_state(),
                # with durations: the editor shows them, and a reference clip
                # past ten seconds is worth seeing, since chatterbox reads only
                # the first ten and the rest has never been heard
                "voices": sorted(({"name": p.stem, "secs": clip_secs(p)}
                                  for p in VOICES.glob("*.wav")),
                                 key=lambda v: v["name"]),
                # `added` is the file's mtime — rename preserves it, so a clip
                # keeps the date it arrived rather than the date you retitled it
                "clips": sorted(({"name": p.stem, "secs": clip_secs(p),
                                  "added": round(p.stat().st_mtime),
                                  "size": p.stat().st_size}
                                 for p in CLIPS.glob("*.wav")),
                                key=lambda c: c["name"]),
                "media": media_list(),
                # the cast rides along whole: a registry of briefs and plate
                # names is small next to the media list, and the tab and the
                # board both draw straight from it
                "cast": cast(),
                "profiles": profiles(), "profile_counts": pcounts,
                "clip_counts": ccounts, "media_counts": mcounts,
                # which story knows each picture, and how — the visuals pool
                # draws its folders from this and files nothing by hand
                "media_home": mhome,
                "defaults": DEFAULTS, "bake": _bake,
                "engines": list(ENGINES), "omnivoice": ov_available(),
                "chatterbox": cb_available(),
                "kokoro": kokoro_available(), "kvoices": kokoro_voices(),
                "stale_build": build_stale(),
                # whether the discuss agent has a Claude to speak with —
                # checked fresh each time, so installing it mid-session is
                # noticed on the next refresh. Whether that Claude is signed
                # in is only knowable by asking, so the panel learns it then.
                "claude": Path(claude_path()).exists(),
                # which brain Discuss speaks with — the panel words its
                # invitation around it, and a local provider needs no claude
                "llm_provider": settings()["llm"]["provider"],
                # whether generate_media can paint — like the claude check,
                # fresh each time, so pasting a key in works without a
                # restart. Still called nanobanana on the wire: the ✨
                # button and the agent's overview both read this name.
                "nanobanana": image_ready(),
                "model": "warm" if _cb["warm"] else "cold"})
        if u.path == "/api/settings":
            # localhost and token-guarded, the author's own machine asking:
            # the keys are theirs to see back
            return self._send(200, settings())
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
        if u.path == "/api/takes":
            # Every reading this card has on disk, for the take menu. The
            # files are the truth: nothing records how far a card was ever
            # rolled, so probe a window of seeds past everything still
            # referenced and report which hashes exist. A take orphaned by a
            # text edit simply stops appearing — accurate, since its words
            # are no longer these words.
            doc = load(q.get("name", [""])[0])
            if not doc:
                return self._send(404, {"error": "no such project"})
            cid = int(q.get("id", ["-1"])[0])
            c = next((x for x in doc["chunks"] if x["id"] == cid), None)
            if c is None or not is_renderable(c):
                return self._send(400, {"error": "that card does not render"})
            cur = int(c.get("seed") or 0)
            hidden = sorted({int(h) for h in (c.get("hidden_takes") or [])})
            top = max([cur, 24] + [h for h in hidden]) + 8
            probe = dict(c)
            takes = []
            for n in range(0, top + 1):
                if n:
                    probe["seed"] = n
                else:
                    probe.pop("seed", None)
                f = AUDIO / f"{chunk_hash(probe, doc)}.wav"
                have = f.exists()
                # unrendered seeds are only worth naming when something
                # still points at them: the current pick, or a hidden mark
                if have or n == cur or n in hidden:
                    takes.append({"take": n, "ready": have,
                                  **({"secs": round(clip_secs(f), 2)}
                                     if have else {})})
            return self._send(200, {"takes": takes, "current": cur,
                                    "hidden": hidden})
        if u.path == "/api/doc":
            doc = load(q.get("name", [""])[0])
            if not doc:
                return self._send(404, {"error": "no such project"})
            for c in doc["chunks"]:
                c.setdefault("mute", False)
                c.setdefault("tags", [])
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
                elif c["type"] == "visual":
                    try:
                        f = media_file(c.get("media") or "")
                    except FileNotFoundError:
                        f = None
                    c["ready"] = f is not None
                    c["mediakind"] = media_kind(f) if f else ""
                elif c["type"] == "choice":
                    c["ready"] = True          # nothing to render, ever
                    c.setdefault("options", [])
                    c.setdefault("auto", False)
                    c.setdefault("wait", 0)
                else:              # silence and titles have nothing to render
                    c["ready"] = True
            return self._send(200, doc)
        if u.path == "/api/engines":
            return self._send(200, engines_status())
        if u.path == "/api/jobs":
            nm = q.get("name", [""])[0]
            js = [j for j in _jobs.values() if not nm or j["project"] == nm]
            js.sort(key=lambda j: j["queued_at"])
            return self._send(200, {"jobs": js[-60:],
                                    "busy": sum(1 for j in js
                                                if j["status"] in ("queued", "running"))})
        if u.path == "/api/queue":
            # the printer-queue window: every job in the table, every
            # project — /api/jobs narrows to one document for the card
            # buttons, this answers for the whole studio
            with _qlock:
                js = [dict(j) for j in _jobs.values()]
                paused = _qstate["paused"]
            js.sort(key=lambda j: j["queued_at"])
            return self._send(200, {
                "jobs": js[-80:], "paused": paused, "bake": _bake,
                "busy": sum(1 for j in js
                            if j["status"] in ("queued", "running"))})
        if u.path == "/api/history":
            # the render-history menu: every way this card has been
            # rendered, each entry told whether its wav is still on this
            # machine (a transferred project keeps the entries, not the
            # audio — restoring one of those re-renders, and says so)
            doc = load(q.get("name", [""])[0])
            if not doc:
                return self._send(404, {"error": "no such project"})
            cid = int(q.get("id", ["-1"])[0])
            c = next((x for x in doc["chunks"] if x["id"] == cid), None)
            if c is None or not is_renderable(c):
                return self._send(400, {"error": "that card does not render"})
            out = []
            for e in (c.get("hist") or []):
                if not isinstance(e, dict) or not e.get("h"):
                    continue
                f = AUDIO / f"{e['h']}.wav"
                have = f.exists()
                out.append({**e, "ready": have,
                            **({"secs": round(clip_secs(f), 2)}
                               if have else {})})
            return self._send(200, {"hist": out,
                                    "current": chunk_hash(c, doc)})

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
            # is a multiply, so it happens on the way out with no cache. The
            # card's mixer channel and the Master bus fold in for the same
            # reason: this button promises the book's own sound.
            g = (float(pp.get("gain", 100))
                 * channel_gain_of(c, channel_gains(doc)) * master_gain(doc))
            if g != 100.0:
                import io
                import soundfile as sf
                audio, asr = sf.read(str(f), dtype="float32")
                buf = io.BytesIO()
                sf.write(buf, audio * (g / 100.0), asr, format="WAV", subtype="FLOAT")
                return self._send(200, buf.getvalue(), "audio/wav")
            return self._send_file(f, "audio/wav")
        if u.path == "/api/card_wav":
            # the desk's feed: one card, fx-rendered and GAIN-FREE — profile,
            # channel and Master levels are the browser graph's business now,
            # applied live on its own nodes. card_audio above keeps them
            # baked, for the lone ▶ Full button that plays outside the desk.
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
            eff = fx_of(params_for(c, doc))
            if eff:
                try:
                    f = fx_render(f, eff)
                except Exception as ex:
                    print(f"plugin failed on card {cid}: "
                          f"{type(ex).__name__}: {ex}", flush=True)
            return self._send_file(f, "audio/wav")
        if u.path == "/api/take":
            nm = re.sub(r"[^a-z0-9]", "", q.get("f", [""])[0])
            f = take_path(nm)
            if not nm or not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "audio/wav")
        if u.path == "/api/media":
            nm = re.sub(r"[^a-z0-9_-]", "", q.get("f", [""])[0])
            try:
                f = media_file(nm) if nm else None
            except FileNotFoundError:
                f = None
            if not f:
                return self._send(404, b"", "text/plain")
            # film runs to real megabytes; stream it like a book
            return self._send_file(f, MEDIA_MIME.get(f.suffix.lower(),
                                                     "application/octet-stream"))
        if u.path == "/api/plate":
            # a plate by member and file name. Both halves are checked against
            # the alphabets their writers use, so this cannot walk a path.
            slug = q.get("slug", [""])[0]
            fn = q.get("f", [""])[0]
            if (not CAST_SLUG_RE.match(slug)
                    or not re.fullmatch(r"[a-z0-9_.-]{1,80}", fn)
                    or ".." in fn):
                return self._send(404, b"", "text/plain")
            f = CAST / slug / fn
            if not f.is_file():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, MEDIA_MIME.get(f.suffix.lower(),
                                                     "application/octet-stream"))
        if u.path == "/api/share_info":
            # the standing darkride link, if this story has one — the token
            # stays home; only the URL is anyone's business
            try:
                s = json.loads((pdir(q.get("name", [""])[0]) / "share.json")
                               .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                s = {}
            return self._send(200, {"url": s.get("url"), "at": s.get("at"),
                                    "unlisted": bool(s.get("unlisted")),
                                    "source_url": (s.get("source") or {}).get("url"),
                                    "source_at": (s.get("source") or {}).get("at")})
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
        if u.path == "/api/download_video":
            nm = q.get("name", [""])[0]
            f = pdir(nm) / "out" / f"{pdir(nm).name}.mp4"
            if not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "video/mp4", f.name)
        if u.path == "/api/download_web":
            nm = q.get("name", [""])[0]
            f = pdir(nm) / "out" / f"{pdir(nm).name}-web.zip"
            if not f.exists():
                return self._send(404, b"", "text/plain")
            return self._send_file(f, "application/zip", f.name)

        if u.path in ("/api/export", "/api/export_plan"):
            names = ([p["name"] for p in projects() if not p.get("broken")]
                     if q.get("all", [""])[0] == "1" else q.get("name", []))
            plan = plan_export(names, q.get("audio", ["1"])[0] == "1")
            if u.path == "/api/export_plan":
                plan.pop("_audio", None)
                plan.pop("_profiles", None)
                plan.pop("_cast", None)
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
        lands in clips/ is always a plain PCM wav this program understands.

        And it never overwrites — the rule voices have always lived by. Cards
        point at a clip by name, so different audio landing under a taken name
        would change what they play with nothing to mark the seam; and the
        recording desk files takes under friendly stems that repeat, which
        must never eat yesterday's take. The same bytes again cost nothing;
        different bytes wanting a taken name land beside it."""
        fn = parse_qs(u.query).get("fn", ["clip"])[0]
        stem = re.sub(r"[^a-z0-9_-]+", "-", Path(fn).stem.lower()).strip("-")[:40] or "clip"
        if not shutil.which("ffmpeg"):
            return self._send(400, {"error": "ffmpeg is needed to import audio clips"})
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".clip-",
                                   suffix=Path(fn).suffix or ".bin")
        wav = None
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            CLIPS.mkdir(parents=True, exist_ok=True)
            fd2, wav = tempfile.mkstemp(dir=CLIPS, prefix=".clip-", suffix=".wav")
            os.close(fd2)
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", tmp, wav], capture_output=True, text=True)
            if r.returncode:
                return self._send(400, {"error": "ffmpeg could not read that file: "
                                        + (r.stderr or "").strip()[-300:]})
            name, renamed = stem, False
            dest = CLIPS / f"{stem}.wav"
            if dest.exists() and sha256_file(dest) == sha256_file(Path(wav)):
                Path(wav).unlink(missing_ok=True)   # byte for byte what is here
            else:
                if dest.exists():
                    taken = {p.stem for p in CLIPS.glob("*.wav")}
                    name, renamed = _free_name(taken, stem, "new"), True
                    dest = CLIPS / f"{name}.wav"
                os.replace(wav, dest)
                os.chmod(dest, 0o644)              # mkstemp makes it 0600
            wav = None
            return self._send(200, {"ok": True, "clip": name, "secs": clip_secs(dest),
                                    "renamed": renamed})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)
            if wav:
                Path(wav).unlink(missing_ok=True)

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

    def _media_upload(self, u):
        """A picture or a piece of film for visual cards.

        Raw body like every other upload. Film passes through untouched, but
        a still is pressed to WebP on arrival (webp_still — quality 82, only
        kept when smaller): the stage shows it identically, and every export,
        source archive and darkride upload gets lighter for free. Never
        overwrites, same rule as clips: media is global, and replacing a
        name would change every episode showing it.

        Except on purpose. ?over=1 is the asset inspector's Apply: an edit of
        the picture BEHIND the name, asked for by the person looking at it —
        the press to WebP set the precedent that bytes may change while the
        name holds, and every card showing it following along is the point."""
        over = parse_qs(u.query).get("over", ["0"])[0] == "1"
        fn = parse_qs(u.query).get("fn", ["media"])[0]
        ext = Path(fn).suffix.lower()
        if ext not in IMG_EXT + VID_EXT:
            return self._send(400, {"error": f"cannot show “{ext or fn}” — images "
                                    "are png/jpg/webp/gif, film is mp4/webm/mov"})
        stem = re.sub(r"[^a-z0-9_-]+", "-", Path(fn).stem.lower()).strip("-")[:40] or "media"
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".media-", suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            # stills arrive pressed to WebP — BEFORE the dedupe below, so
            # importing the same picture twice still hashes to one file
            w = webp_still(Path(tmp))
            if w:
                Path(tmp).unlink(missing_ok=True)
                tmp, ext = str(w), ".webp"
            MEDIA.mkdir(parents=True, exist_ok=True)
            name = stem
            try:
                existing = media_file(stem)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if sha256_file(existing) == sha256_file(Path(tmp)):
                    return self._send(200, {"ok": True, "media": stem,
                                            "kind": media_kind(existing)})
                if over:
                    existing.unlink()          # the old ext may differ; clear it
                else:
                    taken = {p.stem for p in MEDIA.iterdir() if p.is_file()}
                    name = _free_name(taken, stem, "new")
            dest = MEDIA / f"{name}{ext}"
            os.replace(tmp, dest)              # same filesystem: tmp lives in ROOT
            os.chmod(dest, 0o644)              # mkstemp makes it 0600
            return self._send(200, {"ok": True, "media": name,
                                    "kind": media_kind(dest),
                                    "renamed": name != stem, "asked": stem})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _cast_upload(self, u):
        """A plate arriving from Finder, straight into a member's folder.

        Same manners as the pool: a still is pressed to WebP on arrival, and
        nothing is ever overwritten — a plate's file name is about to live
        inside stored refs, so a second arrival under the same stem gets a
        new name rather than replacing the first. The slot is named after the
        file and is the author's to rename; the first plate a member owns
        becomes its key, since a key of nothing serves nobody."""
        qq = parse_qs(u.query)
        slug = qq.get("slug", [""])[0]
        fn = qq.get("fn", ["plate"])[0]
        ext = Path(fn).suffix.lower()
        if ext not in IMG_EXT:
            return self._send(400, {"error": f"a plate is a picture — "
                                    f"png/jpg/webp/gif, not “{ext or fn}”"})
        if not CAST_SLUG_RE.match(slug):
            return self._send(404, {"error": "no such cast member"})
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".plate-", suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                self._read_body_to(f)
            w = webp_still(Path(tmp))
            if w:
                Path(tmp).unlink(missing_ok=True)
                tmp, ext = str(w), ".webp"
            with _docmut:
                c = cast()
                m = c.get(slug)
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                stem = (re.sub(r"[^a-z0-9_-]+", "-", Path(fn).stem.lower())
                        .strip("-")[:40] or "plate")
                folder = CAST / slug
                folder.mkdir(parents=True, exist_ok=True)
                taken = {p.stem for p in folder.iterdir() if p.is_file()}
                fname = _free_name(taken, stem, "new") + ext
                dest = folder / fname
                os.replace(tmp, dest)          # same filesystem: tmp is in ROOT
                os.chmod(dest, 0o644)          # mkstemp makes it 0600
                plates = m.setdefault("plates", {})
                slot = _free_name(set(plates), stem, "new")
                plates[slot] = {"file": fname}
                if not m.get("key"):
                    m["key"] = slot
                save_cast(c)
            return self._send(200, {"ok": True, "plate": slot, "file": fname})
        except Exception as ex:
            return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            Path(tmp).unlink(missing_ok=True)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "unauthorised"})
        if u.path == "/api/import_archive":
            return self._import_archive(u)
        if u.path == "/api/cast/upload":       # raw body, so before the JSON parse
            return self._cast_upload(u)
        if u.path == "/api/media/upload":      # likewise
            return self._media_upload(u)
        if u.path == "/api/clip/upload":       # likewise
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
        # The engine manager: no project and no doc lock — an install runs in
        # its own thread, and these requests only start it, stop it or take
        # an engine out again.
        if u.path == "/api/engines/install":
            try:
                install_engine(str(d.get("engine") or ""))
            except (ValueError, RuntimeError) as ex:
                return self._send(400, {"error": str(ex)})
            return self._send(200, {"ok": True, **engines_status()})
        if u.path == "/api/engines/cancel":
            cancel_install()
            return self._send(200, {"ok": True})
        if u.path == "/api/engines/remove":
            try:
                remove_engine(str(d.get("engine") or ""))
            except (ValueError, RuntimeError) as ex:
                return self._send(400, {"error": str(ex)})
            return self._send(200, {"ok": True, **engines_status()})
        # Nanobanana: tens of seconds and library-only — it touches no doc, so
        # it answers here with the other lockless routes and never blocks
        # typing. Each thread is its own request, so parallel paintings are
        # fine; the never-overwrite rule keeps their names from colliding.
        if u.path == "/api/media/generate":
            # when the caller says which card is painting, the style tiers
            # above it ride along (CAST.md §5) — unless the card said no.
            # Composed here and not in generate_media because only the doc
            # knows its shelf and only the card knows its `nostyle`.
            style = None
            nm = str(d.get("name") or "")
            if nm:
                doc = load(nm)
                c = (next((x for x in doc["chunks"] if x["id"] == d.get("id")),
                          None) if doc else None)
                if doc and not (c or {}).get("nostyle"):
                    style = style_of(doc)
            jid = job_start("paint", nm, d.get("id"),
                            str(d.get("prompt") or ""))
            try:
                mname = generate_media(str(d.get("prompt") or ""),
                                       str(d.get("media") or ""),
                                       str(d.get("aspect") or ""),
                                       d.get("ref") or "",   # name or list
                                       style,
                                       str(d.get("vary") or ""))
            except (ValueError, RuntimeError) as ex:
                job_end(jid, error=str(ex))
                return self._send(400, {"error": str(ex)})
            job_end(jid)
            if nm and d.get("id") is not None:
                # the words that painted this variant stay with it (the
                # visual twin of a speech card's render history)
                file_paint_history(nm, d.get("id"), mname,
                                   str(d.get("prompt") or ""),
                                   d.get("ref") or "",
                                   str(d.get("vary") or ""), style)
            return self._send(200, {"ok": True, "media": mname,
                                    "kind": "image"})
        # A darkride source link, imported without the browser detour: the
        # server downloads the .sagaproj the link points at, then restores
        # it exactly as a dropped file would be. The download runs OUTSIDE
        # every lock — a gigabyte on finca wifi must not freeze typing —
        # and only the restore itself takes _docmut, like any drop.
        if u.path == "/api/import_link":
            mode = str(d.get("mode") or "skip")
            if mode not in ("skip", "replace", "copy"):
                return self._send(400, {"error": f"unknown mode {mode!r}"})
            fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".linkimport-",
                                       suffix=".tgz")
            os.close(fd)
            try:
                try:
                    fetch_source_link(str(d.get("url") or "").strip(),
                                      Path(tmp))
                except RuntimeError as ex:
                    return self._send(400, {"error": str(ex)})
                with _docmut:
                    return self._send(200, {"ok": True,
                                            **import_archive(Path(tmp), mode)})
            except Exception as ex:
                return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})
            finally:
                Path(tmp).unlink(missing_ok=True)
        # Press every still already in the pool to WebP: names keep, bytes
        # drop, and the next export or upload is lighter for it. Files only
        # ever REPLACED by a smaller same-picture, so pool law holds in
        # spirit: no name changes meaning.
        if u.path == "/api/media/compress":
            # the whole pool by default; with a name, just that one still —
            # the asset inspector's own press button
            only = re.sub(r"[^a-z0-9_-]", "", str(d.get("media") or ""))
            pressed, saved = 0, 0
            for p in sorted(MEDIA.iterdir()) if MEDIA.exists() else []:
                if not p.is_file() \
                        or p.suffix.lower() not in (".png", ".jpg", ".jpeg") \
                        or (only and p.stem != only):
                    continue
                was = p.stat().st_size
                w = webp_still(p)
                if w:
                    os.chmod(w, 0o644)
                    p.unlink()
                    pressed += 1
                    saved += was - w.stat().st_size
            return self._send(200, {"ok": True, "pressed": pressed,
                                    "saved": saved})
        if u.path == "/api/clip/trim":
            # A trim never edits the clip: cards point at it by name, and a
            # shorter file behind the same name would change what they play
            # with nothing to mark the seam. The kept stretch lands beside it
            # as a copy, named for what it is.
            try:
                src = clip_file(re.sub(r"[^a-z0-9_-]", "",
                                       str(d.get("clip") or "")))
            except FileNotFoundError:
                return self._send(404, {"error": "no such clip"})
            try:
                a, b = float(d.get("start") or 0), float(d.get("end") or 0)
            except (TypeError, ValueError):
                return self._send(400, {"error": "start and end are seconds"})
            total = clip_secs(src)
            a, b = max(0.0, min(a, total)), max(0.0, min(b, total))
            if b - a < 0.1:
                return self._send(400, {"error": "keep at least a tenth of a second"})
            if not shutil.which("ffmpeg"):
                return self._send(400, {"error": "ffmpeg is needed to trim a clip"})
            taken = {p.stem for p in CLIPS.glob("*.wav")}
            name, i = f"{src.stem}-trim", 2
            while name in taken:
                name, i = f"{src.stem}-trim-{i}", i + 1
            dest = CLIPS / f"{name}.wav"
            # -ss/-to on the OUTPUT side of a wav decode is sample-accurate;
            # there are no keyframes in PCM to snap to
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                "-y", "-i", str(src), "-ss", f"{a:.3f}",
                                "-to", f"{b:.3f}", str(dest)],
                               capture_output=True, text=True)
            if r.returncode:
                dest.unlink(missing_ok=True)
                return self._send(400, {"error": "ffmpeg could not trim that: "
                                        + (r.stderr or "").strip()[-300:]})
            os.chmod(dest, 0o644)
            return self._send(200, {"ok": True, "clip": name,
                                    "secs": clip_secs(dest)})
        if u.path == "/api/take/trim":
            # A performance trimmed is a NEW take. Takes are content-addressed
            # precisely so a rendered card keeps the exact recording it
            # rendered from — so the trim hashes to its own name, the caller
            # repoints the card, and the dot goes stale honestly.
            name = re.sub(r"[^0-9a-f]", "", str(d.get("take") or ""))[:20]
            src = take_path(name) if name else None
            if not name or not src.exists():
                return self._send(404, {"error": "no such take"})
            try:
                a, b = float(d.get("start") or 0), float(d.get("end") or 0)
            except (TypeError, ValueError):
                return self._send(400, {"error": "start and end are seconds"})
            total = clip_secs(src)
            a, b = max(0.0, min(a, total)), max(0.0, min(b, total))
            if b - a < 0.1:
                return self._send(400, {"error": "keep at least a tenth of a second"})
            if not shutil.which("ffmpeg"):
                return self._send(400, {"error": "ffmpeg is needed to trim a take"})
            fd2, wav = tempfile.mkstemp(dir=TAKES, prefix=".take-", suffix=".wav")
            os.close(fd2)
            try:
                r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                    "-y", "-i", str(src), "-ss", f"{a:.3f}",
                                    "-to", f"{b:.3f}", wav],
                                   capture_output=True, text=True)
                if r.returncode:
                    return self._send(400, {"error": "ffmpeg could not trim that: "
                                            + (r.stderr or "").strip()[-300:]})
                new = sha256_file(Path(wav))[:20]
                dest = take_path(new)
                if dest.exists():
                    Path(wav).unlink(missing_ok=True)  # already have this one
                else:
                    os.replace(wav, dest)
                    os.chmod(dest, 0o644)
                wav = None
                return self._send(200, {"ok": True, "perf": new,
                                        "secs": clip_secs(dest)})
            finally:
                if wav:
                    Path(wav).unlink(missing_ok=True)
        if u.path == "/api/take/to_clip":
            # a performance crossing the fence into the media pool, as a copy
            # — the take stays a take, the card keeps its pointer, and the
            # copy takes a clip name and every clip verb with it
            name = re.sub(r"[^0-9a-f]", "", str(d.get("take") or ""))[:20]
            src = take_path(name) if name else None
            if not name or not src.exists():
                return self._send(404, {"error": "no such take"})
            fn = str(d.get("fn") or "take")
            stem = re.sub(r"[^a-z0-9_-]+", "-", fn.lower()).strip("-")[:40] or "take"
            CLIPS.mkdir(parents=True, exist_ok=True)
            dest = CLIPS / f"{stem}.wav"
            if dest.exists() and sha256_file(dest) == sha256_file(src):
                pass                                   # already crossed over
            else:
                if dest.exists():
                    taken = {p.stem for p in CLIPS.glob("*.wav")}
                    stem = _free_name(taken, stem, "new")
                    dest = CLIPS / f"{stem}.wav"
                shutil.copy2(src, dest)
                os.chmod(dest, 0o644)
            return self._send(200, {"ok": True, "clip": stem,
                                    "secs": clip_secs(dest)})
        # Keeping current. Both are lockless: asking GitHub and replacing
        # program files touches no document, and the check in particular
        # must never make the editor wait on a slow network.
        if u.path == "/api/update/check":
            return self._send(200, update_check())
        if u.path == "/api/update/apply":
            try:
                note, now = update_apply()
            except Exception as ex:
                return self._send(400, {"error": str(ex)[:300]})
            return self._send(200, {"ok": True, "note": note, "current": now})
        # Editor settings: one small file of its own, no doc and no lock.
        if u.path == "/api/settings":
            return self._send(200, save_settings(d))
        # The agent's remember tool lands here: append-only into the memory
        # file, which has its own lock — never _docmut.
        if u.path == "/api/agent_note":
            note = str(d.get("note") or "").strip()
            if not note:
                return self._send(400, {"error": "an empty note remembers "
                                                 "nothing"})
            note_append("## " + time.strftime("%Y-%m-%d %H:%M") + " · note\n"
                        + note[:2000])
            return self._send(200, {"ok": True})
        # The Settings tab's paste button for the darkride key. Deliberately
        # narrow: it reads the clipboard, and what comes back to the page is
        # a key or a refusal, never whatever else was on there. The check is
        # the point: a key-shaped thing gets saved, a sentence about a key
        # gets turned away at the door instead of being carried to darkride
        # on every upload from here on.
        if u.path == "/api/paste_key":
            got = clipboard_text().strip()
            if not got:
                return self._send(400, {"error": "the clipboard is empty"})
            if not DK_KEY.fullmatch(got):
                n = len(got)
                return self._send(400, {"error": (
                    f"the clipboard holds {n} character{'s' if n != 1 else ''} "
                    "that are not a darkride key. Keys read dk_ and then hex; "
                    "generate one on My Studio at darkride.ai and press the "
                    "copy button beside it.")})
            cur = save_settings({"darkride": {"key": got}})
            return self._send(200, {"ok": True, "key": cur["darkride"]["key"]})
        # The native application chooser, for the Settings tab's 📁 buttons.
        # Server-side via osascript rather than an electron dialog: the
        # studio page is ordinary web content on purpose (see preload.js),
        # and this way the classic browser launch gets the picker too.
        if u.path == "/api/choose_app":
            if sys.platform != "darwin":
                return self._send(400, {"error": "the picker is macOS only; "
                                        "type the command name instead"})
            what = {"image": "images", "audio": "audio",
                    "video": "video"}.get(str(d.get("kind") or ""), "files")
            try:
                # `as alias`, or the chooser hands back an application OBJECT
                # — and "POSIX path of" throws on those, which read as a
                # cancel here: the picker "worked" but nothing ever landed
                # in the settings field.
                r = subprocess.run(
                    ["osascript", "-e",
                     'POSIX path of (choose application with prompt '
                     f'"Which application opens your {what}?" as alias)'],
                    capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                return self._send(400, {"error": "the chooser timed out"})
            if r.returncode:                     # cancel arrives as an error
                return self._send(200, {"canceled": True})
            app = r.stdout.strip().rstrip("/")
            if not app:
                return self._send(200, {"canceled": True})
            return self._send(200, {"ok": True, "app": app})
        # The outside-editor watch: the page polls while a file is out for
        # surgery, and the answer says whether it changed on disk. A clip
        # that came back in a shape the wave module cannot read (float wav,
        # 24-bit — editors love those) is quietly re-encoded to the plain
        # PCM this program writes, in place, same name: the re-import.
        if u.path == "/api/edit_check":
            kind = str(d.get("kind") or "")
            name = str(d.get("file") or "")
            if not name or "/" in name or "\\" in name or name.startswith("."):
                return self._send(400, {"error": "a plain file name"})
            try:
                path = (CLIPS / f"{name}.wav") if kind == "clip" \
                    else media_file(name)
                st_ = path.stat()
            except (FileNotFoundError, OSError) as ex:
                return self._send(404, {"error": str(ex)})
            cur = f"{st_.st_mtime_ns}:{st_.st_size}"
            last = str(d.get("stamp") or "")
            if not last or last == cur:
                return self._send(200, {"stamp": cur, "changed": False})
            if time.time() - st_.st_mtime < 1.2:
                # mid-save: answer "no change yet" and let the next poll act
                return self._send(200, {"stamp": last, "changed": False,
                                        "settling": True})
            resynced = False
            if kind == "clip":
                try:
                    with wave.open(str(path)):
                        pass
                except Exception:
                    if not shutil.which("ffmpeg"):
                        return self._send(400, {"error": "the clip came back "
                                                "in a format this program "
                                                "cannot read, and ffmpeg is "
                                                "not here to convert it"})
                    fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".resync-",
                                               suffix=".wav")
                    os.close(fd)
                    r = subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error",
                         "-y", "-i", str(path), tmp],
                        capture_output=True, text=True)
                    if r.returncode:
                        Path(tmp).unlink(missing_ok=True)
                        return self._send(400, {"error": "ffmpeg could not "
                                                "read the edited clip: "
                                                + (r.stderr or "").strip()[-200:]})
                    os.replace(tmp, path)
                    os.chmod(path, 0o644)
                    resynced = True
                    st_ = path.stat()
                    cur = f"{st_.st_mtime_ns}:{st_.st_size}"
            return self._send(200, {"stamp": cur, "changed": True,
                                    "resynced": resynced})
        # Hand a clip or a picture to the author's chosen outside editor.
        # The file is found by its pool name, never by a path from the
        # request, so this cannot be pointed outside the library.
        if u.path == "/api/open_in":
            kind = {"image": "image", "video": "video",
                    "clip": "audio"}.get(str(d.get("kind") or ""))
            name = str(d.get("file") or "")
            if not kind or not name or "/" in name or "\\" in name \
                    or name.startswith("."):
                return self._send(400, {"error": "a kind and a plain "
                                                 "file name"})
            try:
                path = (CLIPS / f"{name}.wav") if d.get("kind") == "clip" \
                    else media_file(name)
            except FileNotFoundError as ex:
                return self._send(404, {"error": str(ex)})
            if not path.exists():
                return self._send(404, {"error": f"no clip '{name}'"})
            app = settings()["apps"].get(kind, "")
            if not app:
                return self._send(400, {"error": f"no {kind} editor chosen "
                                        "yet. The ⚙ Settings tab holds it."})
            if sys.platform == "darwin":
                r = subprocess.run(["open", "-a", app, str(path)],
                                   capture_output=True, text=True)
                if r.returncode:
                    return self._send(400, {"error": f'could not open '
                                            f'"{app}". Is that its name in '
                                            "/Applications?"})
            else:
                try:
                    subprocess.Popen([app, str(path)])
                except OSError as ex:
                    return self._send(400, {"error":
                                            f"could not run {app}: {ex}"})
            return self._send(200, {"ok": True, "app": app})
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
                                  "/api/mix_plan",
                                  "/api/reveal", "/api/open_link",
                                  "/api/cast/reveal", "/api/cast/open",
                                  "/api/media/open",
                                  # a painting takes ~15s and must not block
                                  # typing; its one registry write takes the
                                  # lock inside cast_paint
                                  "/api/cast/paint",
                                  "/api/profile/impact",
                                  # slow and read-only, the same shape as
                                  # assemble — they must not block typing
                                  # (share touches only share.json and out/)
                                  "/api/publish_video", "/api/publish_html",
                                  "/api/share", "/api/share_source") else _docmut
        if lock:
            lock.acquire()
        try:
            if u.path == "/api/import":
                doc = import_md(d["title"], d["markdown"])
                if d.get("draft"):
                    # the discuss agent's new stories are born as drafts:
                    # real once the author keeps them, gone if discarded
                    doc["draft"] = True
                    save(doc)
                return self._send(200, {"ok": True, "name": doc["name"],
                                        "chunks": len(doc["chunks"])})
            if u.path == "/api/chunk":
                doc = load(d["name"])
                snapshot(doc, "edit card")
                for c in doc["chunks"]:
                    if c["id"] == d["id"]:
                        # The lock, enforced where every edit lands rather than
                        # in whichever controls remember to ask. Presentation
                        # still moves — collapsing, naming, resizing a locked
                        # card changes how it sits, not what it says — and
                        # unlocking is its own request, never a rider on an
                        # edit that would then slip through with it.
                        if (c.get("locked")
                                and set(d) - {"name", "id", "locked",
                                              "min", "height", "label"}):
                            return self._send(400, {"error": "that card is locked "
                                              "— click its 🔒 to unlock it"})
                        if "locked" in d:
                            c["locked"] = bool(d["locked"])
                        if "text" in d:
                            c["text"] = normalise(d["text"])
                        if "note" in d:
                            c["note"] = d["note"]
                        if "tags" in d:
                            c["tags"] = clean_tags(d["tags"])
                        if "sub" in d:
                            # What the stage SHOWS while the card plays, when
                            # that differs from what is spoken — "Teide" on
                            # screen while the model is fed "tay-dee", or any
                            # words at all for a voiced card. Display only:
                            # never in any hash, so captioning costs nothing.
                            c["sub"] = str(d["sub"] or "")[:500]
                        if "tw" in d:
                            # this card's own typewriter word; absent inherits
                            # the story's. Display only — never in a hash.
                            if d["tw"] is None:
                                c.pop("tw", None)
                            else:
                                c["tw"] = 1 if d["tw"] else 0
                        if "twsfx" in d:
                            if d["twsfx"] is None:
                                c.pop("twsfx", None)
                            else:
                                c["twsfx"] = 1 if d["twsfx"] else 0
                        if "chain" in d:
                            # the caption stands when this card ends, and the
                            # next caption appends — one block across cards
                            # that were split for delivery, not for meaning
                            c["chain"] = bool(d["chain"])
                        if "when" in d:        # plays only when this holds
                            c["when"] = clean_when(d["when"])
                        # choice-card fields, validated against the grammar —
                        # the shape discipline params get, for the same reason
                        if "options" in d:
                            c["options"] = clean_options(d["options"])
                        if "auto" in d:
                            c["auto"] = bool(d["auto"])
                        if "wait" in d:    # 0 = wait for the listener forever
                            c["wait"] = clean_wait(d["wait"])
                        if "profile" in d:
                            c["profile"] = d["profile"]
                        if "mute" in d:
                            c["mute"] = bool(d["mute"])
                        if "channel" in d:
                            # which mixer fader this card answers to. Main is
                            # the default and stays unwritten; never in any
                            # hash, so sending a card elsewhere re-bakes nothing.
                            ch = re.sub(r"[^a-z0-9_-]", "",
                                        str(d["channel"] or ""))[:16]
                            if ch and ch != "main":
                                c["channel"] = ch
                            else:
                                c.pop("channel", None)
                        if "runon" in d:       # no rest before this card
                            c["runon"] = bool(d["runon"])
                        if "height" in d:          # editor height, persisted
                            c["height"] = int(d["height"])
                        if "min" in d:             # collapsed to its header bar
                            c["min"] = bool(d["min"])
                        if "label" in d:
                            # a name for the collapsed bar — "Maisie discovers
                            # the plan" reads better than a text fragment
                            c["label"] = str(d["label"] or "")[:80]
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
                        if "hidden_takes" in d:
                            # takes the menu keeps out of sight. Presentation
                            # only — never hashed, and the files stay on disk,
                            # so bringing one back costs nothing.
                            hs = sorted({int(x) for x in (d["hidden_takes"] or [])
                                         if isinstance(x, (int, float))
                                         and 0 <= int(x) < 10 ** 6})
                            if hs:
                                c["hidden_takes"] = hs
                            else:
                                c.pop("hidden_takes", None)
                        # audio-card fields; clamp here so a stray value can
                        # never put a negative duration on the timeline
                        if "clip" in d:
                            c["clip"] = re.sub(r"[^a-z0-9_-]", "", d["clip"] or "")
                        if "media" in d:       # visual-card pointer, clip-shaped
                            c["media"] = re.sub(r"[^a-z0-9_-]", "", d["media"] or "")
                        if "ref" in d:         # paint reference(s): name or list
                            c["ref"] = ref_store(ref_list(d["ref"]))
                            if not c["ref"]:
                                c.pop("ref", None)
                        if "nostyle" in d:     # this card opts out of the tiers
                            if d["nostyle"]:
                                c["nostyle"] = True
                            else:
                                c.pop("nostyle", None)
                        if "gen" in d:
                            # the variants painted for this visual card —
                            # names into the pool, presentation bookkeeping
                            # only: the files themselves are never deleted
                            gen = [re.sub(r"[^a-z0-9_-]", "", str(x))
                                   for x in (d["gen"] or [])
                                   if str(x or "").strip()][:40]
                            if gen:
                                c["gen"] = gen
                            else:
                                c.pop("gen", None)
                        if d.get("mode") in ("full", "after"):
                            c["mode"] = d["mode"]
                        if "after" in d:
                            c["after"] = max(0.0, float(d["after"] or 0))
                        if "gain" in d:
                            c["gain"] = max(0.0, min(200.0, float(d["gain"])))
                        if "fade" in d:
                            if c.get("type") == "title":
                                # seconds here, not percentages: [in, out] of
                                # the title's fades, and out never chases in
                                fi, fo = (list(d["fade"]) + [0.6, 0.6])[:2]
                                c["fade"] = [max(0.0, min(30.0, float(fi))),
                                             max(0.0, min(30.0, float(fo)))]
                            else:
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

            if u.path == "/api/minall":
                # fold or unfold the whole deck. Presentation, so the lock
                # does not object — the same rule a single card's collapse
                # follows — and no snapshot: a view change should not spend
                # an undo slot the way an edit does.
                doc = load(d["name"])
                on = bool(d.get("min"))
                for c in doc["chunks"]:
                    c["min"] = on
                save(doc)
                return self._send(200, {"ok": True, "min": on})

            if u.path == "/api/insert":
                doc = load(d["name"])
                snapshot(doc, f"insert {d.get('kind', 'text')} card")
                kind = d.get("kind", "speech")
                if kind == "audio":
                    c = {"id": 0, "type": "audio", "clip": "", "mode": "full",
                         "after": 5.0, "fade": [0, 100], "gain": 100, "note": ""}
                elif kind == "silence":
                    c = {"id": 0, "type": "silence", "secs": 1.0, "note": ""}
                elif kind == "visual":
                    c = {"id": 0, "type": "visual", "media": "", "note": ""}
                elif kind == "title":
                    # words on the wall with nobody speaking: fade in, hold,
                    # fade out — all three are seconds of silence in the mix
                    c = {"id": 0, "type": "title", "text": "", "secs": 3.0,
                         "fade": [0.6, 0.6], "note": ""}
                elif kind == "choice":
                    # two blank options, because a choice is usually a fork —
                    # and a chooser with one button is a button
                    c = {"id": 0, "type": "choice", "auto": False, "wait": 0,
                         "options": [{"label": "", "goto": "", "set": [], "when": ""},
                                     {"label": "", "goto": "", "set": [], "when": ""}],
                         "note": ""}
                elif kind == "voiced":
                    c = {"id": 0, "type": "voiced", "perf": "", "perfname": "",
                         "note": ""}
                elif kind == "group":
                    # an empty group is a valid card: a scene with a bar and a
                    # position, waiting for members — and, tagged, a jump target
                    taken = {x.get("gname") for x in doc["chunks"]
                             if x.get("type") == "group"}
                    taken |= {x.get("group") for x in doc["chunks"]
                              if x.get("group")}
                    # a caller may name the bar at birth — the discuss agent
                    # does — and a taken name is an error, not a quiet suffix:
                    # the caller believes in the name it asked for
                    want = re.sub(r"[\"'`\\<>&]", "",
                                  str(d.get("gname") or "")).strip()[:60]
                    if want and want in taken:
                        return self._send(400, {"error":
                                                f"“{want}” is already a group here"})
                    g, k = want or "New Group", 2
                    while g in taken:
                        g, k = f"New Group ({k})", k + 1
                    c = {"id": 0, "type": "group", "gname": g, "note": ""}
                else:
                    c = {"id": 0, "text": "", "params": {}, "note": ""}
                at = max(0, min(int(d.get("at", 0)), len(doc["chunks"])))
                grp = str(d.get("group") or "")
                if kind == "group":
                    # groups do not nest: born inside another's indent, the new
                    # bar slides down past that run rather than tearing it
                    ch = doc["chunks"]
                    if 0 < at < len(ch) and ch[at].get("group") \
                            and ch[at - 1].get("group") == ch[at].get("group"):
                        g0 = ch[at]["group"]
                        while at < len(ch) and ch[at].get("group") == g0:
                            at += 1
                elif grp and group_exists(doc["chunks"], grp):
                    # born inside a group's indent: a member, or the run
                    # would be torn in two by its own insert strip
                    c["group"] = grp
                doc["chunks"].insert(at, c)
                for i, c in enumerate(doc["chunks"]):
                    c["id"] = i
                save(doc)
                return self._send(200, {"ok": True, "id": at})

            if u.path == "/api/paste":
                doc = load(d["name"])
                at = max(0, min(int(d.get("at", 0)), len(doc["chunks"])))
                grp = str(d.get("group") or "")
                cards = d.get("cards")
                if isinstance(cards, list) and cards:
                    # A copied group arrives whole and lands as ONE undo
                    # step. Pasted card by card it took N presses of ⌘Z to
                    # take back — and the first press dissolved only the
                    # bar, leaving the members loose in the story.
                    gname = re.sub(r"[\"'`\\<>&]", "",
                                   str(d.get("gname") or "")).strip()[:60]
                    pcs = [paste_card(c) for c in cards if isinstance(c, dict)]
                    if not pcs:
                        return self._send(400, {"error": "nothing to paste"})
                    snapshot(doc, f"paste “{gname}”" if gname else "paste cards")
                    into = grp and group_exists(doc["chunks"], grp)
                    for i, pc in enumerate(pcs):
                        if pc.get("type") != "group":
                            if into:
                                pc["group"] = grp     # joins the host group
                            elif gname:
                                pc["group"] = gname   # becomes its own again
                        doc["chunks"].insert(at + i, pc)
                    for i, c in enumerate(doc["chunks"]):
                        c["id"] = i
                    save(doc)   # normalize_groups seats the new bar on load
                    return self._send(200, {"ok": True, "id": at,
                                            "count": len(pcs)})
                card = d.get("card")
                if not isinstance(card, dict):
                    return self._send(400, {"error": "nothing to paste"})
                snapshot(doc, "paste card")
                pc = paste_card(card)
                if grp and pc.get("type") != "group" \
                        and group_exists(doc["chunks"], grp):
                    pc["group"] = grp
                doc["chunks"].insert(at, pc)
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
                # The client says which side of a group's indent the drop
                # landed on — `into` a group, or `out` in the open — because
                # geometry the user can SEE must outrank inference. The old
                # neighbour guessing had one honest failure: the slot right
                # under a group leaves the card adjacent to its own run, and
                # adjacency read as membership, so dragging a member just
                # below its group silently rejoined it.
                into, out = d.get("into"), bool(d.get("out"))
                if to == src:
                    # not moving, but perhaps changing sides — "step out of
                    # the group" for a last member is exactly this
                    mv = ch[src]
                    changed = False
                    if out and mv.get("group") is not None:
                        snapshot(doc, "step out of the group")
                        mv.pop("group", None)
                        changed = True
                    elif (into and mv.get("group") != into
                          and group_exists(ch, into, mv)):
                        snapshot(doc, "join the group")
                        mv["group"] = into
                        changed = True
                    if changed:
                        save(doc)
                    return self._send(200, {"ok": True, "moved": changed})
                snapshot(doc, "move card")
                ch.insert(to, ch.pop(src))
                mv = ch[to]
                # neighbour inference still covers callers that say nothing
                prevg = ch[to - 1].get("group") if to > 0 else None
                nextg = ch[to + 1].get("group") if to + 1 < len(ch) else None
                if prevg and prevg == nextg:
                    mv["group"] = prevg
                elif mv.get("group") not in (prevg, nextg):
                    mv.pop("group", None)
                if out:
                    mv.pop("group", None)
                elif into and group_exists(ch, into, mv):
                    mv["group"] = into
                for i, c in enumerate(ch):
                    c["id"] = i
                save(doc)
                return self._send(200, {"ok": True, "moved": True})

            # ── groups ── a group is a contiguous run of cards sharing a name:
            # a chapter, a scene, a beat. Presentation and one drag-handle —
            # nothing downstream reads it: not the mix, not the walk, not the
            # exports. The run stays contiguous by construction: grouping
            # fills the span between the outermost picks, and a card dragged
            # out of its run leaves the group (see /api/move).
            if u.path == "/api/story":
                # story-level settings: how the tale presents itself, kept in
                # the doc so they travel with it — into the stage and the
                # exports alike. Never in any hash; changing them re-bakes
                # nothing.
                doc = load(d["name"])
                changed = False
                for k in ("typewriter", "typesfx"):
                    if k in d:
                        doc[k] = bool(d[k])
                        changed = True
                if "style" in d:
                    # the story's picture style (CAST.md §5): a TOP-LEVEL doc
                    # key, deliberately not a corner of params — params is
                    # the delivery bag, and style is not delivery. It rides
                    # with the doc through save, copy and export for free.
                    st = d.get("style") or {}
                    if not isinstance(st, dict):
                        return self._send(400, {"error": "style is "
                                                "{text, refs}"})
                    txt = _clean(st.get("text"), 500)
                    refs = ref_list(st.get("refs"))[:8]
                    if txt or refs:
                        doc["style"] = {"text": txt, "refs": refs}
                    else:
                        doc.pop("style", None)
                    changed = True
                if "channels" in d:
                    # the story's mixer: [{id, name, gain, mute}], main always
                    # present and always first. Ids are the identity and names
                    # are paint, so a rename rewrites no card. Gains land at
                    # mix time only — a fader never turns a rendered card
                    # amber. The default desk (main alone, at 100, unmuted)
                    # leaves no mark on the doc at all.
                    raw = d.get("channels")
                    if not isinstance(raw, list):
                        return self._send(400, {"error": "channels is a list "
                                                "of {id, name, gain, mute}"})
                    chans, seen = [], set()
                    for ch in raw[:24]:
                        if not isinstance(ch, dict):
                            continue
                        cid = re.sub(r"[^a-z0-9_-]", "",
                                     str(ch.get("id") or ""))[:16]
                        if not cid or cid in seen:
                            continue
                        seen.add(cid)
                        name = re.sub(r"[\"'`\\<>&]", "",
                                      str(ch.get("name") or "")).strip()[:40]
                        chans.append({"id": cid, "name": name or cid,
                                      "gain": _num(ch.get("gain"),
                                                   100.0, 0.0, 200.0),
                                      "mute": bool(ch.get("mute"))})
                    if "main" not in seen:
                        chans.insert(0, {"id": "main", "name": "Channel 1",
                                         "gain": 100.0, "mute": False})
                    else:
                        chans.sort(key=lambda ch: ch["id"] != "main")
                    # a card pointing at a channel that left returns to main —
                    # remove is atomic here, not a chore the UI might forget
                    live = {ch["id"] for ch in chans}
                    for c in doc["chunks"]:
                        if c.get("channel") and c["channel"] not in live:
                            c.pop("channel", None)
                    if (len(chans) == 1 and chans[0]["gain"] == 100.0
                            and not chans[0]["mute"]):
                        doc.pop("channels", None)
                    else:
                        doc["channels"] = chans
                    changed = True
                if "master" in d:
                    # the Master bus: {gain, mute}, over the whole mix at mix
                    # time. Unity and unmuted is the desk at rest, and leaves
                    # no mark on the doc.
                    m = d.get("master") or {}
                    if not isinstance(m, dict):
                        return self._send(400, {"error": "master is "
                                                "{gain, mute}"})
                    mg = _num(m.get("gain"), 100.0, 0.0, 200.0)
                    mm = bool(m.get("mute"))
                    if mg == 100.0 and not mm:
                        doc.pop("master", None)
                    else:
                        doc["master"] = {"gain": mg, "mute": mm}
                    changed = True
                if changed:
                    save(doc)
                return self._send(200, {"ok": True,
                                        "typewriter": bool(doc.get("typewriter")),
                                        "typesfx": bool(doc.get("typesfx")),
                                        "style": doc.get("style"),
                                        "channels": doc.get("channels"),
                                        "master": doc.get("master")})

            if u.path == "/api/group":
                doc = load(d["name"])
                # names land in onclick attributes and menu labels, so the
                # characters that could break out of either never get in
                gname = re.sub(r"[\"'`\\<>&]", "", str(d.get("gname") or "")).strip()[:60]
                if not gname:
                    return self._send(400, {"error": "a group needs a name"})
                ids = {int(i) for i in (d.get("ids") or [])}
                idx = [k for k, c in enumerate(doc["chunks"]) if c["id"] in ids]
                if not idx:
                    return self._send(404, {"error": "pick some cards first — "
                                            "shift-click their headers"})
                a, b = min(idx), max(idx)
                # groups do not nest, and a span that swallows another group's
                # bar would mark the bar itself as a member — the kind of quiet
                # corruption that surfaces as "where did my group go"
                if any(c.get("type") == "group"
                       for c in doc["chunks"][a:b + 1]):
                    return self._send(400, {"error": "that selection contains a "
                                            "group — ungroup it first, or group "
                                            "around it"})
                if any(c.get("group") == gname
                       or (c.get("type") == "group" and c.get("gname") == gname)
                       for c in doc["chunks"][:a] + doc["chunks"][b + 1:]):
                    return self._send(400, {"error": f"“{gname}” is already a group "
                                            f"here — pick another name"})
                snapshot(doc, f"group “{gname}”")
                for c in doc["chunks"][a:b + 1]:
                    c["group"] = gname
                save(doc)
                return self._send(200, {"ok": True, "gname": gname, "cards": b - a + 1})

            if u.path == "/api/ungroup":
                doc = load(d["name"])
                gname = d.get("gname")
                snapshot(doc, f"ungroup “{gname}”")
                n = 0
                for c in doc["chunks"]:
                    if c.get("group") == gname:
                        c.pop("group", None)
                        n += 1
                # dissolving the group takes its bar — and its anchor — with it
                doc["chunks"] = [c for c in doc["chunks"]
                                 if not (c.get("type") == "group"
                                         and c.get("gname") == gname)]
                save(doc)
                return self._send(200, {"ok": True, "cards": n})

            if u.path == "/api/group_rename":
                doc = load(d["name"])
                old = d.get("gname")
                new = re.sub(r"[\"'`\\<>&]", "", str(d.get("to") or "")).strip()[:60]
                if not new:
                    return self._send(400, {"error": "a name is needed"})
                owns = lambda c, g: (c.get("group") == g
                                     or (c.get("type") == "group"
                                         and c.get("gname") == g))
                if not any(owns(c, old) for c in doc["chunks"]):
                    return self._send(404, {"error": f"no group “{old}”"})
                if new != old and any(owns(c, new) for c in doc["chunks"]):
                    return self._send(400, {"error": f"“{new}” is already a group here "
                                            f"— pick another name"})
                snapshot(doc, f"rename group “{old}”")
                n = 0
                for c in doc["chunks"]:
                    if c.get("group") == old:
                        c["group"] = new
                        n += 1
                    if c.get("type") == "group" and c.get("gname") == old:
                        c["gname"] = new
                save(doc)
                return self._send(200, {"ok": True, "gname": new, "cards": n})

            if u.path == "/api/move_group":
                doc = load(d["name"])
                gname = d.get("gname")
                ch = doc["chunks"]
                # the bar rides with its run — and for an empty group the bar
                # IS the whole block
                idx = [k for k, c in enumerate(ch)
                       if c.get("group") == gname
                       or (c.get("type") == "group"
                           and c.get("gname") == gname)]
                if not idx:
                    return self._send(404, {"error": f"no group “{gname}”"})
                a, b = min(idx), max(idx) + 1
                to = max(0, min(int(d["to"]), len(ch)))
                # a group landing inside another's run would split it in two —
                # slide past to the end of that run instead
                if 0 < to < len(ch) and ch[to].get("group") \
                        and ch[to - 1].get("group") == ch[to].get("group") \
                        and ch[to].get("group") != gname:
                    g0 = ch[to]["group"]
                    while to < len(ch) and ch[to].get("group") == g0:
                        to += 1
                if a <= to <= b:
                    return self._send(200, {"ok": True, "moved": False})
                snapshot(doc, f"move group “{gname}”")
                block = ch[a:b]
                del ch[a:b]
                if to > b:
                    to -= len(block)
                ch[to:to] = block
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
                        dup = json.loads(json.dumps(c))
                        # a note is marginalia about where a card sits —
                        # except on a visual card, where it IS the paint
                        # prompt: the thing a duplicate most wants to keep
                        if c.get("type") != "visual":
                            dup["note"] = ""
                        out.append(dup)
                for i, c in enumerate(out):
                    c["id"] = i
                doc["chunks"] = out
                save(doc)
                return self._send(200, {"ok": True})

            if u.path == "/api/remove":
                doc = load(d["name"])
                if d.get("scope") in ("above", "below"):
                    # Trimming an import: everything on one side of this card
                    # goes, in one undo step. Locked cards stay — the lock
                    # means "finished", and a sweep is exactly the careless
                    # hand it exists to guard against. A swept group bar whose
                    # members survive grows back on save, which is right: the
                    # survivors keep their scene.
                    scope = d["scope"]
                    pos = next((i for i, c in enumerate(doc["chunks"])
                                if c["id"] == d["id"]), None)
                    if pos is None:
                        return self._send(404, {"error": "no such card"})
                    snapshot(doc, f"remove all {scope}")
                    keep, removed, kept = [], 0, 0
                    for i, c in enumerate(doc["chunks"]):
                        inside = i < pos if scope == "above" else i > pos
                        if inside and not c.get("locked"):
                            removed += 1
                            continue
                        if inside:
                            kept += 1
                        keep.append(c)
                    doc["chunks"] = keep
                    for i, c in enumerate(doc["chunks"]):
                        c["id"] = i
                    save(doc)
                    # after renumbering, the card's id is its index: unmoved
                    # for a below-sweep, shifted up by what left for an above
                    new_id = pos if scope == "below" else pos - removed
                    return self._send(200, {"ok": True, "removed": removed,
                                            "kept": kept, "id": new_id})
                gone = next((c for c in doc["chunks"] if c["id"] == d["id"]), None)
                if gone is not None and gone.get("locked"):
                    return self._send(400, {"error": "that card is locked — "
                                            "unlock it before removing it"})
                snapshot(doc, "remove card")
                if gone is not None and gone.get("type") == "group":
                    # removing the bar dissolves the group; the members stay,
                    # in place and in order — or save() would grow it right back
                    for c in doc["chunks"]:
                        if c.get("group") == gone.get("gname"):
                            c.pop("group", None)
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
                new["kvoice"] = re.sub(r"[^a-z0-9_]", "",
                                       str(new.get("kvoice") or "af_heart"))[:24] or "af_heart"
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
                # A kokoro profile has no clip: its preset auditions itself,
                # through the same loudness and plugin chain as everyone else.
                if d.get("kvoice"):
                    if not kokoro_available():
                        return self._send(400, {"error": "kokoro is not "
                                                "installed on this machine"})
                    try:
                        vf = kokoro_sample(d["kvoice"],
                                           _num(d.get("speed"), 0.0, 0.0, 3.0))
                    except BaseException as ex:
                        return self._send(500, {"error": f"kokoro: {ex}"})
                else:
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
                    if not is_renderable(c) or c.get("locked"):
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
                    # a locked card's words are exactly what the lock protects
                    if not is_speech(c) or c.get("locked"):
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


            if u.path == "/api/voice/from_card":
                # A rendered card becomes a reference clip, and a profile to
                # wear it. The point is the voiced card: you drive it with one
                # performance and a character's timbre, and what comes out is
                # a voice that never existed before — an accent that is not
                # yours in a mouth that is. Until now the only way to keep it
                # was to find the wav in the cache by hand.
                doc = load(d.get("name") or "")
                if doc is None:
                    return self._send(404, {"error": "no such project"})
                try:
                    cid = int(d.get("id"))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "which card?"})
                c = next((x for x in doc["chunks"] if x["id"] == cid), None)
                if c is None or not is_renderable(c):
                    return self._send(400, {"error": "that card makes no audio"})
                src = AUDIO / f"{chunk_hash(c, doc)}.wav"
                if not src.exists():
                    return self._send(400, {"error": "render this card first — "
                                            "there is no audio to clone yet"})
                pname = _clean(d.get("profile"), 60)
                if not pname:
                    return self._send(400, {"error": "a name is needed"})
                profs = profiles()
                if pname in profs:
                    return self._send(400, {"error": f"there is already a "
                                            f"profile called {pname!r}"})
                stem = re.sub(r"[^a-z0-9_-]+", "-", pname.lower()).strip("-")[:40] or "voice"
                VOICES.mkdir(parents=True, exist_ok=True)
                # Never overwrite a voice: its NAME is part of every chunk hash
                # that used it, so putting different audio behind one would
                # change what already-rendered cards mean without changing a
                # single hash. Same rule as import and upload.
                taken = {q.stem for q in VOICES.iterdir() if q.is_file()}
                vname = stem if stem not in taken else _free_name(taken, stem, "new")
                shutil.copy2(src, VOICES / f"{vname}.wav")
                # The new profile starts as the card's own, so the knobs that
                # shaped it carry over; only the clip changes. Kokoro speaks
                # presets rather than clips, so a card of that kind hands the
                # new profile to the engine that can actually use one.
                base = dict(profs.get(c.get("profile", "Default"))
                            or profs.get("Default") or BASE_PROFILE)
                base.pop("fx", None)
                base["voices"] = [vname]
                base["active"] = 0
                if base.get("engine") == "kokoro":
                    base["engine"] = "chatterbox"
                profs[pname] = base
                save_profiles(profs)
                return self._send(200, {"ok": True, "profile": pname,
                                        "voice": vname, "engine": base["engine"],
                                        "secs": clip_secs(VOICES / f"{vname}.wav")})
            if u.path == "/api/voice/from_clip":
                # from_card's sibling, reaching the other shelf: a clip in the
                # media pool — recorded at the desk, or imported — becomes the
                # reference clip of a new profile. A copy crosses over, never
                # the clip itself, so the audio cards pointing at it keep
                # playing exactly what they always played.
                try:
                    src = clip_file(str(d.get("clip") or ""))
                except FileNotFoundError:
                    return self._send(404, {"error": "no such clip"})
                pname = _clean(d.get("profile"), 60)
                if not pname:
                    return self._send(400, {"error": "a name is needed"})
                profs = profiles()
                if pname in profs:
                    return self._send(400, {"error": f"there is already a "
                                            f"profile called {pname!r}"})
                if not shutil.which("ffmpeg"):
                    return self._send(400, {"error": "ffmpeg is needed to make a voice"})
                stem = re.sub(r"[^a-z0-9_-]+", "-", pname.lower()).strip("-")[:40] or "voice"
                VOICES.mkdir(parents=True, exist_ok=True)
                # Never overwrite a voice: its NAME is part of every chunk
                # hash that used it. Same rule as import, upload and from_card.
                taken = {q.stem for q in VOICES.iterdir() if q.is_file()}
                vname = stem if stem not in taken else _free_name(taken, stem, "new")
                dest = VOICES / f"{vname}.wav"
                # mono on the way over, as _voice_upload does — the engine
                # reads one channel, and a stereo clip should not weigh double
                r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                    "-y", "-i", str(src), "-ac", "1", str(dest)],
                                   capture_output=True, text=True)
                if r.returncode:
                    dest.unlink(missing_ok=True)
                    return self._send(400, {"error": "ffmpeg could not read that clip: "
                                            + (r.stderr or "").strip()[-300:]})
                # The profile starts from Default — a clip carries no knob
                # settings the way a card does. Kokoro speaks presets rather
                # than clips, so hand the profile to the engine that clones.
                base = dict(profs.get("Default") or BASE_PROFILE)
                base.pop("fx", None)
                base["voices"] = [vname]
                base["active"] = 0
                if base.get("engine") == "kokoro":
                    base["engine"] = "chatterbox"
                profs[pname] = base
                save_profiles(profs)
                return self._send(200, {"ok": True, "profile": pname,
                                        "voice": vname, "engine": base["engine"],
                                        "secs": clip_secs(dest)})
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
                    if c["id"] == d["id"] and c.get("locked"):
                        return self._send(400, {"error": "that card is locked — "
                                                "unlock it before splitting it"})
                    if c["id"] == d["id"] and is_speech(c) and 0 < d["at"] < len(c["text"]):
                        a, b = c["text"][:d["at"]].strip(), c["text"][d["at"]:].strip()
                        out.append({**c, "text": a})
                        tail = {**c, "text": b, "note": ""}
                        # a jump lands at the start of a card, and the start
                        # stays with the head — copying the tags would make
                        # every anchor on this card ambiguous
                        tail.pop("tags", None)
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
                            and (c.get("locked")
                                 or doc["chunks"][i + 1].get("locked"))):
                        return self._send(400, {"error": "a locked card cannot "
                                                "be merged — unlock it first"})
                    if (c["id"] == d["id"] and i + 1 < len(doc["chunks"])
                            and is_speech(c) and is_speech(doc["chunks"][i + 1])):
                        nxt = doc["chunks"][i + 1]
                        merged = {**c, "text": f'{c["text"]} {nxt["text"]}'.strip()}
                        # the vanished card's tags ride along rather than
                        # dangle: a jump aimed at it should land here
                        tags = clean_tags((c.get("tags") or [])
                                          + (nxt.get("tags") or []))
                        if tags:
                            merged["tags"] = tags
                        out.append(merged)
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
                                        "job": enqueue("render", d["name"], d["id"],
                                                       label=d.get("label"))})
            if u.path == "/api/preview":
                sel = (d.get("text") or "").strip()
                if not sel:
                    return self._send(400, {"error": "select some text in the card first"})
                return self._send(200, {"ok": True,
                                        "job": enqueue("preview", d["name"], d["id"], sel,
                                                       label=sel)})
            # ── the queue's own verbs ── pause holds the presses between
            # jobs (never mid-card, see _qstate); cancel takes a WAITING job
            # out of the line; retry sends a finished render to the back of
            # it. Refusals are 200 {ok:False} like /api/bake's, not errors:
            # racing the worker is normal, not exceptional.
            if u.path == "/api/queue/pause":
                with _qlock:
                    _qstate["paused"] = bool(d.get("on"))
                    _qlock.notify_all()
                return self._send(200, {"ok": True, "paused": _qstate["paused"]})
            if u.path == "/api/queue/cancel":
                with _qlock:
                    j = _jobs.get(str(d.get("job") or ""))
                    if not j or j["status"] != "queued":
                        return self._send(200, {"ok": False, "error":
                            "only a job still waiting can be taken out — "
                            "one mid-render finishes on its own"})
                    _queue.remove(j["id"])
                    j["status"] = "canceled"
                return self._send(200, {"ok": True})
            if u.path == "/api/queue/retry":
                j = _jobs.get(str(d.get("job") or ""))
                if not j or j["kind"] != "render" \
                        or j["status"] not in ("done", "error", "canceled"):
                    return self._send(200, {"ok": False, "error":
                        "only a finished render can go again"})
                return self._send(200, {"ok": True,
                    "job": enqueue("render", j["project"], j["chunk"],
                                   label=j.get("label"))})
            if u.path == "/api/queue/clear":
                with _qlock:
                    for k in [k for k, j in _jobs.items()
                              if j["status"] in ("done", "error", "canceled")]:
                        _jobs.pop(k)
                return self._send(200, {"ok": True})
            if u.path == "/api/history/drop":
                # forget one entry from a card's render history. The wav
                # stays in audio/ — the same law as dropping an image
                # variant: only the shortlist forgets
                doc = load(d["name"])
                c = next((x for x in doc["chunks"]
                          if x["id"] == d.get("id")), None)
                if c is None:
                    return self._send(404, {"error": "no such card"})
                h = str(d.get("h") or "")
                hist = [e for e in (c.get("hist") or [])
                        if isinstance(e, dict) and e.get("h") != h]
                if hist:
                    c["hist"] = hist
                else:
                    c.pop("hist", None)
                save(doc)
                return self._send(200, {"ok": True, "left": len(hist)})


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
                f, missing = assemble(d["name"],
                                      fmt=str(d.get("fmt") or "mp3").lower())
                return self._send(200, {"ok": bool(f), "file": str(f) if f else None,
                                        "missing": missing})
            if u.path == "/api/publish_video":
                try:
                    f, missing, frames = publish_video(
                        d["name"], int(_num(d.get("height"), 1080, 240, 2160)))
                except RuntimeError as ex:
                    return self._send(400, {"error": str(ex)})
                return self._send(200, {"ok": True, "file": str(f),
                                        "bytes": f.stat().st_size,
                                        "missing": missing, "frames": frames})
            if u.path == "/api/publish_html":
                try:
                    f, nsegs, missing = publish_html(d["name"])
                except RuntimeError as ex:
                    return self._send(400, {"error": str(ex)})
                return self._send(200, {"ok": True, "file": str(f),
                                        "bytes": f.stat().st_size,
                                        "segments": nsegs, "missing": missing})
            if u.path == "/api/share":
                unl = d.get("unlisted")
                try:
                    url, built, nbytes, unl = share_web(
                        d["name"], None if unl is None else bool(unl))
                except RuntimeError as ex:
                    return self._send(400, {"error": str(ex)})
                return self._send(200, {"ok": True, "url": url,
                                        "built": built, "bytes": nbytes,
                                        "unlisted": unl})
            if u.path == "/api/share_source":
                try:
                    url, nbytes = share_source(d["name"])
                except RuntimeError as ex:
                    return self._send(400, {"error": str(ex)})
                return self._send(200, {"ok": True, "url": url,
                                        "bytes": nbytes})
            if u.path == "/api/mix_plan":
                # The live desk's score: the same cursor walk mixdown sums,
                # served as placements for the browser's own node graph.
                # Channel and Master gains are deliberately absent — the desk
                # owns those live; only each card's STATIC factor travels
                # (profile gain for the spoken kinds, card gain for a clip).
                # Durations come off sf.info headers of the fx-rendered
                # files, so a plan costs what the first preview already paid.
                doc = load(d["name"])
                if not doc:
                    return self._send(404, {"error": "no such story"})
                frm, upto = d.get("from"), d.get("upto")
                import soundfile as sf
                memo = {}

                def secs(p):
                    if p not in memo:
                        i = sf.info(str(p))
                        memo[p] = i.frames / i.samplerate
                    return memo[p]
                events, cursor, missing, marks = mix_events(
                    doc, frm=None if frm is None else int(frm),
                    upto=None if upto is None else int(upto),
                    chime=True, secs_of=secs)
                profs = profiles()
                nm = quote(d["name"])
                evs, total = [], cursor
                for start, kind, path, c in events:
                    at = round(start, 3)
                    if kind == "chime":
                        evs.append({"id": c["id"], "at": at, "kind": "chime",
                                    "dur": CHIME_SECS})
                        total = max(total, start + CHIME_SECS)
                        continue
                    dur = secs(path)
                    e = {"id": c["id"], "at": at, "kind": kind,
                         "dur": round(dur, 3),
                         "chan": c.get("channel") or "main"}
                    if kind == "audio":
                        e["url"] = "/api/clip?f=" + quote(c.get("clip", ""))
                        e["gain"] = float(c.get("gain", 100)) / 100.0
                        if c.get("fade"):
                            e["fade"] = list(c["fade"])[:2]
                    else:
                        # h names the CONTENT. The desk caches decoded audio
                        # by URL across plays, and a split, insert or delete
                        # renumbers every id downstream — so an id-only URL
                        # would hand a renumbered card its predecessor's
                        # decode, one slot off and the wrong length. The
                        # server ignores h; it exists to make the cache key
                        # honest.
                        e["url"] = (f"/api/card_wav?name={nm}&id={c['id']}"
                                    f"&h={chunk_hash(c, doc, profs)}")
                        e["gain"] = float(params_for(c, doc, profs)
                                          .get("gain", 100)) / 100.0
                    evs.append(e)
                    total = max(total, start + dur)
                return self._send(200, {"ok": bool(evs), "total": round(total, 3),
                                        "missing": missing, "from": frm,
                                        "marks": marks, "events": evs})
            if u.path == "/api/book_preview":
                frm, upto = d.get("from"), d.get("upto")
                f, secs, missing, marks = preview_book(
                    d["name"], None if frm is None else int(frm),
                    None if upto is None else int(upto))
                return self._send(200, {"ok": bool(f), "secs": secs,
                                        "missing": missing, "from": frm,
                                        "marks": marks})
            if u.path == "/api/chat":
                return self._chat(d)
            if u.path == "/api/chat_stop":
                with _chats_lock:
                    pr = _chats.get(d.get("name") or "")
                if pr is None or pr.poll() is not None:
                    return self._send(200, {"ok": False})
                pr.terminate()
                return self._send(200, {"ok": True})
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
            if u.path == "/api/open_link":
                # A choice option's link, opened in the author's own browser.
                # The stage asks the server rather than calling window.open
                # itself: the popped-out stage is a child window of the
                # Electron shell, and a child does not inherit the handler
                # that turns window.open into "the real browser" — so the one
                # path that works from every stage is this one. The exported
                # player is a web page with no server behind it and opens its
                # own links; both go through clean_url first, so http and
                # https are the only schemes either can reach.
                link = clean_url(d.get("url"))
                if not link:
                    return self._send(400, {"error": "that is not an http link"})
                subprocess.run([OPEN_CMD, link], check=False)
                return self._send(200, {"ok": True})
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

            # ── shelves ── a series is an ordered list of project names and
            # nothing else. Every verb here writes series.json alone: no
            # document is opened, no card is touched, no hash can move. That
            # is deliberate, and it is what makes a shelf safe to rearrange
            # while the story on it is open in the editor.
            if u.path == "/api/series/new":
                try:
                    slug = series_new(d.get("title"))
                except ValueError as ex:
                    return self._send(400, {"error": str(ex)})
                return self._send(200, {"ok": True, "slug": slug})
            if u.path == "/api/series/edit":
                recs = series()
                rec = recs.get(d.get("slug") or "")
                if rec is None:
                    return self._send(404, {"error": "no such collection"})
                for k, cap in (("title", 80), ("noun", 24), ("member", 24),
                               ("blurb", 500), ("cover", 120)):
                    if k not in d:
                        continue
                    v = _clean(d.get(k), cap)
                    if not v and k in ("title", "noun", "member"):
                        return self._send(400, {"error": f"a {k} is needed"})
                    rec[k] = v
                if "style" in d:
                    # the shelf's picture style (CAST.md §5): words and refs
                    # every card on the shelf paints under. Empty means none.
                    st = d.get("style") or {}
                    if not isinstance(st, dict):
                        return self._send(400, {"error": "style is "
                                                "{text, refs}"})
                    txt = _clean(st.get("text"), 500)
                    refs = ref_list(st.get("refs"))[:8]
                    if txt or refs:
                        rec["style"] = {"text": txt, "refs": refs}
                    else:
                        rec.pop("style", None)
                save_series(recs)
                return self._send(200, {"ok": True})
            if u.path == "/api/series/delete":
                # the shelf goes, the stories do not. Nothing on disk outside
                # series.json is even opened.
                recs = series()
                if recs.pop(d.get("slug") or "", None) is None:
                    return self._send(404, {"error": "no such collection"})
                save_series(recs)
                return self._send(200, {"ok": True})
            if u.path == "/api/series/assign":
                try:
                    series_assign(d["name"], d.get("to") or None, d.get("at"))
                except KeyError:
                    return self._send(404, {"error": "no such collection"})
                return self._send(200, {"ok": True})

            # ── the cast ── every verb here writes cast.json alone (an
            # upload adds one file under cast/<slug>/ and is handled above,
            # before the JSON parse). No document is opened and no card is
            # touched: the registry is a library-level thing, like a shelf.
            # Slots may be renamed freely — a slot is a key in `plates`, and
            # the file underneath never moves once a stored ref can name it.
            if u.path == "/api/cast/new":
                t = _clean(d.get("title"), 80)
                if not t:
                    return self._send(400, {"error": "a title is needed"})
                c = cast()
                slug = series_slug(t, c, fallback="member")
                c[slug] = {"kind": _clean(d.get("kind"), 24).lower() or "character",
                           "title": t,
                           "brief": _clean(d.get("brief"), 500),
                           "scope": _clean(d.get("scope"), 60),
                           "key": "", "plates": {},
                           "created": time.strftime("%Y-%m-%d %H:%M")}
                save_cast(c)
                return self._send(200, {"ok": True, "slug": slug})
            if u.path == "/api/cast/edit":
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                for k, cap in (("title", 80), ("kind", 24), ("brief", 500),
                               ("scope", 60)):
                    if k not in d:
                        continue
                    v = _clean(d.get(k), cap)
                    if not v and k == "title":
                        return self._send(400, {"error": "a title is needed"})
                    m[k] = v.lower() if k == "kind" else v
                if "key" in d:
                    v = str(d.get("key") or "")
                    if v and v not in (m.get("plates") or {}):
                        return self._send(400, {"error": f'no plate "{v}" '
                                                "to be the key"})
                    m["key"] = v
                if "voice" in d:
                    # engine → profile name in profiles.json. A LINK, never a
                    # copy: a profile is engine-bound and a character is not,
                    # which is why the map has one seat per engine.
                    v = d.get("voice")
                    if not isinstance(v, dict):
                        return self._send(400, {"error": "voice is a map of "
                                                "engine to profile"})
                    m["voice"] = {e: _clean(v[e], 60) for e in ENGINES
                                  if isinstance(v.get(e), str) and v[e].strip()}
                    if not m["voice"]:
                        m.pop("voice", None)
                save_cast(c)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/delete":
                # the member leaves the registry; its folder and files stay
                # on disk. Nothing in the app deletes a picture — pool law.
                c = cast()
                if c.pop(d.get("slug") or "", None) is None:
                    return self._send(404, {"error": "no such cast member"})
                save_cast(c)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/plate/rename":
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                old = str(d.get("from") or "")
                new = re.sub(r"[^a-z0-9_-]+", "-",
                             str(d.get("to") or "").lower()).strip("-")[:40]
                plates = m.get("plates") or {}
                if old not in plates:
                    return self._send(404, {"error": f'no plate "{old}"'})
                if not new:
                    return self._send(400, {"error": "a slot name is needed"})
                if new != old and new in plates:
                    return self._send(400, {"error": f'"{new}" is already '
                                            "a slot here"})
                # one key rewritten in place, order kept, no bytes touched
                m["plates"] = {(new if k == old else k): v
                               for k, v in plates.items()}
                if m.get("key") == old:
                    m["key"] = new
                save_cast(c)
                return self._send(200, {"ok": True, "plate": new})
            if u.path == "/api/cast/plate/edit":
                # look and view are labels, not addresses: free text, either
                # may be empty, and the slot stays the only name a ref holds
                c = cast()
                m = c.get(d.get("slug") or "")
                p = (m or {}).get("plates", {}).get(str(d.get("plate") or ""))
                if p is None:
                    return self._send(404, {"error": "no such plate"})
                for k in ("look", "view"):
                    if k not in d:
                        continue
                    v = _clean(d.get(k), 60)
                    if v:
                        p[k] = v
                    else:
                        p.pop(k, None)
                save_cast(c)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/plate/remove":
                # unlink, never delete: the slot goes, the file stays in the
                # member's folder as a candidate — the same law as dropping a
                # variant from a card's shortlist, and the reason no undo
                # machinery is needed here.
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                nm = str(d.get("plate") or "")
                p = (m.get("plates") or {}).pop(nm, None)
                if p is None:
                    return self._send(404, {"error": f'no plate "{nm}"'})
                if p.get("file"):
                    cand = m.setdefault("candidates", [])
                    if p["file"] not in cand:
                        cand.append(p["file"])
                if m.get("key") == nm:
                    m["key"] = next(iter(m.get("plates") or {}), "")
                save_cast(c)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/plate/order":
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                plates = m.get("plates") or {}
                want = [str(x) for x in (d.get("order") or [])
                        if str(x) in plates]
                # the listed slots in their new order, then anything the list
                # missed — a stale view must not be able to drop a plate
                m["plates"] = {k: plates[k] for k in
                               want + [k for k in plates if k not in want]}
                save_cast(c)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/promote":
                # §6: the gesture that was missing — a pool picture becomes
                # canon. The file is COPIED into the member's folder and the
                # pool copy stays: pool law untouched, and any card showing
                # it goes on showing it. The member may be new: promotion is
                # one of the three doors a member arrives through (§7e).
                name = re.sub(r"[^a-z0-9_-]", "", str(d.get("media") or ""))
                try:
                    src = media_file(name) if name else None
                except FileNotFoundError:
                    src = None
                if src is None:
                    return self._send(404, {"error": f'no media "{name}"'})
                if src.suffix.lower() not in IMG_EXT:
                    return self._send(400, {"error": "only a picture can be "
                                            "a plate — not film"})
                c = cast()
                slug = str(d.get("slug") or "")
                if d.get("new_title"):
                    t = _clean(d.get("new_title"), 80)
                    if not t:
                        return self._send(400, {"error": "a title is needed"})
                    slug = series_slug(t, c, fallback="member")
                    c[slug] = {"kind": _clean(d.get("new_kind"), 24).lower()
                               or "character",
                               "title": t, "brief": "",
                               "scope": _clean(d.get("scope"), 60),
                               "key": "", "plates": {},
                               "created": time.strftime("%Y-%m-%d %H:%M")}
                m = c.get(slug)
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                plates = m.setdefault("plates", {})
                want = re.sub(r"[^a-z0-9_-]+", "-",
                              str(d.get("plate") or "").lower()).strip("-")[:40]
                if want and want in plates:
                    return self._send(400, {"error": f'"{want}" is already '
                                            "a slot here"})
                slot = want or _free_name(set(plates),
                                          src.stem[:40] or "plate", "new")
                folder = CAST / slug
                folder.mkdir(parents=True, exist_ok=True)
                taken = {p.stem for p in folder.iterdir() if p.is_file()}
                fname = _free_name(taken, slot, "new") + src.suffix.lower()
                shutil.copy2(src, folder / fname)
                plates[slot] = {"file": fname}
                if d.get("key") or not m.get("key"):
                    m["key"] = slot
                save_cast(c)
                return self._send(200, {"ok": True, "slug": slug,
                                        "plate": slot})
            if u.path == "/api/cast/accept":
                # a candidate chosen into a named slot: canon by decision,
                # never by accumulation (§7c). The file does not move.
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                f = str(d.get("file") or "")
                cand = m.get("candidates") or []
                if f not in cand:
                    return self._send(404, {"error": "no such candidate"})
                slot = re.sub(r"[^a-z0-9_-]+", "-",
                              str(d.get("plate") or "").lower()).strip("-")[:40]
                if not slot:
                    return self._send(400, {"error": "a slot name is needed"})
                plates = m.setdefault("plates", {})
                if slot in plates and not d.get("replace"):
                    # occupied — the caller asks the author and comes back
                    # with replace:true, never overwriting on a name clash
                    return self._send(400, {"error": f'"{slot}" is already '
                                            "a slot here — accepting again "
                                            "replaces its picture"})
                cand.remove(f)
                if slot in plates:
                    # the slot keeps its name, its look and view, and every
                    # stored ref pointing at it; only the art changes. The
                    # old picture steps down into the candidates row — the
                    # never-delete law, walked backwards.
                    old = plates[slot].get("file")
                    if old and old != f and old not in cand:
                        cand.append(old)
                    plates[slot]["file"] = f
                else:
                    plates[slot] = {"file": f}
                if not cand:
                    m.pop("candidates", None)
                if d.get("key") or not m.get("key"):
                    m["key"] = slot
                save_cast(c)
                return self._send(200, {"ok": True, "plate": slot})
            if u.path == "/api/cast/candidate/drop":
                # off the row, not off the disk — the same law as dropping a
                # variant from a card's shortlist
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                f = str(d.get("file") or "")
                cand = m.get("candidates") or []
                if f not in cand:
                    return self._send(404, {"error": "no such candidate"})
                cand.remove(f)
                if not cand:
                    m.pop("candidates", None)
                save_cast(c)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/dup_look":
                # §7c: one gesture for "she becomes a cosmonaut". Every plate
                # carrying the look is copied into a new look, and each copy
                # points at the very file its original shows — the repaint
                # that follows then has something to match, and accepting a
                # repaint repoints only the copy.
                c = cast()
                m = c.get(d.get("slug") or "")
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                look = _clean(d.get("look"), 60)
                to = _clean(d.get("to"), 60)
                if not to:
                    return self._send(400, {"error": "a name for the new "
                                            "look is needed"})
                plates = m.setdefault("plates", {})
                src_p = [(s, p) for s, p in plates.items()
                         if (p.get("look") or "") == look]
                if not src_p:
                    return self._send(404, {"error": "no plates carry "
                                            "that look"})
                tos = (re.sub(r"[^a-z0-9_-]+", "-", to.lower())
                       .strip("-")[:24] or "look")
                made = []
                for s, p in src_p:
                    view = re.sub(r"[^a-z0-9_-]+", "-",
                                  (p.get("view") or s).lower()).strip("-")
                    ns = _free_name(set(plates),
                                    f"{tos}-{view}"[:40].strip("-"), "new")
                    plates[ns] = {**p, "look": to}
                    made.append(ns)
                save_cast(c)
                return self._send(200, {"ok": True, "plates": made})
            if u.path == "/api/cast/dup_member":
                # the whole member, brief and voice bindings included (§7c).
                # Plates live in the member's OWN folder, so the files are
                # copied across — a slug in a path is a fence, not a label.
                c = cast()
                slug = str(d.get("slug") or "")
                m = c.get(slug)
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                t = f"{m.get('title') or slug} copy"[:80]
                new = series_slug(t, c, fallback="member")
                nm = json.loads(json.dumps(m))
                nm["title"] = t
                nm["created"] = time.strftime("%Y-%m-%d %H:%M")
                files = [p.get("file") for p in (nm.get("plates") or {}).values()]
                files += nm.get("candidates") or []
                (CAST / new).mkdir(parents=True, exist_ok=True)
                for fn in files:
                    if fn and (CAST / slug / fn).is_file():
                        shutil.copy2(CAST / slug / fn, CAST / new / fn)
                c[new] = nm
                save_cast(c)
                return self._send(200, {"ok": True, "slug": new})
            if u.path == "/api/cast/to_pool":
                # promotion's mirror: a plate or candidate COPIES into the
                # media pool, because a reference and a card's picture are
                # different citizenships — references condition paints from
                # behind the fence, a visual card shows pool media. One way
                # and never a move: the cast keeps its file, and the copy
                # takes a pool name that never changes, pool law.
                c = cast()
                slug = str(d.get("slug") or "")
                m = c.get(slug)
                if m is None:
                    return self._send(404, {"error": "no such cast member"})
                plate = str(d.get("plate") or "")
                fn = str(d.get("file") or "")
                if plate:
                    p = (m.get("plates") or {}).get(plate)
                    if not p or not p.get("file"):
                        return self._send(404, {"error": "no such plate"})
                    fn = p["file"]
                elif fn not in (m.get("candidates") or []):
                    return self._send(404, {"error": "no such candidate"})
                src = CAST / slug / fn
                if not src.is_file():
                    return self._send(404, {"error":
                                            "its file is missing from disk"})
                stem = re.sub(r"[^a-z0-9_-]+", "-",
                              f"{slug}-{plate or fn.rsplit('.', 1)[0]}"
                              .lower()).strip("-")[:40] or "cast"
                MEDIA.mkdir(parents=True, exist_ok=True)
                pool = {p.stem for p in MEDIA.iterdir() if p.is_file()}
                name = _free_name(pool, stem, "new")
                dest = MEDIA / f"{name}{src.suffix.lower()}"
                shutil.copy2(src, dest)
                os.chmod(dest, 0o644)
                w = webp_still(dest)          # pool stills arrive pressed
                if w:
                    os.chmod(w, 0o644)
                    dest.unlink()
                return self._send(200, {"ok": True, "media": name})
            if u.path == "/api/cast/paint":
                # slow and lockless, like /api/media/generate — cast_paint
                # takes the lock itself for the one registry append at the end
                jid = job_start("paint", "", None,
                                f"@{d.get('slug') or ''} · "
                                f"{str(d.get('prompt') or '')}")
                try:
                    fname = cast_paint(str(d.get("slug") or ""),
                                       str(d.get("prompt") or ""),
                                       str(d.get("plate") or ""),
                                       str(d.get("stem") or ""),
                                       str(d.get("file") or ""))
                except (ValueError, RuntimeError) as ex:
                    job_end(jid, error=str(ex))
                    return self._send(400, {"error": str(ex)})
                job_end(jid)
                return self._send(200, {"ok": True, "file": fname})
            if u.path == "/api/media/open":
                # any pool picture or film, in whatever this machine opens
                # it with — the viewer gesture the cast board taught, now
                # answering for the pool and the visual cards too. The path
                # is built from the pool name, never from the request.
                nm = re.sub(r"[^a-z0-9_-]", "", str(d.get("media") or ""))
                try:
                    f = media_file(nm) if nm else None
                except FileNotFoundError:
                    f = None
                if f is None:
                    return self._send(404, {"error": f'no media "{nm}"'})
                subprocess.run([OPEN_CMD, str(f)], check=False)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/reveal":
                # the member's folder in Finder — where its plates live, and
                # the honest answer to "where did my picture go"
                slug = str(d.get("slug") or "")
                if not CAST_SLUG_RE.match(slug) or cast().get(slug) is None:
                    return self._send(404, {"error": "no such cast member"})
                folder = CAST / slug
                if not folder.is_dir():
                    return self._send(404, {"error": "no plates on disk yet — "
                                            "drop a picture on the board first"})
                subprocess.run([OPEN_CMD, str(folder)], check=False)
                return self._send(200, {"ok": True})
            if u.path == "/api/cast/open":
                # one plate or candidate, in whatever this machine opens
                # pictures with. The file name must be one the REGISTRY
                # holds for this member — the path is never the request's.
                slug = str(d.get("slug") or "")
                if not CAST_SLUG_RE.match(slug):
                    return self._send(404, {"error": "no such cast member"})
                m = cast().get(slug) or {}
                p = m.get("plates", {}).get(str(d.get("plate") or ""))
                fn = (p or {}).get("file") or ""
                if not fn and str(d.get("file") or "") in (m.get("candidates")
                                                          or []):
                    fn = str(d["file"])
                f = (CAST / slug / fn) if fn else None
                if (f is None or not f.is_file()
                        or not re.fullmatch(r"[a-z0-9_.-]{1,80}", fn)
                        or ".." in fn):
                    return self._send(404, {"error": "no file for that plate"})
                subprocess.run([OPEN_CMD, str(f)], check=False)
                return self._send(200, {"ok": True})

            # ── drafts ── the discuss agent's sandbox. A draft is an
            # ordinary project with two extra fields: `draft: true`, which is
            # the only licence saga_mcp.py accepts for a write, and
            # `draft_of`, naming the story it shadows. Like any duplicate it
            # is born fully rendered — the content-addressed cache serves
            # both — so trying the agent's work costs nothing, and applying
            # it re-renders nothing. Apply and discard are the author's
            # buttons; the agent has no tool that reaches either.
            if u.path == "/api/draft":
                doc = load(d["name"])
                if doc is None:
                    return self._send(404, {"error": "no such story"})
                if doc.get("draft"):
                    # already a sandbox — hand it straight back
                    return self._send(200, {"ok": True, "name": doc["name"],
                                            "title": doc.get("title",
                                                             doc["name"]),
                                            "existing": True})
                for p in sorted(ROOT.iterdir()):
                    f = p / "doc.json"
                    if not f.exists():
                        continue
                    try:
                        dd = json.loads(f.read_text())
                    except json.JSONDecodeError:
                        continue
                    if dd.get("draft_of") == doc["name"]:
                        # one draft per story: two agents' work interleaving
                        # in two copies would be nobody's story
                        return self._send(200, {"ok": True, "name": dd["name"],
                                                "title": dd.get("title",
                                                                dd["name"]),
                                                "existing": True})
                taken = {p.name for p in ROOT.iterdir()
                         if (p / "doc.json").exists()}
                new = _free_name(taken, doc["name"], "draft")
                src = pdir(doc["name"])
                nd = json.loads(json.dumps(doc))
                nd["name"] = new
                nd["title"] = f"{doc.get('title', new)} (draft)"[:120]
                nd["draft"] = True
                nd["draft_of"] = doc["name"]
                nd.pop("_undo", None)
                pdir(new).mkdir(parents=True, exist_ok=True)
                if (src / "source.md").exists():
                    shutil.copy2(src / "source.md", pdir(new) / "source.md")
                save(nd)
                return self._send(200, {"ok": True, "name": new,
                                        "title": nd["title"]})

            if u.path == "/api/draft_apply":
                dr = load(d["name"])
                if dr is None or not dr.get("draft"):
                    return self._send(400, {"error": "that is not a draft"})
                orig = load(dr["draft_of"]) if dr.get("draft_of") else None
                if orig is None:
                    # born new — or the original has gone. Keeping it IS the
                    # apply: the flags come off and it is simply a story.
                    dr.pop("draft", None)
                    dr.pop("draft_of", None)
                    dr["title"] = re.sub(r"\s*\(draft\)$", "",
                                         dr.get("title") or dr["name"]
                                         ) or dr["name"]
                    save(dr)
                    return self._send(200, {"ok": True, "name": dr["name"],
                                            "kept": True})
                snapshot(orig, "apply the draft")
                for k, v in dr.items():
                    if k in ("name", "title", "created", "_undo",
                             "draft", "draft_of"):
                        continue
                    orig[k] = json.loads(json.dumps(v))
                save(orig)
                # the draft is spent; its renders are global and stay
                shutil.rmtree(pdir(dr["name"]), ignore_errors=True)
                return self._send(200, {"ok": True, "name": orig["name"],
                                        "applied": True})

            if u.path == "/api/draft_discard":
                dr = load(d["name"])
                if dr is None or not dr.get("draft"):
                    return self._send(400, {"error": "that is not a draft"})
                shutil.rmtree(pdir(dr["name"]), ignore_errors=True)
                return self._send(200, {"ok": True,
                                        "was": dr.get("draft_of") or ""})

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

            if u.path == "/api/media/rename":
                # exactly the clip rename: no hash names media, so this is a
                # file rename and a pointer sweep, never onto a taken name
                old = re.sub(r"[^a-z0-9_-]", "", str(d.get("media") or ""))
                new = re.sub(r"[^a-z0-9_-]+", "-",
                             str(d.get("to") or "").lower()).strip("-")[:40]
                if not new:
                    return self._send(400, {"error": "a name is needed"})
                try:
                    src = media_file(old) if old else None
                except FileNotFoundError:
                    src = None
                if src is None:
                    return self._send(404, {"error": f"no media “{old}”"})
                if new == old:
                    return self._send(200, {"ok": True, "media": old, "cards": 0})
                try:
                    media_file(new)
                    return self._send(400, {"error": f"media called “{new}” is already here"})
                except FileNotFoundError:
                    pass
                src.rename(MEDIA / f"{new}{src.suffix}")
                cards = 0
                for pr in projects():
                    doc = load(pr["name"])
                    touched = False
                    for c in doc["chunks"]:
                        if c.get("type") == "visual" and c.get("media") == old:
                            c["media"] = new
                            cards += 1
                            touched = True
                    if touched:
                        save(doc)
                return self._send(200, {"ok": True, "media": new, "cards": cards})

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
def _stop_workers():
    """Take the workers down with us. Each holds gigabytes of warm model and
    has no reason to outlive the studio that started it."""
    for w in (_cb, _ov):
        pr = w.get("proc")
        if pr is not None and pr.poll() is None:
            pr.terminate()
            try:
                pr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pr.kill()


if __name__ == "__main__":
    # SIGTERM must reach atexit — it is how the desktop app stops this
    # server, and the workers would otherwise be orphaned, warm, and unpaid.
    __import__("signal").signal(__import__("signal").SIGTERM,
                                lambda *_: sys.exit(0))
    where = "127.0.0.1" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"Saga Studio  ->  http://{where}:{PORT}" + ("?k=<token>" if TOKEN else ""))
    if HOST == "0.0.0.0":
        print("Reachable on the local network. No login unless SAGA_TOKEN is set.")
    print("(model loads on the first render, not at boot)")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
