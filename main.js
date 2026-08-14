'use strict'
// Saga Studio, as a desktop app.
//
// Electron does not reimplement anything: it starts the same studio.py the
// command line starts, on a private port, and shows the page that server
// already serves. What the app adds is the things a browser tab cannot — a
// place to keep the settings, a native menu, a chooser for the library
// folder, and a Python process whose life is tied to the window's.
const fs = require('fs')
const path = require('path')
const { app, BrowserWindow, dialog, ipcMain, shell, Menu } = require('electron')

const backend = require('./lib/backend')
const config = require('./lib/config')
const paths = require('./lib/paths')
const buildMenu = require('./lib/menu')

let win = null
let starting = false

// ── window ──────────────────────────────────────────────────────────────
function createWindow () {
  const saved = config.get('bounds', {})
  win = new BrowserWindow({
    width: saved.width || 1500,
    height: saved.height || 950,
    x: saved.x,
    y: saved.y,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'Saga Studio',
    backgroundColor: '#0b0d0c',            // the UI's own background: no white flash
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: true                     // it is a manuscript editor
    }
  })

  win.once('ready-to-show', () => win.show())
  win.webContents.on('dom-ready', () => {
    if (!win.webContents.getURL().startsWith('file://')) fitToTitleBar()
  })
  win.on('close', () => {
    if (win && !win.isDestroyed() && !win.isMinimized()) {
      config.save({ bounds: win.getNormalBounds() })
    }
  })
  win.on('closed', () => { win = null })

  // Links to the wider world open in the real browser; the app window itself
  // never leaves the local server.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (e, url) => {
    const ok = backend.url && url.startsWith(new URL(backend.url).origin)
    if (!ok && !url.startsWith('file://')) {
      e.preventDefault()
      if (/^https?:/.test(url)) shell.openExternal(url)
    }
  })

  return win
}

// With the title bar hidden, the traffic lights float over the page — right
// on top of the app's own header. Rather than fork the vendored UI for the
// sake of 84 pixels, the app pushes its header clear from out here and makes
// the strip draggable, so the window behaves like a native one. The header
// holds no controls, so nothing loses a click to the drag region.
// Injected on every dom-ready rather than once after the first load: injected
// style does not survive a navigation, so ⌘R would otherwise drop the window
// back to the traffic lights sitting on top of the word SAGA.
//
// A <style> element rather than webContents.insertCSS: insertCSS resolved
// without error but left the computed padding at the page's own 14px, so the
// lights stayed on top of the word SAGA. An appended tag wins, and — the part
// that actually matters — the same call reads the computed value back, so a
// silent no-op like that one shows up in the log instead of in a screenshot.
const TITLEBAR_CSS = `
header{padding-left:88px !important;-webkit-app-region:drag}
header select,header input,header button,header label{-webkit-app-region:no-drag}`

function fitToTitleBar () {
  if (process.platform !== 'darwin' || !win) return Promise.resolve()
  return win.webContents.executeJavaScript(`(() => {
    const id = 'saga-electron-titlebar'
    if (!document.getElementById(id)) {
      const s = document.createElement('style')
      s.id = id
      s.textContent = ${JSON.stringify(TITLEBAR_CSS)}
      document.head.appendChild(s)
    }
    const h = document.querySelector('header')
    return h ? getComputedStyle(h).paddingLeft : 'no header'
  })()`).then(got => {
    if (got !== '88px') console.error('[titlebar] padding is', got, '— expected 88px')
  }).catch(err => console.error('[titlebar] failed:', err.message))
}

function showShell (state, detail) {
  if (!win) return
  const send = () => win.webContents.send('saga:state', { state, detail })
  const here = win.webContents.getURL()
  if (here.startsWith('file://')) return send()
  win.webContents.once('did-finish-load', send)
  win.loadFile(path.join(__dirname, 'shell.html'))
}

// ── starting and restarting the backend ─────────────────────────────────
async function launch () {
  if (starting) return
  starting = true
  showShell('launching', { library: paths.library() })
  try {
    const info = await backend.start({ onExit: onBackendDied })
    await win.loadURL(info.url)
    win.setTitle(`Saga Studio — ${path.basename(info.library)}`)
    Menu.setApplicationMenu(buildMenu(api))
    maybeWarnAboutClassic()
  } catch (err) {
    showShell('error', {
      message: err.message || String(err),
      kind: err.kind || 'failed',
      python: (paths.python().path || null),
      library: paths.library(),
      log: backend.logText()
    })
  } finally {
    starting = false
  }
}

function onBackendDied () {
  if (!win || win.isDestroyed()) return
  showShell('error', {
    message: 'The backend stopped unexpectedly.',
    kind: 'crashed',
    python: paths.python().path,
    library: paths.library(),
    log: backend.logText()
  })
}

async function restart () {
  await backend.stop()
  await launch()
}

// The classic server keeps no lock, so two processes can open one library and
// the second write wins. Say so once, rather than silently risking it.
async function maybeWarnAboutClassic () {
  if (config.get('hideClassicWarning')) return
  if (!(await backend.classicRunning())) return
  const known = paths.KNOWN_LIBRARIES.includes(paths.library())
  if (!known) return
  const { response, checkboxChecked } = await dialog.showMessageBox(win, {
    type: 'info',
    buttons: ['Continue', 'Open the Library Folder'],
    defaultId: 0,
    message: 'The classic Saga Studio server is running',
    detail: 'Something is answering on port 5010, and this app has opened a ' +
      'library that server may also be using.\n\nBoth can read it safely, but ' +
      'editing the same episode in both at once means the last save wins. ' +
      'Best to work in one at a time.',
    checkboxLabel: 'Do not remind me',
    checkboxChecked: false
  })
  if (checkboxChecked) config.save({ hideClassicWarning: true })
  if (response === 1) shell.openPath(paths.library())
}

// ── things the menu and the shell page can ask for ──────────────────────
const api = {
  restart,
  reload: () => win && backend.running && win.loadURL(backend.url),
  library: () => paths.library(),
  logText: () => backend.logText(),

  async chooseLibrary () {
    const r = await dialog.showOpenDialog(win, {
      title: 'Choose a library folder',
      message: 'Episodes, rendered audio and clips are kept here.',
      defaultPath: paths.library(),
      properties: ['openDirectory', 'createDirectory']
    })
    if (r.canceled || !r.filePaths[0]) return false
    config.save({ libraryDir: r.filePaths[0] })
    await restart()
    return true
  },

  async chooseVoices () {
    const r = await dialog.showOpenDialog(win, {
      title: 'Choose a voices folder',
      message: 'The reference clips voices are cloned from.',
      defaultPath: paths.voices(),
      properties: ['openDirectory', 'createDirectory']
    })
    if (r.canceled || !r.filePaths[0]) return false
    config.save({ voicesDir: r.filePaths[0] })
    await restart()
    return true
  },

  async choosePython () {
    const r = await dialog.showOpenDialog(win, {
      title: 'Choose a Python interpreter',
      message: 'Pick the python inside a virtual environment that has ' +
        'chatterbox-tts and torch installed.',
      defaultPath: paths.python().path || '/usr/bin',
      properties: ['openFile', 'showHiddenFiles'],
      buttonLabel: 'Use this Python'
    })
    if (r.canceled || !r.filePaths[0]) return false
    config.save({ pythonPath: r.filePaths[0] })
    await restart()
    return true
  },

  openLibrary: () => shell.openPath(paths.library()),
  openVoices: () => shell.openPath(paths.voices()),

  openLog () {
    const p = path.join(app.getPath('userData'), 'backend.log')
    fs.writeFileSync(p, backend.logText() + '\n')
    shell.openPath(p)
  },

  info: () => ({
    library: paths.library(),
    voices: paths.voices(),
    python: paths.python(),
    ffmpeg: paths.which('ffmpeg', paths.python().path),
    config: config.file(),
    url: backend.url
  }),

  quit: () => app.quit()
}

ipcMain.handle('saga:call', async (_e, name, ...args) => {
  if (typeof api[name] !== 'function') throw new Error(`no such action: ${name}`)
  return api[name](...args)
})

// ── lifecycle ───────────────────────────────────────────────────────────
// One instance only: two would race for the same library, which is the very
// thing the classic-server warning exists to prevent.
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  app.whenReady().then(() => {
    // Packaged builds get the icon from the bundle; a dev run would otherwise
    // sit in the Dock as a generic Electron atom.
    if (!app.isPackaged && process.platform === 'darwin') {
      const icon = path.join(__dirname, 'build', 'icon.png')
      if (paths.isFile(icon)) app.dock.setIcon(icon)
    }
    createWindow()
    Menu.setApplicationMenu(buildMenu(api))
    launch()

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow()
        if (backend.running) win.loadURL(backend.url)
        else launch()
      }
    })
  })

  // macOS convention is to stay alive with no windows — and it pays here,
  // because the Python process stays alive with it and the voice model stays
  // warm. Reopening from the Dock is then instant instead of a ten-second
  // model load.
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })

  // The child does not outlive the app. before-quit is async-hostile, so the
  // quit is deferred exactly once while the backend is asked to stop.
  let cleanedUp = false
  app.on('before-quit', e => {
    if (cleanedUp || !backend.running) return
    e.preventDefault()
    backend.stop().then(() => { cleanedUp = true; app.quit() })
  })
}
