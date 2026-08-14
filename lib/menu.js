'use strict'
const { app, dialog, shell, BrowserWindow } = require('electron')

const mac = process.platform === 'darwin'

module.exports = function buildMenu (api) {
  const t = []

  if (mac) {
    t.push({
      label: app.name,
      submenu: [
        { label: 'About Saga Studio', click: () => about(api) },
        { type: 'separator' },
        { label: 'Choose Python Interpreter…', click: () => api.choosePython() },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' }
      ]
    })
  }

  t.push({
    label: 'File',
    submenu: [
      { label: 'Choose Library Folder…', accelerator: 'CmdOrCtrl+Shift+O', click: () => api.chooseLibrary() },
      { label: 'Choose Voices Folder…', click: () => api.chooseVoices() },
      { type: 'separator' },
      { label: 'Show Library in Finder', accelerator: 'CmdOrCtrl+Shift+F', click: () => api.openLibrary() },
      { label: 'Show Voices in Finder', click: () => api.openVoices() },
      { type: 'separator' },
      { label: 'Restart Backend', click: () => api.restart() },
      ...(mac ? [] : [{ type: 'separator' }, { role: 'quit' }])
    ]
  })

  // Deliberately no Undo/Redo items. Saga Studio binds ⌘Z itself: inside a
  // card it is the textarea's own undo, and outside one it is the 25-deep
  // card-history undo on the server. A menu accelerator would swallow the key
  // before the page ever saw it and break the second of those.
  t.push({
    label: 'Edit',
    submenu: [
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' },
      ...(mac ? [{ role: 'pasteAndMatchStyle' }] : []),
      { role: 'delete' },
      { type: 'separator' },
      { role: 'selectAll' }
    ]
  })

  t.push({
    label: 'View',
    submenu: [
      { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => api.reload() },
      { role: 'toggleDevTools' },
      { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' },
      { role: 'togglefullscreen' }
    ]
  })

  t.push({
    label: 'Window',
    submenu: mac
      ? [{ role: 'minimize' }, { role: 'zoom' }, { type: 'separator' }, { role: 'front' }]
      : [{ role: 'minimize' }, { role: 'close' }]
  })

  t.push({
    role: 'help',
    submenu: [
      { label: 'Show Backend Log', click: () => api.openLog() },
      ...(mac ? [] : [{ label: 'Choose Python Interpreter…', click: () => api.choosePython() }]),
      { type: 'separator' },
      {
        label: 'Saga Studio on GitHub',
        click: () => shell.openExternal('https://github.com/caitlynmeeks/saga-studio')
      }
    ]
  })

  return require('electron').Menu.buildFromTemplate(t)
}

function about (api) {
  const i = api.info()
  dialog.showMessageBox(BrowserWindow.getFocusedWindow(), {
    type: 'info',
    message: 'Saga Studio',
    detail: [
      `App ${app.getVersion()} · Electron ${process.versions.electron}`,
      '',
      `Library:  ${i.library}`,
      `Voices:   ${i.voices}`,
      `Python:   ${i.python.path || 'not found'}${i.python.torch ? '' : '  (torch not found)'}`,
      `ffmpeg:   ${i.ffmpeg || 'not found'}`,
      `Serving:  ${i.url ? i.url.replace(/\?k=.*/, '') : 'not running'}`,
      '',
      `Settings: ${i.config}`
    ].join('\n'),
    buttons: ['OK']
  })
}
