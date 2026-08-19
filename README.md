# Saga Studio — desktop

[Saga Studio](https://github.com/caitlynmeeks/saga-studio) as a standalone
Mac app: double-click, and the studio opens on your library. No terminal, no
`./.venv/bin/python studio.py`, no browser tab that you have to remember is
holding a server.

```sh
npm install
npm start
```

## Build it yourself

```sh
git clone https://github.com/caitlynmeeks/saga-studio-electron.git
cd saga-studio-electron
./build.sh
```

One prerequisite: [Node.js](https://nodejs.org) (`brew install node`). The
script fetches Electron, a self-contained Python 3.11 and the Kokoro voice
model — about 500 MB, all from GitHub, PyPI and npm — builds
`Saga Studio.app` for your Mac, Apple Silicon or Intel, and opens it. Drag
it into /Applications to keep it.

Building it yourself is also the trust story: an app your own machine built
carries no quarantine flag, so macOS never shows the "damaged app" dialog
that greets unsigned downloads — and every script that ran is short enough
to read first.

Kokoro speaks out of the box. Chatterbox (recommended — MIT, free for any
use) and OmniVoice (CC-BY-NC: research and personal use only) are installed
from inside the app: **Voices tab → Voice Engines**. `brew install ffmpeg`
if you want clip import and mp3/video export.

`npm run dist` builds a shareable dmg instead.

## How it works

Electron does not reimplement the studio. It starts **the same `studio.py`**
on a private port and shows the page that server already serves.

```
Electron main process
  ├── picks a free port (never 5010 — the classic server may be there)
  ├── mints a one-launch token so nothing else local can read the API
  ├── spawns python studio.py with SAGA_DATA / SAGA_VOICES / PORT / SAGA_TOKEN
  ├── waits for /api/state to answer
  └── loads http://127.0.0.1:<port>/?k=<token> in the window
```

Because the page and the API are the same origin, there is no second copy of
the front end to keep in step, no custom protocol, and no cross-origin
plumbing. `python/studio_ui.html` is byte-identical to upstream, and
`python/studio.py` needed **no changes at all** — it already took `PORT`,
`SAGA_HOST`, `SAGA_DATA`, `SAGA_VOICES` and `SAGA_TOKEN` from the environment.

What the app adds is the part a browser tab cannot: somewhere to keep the
settings, a native menu, folder pickers, an icon in the Dock, and a Python
process whose life is tied to the window's.

## It adopts your existing installation

Nothing is copied or moved. On first launch the app looks for what is already
here and opens it:

| | order of preference |
| --- | --- |
| **Library** | `~/saga-studio-data`, then `~/.saga-studio`, then `~/Documents/Saga Studio` |
| **Voices** | an existing `voice-studio/voices` or `saga_studio/voices` holding clips, else `<library>/voices` |
| **Python** | a bundled runtime, a `.venv` beside this project, the `voice-studio` / `saga_studio` venvs, then the system `python3` |

Change any of them under **File** and **Saga Studio › Choose Python
Interpreter…**; the choice is remembered in
`~/Library/Application Support/saga-studio-desktop/config.json`.

A candidate interpreter only counts if **torch** is importable from it —
checked on the filesystem rather than by running Python, because asking an
interpreter to `import torch` costs seconds and this happens at every launch.
If none qualifies, the app says so and offers a picker instead of failing
halfway through your first render.

### The classic server can keep running

They are independent processes: this app never takes port 5010 and never
writes to the classic installation. Both can read one library safely, but if
the app notices something answering on 5010 while it has opened a library that
server may share, it says so once — editing the same episode in two places
means the last save wins.

## Things that only matter on the desktop

- **ffmpeg.** A GUI app inherits launchd's minimal `PATH`, not your shell's,
  so `/opt/homebrew/bin` is invisible to it and clip import and MP3 export
  would fail on a machine that plainly has ffmpeg. The app puts the usual
  places back before spawning Python.
- **⌘Z is not in the Edit menu.** Saga Studio binds it itself — inside a card
  it is the textarea's own undo, outside one it is the 25-deep card history. A
  menu accelerator would swallow the key before the page ever saw it.
- **Closing the window does not quit.** Mac convention, and it pays here: the
  Python process stays alive with the app, so the voice model stays warm and
  reopening from the Dock is instant instead of a ten-second load.
- **Quitting does kill the backend**, with a SIGTERM it will insist on. No
  orphan is left holding your library.

## Packaging

```sh
npm i -D electron-builder
npm run dist            # → dist/Saga Studio-<version>.dmg
```

That produces an app that still needs a Python with `chatterbox-tts` and
`torch` on the machine. Bundling the runtime as well — dropping a relocatable
Python under `Resources/python`, which `lib/paths.js` already prefers over
everything else — is the remaining step to a genuinely self-contained
download, and it is a multi-gigabyte one: torch is most of it.

The icon is generated, not hand-drawn:

```sh
python build/make-icon.py
```

## Layout

```
main.js            window, lifecycle, the actions the menu and shell call
preload.js         the IPC bridge — exposed only to the app's own file:// page
shell.html         launching and error screens, in the studio's own palette
lib/backend.js     spawn, health-check and reap the Python server
lib/paths.js       where the library, voices and interpreter are
lib/config.js      the settings file
lib/menu.js        the native menu
python/            a vendored copy of studio.py + studio_ui.html
build/             icon and its generator
```

`python/` is a **copy**, taken from saga-studio at `6a4901a`. That is what
makes this app standalone, and it is also the thing to watch: fixes made
upstream do not arrive here on their own.

## Licence

MIT, as upstream.
