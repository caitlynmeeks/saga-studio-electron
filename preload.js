'use strict'
// This preload is attached to the window for its whole life, but the window
// spends nearly all of that life showing the Python server's page. That page
// is ordinary web content and must not be handed privileged functions, so the
// bridge is only exposed to the app's own file:// shell.
const { contextBridge, ipcRenderer } = require('electron')

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('saga', {
    call: (name, ...args) => ipcRenderer.invoke('saga:call', name, ...args),
    onState: cb => ipcRenderer.on('saga:state', (_e, payload) => cb(payload))
  })
}
