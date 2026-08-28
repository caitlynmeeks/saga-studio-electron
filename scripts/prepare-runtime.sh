#!/bin/bash
# Builds build/runtime/ — the Python that ships inside the app — and
# build/models/ — the Kokoro voice model. Run once before `npm run dist`;
# everything it fetches is cached, so a second run is seconds.
#
# The runtime is deliberately small: numpy, soundfile and soxr for the
# mixdown, kokoro-onnx for the built-in engine, pedalboard for the plugin
# host. No torch anywhere — the torch engines (Chatterbox, OmniVoice) are
# fetched by the engine manager, on the user's machine, at the user's ask.
set -euo pipefail
cd "$(dirname "$0")/.."

PBS_TAG=20260814
PBS_PY=3.11.16          # 3.11: the version the torch engines are proven on
case "$(uname -m)" in
  arm64) ARCH=aarch64-apple-darwin ;;
  x86_64) ARCH=x86_64-apple-darwin ;;
  *) echo "unknown architecture $(uname -m) — this builds Mac apps"; exit 1 ;;
esac
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PBS_PY}+${PBS_TAG}-${ARCH}-install_only_stripped.tar.gz"
UV_URL="https://github.com/astral-sh/uv/releases/latest/download/uv-${ARCH}.tar.gz"

RT=build/runtime
mkdir -p "$RT" build/models

# ── the interpreter ─────────────────────────────────────────────────────
if [ ! -x "$RT/python/bin/python3" ]; then
  echo "fetching CPython $PBS_PY ($PBS_TAG)…"
  curl -L --fail -o "$RT/pbs.tar.gz" "$PBS_URL"
  tar -xzf "$RT/pbs.tar.gz" -C "$RT"          # unpacks to $RT/python
  rm "$RT/pbs.tar.gz"
fi
PY="$RT/python/bin/python3"
"$PY" -V

# ── the packages the studio itself needs ────────────────────────────────
"$PY" -m pip install --upgrade --no-compile --quiet \
  numpy soundfile soxr kokoro-onnx pedalboard

# ── prune what a shipped runtime never uses ─────────────────────────────
LIB="$RT/python/lib/python3.11"
rm -rf "$LIB/test" "$LIB/idlelib" "$LIB/tkinter" "$LIB/turtledemo" \
       "$RT/python/share" "$RT/python/lib/tcl8"* "$RT/python/lib/tk8"* \
       "$RT/python/lib/libtcl"* "$RT/python/lib/libtk"* \
       "$LIB/lib-dynload/_tkinter"*.so
find "$RT/python" -name '__pycache__' -type d -prune -exec rm -rf {} +

# ── uv, so the engine manager's installs survive a flaky connection ─────
if [ ! -x "$RT/uv/uv" ]; then
  echo "fetching uv…"
  mkdir -p "$RT/uv"
  curl -L --fail -o "$RT/uv.tar.gz" "$UV_URL"
  tar -xzf "$RT/uv.tar.gz" -C "$RT/uv" --strip-components 1
  rm "$RT/uv.tar.gz"
fi
"$RT/uv/uv" --version

# ── the Kokoro model — copied from a library that already has it, else
#    fetched from the kokoro-onnx release ────────────────────────────────
K=build/models/kokoro
mkdir -p "$K"
SRC="${SAGA_DATA:-$HOME/saga-studio-data}/models/kokoro"
for f in kokoro-v1.0.int8.onnx voices-v1.0.bin; do
  if [ ! -f "$K/$f" ]; then
    if [ -f "$SRC/$f" ]; then
      echo "copying $f from $SRC"
      cp "$SRC/$f" "$K/$f"
    else
      # braced because Apple's bash is 3.2, which reads $f followed by a
      # multibyte … as a variable NAMED "f…" — and under set -u that is
      # unbound and fatal, but only on the branch a fresh machine takes
      echo "fetching ${f}…"
      curl -L --fail -o "$K/$f" \
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
    fi
  fi
done

du -sh "$RT/python" "$RT/uv" build/models
echo "runtime ready"
