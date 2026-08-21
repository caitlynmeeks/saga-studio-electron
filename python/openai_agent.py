#!/usr/bin/env python3
"""The discuss agent, for studios whose story brain is not Claude.

Speaks any OpenAI-compatible chat API — LM Studio, llama.cpp's server,
OpenAI itself — and gives that model the same hands headless Claude gets:
the tool table in saga_mcp.py, whose every call goes through the studio's
own HTTP API. The draft-sandbox law is enforced in those tools, so it
binds a local model exactly as it binds Claude.

Spawned by studio.py with the ask in SAGA_LLM (JSON: url, key, model,
rules, prompt, transcript, fresh) and SAGA_API / SAGA_TOKEN in the
environment for the tools. Emits the same stream-json events on stdout
that Claude Code emits, so the studio's SSE relay needs no second
dialect. Stdlib only, like everything else the studio spawns.

Memory between asks is the `transcript` file — a JSON list of chat
messages reloaded on every ask and trimmed to a window, the local
equivalent of Claude Code's --resume; `fresh` starts it over. The
library-wide memory file rides inside `rules`, same as it does for
Claude. Errors go to stderr and a nonzero exit: the relay's
"stopped early" tail carries them to the panel.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import saga_mcp                 # the tool table and the studio HTTP proxy

CONF = json.loads(os.environ["SAGA_LLM"])
MAX_ROUNDS = 24                 # tool round-trips per ask before giving up
WINDOW = 60                     # transcript messages carried into an ask


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def say(text):
    emit({"type": "stream_event",
          "event": {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text}}})


def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def call(messages):
    """One model call, streamed when the server streams. Text deltas go to
    the panel as they arrive; returns the assembled assistant message in
    the API's own shape, ready to append to the transcript."""
    body = {"messages": messages, "stream": True,
            "tools": [{"type": "function",
                       "function": {"name": n, "description": d,
                                    "parameters": s}}
                      for n, d, s, _ in saga_mcp.TOOLS]}
    if CONF.get("model"):
        body["model"] = CONF["model"]
    req = urllib.request.Request(
        CONF["url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + CONF["key"]}
                    if CONF.get("key") else {})})
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            got = json.loads(ex.read().decode())
            err = got.get("error")
            detail = (err.get("message", "") if isinstance(err, dict)
                      else str(err or ""))
        except Exception:
            pass
        die(f"the model server said {ex.code}"
            + (f": {detail[:200]}" if detail else ""))
    except urllib.error.URLError as ex:
        die(f"could not reach the model server at {CONF['url']} "
            f"({ex.reason}). Is it running? The Settings tab holds "
            "the address.")
    text, calls = "", {}
    with resp:
        if "text/event-stream" not in (resp.headers.get("Content-Type")
                                       or ""):
            # a server that ignored stream=true answers in one piece
            try:
                got = json.loads(resp.read().decode())
            except ValueError:
                die("the model server sent something that is not JSON")
            msg = (got.get("choices") or [{}])[0].get("message") or {}
            if msg.get("content"):
                say(msg["content"])
            return {"role": "assistant",
                    "content": msg.get("content"),
                    **({"tool_calls": msg["tool_calls"]}
                       if msg.get("tool_calls") else {})}
        for raw in resp:
            raw = raw.decode("utf-8", "replace").strip()
            if not raw.startswith("data:"):
                continue
            raw = raw[5:].strip()
            if raw == "[DONE]":
                break
            try:
                ch = json.loads(raw)
            except ValueError:
                continue
            delta = ((ch.get("choices") or [{}])[0].get("delta")) or {}
            if delta.get("content"):
                text += delta["content"]
                say(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0),
                                        {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    first = not slot["name"]
                    slot["name"] += fn["name"]
                    if first:
                        # the panel's "○ name…" chip, replaced on completion
                        emit({"type": "stream_event",
                              "event": {"type": "content_block_start",
                                        "content_block": {
                                            "type": "tool_use",
                                            "name": slot["name"]}}})
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
    out = {"role": "assistant", "content": text or None}
    if calls:
        out["tool_calls"] = [
            {"id": c["id"] or f"call_{i}", "type": "function",
             "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
            for i, c in sorted(calls.items())]
    return out


def run_tool(name, args):
    """Execute one tool locally, mirroring saga_mcp's own error manners:
    a ToolError is a message for the model, never a death."""
    fn = next((f for n, _, _, f in saga_mcp.TOOLS if n == name), None)
    if fn is None:
        return f"no tool called {name}", True
    try:
        return json.dumps(fn(args)), False
    except saga_mcp.ToolError as ex:
        return str(ex), True
    except Exception as ex:                          # never die mid-chat
        return f"{type(ex).__name__}: {ex}", True


def main():
    transcript = CONF.get("transcript") or ""
    msgs = []
    if transcript and not CONF.get("fresh"):
        try:
            msgs = json.loads(open(transcript, encoding="utf-8").read())
        except (OSError, ValueError):
            msgs = []
    emit({"type": "system", "subtype": "init"})
    msgs.append({"role": "user", "content": CONF["prompt"]})
    system = [{"role": "system", "content": CONF["rules"]}]
    for _ in range(MAX_ROUNDS):
        window = msgs[-WINDOW:]
        # never open the window on an orphaned tool result — the strict
        # APIs refuse a tool message whose call was trimmed away
        while window and window[0].get("role") == "tool":
            window.pop(0)
        m = call(system + window)
        msgs.append(m)
        if not m.get("tool_calls"):
            break
        for tc in m["tool_calls"]:
            name = (tc.get("function") or {}).get("name") or ""
            try:
                args = json.loads((tc.get("function") or {})
                                  .get("arguments") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            out, err = run_tool(name, args)
            # the panel's chips: "● name {input}", warnings for errors
            emit({"type": "assistant",
                  "message": {"content": [{"type": "tool_use", "name": name,
                                           "input": args}]}})
            if err:
                emit({"type": "user",
                      "message": {"content": [{"type": "tool_result",
                                               "is_error": True,
                                               "content": out}]}})
            msgs.append({"role": "tool",
                         "tool_call_id": tc.get("id") or name,
                         "content": out})
    else:
        say("\n(I stopped here: too many tool calls in one ask. "
            "Ask again to continue.)")
    if transcript:
        try:
            os.makedirs(os.path.dirname(transcript), exist_ok=True)
            with open(transcript, "w", encoding="utf-8") as f:
                # twice the prompt window on disk: enough that trimming for
                # the model never loses what the next ask reloads
                json.dump(msgs[-2 * WINDOW:], f, indent=1)
        except OSError:
            pass
    emit({"type": "result"})


if __name__ == "__main__":
    main()
