#!/usr/bin/env python
"""A warm Chatterbox worker, spoken to over localhost by studio.py.

Why a separate process at all
-----------------------------
studio.py itself runs on a small interpreter — stdlib, numpy, soundfile,
kokoro — so the desktop app can ship it without shipping PyTorch. Chatterbox
needs torch, so it lives where torch lives: behind a process boundary, exactly
as OmniVoice always has. It is not a microservice and does not want to be one —
it is one model, held warm, reachable from the one process that needs it.

The model loads on a background thread so the port opens immediately:
studio.py can then wait on /health and show "loading" rather than hanging on
a connect.

Audio is written straight to a path the caller chooses rather than sent back
over the wire. Both processes are on this machine and a minute of speech is a
few megabytes; there is nothing to gain by copying it twice.

    <chatterbox venv>/bin/python chatterbox_server.py --port 5022
"""
import argparse
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VC_SR = 16000             # chatterbox VC consumes 16k mono, whatever you give it
VC_CHUNK_SECS = 25        # convert at most this much performance per model call
VC_TOP_DB = 40            # silence threshold when splitting a long take

_model = None
_vc = None
_vc_voice = [None]
_state = {"ready": False, "error": None, "loading": True}
_lock = threading.Lock()          # MPS: one generate at a time, as in studio.py


def load(device):
    global _model
    try:
        from chatterbox.tts import ChatterboxTTS
        import torch
        dev = device
        if dev == "auto":
            dev = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"loading Chatterbox on {dev} …", flush=True)
        _model = ChatterboxTTS.from_pretrained(device=dev)
        _state.update(ready=True, loading=False)
        print("Chatterbox warm", flush=True)
    except Exception as ex:
        _state.update(error=f"{type(ex).__name__}: {ex}", loading=False)
        traceback.print_exc()


def seed_take(n):
    """Pin the sampler for take n, or leave it running free for take 0.

    Chatterbox samples with temperature and never seeds, so two generate()
    calls on the same words come out differently — that is what makes
    re-render a fresh reading. A numbered take pins the global RNG so the same
    take is the same performance every time. Reproducible on this machine and
    this build of torch — a seed is not a portable description of a voice."""
    if n:
        import torch
        torch.manual_seed(int(n))


def get_vc():
    """The voice-conversion model — which is s3gen, and nothing else.

    VC renders speech tokens through the same decoder the TTS model already
    holds, so it borrows the warm s3gen rather than loading a second gigabyte
    of identical weights. The TTS model is always warm by the time this runs:
    /vc is refused until /health says ready."""
    global _vc
    if _vc is None:
        from chatterbox.vc import ChatterboxVC
        print("voice conversion: borrowing the warm s3gen", flush=True)
        _vc = ChatterboxVC(s3gen=_model.s3gen, device=str(_model.device))
        _vc_voice[0] = None
        print("voice conversion warm", flush=True)
    return _vc


def speech_spans(y):
    """(start, end) sample spans of at most VC_CHUNK_SECS, cut only inside
    silences so a word is never bisected.

    Chatterbox VC has no length cap and no chunking of its own, but a long
    take costs memory linearly and drifts, so a whole scene is converted a
    breath at a time and laid back onto the original timeline — which is also
    what keeps your pauses yours."""
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


def tts(d):
    """One line of text through the model, onto disk at d["out"]."""
    import torchaudio as ta
    with _lock:
        seed_take(d.get("seed"))
        wav = _model.generate(d["text"], audio_prompt_path=d["ref_audio"],
                              exaggeration=float(d["exag"]),
                              cfg_weight=float(d["cfg"]),
                              temperature=float(d["temp"]),
                              repetition_penalty=float(d["rep"]))
    ta.save(d["out"], wav, _model.sr)
    return {"ok": True, "secs": round(wav.shape[-1] / _model.sr, 2),
            "sr": _model.sr}


def vc(d):
    """Re-speak the recorded performance at d["src"] in the voice at
    d["ref_audio"], onto disk at d["out"].

    The take is converted a span at a time and laid back onto the original
    timeline, sized to whichever is longer — a conversion can run past its
    source, and clipping it would eat the end of the final word."""
    import torch
    import torchaudio as ta
    import librosa
    import tempfile
    from pathlib import Path
    m = get_vc()
    y, _ = librosa.load(d["src"], sr=VC_SR, mono=True)
    if not len(y):
        raise ValueError("that performance has no audio in it")
    spans = speech_spans(y)
    ratio = m.sr / VC_SR
    pieces = []
    with _lock:
        # embed_ref is the costly half and the voice rarely changes from one
        # card to the next, so hold it and re-embed only when it actually moves
        if _vc_voice[0] != d["ref_audio"]:
            m.set_target_voice(d["ref_audio"])
            _vc_voice[0] = d["ref_audio"]
        seed_take(d.get("seed"))
        with tempfile.TemporaryDirectory(prefix="saga-vc-") as tmpd:
            piece = Path(tmpd) / "piece.wav"
            for s, e in spans:
                ta.save(str(piece), torch.from_numpy(y[s:e]).unsqueeze(0), VC_SR)
                pieces.append((int(s * ratio), m.generate(str(piece)).cpu()))
    total = max(int(len(y) * ratio), max(pos + w.shape[-1] for pos, w in pieces))
    out = torch.zeros(1, total)
    for pos, w in pieces:
        out[:, pos:pos + w.shape[-1]] = w
    ta.save(d["out"], out, m.sr)
    return {"ok": True, "secs": round(total / m.sr, 2), "sr": m.sr}


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
            sr = _model.sr if _model is not None else None
            return self._send(200, {**_state, "sr": sr})
        return self._send(404, {"error": "?"})

    def do_POST(self):
        route = {"/gen": tts, "/vc": vc}.get(self.path.split("?")[0])
        if route is None:
            return self._send(404, {"error": "?"})
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or "{}")
        if _state["error"]:
            return self._send(500, {"error": _state["error"]})
        if not _state["ready"]:
            return self._send(503, {"error": "still loading"})
        try:
            return self._send(200, route(d))
        except Exception as ex:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(ex).__name__}: {ex}"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5022)
    ap.add_argument("--device", default="auto")
    a = ap.parse_args()
    threading.Thread(target=load, args=(a.device,), daemon=True).start()
    print(f"chatterbox worker on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
