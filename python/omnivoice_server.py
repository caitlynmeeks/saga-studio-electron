#!/usr/bin/env python
"""A tiny warm OmniVoice worker, spoken to over localhost by studio.py.

Why a separate process at all
-----------------------------
Chatterbox pins `transformers==5.2.0` and `safetensors==0.5.3` as exact
equalities; OmniVoice needs transformers >= 5.3. They cannot share a virtualenv,
so the second engine has to live behind a process boundary. It is not a
microservice and does not want to be one — it is one model, held warm, reachable
from the one process that needs it.

The model loads on a background thread so the port opens immediately: studio.py
can then wait on /health and show "loading" rather than hanging on a connect.

Audio is written straight to a path the caller chooses rather than sent back
over the wire. Both processes are on this machine and a minute of speech is a
few megabytes; there is nothing to gain by copying it twice.

    <omnivoice venv>/bin/python omnivoice_server.py --port 5021
"""
import argparse
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "k2-fsa/OmniVoice"
SR = 24000

_model = None
_state = {"ready": False, "error": None, "loading": True}
_lock = threading.Lock()          # MPS: one generate at a time, as in studio.py


def load(device, dtype):
    global _model
    try:
        import torch
        from omnivoice import OmniVoice
        dt = {"float32": torch.float32, "float16": torch.float16,
              "bfloat16": torch.bfloat16}[dtype]
        print(f"loading OmniVoice on {device} ({dtype}) …", flush=True)
        _model = OmniVoice.from_pretrained(MODEL_ID, device_map=device, dtype=dt)
        _state.update(ready=True, loading=False)
        print("OmniVoice warm", flush=True)
    except Exception as ex:
        _state.update(error=f"{type(ex).__name__}: {ex}", loading=False)
        traceback.print_exc()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {**_state, "sr": SR})
        return self._send(404, {"error": "?"})

    def do_POST(self):
        if not self.path.startswith("/gen"):
            return self._send(404, {"error": "?"})
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or "{}")
        if _state["error"]:
            return self._send(500, {"error": _state["error"]})
        if not _state["ready"]:
            return self._send(503, {"error": "still loading"})
        try:
            import soundfile as sf
            kw = {"text": d["text"], "ref_audio": d["ref_audio"]}
            # Only pass what was asked for: OmniVoice picks its own defaults,
            # and sending speed=None is not the same as not sending speed.
            if d.get("language"):
                kw["language"] = d["language"]
            if d.get("ref_text"):
                kw["ref_text"] = d["ref_text"]
            if d.get("speed"):
                kw["speed"] = float(d["speed"])
            if d.get("duration"):
                kw["duration"] = float(d["duration"])
            with _lock:
                wavs = _model.generate(**kw)
            w = wavs[0]
            sf.write(d["out"], w, SR)
            return self._send(200, {"ok": True, "secs": round(len(w) / SR, 2), "sr": SR})
        except Exception as ex:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(ex).__name__}: {ex}"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5021)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--dtype", default="float32")
    a = ap.parse_args()
    threading.Thread(target=load, args=(a.device, a.dtype), daemon=True).start()
    print(f"omnivoice worker on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
