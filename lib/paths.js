'use strict'
// Where the library, the voice clips and the Python interpreter are.
//
// Two rules shape all of this:
//
//   The user's material never lives inside the app. A packaged .app is
//   read-only and gets replaced wholesale on update, so anything written
//   there would be lost. studio.py defaults SAGA_VOICES to a directory beside
//   itself, which is exactly the wrong place once it is bundled — so the
//   voices path is always passed explicitly.
//
//   An existing installation is adopted, never disturbed. If the classic
//   server's library is on this machine, the app opens it rather than
//   starting an empty one somewhere else and looking broken.
const fs = require('fs')
const os = require('os')
const path = require('path')
const { app } = require('electron')
const config = require('./config')

const HOME = os.homedir()
const isDir = p => { try { return fs.statSync(p).isDirectory() } catch (_) { return false } }
const isFile = p => { try { return fs.statSync(p).isFile() } catch (_) { return false } }

// Libraries the classic server is known to use, best first. ~/.saga-studio is
// studio.py's own default; ~/saga-studio-data is what a SAGA_DATA launch
// tends to point at.
const KNOWN_LIBRARIES = [
  path.join(HOME, 'saga-studio-data'),
  path.join(HOME, '.saga-studio')
]

function defaultLibrary () {
  for (const p of KNOWN_LIBRARIES) if (isDir(p)) return p
  return path.join(app.getPath('documents'), 'Saga Studio')
}

function library () {
  return config.get('libraryDir') || defaultLibrary()
}

// Voice clips are global to every project, so an existing collection is worth
// finding: without one, every render fails with "no voice 'caitlyn2'".
const KNOWN_VOICES = [
  path.join(HOME, 'git', 'voice-studio', 'voices'),
  path.join(HOME, 'git', 'saga_studio', 'voices')
]

function defaultVoices () {
  for (const p of KNOWN_VOICES) {
    if (isDir(p) && fs.readdirSync(p).some(f => /\.(wav|mp3|flac|m4a)$/i.test(f))) return p
  }
  return path.join(library(), 'voices')
}

function voices () {
  return config.get('voicesDir') || defaultVoices()
}

// ── the interpreter ─────────────────────────────────────────────────────
// Chatterbox needs torch, and torch is a multi-gigabyte dependency nobody
// installs by accident. So a candidate is only credible if torch is actually
// importable from it — which is a filesystem question, not a subprocess:
// asking python to import torch costs seconds, and this runs at every launch.
function hasTorch (py) {
  const venv = path.dirname(path.dirname(py))          // .../bin/python -> .../
  let libs
  try {
    libs = fs.readdirSync(path.join(venv, 'lib'))
  } catch (_) { return false }
  return libs.some(l => isDir(path.join(venv, 'lib', l, 'site-packages', 'torch')))
}

function candidates () {
  const out = []
  // 1. a runtime bundled inside the packaged app
  if (process.resourcesPath) {
    out.push(path.join(process.resourcesPath, 'python', 'bin', 'python3'))
  }
  // 2. a venv beside this project — what `npm run setup-python` makes
  out.push(path.join(app.getAppPath(), '.venv', 'bin', 'python'))
  // 3. the venvs the classic installation uses
  out.push(path.join(HOME, 'git', 'voice-studio', '.venv', 'bin', 'python'))
  out.push(path.join(HOME, 'git', 'saga_studio', '.venv', 'bin', 'python'))
  // 4. whatever is on the system
  out.push('/opt/homebrew/bin/python3.11', '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3', '/usr/bin/python3')
  return out.filter(isFile)
}

/** The interpreter to launch with, and why it was chosen. */
function python () {
  const chosen = config.get('pythonPath')
  if (chosen && isFile(chosen)) return { path: chosen, torch: hasTorch(chosen), source: 'chosen' }
  // A bundled runtime always wins. It has no torch on purpose — the studio
  // itself no longer needs it, and the torch engines run from per-machine
  // venvs the engine manager builds under Application Support.
  if (process.resourcesPath) {
    const bundled = path.join(process.resourcesPath, 'python', 'bin', 'python3')
    if (isFile(bundled)) return { path: bundled, torch: false, source: 'bundled' }
  }
  const all = candidates()
  const withTorch = all.find(hasTorch)
  if (withTorch) return { path: withTorch, torch: true, source: 'found' }
  if (all.length) return { path: all[0], torch: false, source: 'fallback' }
  return { path: null, torch: false, source: 'none' }
}

// ── what ships beside the runtime ───────────────────────────────────────
/** Where the engine manager keeps its venvs: per-machine, never in the
    library — a library that travels must not carry one machine's torch. */
function engines () {
  return path.join(app.getPath('userData'), 'engines')
}

/** The bundled uv, or null — the engine manager falls back to pip. */
function uv () {
  if (!process.resourcesPath) return null
  const p = path.join(process.resourcesPath, 'uv', 'uv')
  return isFile(p) ? p : null
}

/** The bundled Kokoro model dir, or null when the library has its own copy —
    which wins, since it may hold a bigger-precision model. */
function kokoroDir (libraryDir) {
  for (const n of ['kokoro-v1.0.onnx', 'kokoro-v1.0.fp16.onnx', 'kokoro-v1.0.int8.onnx']) {
    if (isFile(path.join(libraryDir, 'models', 'kokoro', n))) return null
  }
  if (!process.resourcesPath) return null
  const b = path.join(process.resourcesPath, 'models', 'kokoro')
  return isFile(path.join(b, 'voices-v1.0.bin')) ? b : null
}

// A GUI app inherits launchd's minimal PATH, not the login shell's — so
// ffmpeg in /opt/homebrew/bin is invisible to it, and clip import and mp3
// export would fail with "ffmpeg is needed" on a machine that plainly has
// ffmpeg. Put the usual places back.
function pathFor (pythonPath) {
  const extra = ['/opt/homebrew/bin', '/usr/local/bin', path.join(HOME, '.local', 'bin')]
  if (pythonPath) extra.unshift(path.dirname(pythonPath))
  const seen = new Set()
  return extra.concat((process.env.PATH || '').split(':'))
    .filter(p => p && !seen.has(p) && seen.add(p))
    .join(':')
}

function which (cmd, pythonPath) {
  for (const dir of pathFor(pythonPath).split(':')) {
    const p = path.join(dir, cmd)
    try {
      fs.accessSync(p, fs.constants.X_OK)
      return p
    } catch (_) { /* keep looking */ }
  }
  return null
}

module.exports = {
  library, defaultLibrary, voices, defaultVoices, python, candidates,
  hasTorch, pathFor, which, isDir, isFile, KNOWN_LIBRARIES,
  engines, uv, kokoroDir
}
