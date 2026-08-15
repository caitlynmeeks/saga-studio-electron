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
    studio/<project>/out/*.mp3       assembled book

Cards come in three kinds. A card with no "type" is speech — the original kind,
rendered by the model and content-addressed as above. type "audio" places an
imported clip on the timeline (music, an effect), and type "silence" is a
timed rest. Neither of those is rendered or hashed: their audio either exists
in clips/ or is nothing at all, so every c["text"] / chunk_hash site guards
with is_speech() rather than assuming the world is made of prose.
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
VOICES = Path(os.environ.get("SAGA_VOICES") or (HERE / "voices")).expanduser()
PORT = int(os.environ.get("PORT", "5010"))
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

_model = None
_lock = threading.Lock()          # MPS: one generate() at a time
_bake = {"running": False, "done": 0, "total": 0, "project": "", "label": "",
         "cancel": False, "stopped": False}
_docmut = threading.Lock()        # one doc.json read-modify-write at a time

DEFAULTS = {"voice": "caitlyn2", "exag": 0.4, "cfg": 0.35,
            "temp": 0.7, "rep": 1.2}

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
BASE_PROFILE = {"voices": ["caitlyn2"], "active": 0, "exag": 0.4, "cfg": 0.35,
                "temp": 0.7, "rep": 1.2, "note": ""}


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
    return {"voice": voices[idx],
            "exag": prof.get("exag", 0.4), "cfg": prof.get("cfg", 0.35),
            "temp": prof.get("temp", 0.7), "rep": prof.get("rep", 1.2)}


def params_for(c, doc, profs=None):
    """DEFAULTS <- doc defaults <- the card's profile <- per-card override."""
    return {**DEFAULTS, **doc.get("params", {}),
            **profile_params(c.get("profile", "Default"), profs),
            **c.get("params", {})}


def chunk_hash(c, doc, profs=None):
    p = params_for(c, doc, profs)
    key = [c["text"], p["voice"], p["exag"], p["cfg"], p["temp"], p["rep"]]
    # Take 0 hashes exactly as it did before takes existed, so nothing already
    # rendered goes stale — only a card you have actually re-rolled gets a new
    # name, and each take keeps its own file, so stepping back to take 2 plays
    # take 2 again instead of re-rendering it.
    if c.get("seed"):
        key.append({"take": int(c["seed"])})
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:20]


def is_speech(c):
    return c.get("type", "speech") == "speech"


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
            # the progress bar tracks what needs rendering, which is speech —
            # an audio or silence card is never stale
            sp = [c for c in doc["chunks"] if is_speech(c)]
            ready = sum(1 for c in sp
                        if (AUDIO / f"{chunk_hash(c, doc, profs)}.wav").exists())
            out.append({"name": doc["name"], "title": doc.get("title", doc["name"]),
                        "chunks": len(sp), "ready": ready,
                        "words": sum(len(c["text"].split()) for c in sp)})
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
        if not is_speech(c):
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
            "clips": {}, "unreadable": [],
            "missing_voices": [], "missing_clips": [], "bytes": 0}
    vnames, pnames, cnames, audio = set(), set(), set(), {}
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
        rendered = 0
        for c in doc["chunks"]:
            if not is_speech(c):
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
               for k in ("voices", "active", "exag", "cfg", "temp", "rep"))


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
           "audio": 0, "skipped": []}
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
            old = [chunk_hash(c, doc, arc_profs) if is_speech(c) else None
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
            new = [chunk_hash(c, doc, local_profs) if is_speech(c) else None
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
    key = json.dumps([spoken, p["voice"], p["exag"], p["cfg"], p["temp"], p["rep"],
                      "prev", int(c.get("seed") or 0)], sort_keys=True)
    h = "p" + hashlib.sha256(key.encode()).hexdigest()[:19]
    dest = AUDIO / f"{h}.wav"
    if dest.exists() and not force:
        return h, True, spoken
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
                h, cached = render(c, doc, force=True)
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
    todo = [c for c in doc["chunks"] if is_speech(c) and not c.get("mute")
            and not (AUDIO / f"{chunk_hash(c, doc)}.wav").exists()]
    _bake.update(running=True, done=0, total=len(todo), project=name, label="",
                 cancel=False, stopped=False)
    try:
        for c in todo:
            if _bake["cancel"]:
                _bake["stopped"] = True
                break
            _bake["label"] = c["text"][:60]
            render(c, doc)
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
    as before). A silence card just advances it. An audio card is placed at
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
    cards = doc["chunks"]
    if frm is not None:
        i = next((k for k, c in enumerate(cards) if c["id"] == frm), None)
        if i is not None:
            cards = cards[i:]
    events, cursor, missing, marks = [], 0.0, 0, []

    def note(c):                              # where this card begins
        marks.append({"id": c["id"], "at": round(cursor, 3)})

    def unready(c):
        """Not in the book. In a preview, at least make it audible."""
        nonlocal cursor, missing
        missing += 1
        if not chime:
            return
        note(c)
        events.append((cursor, "chime", None, None, c))
        cursor += CHIME_SECS + gap

    for c in cards:
        if c.get("mute"):                     # muted cards are simply not in the book
            continue
        kind = c.get("type", "speech")
        if kind == "silence":
            note(c)
            cursor += max(0.0, float(c.get("secs", 1.0)))
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
            continue
        f = AUDIO / f"{chunk_hash(c, doc)}.wav"
        if not f.exists():
            unready(c)
            continue
        w, wsr = ta.load(str(f))
        note(c)
        events.append((cursor, kind, w, wsr, c))
        cursor += w.shape[-1] / wsr + (1.1 if c["text"].strip().startswith("❦") else gap)
    if not events:
        return None, 0, missing, []
    # every speech chunk is at the model's rate; only clips ever need resampling.
    # A preview of a chapter nobody has rendered yet is all chimes and carries no
    # rate of its own, so fall back to a clip's, then to the model's.
    sr = (next((e[3] for e in events if e[1] == "speech"), None)
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
            return self._send(200, {
                "projects": projects(),
                "voices": sorted(p.stem for p in VOICES.glob("*.wav")),
                "clips": sorted(({"name": p.stem, "secs": clip_secs(p)}
                                 for p in CLIPS.glob("*.wav")),
                                key=lambda c: c["name"]),
                "profiles": profiles(),
                "defaults": DEFAULTS, "bake": _bake,
                "model": "warm" if _model else "cold"})
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

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "unauthorised"})
        if u.path == "/api/import_archive":
            return self._import_archive(u)
        if u.path == "/api/clip/upload":       # raw body, so before the JSON parse
            return self._clip_upload(u)
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or "{}")
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
        lock = None if u.path in ("/api/chat", "/api/assemble", "/api/book_preview",
                                  "/api/reveal") else _docmut
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
                        if "height" in d:          # editor height, persisted
                            c["height"] = int(d["height"])
                        if "params" in d:
                            c["params"] = {k: v for k, v in d["params"].items() if v is not None}
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
                else:
                    c = {"id": 0, "text": "", "params": {}, "note": ""}
                at = max(0, min(int(d.get("at", 0)), len(doc["chunks"])))
                doc["chunks"].insert(at, c)
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
                p[nm] = {**BASE_PROFILE, **p.get(nm, {}), **d.get("data", {})}
                save_profiles(p)
                return self._send(200, {"ok": True, "profiles": p})

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

            if u.path == "/api/voice/upload":
                import base64
                stem = re.sub(r"[^a-z0-9_-]+", "-", Path(d["filename"]).stem.lower())[:40]
                dest = VOICES / f"{stem}.wav"
                dest.write_bytes(base64.b64decode(d["data"]))
                return self._send(200, {"ok": True, "voice": stem})

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
                        out.append({**c, "text": b, "note": ""})
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
            if u.path == "/api/delete":
                shutil.rmtree(pdir(d["name"]), ignore_errors=True)
                return self._send(200, {"ok": True})
        except Exception as ex:
            return self._send(500, {"error": f"{type(ex).__name__}: {ex}"})
        finally:
            if lock:
                lock.release()
        return self._send(404, {"error": "?"})


if __name__ == "__main__":
    where = "127.0.0.1" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"Saga Studio  ->  http://{where}:{PORT}" + ("?k=<token>" if TOKEN else ""))
    if HOST == "0.0.0.0":
        print("Reachable on the local network. No login unless SAGA_TOKEN is set.")
    print("(model loads on the first render, not at boot)")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
