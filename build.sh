#!/bin/bash
# Build Saga Studio from source, on your own Mac.
#
#   git clone <this repo>
#   cd saga-studio-electron
#   ./build.sh
#
# Building it yourself is the whole security story: an app your machine
# compiled carries no quarantine flag, so there is no "damaged app" dialog,
# no right-click ritual, and nothing for a certificate to prove. Everything
# fetched comes from GitHub, PyPI and npm — read the scripts, they are short.
#
# Needs Node.js (brew install node, or nodejs.org). Everything else is
# stock macOS. Apple Silicon and Intel both work; the app is built for
# whichever this machine is.
set -euo pipefail
cd "$(dirname "$0")"

command -v node >/dev/null 2>&1 || {
  echo "Saga Studio builds with Node.js, which is not installed."
  echo "  brew install node     (or download it from nodejs.org)"
  echo "then run ./build.sh again."
  exit 1
}

echo "── 1/3 installing Electron (npm)…"
npm install --no-audit --no-fund

echo "── 2/3 fetching the Python runtime and the Kokoro voices…"
npm run --silent runtime

echo "── 3/3 building the app…"
npx electron-builder --dir

APP=$(find dist -maxdepth 2 -name "Saga Studio.app" -print -quit)
[ -n "$APP" ] || { echo "the build did not produce an app — see above"; exit 1; }

echo
echo "Built: $APP"
echo "Opening it now. To keep it, drag it into /Applications — it runs"
echo "from anywhere. Voices beyond the built-in Kokoro are downloaded"
echo "from inside the app: Voices tab → Voice Engines."
open "$APP"
