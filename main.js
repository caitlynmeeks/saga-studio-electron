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

// The stage may make sound the moment the editor asks it to — in the app we
// own the browser, so the autoplay veil that guards a plain Chrome pop-out
// never appears here.
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')

let win = null
let starting = false

// ── the text-field context menu ─────────────────────────────────────────
// Right-click in a text field gets the menu a browser would have given it;
// Electron ships none of its own. Spelling first, the way native fields do
// it — this is a manuscript editor, and a squiggle with no suggestions under
// it was the sorest missing piece. Attached per webContents so the stage
// pop-outs get it too. The app's own right-click menus (clips, media, cards)
// live where nothing is editable and nothing is selected, so the guard at
// the bottom keeps this one out of their way. No Undo item on purpose: ⌘Z
// is story undo, and the card fields dispatch their own (see lib/menu.js).
function attachTextMenu (contents) {
  contents.on('context-menu', (_e, p) => {
    const items = []
    for (const word of (p.dictionarySuggestions || []).slice(0, 4)) {
      items.push({ label: word, click: () => contents.replaceMisspelling(word) })
    }
    if (p.misspelledWord) {
      items.push({
        label: 'Add to Dictionary',
        click: () => contents.session.addWordToSpellCheckerDictionary(p.misspelledWord)
      })
      items.push({ type: 'separator' })
    }
    if (p.isEditable) {
      items.push(
        { role: 'cut', enabled: p.editFlags.canCut },
        { role: 'copy', enabled: p.editFlags.canCopy },
        { role: 'paste', enabled: p.editFlags.canPaste },
        { type: 'separator' },
        { role: 'selectAll' }
      )
    } else if ((p.selectionText || '').trim()) {
      items.push({ role: 'copy' })
    }
    if (items.length) Menu.buildFromTemplate(items).popup()
  })
}
app.on('web-contents-created', (_e, contents) => attachTextMenu(contents))

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
  // never leaves the local server. The one exception is the server's own
  // pages — the stage window — which open as real child windows, detachable
  // onto a second monitor. No title-bar injection for them: children keep the
  // normal frame, and only the main window hides its own.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (backend.url && url.startsWith(new URL(backend.url).origin)) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          backgroundColor: '#000000',
          width: 1280,
          height: 800,
          // the only child the server opens is the stage, and a stage that
          // slips behind the editor is a stage you forget is playing
          alwaysOnTop: true
        }
      }
    }
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
  // read before start(): the backend scaffolds the library folder, and
  // "did this exist before we touched it" is the adoption question
  const libExisted = paths.isDir(paths.library())
  try {
    const info = await backend.start({ onExit: onBackendDied })
    await win.loadURL(info.url)
    win.setTitle(`Saga Studio — ${path.basename(info.library)}`)
    Menu.setApplicationMenu(buildMenu(api))
    maybeWarnAboutClassic()
    maybeAnnounceLibrary(info.library, libExisted)
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
    message: 'It closed on its own — the log below may say why.',
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

// A studio launched from a terminal keeps no lock, so it and the app can hold
// the same library and the second write wins. The rule is one Saga Studio at a
// time — so the dialog's one job is to offer to close the other copy. No
// "do not remind me": this is a state to leave, not to get used to. And no
// user-facing talk of servers or ports — from in here it is simply another
// copy of the app.
async function maybeWarnAboutClassic () {
  const other = await backend.classicInfo()
  if (!other) return
  const { response } = await dialog.showMessageBox(win, {
    type: 'warning',
    buttons: ['Close the Other Copy', 'Leave It Running'],
    defaultId: 0,
    cancelId: 1,
    message: 'Another copy of Saga Studio is running',
    detail: 'It was started outside this app — probably from a terminal — and ' +
      'it may be open on the same library.\n\nEditing in two copies at once ' +
      'can lose work: whichever saves last wins.'
  })
  if (response !== 0) return
  if (!(await backend.killClassic(other.pid))) {
    await dialog.showMessageBox(win, {
      type: 'warning',
      buttons: ['OK'],
      message: 'It would not close',
      detail: 'Quit it in the terminal it was started from, then keep working here.'
    })
  }
}

// A library the app ADOPTED is worth announcing, once. The Documents
// fallback (and the classic servers' known spots) can hold a stale library
// from an old build, and opening it silently makes a fresh install look
// broken — old test projects, missing voices — with the real explanation
// visible only on stdout, which a GUI user never sees. Only a folder that
// already had something in it earns the dialog; a fresh empty library is
// exactly what a first launch should make, and needs no ceremony.
async function maybeAnnounceLibrary (lib, existedBeforeLaunch) {
  if (config.get('libraryDir') || config.get('libraryAnnounced')) return
  config.save({ libraryAnnounced: true })
  if (!existedBeforeLaunch || !win) return
  const { response } = await dialog.showMessageBox(win, {
    type: 'info',
    buttons: ['Keep This Library', 'Choose a Different Folder…'],
    defaultId: 0,
    cancelId: 0,
    message: 'Opening your library at ' + lib,
    detail: 'This folder already existed, so the app adopted it — projects, ' +
      'rendered audio and clips are read from and saved to it.\n\n' +
      '"Choose Library Folder…" in the File menu changes it any time.'
  })
  if (response === 1) await api.chooseLibrary()
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
    const p = path.join(app.getPath('userData'), 'studio.log')
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
