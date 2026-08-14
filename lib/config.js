'use strict'
// Settings live in one small JSON file in the OS's per-app directory, not in
// the library folder — the library is the user's material and should stay
// portable between machines that disagree about where Python lives.
const fs = require('fs')
const path = require('path')
const { app } = require('electron')

const FILE = () => path.join(app.getPath('userData'), 'config.json')

let cache = null

function load () {
  if (cache) return cache
  try {
    cache = JSON.parse(fs.readFileSync(FILE(), 'utf8'))
  } catch (_) {
    cache = {}
  }
  return cache
}

function save (patch) {
  const c = Object.assign(load(), patch)
  cache = c
  try {
    fs.mkdirSync(path.dirname(FILE()), { recursive: true })
    fs.writeFileSync(FILE(), JSON.stringify(c, null, 1))
  } catch (err) {
    console.error('could not write config:', err.message)
  }
  return c
}

function get (key, fallback) {
  const v = load()[key]
  return v === undefined ? fallback : v
}

module.exports = { load, save, get, file: FILE }
