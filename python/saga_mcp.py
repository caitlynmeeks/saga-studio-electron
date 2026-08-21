#!/usr/bin/env python3
"""The discuss agent's hands.

An MCP server over stdio — newline-delimited JSON-RPC, the shape Claude Code
speaks — whose every tool is a call on the studio's own HTTP API. Nothing in
here opens a file or touches the library directly: the API is the one door,
so undo history, locks and group normalisation hold for the agent exactly as
they hold for the author.

The sandbox law is enforced HERE, in code, not in the prompt: reads work on
any story, writes only on a draft (`draft: true` in its doc). The agent is
told the law too, in discuss_rules.md — but a rule the model could forget is
a rule this file still refuses to break. There are deliberately no tools for
deleting a story, discarding a draft, applying one, or editing an existing
profile: proposing is the agent's job, disposing is the author's.

Spoken to by studio.py, which passes SAGA_API and SAGA_TOKEN in the
environment when it spawns headless Claude. Stdlib only, so the same
interpreter that runs the studio always runs this.
"""
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = os.environ.get("SAGA_API", "http://127.0.0.1:5010")
TOKEN = os.environ.get("SAGA_TOKEN", "")


class ToolError(Exception):
    """A message for the model — what went wrong and what to do instead."""


def api(path, body=None, **q):
    if TOKEN:
        q["k"] = TOKEN
    url = BASE + path + ("?" + urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except HTTPError as ex:
        try:
            msg = json.loads(ex.read().decode()).get("error", "")
        except Exception:
            msg = ""
        raise ToolError(msg or f"the studio said {ex.code}")
    except URLError as ex:
        raise ToolError(f"could not reach the studio: {ex.reason}")


def doc_of(name):
    if not name:
        raise ToolError("which story? pass its `story` name")
    return api("/api/doc", name=name)


def require_draft(name):
    doc = doc_of(name)
    if not doc.get("draft"):
        raise ToolError(
            f'"{name}" is a real story and cannot be edited directly. '
            f'Call draft_story("{name}") and work on the copy it returns — '
            f"the author reviews the draft and applies or discards it.")
    return doc


# ── views ── compact enough to read a 200-card story without drowning ────

def card_view(c):
    kind = c.get("type") or "text"
    out = {"id": c["id"], "kind": kind}
    keep = {
        "text": ("text", "profile", "tags", "note", "sub", "when", "runon",
                 "mute", "locked", "params", "ready"),
        "voiced": ("perfname", "profile", "tags", "when", "mute", "locked",
                   "ready"),
        "audio": ("clip", "mode", "after", "gain", "fade", "tags", "when"),
        "silence": ("secs",),
        "visual": ("media", "ref", "note", "tags", "when"),
        "title": ("text", "secs", "fade", "tags", "when"),
        "choice": ("options", "auto", "tags", "when"),
        "group": ("gname", "tags"),
    }.get(kind, ("text",))
    for k in keep:
        v = c.get(k)
        if v or v is False and k == "ready" or k in ("text", "gname", "options"):
            if v not in (None, "", [], {}):
                out[k] = v
            elif k == "ready":
                out[k] = bool(v)
    return out


def story_view(doc):
    return {"name": doc["name"], "title": doc.get("title", doc["name"]),
            "draft": bool(doc.get("draft")),
            "draft_of": doc.get("draft_of") or None,
            "cards": [card_view(c) for c in doc.get("chunks", [])]}


def profile_view(name, p):
    out = {"name": name, "engine": p.get("engine", "chatterbox")}
    eng = out["engine"]
    if eng == "kokoro":
        out["kvoice"] = p.get("kvoice", "af_heart")
    else:
        vs = p.get("voices") or []
        out["voice"] = vs[min(p.get("active", 0), len(vs) - 1)] if vs else None
    if eng == "omnivoice":
        out["lang"] = p.get("lang", "en")
    if p.get("note"):
        out["note"] = p["note"]
    return out


# ── the tools ────────────────────────────────────────────────────────────

def t_overview(a):
    s = api("/api/state")
    kv = s.get("kvoices") or []
    return {
        "stories": [{"name": p["name"], "title": p["title"],
                     "cards": p.get("chunks", 0),
                     **({"draft": True} if p.get("draft") else {}),
                     **({"draft_of": p["draft_of"]} if p.get("draft_of") else {})}
                    for p in s.get("projects", [])],
        "profiles": [profile_view(n, p)
                     for n, p in sorted((s.get("profiles") or {}).items())],
        "voices": [v["name"] for v in s.get("voices", [])],
        "clips": [{"name": c["name"], "secs": round(c.get("secs", 0), 1)}
                  for c in s.get("clips", [])],
        "media": [m.get("name") if isinstance(m, dict) else m
                  for m in s.get("media", [])],
        "engines": {"chatterbox": True,
                    "omnivoice": bool(s.get("omnivoice")),
                    "kokoro": bool(s.get("kokoro"))},
        "kokoro_presets": kv,
        "image_gen": bool(s.get("nanobanana")),
    }


def t_read_story(a):
    return story_view(doc_of(a.get("story")))


def t_draft_story(a):
    r = api("/api/draft", {"name": a.get("story")})
    return {"draft": r["name"], "title": r.get("title"),
            "note": ("this draft already existed — read it before assuming "
                     "its state" if r.get("existing") else
                     "work here; the author applies or discards it")}


def t_create_story(a):
    title = (a.get("title") or "").strip()
    text = a.get("text") or ""
    if not title:
        raise ToolError("a title is needed")
    if not text.strip():
        raise ToolError("some text is needed — it becomes the cards")
    r = api("/api/import", {"title": title, "markdown": text, "draft": True})
    return {"draft": r["name"], "cards": r.get("chunks"),
            "note": "born as a draft; the author keeps or discards it"}


EDIT_FIELDS = ("text", "note", "tags", "sub", "label", "when", "runon",
               "mute", "profile", "options", "auto", "params", "seed",
               "clip", "media", "ref", "mode", "after", "gain", "fade",
               "secs", "chain", "tw", "twsfx")


def _fields(a):
    got = {k: a[k] for k in EDIT_FIELDS if k in a and a[k] is not None}
    extra = a.get("fields")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k in EDIT_FIELDS and v is not None:
                got[k] = v
    return got


def t_insert_card(a):
    name = a.get("story")
    require_draft(name)
    kind = a.get("kind") or "text"
    body = {"name": name, "at": int(a.get("at", 0))}
    if kind != "text":
        body["kind"] = kind
    if kind == "group" and a.get("gname"):
        body["gname"] = a["gname"]
    if a.get("group"):
        body["group"] = a["group"]
    r = api("/api/insert", body)
    cid = r.get("id")
    fields = _fields(a)
    if fields:
        api("/api/chunk", {"name": name, "id": cid, **fields})
    return {"id": cid, "note": "ids renumber on every insert/remove/move — "
                               "re-read the story rather than trusting old ones"}


def t_edit_card(a):
    name = a.get("story")
    require_draft(name)
    extra = a.get("fields") if isinstance(a.get("fields"), dict) else {}
    if a.get("gname") is not None or extra.get("gname") is not None:
        raise ToolError("a group's name is renamed with rename_group, "
                        "not edit_card — the members follow it there")
    fields = _fields(a)
    if not fields:
        raise ToolError("nothing to change — pass the fields to set")
    api("/api/chunk", {"name": name, "id": int(a.get("id", -1)), **fields})
    return {"ok": True}


def t_remove_card(a):
    name = a.get("story")
    require_draft(name)
    api("/api/remove", {"name": name, "id": int(a.get("id", -1))})
    return {"ok": True, "note": "ids renumbered"}


def t_move_card(a):
    name = a.get("story")
    require_draft(name)
    api("/api/move", {"name": name, "id": int(a.get("id", -1)),
                      "to": int(a.get("to", 0))})
    return {"ok": True, "note": "ids renumbered"}


def t_rename_group(a):
    name = a.get("story")
    require_draft(name)
    r = api("/api/group_rename", {"name": name, "gname": a.get("gname"),
                                  "to": a.get("to")})
    return {"ok": True, "gname": r.get("gname"), "cards": r.get("cards"),
            "note": "the bar and its member cards all follow the new name; "
                    "tags (and the choices that jump to them) are untouched"}


def t_create_profile(a):
    name = (a.get("name") or "").strip()
    if not name:
        raise ToolError("a profile name is needed")
    s = api("/api/state")
    if name in (s.get("profiles") or {}):
        raise ToolError(f'a profile called "{name}" already exists, and '
                        "existing profiles are shared by every story — "
                        "pick a new name instead of changing it")
    data = {}
    eng = a.get("engine") or "chatterbox"
    data["engine"] = eng
    if a.get("voice"):
        have = {v["name"] for v in s.get("voices", [])}
        if a["voice"] not in have:
            raise ToolError(f'no voice clip "{a["voice"]}" — '
                            f"the clips here are: {sorted(have)}")
        data["voices"] = [a["voice"]]
    if a.get("kvoice"):
        kv = s.get("kvoices") or []
        names = {v.get("id") or v.get("name") if isinstance(v, dict) else v
                 for v in kv}
        if names and a["kvoice"] not in names:
            raise ToolError(f'no kokoro preset "{a["kvoice"]}" — '
                            "list them with overview()")
        data["kvoice"] = a["kvoice"]
    for k in ("lang", "speed", "exag", "cfg", "temp", "rep", "gain", "note"):
        if a.get(k) is not None:
            data[k] = a[k]
    api("/api/profile", {"profile": name, "data": data})
    return {"ok": True, "profile": name,
            "note": "assign it to cards with edit_card(..., profile=...)"}


def t_render_card(a):
    name = a.get("story")
    require_draft(name)
    api("/api/render", {"name": name, "id": int(a.get("id", -1))})
    return {"ok": True, "note": "rendering runs in the background — "
                                "story_status() reports when cards are ready"}


def t_render_story(a):
    name = a.get("story")
    require_draft(name)
    r = api("/api/bake", {"name": name})
    if not r.get("ok"):
        raise ToolError(r.get("error") or "could not start")
    return {"ok": True, "note": "baking every stale card in the background — "
                                "poll story_status(), it can take minutes"}


def t_generate_image(a):
    prompt = (a.get("prompt") or "").strip()
    if not prompt:
        raise ToolError("a prompt is needed — describe the picture to paint")
    r = api("/api/media/generate", {"prompt": prompt,
                                    "media": a.get("name") or "",
                                    "aspect": a.get("aspect") or "",
                                    "ref": a.get("ref") or ""})
    return {"media": r["media"],
            "note": "in the media pool — show it by setting `media` on a "
                    "visual card, and keep the prompt in that card's note "
                    "so a variant can be painted later"}


def t_remember(a):
    note = (a.get("note") or "").strip()
    if not note:
        raise ToolError("pass the note to keep")
    api("/api/agent_note", {"note": note})
    return {"ok": True,
            "note": "kept; it rides into every future conversation"}


def t_story_status(a):
    doc = doc_of(a.get("story"))
    render = [c for c in doc.get("chunks", [])
              if (c.get("type") or "text") in ("text", "voiced")]
    ready = [c["id"] for c in render if c.get("ready")]
    s = api("/api/state")
    return {"cards": len(doc.get("chunks", [])),
            "renderable": len(render), "ready": len(ready),
            "stale": [c["id"] for c in render if not c.get("ready")],
            "baking": bool((s.get("bake") or {}).get("running"))}


STR = {"type": "string"}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def _schema(props, req):
    return {"type": "object", "properties": props, "required": req}


CARD_FIELD_PROPS = {
    "text": {**STR, "description": "the spoken prose (text cards)"},
    "profile": {**STR, "description": "voice profile name"},
    "tags": {"type": "array", "items": STR,
             "description": "labels; also the anchors choice options jump to"},
    "note": STR, "sub": {**STR, "description":
                         "what the screen shows if it differs from the speech"},
    "label": {**STR, "description": "name shown on the card's collapsed bar"},
    "when": {**STR, "description": "condition; card is skipped when false, "
             "e.g. 'met_gertie' or 'coins>=3'"},
    "runon": {**BOOL, "description": "no rest before this card"},
    "mute": BOOL,
    "options": {"type": "array", "description":
                "choice options: {label, goto (a tag; empty ends the story), "
                "set (list like ['brave','!x','coins+1']), when}",
                "items": {"type": "object"}},
    "auto": {**BOOL, "description": "choice takes the first passing option "
             "silently — if/else in a card"},
    "params": {"type": "object", "description":
               "per-card delivery overrides incl. engine"},
    "clip": {**STR, "description": "audio-card clip name"},
    "mode": {**STR, "enum": ["full", "after"]},
    "after": NUM, "gain": NUM,
    "fade": {"type": "array", "items": NUM, "description":
             "audio card: [in, out] as PERCENTAGES of the clip; "
             "title card: [in, out] fades in SECONDS"},
    "secs": {**NUM, "description": "silence length, or a title card's hold"},
    "media": {**STR, "description": "visual-card media name"},
    "ref": {**STR, "description": "visual-card reference image (a media "
            "name) — generate_image matches its style, keeping a story's "
            "pictures one cast"},
}

TOOLS = [
    ("overview", "The whole studio at a glance: stories (drafts marked), "
     "voice profiles, voice clips, kokoro presets, music/effect clips, media, "
     "and which engines are installed. Call this first.",
     _schema({}, []), t_overview),
    ("read_story", "A story's full deck of cards, compactly. Works on any "
     "story, draft or not.",
     _schema({"story": STR}, ["story"]), t_read_story),
    ("draft_story", "Make (or return) the working draft of an existing "
     "story. Edits are only possible in a draft; the author applies or "
     "discards it. Returns the draft's name — use THAT name in every edit.",
     _schema({"story": STR}, ["story"]), t_draft_story),
    ("create_story", "Create a brand-new story from text (markdown or plain "
     "prose; it is split into cards at paragraph and sentence boundaries). "
     "Born as a draft the author keeps or discards.",
     _schema({"title": STR, "text": STR}, ["title", "text"]), t_create_story),
    ("insert_card", "Insert a card at position `at` (0 = top) in a DRAFT. "
     "kind: text, audio, silence, visual, title (words on screen with "
     "nobody speaking — fade in/out in `fade` seconds, hold in `secs`, all "
     "silence on the timeline), choice, or group. Any card fields may be "
     "set in the same call; a group takes its name via `gname`.",
     _schema({"story": STR, "at": INT,
              "kind": {**STR, "enum": ["text", "audio", "silence", "visual",
                                       "title", "choice", "group"]},
              "gname": {**STR, "description": "the group's name (kind group "
                        "only); errors if a group of that name exists"},
              "group": {**STR, "description":
                        "make it a member of this named group"},
              **CARD_FIELD_PROPS}, ["story", "at"]), t_insert_card),
    ("edit_card", "Change fields on card `id` in a DRAFT. Only the fields "
     "passed are touched. Locked cards refuse edits.",
     _schema({"story": STR, "id": INT, **CARD_FIELD_PROPS},
             ["story", "id"]), t_edit_card),
    ("remove_card", "Remove card `id` from a DRAFT. Removing a group bar "
     "dissolves the group; its members stay.",
     _schema({"story": STR, "id": INT}, ["story", "id"]), t_remove_card),
    ("move_card", "Move card `id` to slot `to` (counted before the card is "
     "lifted out) in a DRAFT.",
     _schema({"story": STR, "id": INT, "to": INT},
             ["story", "id", "to"]), t_move_card),
    ("rename_group", "Rename group `gname` to `to` in a DRAFT — the bar and "
     "every member card follow. Tags are untouched, so choices that jump to "
     "the group's tags still land.",
     _schema({"story": STR, "gname": STR, "to": STR},
             ["story", "gname", "to"]), t_rename_group),
    ("create_profile", "Create a NEW voice profile (existing ones are shared "
     "by every story and cannot be changed from here). engine: chatterbox "
     "(clones `voice`, dials exag/cfg/temp/rep), omnivoice (lang, speed), or "
     "kokoro (preset `kvoice`, no clip needed — the quick way to cast a new "
     "character).",
     _schema({"name": STR,
              "engine": {**STR, "enum": ["chatterbox", "omnivoice", "kokoro"]},
              "voice": STR, "kvoice": STR, "lang": STR, "speed": NUM,
              "exag": NUM, "cfg": NUM, "temp": NUM, "rep": NUM, "gain": NUM,
              "note": STR}, ["name"]), t_create_profile),
    ("render_card", "Render one card of a DRAFT to audio, in the background. "
     "Rendering costs real compute — check a voice with one card, not a "
     "whole story.",
     _schema({"story": STR, "id": INT}, ["story", "id"]), t_render_card),
    ("render_story", "Render every stale card of a DRAFT (a bake). Minutes, "
     "not seconds — only when the author asks for the whole thing.",
     _schema({"story": STR}, ["story"]), t_render_story),
    ("story_status", "How rendered a story is: card counts, which ids are "
     "stale, whether a bake is running.",
     _schema({"story": STR}, ["story"]), t_story_status),
    ("generate_image", "Paint a NEW image into the media pool with the "
     "studio's image model (only when overview() says image_gen — it uses "
     "the author's own key). Costs real money and ~15 seconds per picture. "
     "Returns the media name to set on a visual card. Never overwrites "
     "existing media.",
     _schema({"prompt": {**STR, "description":
                         "what to paint — subject, style, mood, light"},
              "name": {**STR, "description": "media name to file it under "
                       "(lowercase words and dashes, e.g. elegy8-gaze)"},
              "aspect": {**STR,
                         "enum": ["16:9", "1:1", "9:16", "4:3", "3:4", "21:9"],
                         "description": "default 16:9, the stage's shape"},
              "ref": {**STR, "description": "a media name to send as a "
                      "reference image — the model matches its style or "
                      "subject; paint one image first, then pass it here "
                      "for every other card so the set stays one cast"}},
             ["prompt"]), t_generate_image),
    ("remember", "Keep one or two sentences in your long-term memory of "
     "this library. It survives new conversations and rides into every "
     "future one; the oldest notes fall off as new ones arrive. For "
     "decisions and their reasons, casting choices, naming schemes, work "
     "left unfinished. What you DID is journaled automatically, so "
     "remember the why, not the what.",
     _schema({"note": STR}, ["note"]), t_remember),
]


# ── the wire ── newline-delimited JSON-RPC over stdio ────────────────────

def reply(mid, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main():
    tools = {name: fn for name, _, _, fn in TOOLS}
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method", "")
        if method == "initialize":
            v = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
            reply(mid, {"protocolVersion": v if re.match(r"^\d{4}-\d\d-\d\d$", v)
                        else "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "saga", "version": "1.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": [{"name": n, "description": d,
                                   "inputSchema": s}
                                  for n, d, s, _ in TOOLS]})
        elif method == "tools/call":
            p = msg.get("params") or {}
            fn = tools.get(p.get("name"))
            if fn is None:
                reply(mid, {"content": [{"type": "text",
                                         "text": f"no tool {p.get('name')}"}],
                            "isError": True})
                continue
            try:
                got = fn(p.get("arguments") or {})
                reply(mid, {"content": [{"type": "text",
                                         "text": json.dumps(got)}]})
            except ToolError as ex:
                reply(mid, {"content": [{"type": "text", "text": str(ex)}],
                            "isError": True})
            except Exception as ex:                      # never die mid-chat
                reply(mid, {"content": [{"type": "text",
                                         "text": f"{type(ex).__name__}: {ex}"}],
                            "isError": True})
        elif method == "ping":
            reply(mid, {})
        elif mid is not None:                            # a request we lack
            reply(mid, error={"code": -32601,
                              "message": f"method not found: {method}"})
        # notifications fall through silently


if __name__ == "__main__":
    main()
