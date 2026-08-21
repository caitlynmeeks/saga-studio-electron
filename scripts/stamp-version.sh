#!/bin/bash
# Stamp python/version.json with the commit the payload came from.
#
# The studio can download a newer self from GitHub into Application Support,
# and that copy outranks the one inside the .app — but only when it is
# genuinely NEWER, which needs the bundle to say when it was cut. Without
# this stamp the bundle reads as time zero, and a fresh install would lose
# to a stale download for ever.
#
# Run from build.sh; safe to run by hand. Falls back to this repo's own
# HEAD when the vendored source repo is not beside it.
set -euo pipefail
cd "$(dirname "$0")/.."

# The vendored python/ is a copy of the saga-studio repo; prefer ITS commit,
# since that is what the payload actually is.
SRC="${SAGA_SOURCE_REPO:-$HOME/git/saga_studio}"
if [ -d "$SRC/.git" ]; then
  REPO="$SRC"
else
  REPO="."
fi

SHA=$(git -C "$REPO" log -1 --format=%H 2>/dev/null || echo "")
AT=$(git -C "$REPO" log -1 --format=%ct 2>/dev/null || echo 0)
SUBJECT=$(git -C "$REPO" log -1 --format=%s 2>/dev/null || echo "")

if [ -z "$SHA" ]; then
  echo "stamp-version: no git history to stamp from; writing a zero stamp"
  AT=0
fi

python3 - "$SHA" "$AT" "$SUBJECT" <<'PY' > python/version.json
import json, sys
sha, at, subject = sys.argv[1], int(sys.argv[2] or 0), sys.argv[3]
print(json.dumps({"sha": sha, "at": at, "subject": subject}, indent=1))
PY

echo "stamped python/version.json — ${SHA:0:7} ($(date -r "$AT" +%Y-%m-%d 2>/dev/null || echo unknown))"
