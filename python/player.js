'use strict'
// The one place the choice grammar runs. The studio's server validates the
// shape of set ops and conditions and never evaluates them; the stage and the
// exported HTML player both drive THIS walk with their own hooks, so a story
// cannot branch one way live and another way after export.
//
// State is a flat {name: number} map. Flags are 0/1. Set ops: `flag`,
// `!flag`, `coins+2`, `coins-1`, `coins=5`. Conditions: `flag`, `!flag`,
// `coins>=3` (and == != <= > <). The server strips whitespace on the way in,
// so one spelling arrives here.
const SagaPlay = (() => {
  function applySet (state, ops) {
    for (const s of ops || []) {
      let m
      if ((m = /^!([a-z][a-z0-9_]*)$/.exec(s))) state[m[1]] = 0
      else if ((m = /^([a-z][a-z0-9_]*)([+\-=])(\d+)$/.exec(s))) {
        const v = +m[3]
        state[m[1]] = m[2] === '=' ? v : (state[m[1]] || 0) + (m[2] === '+' ? v : -v)
      } else if ((m = /^([a-z][a-z0-9_]*)$/.exec(s))) state[m[1]] = 1
    }
  }

  function passes (state, when) {
    if (!when) return true
    let m
    if ((m = /^(!?)([a-z][a-z0-9_]*)$/.exec(when))) {
      const v = !!(state[m[2]] || 0)
      return m[1] ? !v : v
    }
    if ((m = /^([a-z][a-z0-9_]*)(==|!=|>=|<=|>|<)(-?\d+)$/.exec(when))) {
      const a = state[m[1]] || 0, b = +m[3], op = m[2]
      return op === '==' ? a === b : op === '!=' ? a !== b
        : op === '>=' ? a >= b : op === '<=' ? a <= b
        : op === '>' ? a > b : a < b
    }
    return true            // the server should never let this arrive
  }

  // Index of the first card carrying the tag — where a jump lands. Tags are
  // anchors precisely because they survive the renumbering ids do not.
  const findTag = (chunks, tag) =>
    chunks.findIndex(c => (c.tags || []).includes(tag))

  // Where the run starting at i must stop: the next card that playback has to
  // think about rather than simply speak — a choice, or a conditional card.
  // Everything between two stops is one premixed stretch of audio.
  function runEnd (chunks, i) {
    let j = i + 1
    while (j < chunks.length) {
      const c = chunks[j]
      if (c.type === 'choice' || c.when) break
      j++
    }
    return j
  }

  // The walk. Both players drive it with hooks:
  //   playRun(fromId, uptoIdOrNull, fromIndex) -> Promise<boolean>
  //       speak cards [from, upto); resolve true at the natural end, false if
  //       the listener stopped the story and the walk should abandon.
  //   ask(options, card) -> Promise<option>   render the chooser, wait.
  //   onEnd(reason)                           'end' | 'dangling:<tag>'
  // Muted cards are the mixdown's business, not ours: they are silent inside
  // a run and a muted choice card simply does not stop the story.
  async function walk (chunks, hooks, state, startIndex) {
    state = state || {}
    let i = startIndex || 0
    while (i < chunks.length) {
      const c = chunks[i]
      if (c.type === 'choice' && !c.mute) {
        if (c.when && !passes(state, c.when)) { i++; continue }
        const live = (c.options || []).filter(o => o.label || o.goto)
        const open = live.filter(o => passes(state, o.when))
        if (!open.length) { i++; continue }
        const o = c.auto ? open[0] : await hooks.ask(open, c)
        if (!o) return                       // the listener walked away
        applySet(state, o.set)
        if (!o.goto) { hooks.onEnd('end'); return }
        const t = findTag(chunks, o.goto)
        if (t < 0) { hooks.onEnd('dangling:' + o.goto); return }
        i = t
        continue
      }
      if (c.type === 'choice') { i++; continue }        // muted: not a stop
      if (c.when && !passes(state, c.when)) { i++; continue }
      const j = runEnd(chunks, i)
      const uptoId = j < chunks.length ? chunks[j].id : null
      const ok = await hooks.playRun(c.id, uptoId, i)
      if (!ok) return
      i = j
    }
    hooks.onEnd('end')
  }

  // Every stretch the exported player could ever need on disk, before it is
  // asked for: run boundaries as walk() would find them, PLUS a break at
  // every card any goto names — a jump must land on a stretch that starts
  // there, because the export's audio is premixed and cannot begin mid-file.
  function segments (chunks) {
    const targets = new Set()
    for (const c of chunks) {
      if (c.type !== 'choice') continue
      for (const o of c.options || []) if (o.goto) targets.add(o.goto)
    }
    const brk = new Set([0])
    chunks.forEach((c, i) => {
      if (c.type === 'choice' || c.when) { brk.add(i); brk.add(i + 1) }
      else if ((c.tags || []).some(t => targets.has(t))) brk.add(i)
    })
    const cuts = [...brk].filter(i => i < chunks.length).sort((a, b) => a - b)
    const out = []
    for (let k = 0; k < cuts.length; k++) {
      const a = cuts[k], b = k + 1 < cuts.length ? cuts[k + 1] : chunks.length
      if (a >= b) continue
      if (chunks[a].type === 'choice') continue         // the stop, not a stretch
      out.push({ from: a, upto: b })
    }
    return out
  }

  // Reveal a caption at the pace the audio gives it — the typewriter effect,
  // off by default in both players. The rate comes from the stretch of time
  // the card actually occupies, aiming to land the last character a beat
  // before the audio moves on; clamped so a two-word line does not smear and
  // a monologue does not blur. Returns a canceller, because captions change
  // mid-flight and two typewriters on one element write over each other.
  function typeCaption (el, text, secs, from) {
    // `from` is where the fresh words start: a chained caption keeps its
    // standing prefix on screen and types only what this card adds, at a
    // rate set by this card's own stretch of audio — which is what keeps
    // the pace honest across a chain of differently-sized cards.
    from = from || 0
    el.textContent = text.slice(0, from)
    const n = text.length
    if (n <= from) return () => {}
    const fresh = n - from
    const cps = Math.min(120, Math.max(12, fresh / Math.max(0.6, (secs || fresh / 15) * 0.9)))
    let i = from
    const t = setInterval(() => {
      i++
      el.textContent = text.slice(0, i)
      if (i >= n) clearInterval(t)
    }, 1000 / cps)
    return () => clearInterval(t)
  }

  // Chained captions. A card marked `chain` leaves its words standing when it
  // ends, and the next caption-bearing card appends to them — one growing
  // block across cards that were split for delivery, not for meaning. `st` is
  // the player's caption state ({txt, chain}), mutated in place. Returns what
  // to show and where the fresh text begins; `hold` means a wordless card sat
  // down inside a chain and the caption (and any typewriter mid-word) should
  // simply be left alone.
  function chainStep (st, card, text) {
    if (!text) {
      if (st.chain) return { full: st.txt, from: st.txt.length, hold: true }
      st.txt = ''
      return { full: '', from: 0 }
    }
    const joined = st.chain && st.txt
    const full = joined ? st.txt + ' ' + text : text
    const from = joined ? st.txt.length + 1 : 0
    st.txt = card.chain ? full : ''
    st.chain = !!card.chain
    return { full, from }
  }

  return { applySet, passes, findTag, runEnd, walk, segments, typeCaption, chainStep }
})()
if (typeof module !== 'undefined') module.exports = SagaPlay
