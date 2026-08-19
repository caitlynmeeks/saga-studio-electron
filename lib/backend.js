'use strict'
// Supervises the Python server: pick a port, spawn it, wait for it to answer,
// and make sure it dies when the app does.
//
// The window then loads that server's own URL. The UI is served by studio.py
// exactly as it is in the browser, which is the whole trick here — there is no
// second copy of the front end to keep in step, no custom protocol, and no
// cross-origin plumbing, because the page and the API are the same origin.
const crypto = require('crypto')
const fs = require('fs')
const net = require('net')
const path = require('path')
const { spawn, execFile } = require('child_process')
const { app } = require('electron')
const paths = require('./paths')

// The classic server's port. Never take it: a running installation must be
// left alone, and colliding with it would be the one bug that loses work.
const CLASSIC_PORT = 5010

const LOG_LINES = 400
const state = {
  child: null, port: 0, token: '', url: '', log: [], stopping: false,
  onExit: null
}

function note (line) {
  const s = String(line).replace(/\s+$/, '')
  if (!s) return
  state.log.push(s)
  if (state.log.length > LOG_LINES) state.log.splice(0, state.log.length - LOG_LINES)
  console.log('[backend]', s)
}

function logText () {
  return state.log.join('\n')
}

/** An unused localhost port, never the classic server's. */
function freePort () {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.unref()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port
      srv.close(() => (p === CLASSIC_PORT ? freePort().then(resolve, reject) : resolve(p)))
    })
  })
}

/** True if something is already answering on the classic port. */
function classicRunning () {
  return new Promise(resolve => {
    const s = net.connect({ port: CLASSIC_PORT, host: '127.0.0.1' })
    const done = v => { s.destroy(); resolve(v) }
    s.setTimeout(400)
    s.on('connect', () => done(true))
    s.on('timeout', () => done(false))
    s.on('error', () => done(false))
  })
}

function pidOnClassicPort () {
  return new Promise(resolve => {
    execFile('lsof', ['-ti', `tcp:${CLASSIC_PORT}`, '-sTCP:LISTEN'], (_err, out) => {
      const pid = parseInt(String(out || '').trim().split('\n')[0], 10)
      resolve(Number.isFinite(pid) && pid > 0 ? pid : 0)
    })
  })
}

function commandOf (pid) {
  return new Promise(resolve => {
    execFile('ps', ['-p', String(pid), '-o', 'command='], (_err, out) =>
      resolve(String(out || '').trim()))
  })
}

/**
 * The terminal-launched studio, if one is answering on the classic port:
 * {pid, command}, or null. Anything on that port that is not a studio.py is
 * someone else's business and is reported as nothing.
 */
async function classicInfo () {
  if (process.platform === 'win32') return null   // lsof/ps below are unix
  if (!(await classicRunning())) return null
  const pid = await pidOnClassicPort()
  if (!pid || pid === process.pid) return null
  const command = await commandOf(pid)
  if (!/studio\.py/.test(command)) return null
  return { pid, command }
}

/** Ask the classic instance to quit and wait for its port to fall silent. */
async function killClassic (pid) {
  // Re-check the command right before signalling: a pid can change hands,
  // and this must never kill a stranger.
  if (!/studio\.py/.test(await commandOf(pid))) return false
  const wait = async tries => {
    for (let i = 0; i < tries; i++) {
      await new Promise(r => setTimeout(r, 200))
      if (!(await classicRunning())) return true
    }
    return false
  }
  try { process.kill(pid, 'SIGTERM') } catch (_) { return !(await classicRunning()) }
  if (await wait(15)) return true
  try { process.kill(pid, 'SIGKILL') } catch (_) { }
  return wait(10)
}

function waitForServer (port, token, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  const url = `http://127.0.0.1:${port}/api/state?k=${token}`
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (state.stopping) return reject(new Error('cancelled'))
      // The child dying is the common failure — a missing module, a bad
      // interpreter — and its stderr is the actual answer, so surface that
      // rather than sitting here until the timeout expires.
      if (state.child && state.child.exitCode !== null) {
        return reject(new Error(`the backend stopped (exit ${state.child.exitCode})`))
      }
      if (Date.now() > deadline) return reject(new Error('the backend did not start in time'))
      const req = require('http').get(url, res => {
        res.resume()
        if (res.statusCode === 200) resolve()
        else setTimeout(tick, 200)
      })
      req.on('error', () => setTimeout(tick, 200))
      req.setTimeout(1500, () => req.destroy())
    }
    tick()
  })
}

async function start ({ onExit } = {}) {
  const py = paths.python()
  if (!py.path) {
    const err = new Error('No Python interpreter found.')
    err.kind = 'no-python'
    throw err
  }

  const library = paths.library()
  const voices = paths.voices()
  fs.mkdirSync(library, { recursive: true })
  fs.mkdirSync(voices, { recursive: true })

  const script = path.join(app.getAppPath(), 'python', 'studio.py')
  if (!paths.isFile(script)) throw new Error(`studio.py is missing at ${script}`)

  state.port = await freePort()
  // A shared secret for the loopback API. The server is bound to 127.0.0.1,
  // but on a multi-user machine that still means "any local process"; the
  // token makes the manuscript unreadable to anything that did not get this
  // URL from us. It is new on every launch and never written to disk.
  state.token = crypto.randomBytes(16).toString('hex')
  state.url = `http://127.0.0.1:${state.port}/?k=${state.token}`
  state.stopping = false
  state.onExit = onExit || null

  const env = Object.assign({}, process.env, {
    PATH: paths.pathFor(py.path),
    SAGA_DATA: library,
    SAGA_VOICES: voices,
    SAGA_HOST: '127.0.0.1',
    SAGA_TOKEN: state.token,
    PORT: String(state.port),
    PYTHONUNBUFFERED: '1',
    // a packaged .app is read-only, and __pycache__ beside the script would
    // either fail to write or dirty the bundle
    PYTHONDONTWRITEBYTECODE: '1'
  })

  note(`python: ${py.path}${py.torch ? '' : '  (no torch found beside it)'}`)
  note(`library: ${library}`)
  note(`voices: ${voices}`)
  note(`port: ${state.port}`)

  const child = spawn(py.path, [script], {
    cwd: path.dirname(script),
    env,
    stdio: ['ignore', 'pipe', 'pipe']
  })
  state.child = child

  child.stdout.on('data', d => String(d).split('\n').forEach(note))
  child.stderr.on('data', d => String(d).split('\n').forEach(note))
  child.on('error', err => note(`spawn failed: ${err.message}`))
  child.on('exit', (code, signal) => {
    note(`backend exited (code ${code}, signal ${signal || 'none'})`)
    const was = state.child
    state.child = null
    if (!state.stopping && was && state.onExit) state.onExit(code, signal)
  })

  try {
    await waitForServer(state.port, state.token)
  } catch (err) {
    if (!py.torch) {
      err.kind = 'no-torch'
      err.interpreter = py.path
    }
    throw err
  }
  note('ready')
  return { url: state.url, port: state.port, python: py.path, library, voices }
}

/** SIGTERM, then insist. Never leave an orphan holding the library. */
function stop () {
  const child = state.child
  if (!child) return Promise.resolve()
  state.stopping = true
  return new Promise(resolve => {
    const done = () => { clearTimeout(hard); resolve() }
    const hard = setTimeout(() => { try { child.kill('SIGKILL') } catch (_) {} ; resolve() }, 3000)
    child.once('exit', done)
    try { child.kill('SIGTERM') } catch (_) { done() }
  })
}

module.exports = {
  start, stop, logText, classicInfo, killClassic,
  get url () { return state.url },
  get running () { return !!state.child }
}
