// ── SagaDesk — the live mixing desk ─────────────────────────────────────
// The premixed stretch is gone: every ride and every editor book preview
// plays through this node graph instead, so a fader, a mute or the Master
// engages live, mid-word, with no seam and no skip — the Logic Pro model.
//
//   source (AudioBufferSourceNode)
//     → srcGain   (the card's own STATIC factor × its fades, automation)
//     → chGain    (the channel fader, live)      ← {type:'mix'} / local UI
//     → masterGain (the Master bus, live)        ← same
//     → monitorGain (this machine's knob)        ← the volume slider
//     → destination
//
// The score comes from /api/mix_plan — the same cursor walk mixdown() sums
// for exports, so what you hear here is what ships, by construction (modulo
// the clipper: the desk does not hard-clip; Master below unity is the
// author's clip rescue, exactly as on tape). Channel and Master values are
// deliberately NOT in the plan: they belong to this graph, at play time and
// at every moment after.
//
// The engine is a rolling-window scheduler: only the next ~45s of sources
// are fetched, decoded and armed — two hours of decoded book would be
// ~690MB nobody has — topped up from a small tick. pause() is
// ctx.suspend(): the clock and every scheduled source freeze as one, the
// clean win over a media element. Decoded buffers are cached by URL (two
// cards with the same words share a wav), LRU-capped.
//
// Shared by stage_ui.html and studio_ui.html via <script src="/desk.js">.
// NEVER inlined into an export — the exported player has no server and
// stays premixed.

const SagaDesk = (() => {
  'use strict';
  let ctx = null, masterNode = null, monitorNode = null;
  let chNodes = {};                    // channel id -> GainNode, made lazily
  // desired values, held even before the first ride creates the context
  let MIX = { gains: {}, master: 1 };
  let MON = { v: 1, mute: false };
  const SMOOTH = 0.03;                 // setTargetAtTime constant: no zipper
  const AHEAD = 45;                    // seconds of book armed at any moment
  const BUF_CAP = 50;                  // decoded buffers kept, LRU

  function ensure() {
    if (ctx) return ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    ctx = new AC();
    masterNode = ctx.createGain();
    monitorNode = ctx.createGain();
    masterNode.connect(monitorNode);
    monitorNode.connect(ctx.destination);
    masterNode.gain.value = MIX.master;
    monitorNode.gain.value = MON.mute ? 0 : MON.v;
    return ctx;
  }
  // The browser may refuse sound until a click has landed somewhere. True
  // when the clock is actually running; a refusal leaves the caller its
  // "play again from the button" manners, same as the old element's catch.
  async function resume() {
    ensure();
    try {
      await Promise.race([ctx.resume(), new Promise(r => setTimeout(r, 350))]);
    } catch (e) {}
    return ctx.state === 'running';
  }

  // doc-shaped channel row -> linear gain, mute folded to zero — the same
  // arithmetic as the server's channel_gains(), kept in step by hand
  const lin = ch => ch.mute ? 0
    : Math.max(0, Math.min(200, ch.gain == null ? 100 : +ch.gain)) / 100;
  const mainGain = () => MIX.gains.main == null ? 1 : MIX.gains.main;
  const glide = (node, v) => {
    if (node) node.gain.setTargetAtTime(v, ctx.currentTime, SMOOTH);
  };
  // a channel id with no node yet gets one lazily; an id the mixer does not
  // know routes to main's node — mirrors channel_gain_of's fallback
  function chNode(id) {
    if (id !== 'main' && !(id in MIX.gains)) id = 'main';
    if (!chNodes[id]) {
      const g = ensure().createGain();
      g.gain.value = id in MIX.gains ? MIX.gains[id] : 1;
      g.connect(masterNode);
      chNodes[id] = g;
    }
    return chNodes[id];
  }
  // the whole desk at once, doc-shaped: setMix(DOC.channels, DOC.master).
  // Live — a ride in progress hears it mid-word.
  function setMix(channels, master) {
    MIX.gains = {};
    (channels || []).forEach(ch => {
      if (ch && ch.id) MIX.gains[String(ch.id)] = lin(ch);
    });
    const m = master || {};
    MIX.master = m.mute ? 0
      : Math.max(0, Math.min(200, m.gain == null ? 100 : +m.gain)) / 100;
    if (!ctx) return;
    glide(masterNode, MIX.master);
    // a node whose channel was removed mid-ride follows main, as the
    // server's fallback would at the next mix
    Object.keys(chNodes).forEach(id =>
      glide(chNodes[id], id in MIX.gains ? MIX.gains[id] : mainGain()));
  }
  // this machine's loudness, 0..1 — never in any render, never broadcast
  function setMonitor(v, mute) {
    MON = { v: Math.max(0, Math.min(1, +v || 0)), mute: !!mute };
    if (ctx) glide(monitorNode, MON.mute ? 0 : MON.v);
  }

  // decoded buffers by URL. Two cards with the same words share a wav —
  // and so share one decode. The Map's insertion order is the LRU.
  const BUFS = new Map();
  function bufferOf(url) {
    if (BUFS.has(url)) {
      const p = BUFS.get(url);
      BUFS.delete(url); BUFS.set(url, p);
      return p;
    }
    const p = fetch(url)
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
      .then(ab => ensure().decodeAudioData(ab));
    BUFS.set(url, p);
    p.catch(() => BUFS.delete(url));   // a failure is not worth remembering
    while (BUFS.size > BUF_CAP) BUFS.delete(BUFS.keys().next().value);
    return p;
  }
  // the unready-card bell, synthesized here: authoring-only, so it need not
  // match the server's wave — same fifth, same fast decay, close enough
  function chimeBuf(c, dur) {
    const sr = c.sampleRate, n = Math.max(1, Math.round(sr * (dur || 0.45)));
    const b = c.createBuffer(1, n, sr), d = b.getChannelData(0), a = sr * 0.006;
    for (let i = 0; i < n; i++) {
      const t = i / sr;
      const env = Math.exp(-t * 7) * (i < a ? i / a : 1);
      d[i] = (Math.sin(2 * Math.PI * 784 * t)
              + 0.45 * Math.sin(2 * Math.PI * 1176 * t)) * env * 0.13;
    }
    return b;
  }
  // fades are percentages of the clip, ramped on the source's own gain —
  // the same shape mixdown bakes: [10, 90] is up over the first 10%, down
  // over the last 10%, and the card's static gain rides on top
  function fades(param, ev, when, base) {
    const lo = +((ev.fade || [])[0]) || 0;
    const hi = (ev.fade || [])[1] == null ? 100 : +ev.fade[1];
    const dur = +ev.dur || 0;
    if (!dur || (lo <= 0 && hi >= 100)) return;
    if (lo > 0) {
      param.setValueAtTime(0, when);
      param.linearRampToValueAtTime(base, when + dur * lo / 100);
    }
    if (hi < 100) {
      param.setValueAtTime(base, when + dur * hi / 100);
      param.linearRampToValueAtTime(0, when + dur);
    }
  }
  // one event onto a graph: source → srcGain → out. Shared by the live
  // engine and the offline agreement test, so the two cannot drift.
  function wire(c, buf, ev, when, out) {
    const src = c.createBufferSource();
    src.buffer = buf;
    const g = c.createGain();
    const base = ev.gain == null ? 1 : +ev.gain;
    g.gain.value = base;
    if (ev.kind === 'audio') fades(g.gain, ev, when, base);
    src.connect(g);
    g.connect(out);
    return { src, g };
  }

  // ── the engine ──────────────────────────────────────────────────────
  // Desk.play(plan, {ontick, onended}) — the handle rides one stretch.
  function play(plan, opts) {
    opts = opts || {};
    ensure();
    const events = plan.events || [];
    const total = +plan.total || 0;
    const t0 = ctx.currentTime + 0.15;
    const live = new Set();            // started sources, so stop() can
    let next = 0;                      // events arrive in start order
    let over = false, dead = false, timer = null;

    function arm(ev) {
      const when = t0 + ev.at;
      if (ev.kind === 'chime') {
        // no channel, no profile — straight onto the Master bus, exactly
        // where mixdown puts its own bell
        start(wire(ctx, chimeBuf(ctx, ev.dur), ev, when, masterNode), when);
        return;
      }
      bufferOf(ev.url)
        .then(buf => {
          if (dead) return;
          start(wire(ctx, buf, ev, when, chNode(ev.chan || 'main')), when);
        })
        .catch(() => {});              // a hole in the tape, not a crash
    }
    function start(w, when) {
      const now = ctx.currentTime;
      if (when >= now) w.src.start(when);
      else if (w.src.buffer.duration > now - when) {
        w.src.start(now, now - when);  // the decode landed late: join partway
      } else return;                   // its moment has wholly passed
      live.add(w.src);
      w.src.onended = () => live.delete(w.src);
    }
    function tick() {
      if (dead) return;
      const t = ctx.currentTime - t0;  // frozen under suspend, as it should be
      while (next < events.length && events[next].at < t + AHEAD) arm(events[next++]);
      if (opts.ontick) opts.ontick(Math.max(0, Math.min(t, total)));
      if (!over && t >= total) {       // total covers every tail and rest
        over = true;
        clearInterval(timer); timer = null;
        if (opts.onended) opts.onended();
      }
    }
    timer = setInterval(tick, 250);
    // the first arm is a breath away, never synchronous: callers hold the
    // handle in a const their callbacks close over, and a tick fired inside
    // play() would reach it still in the temporal dead zone
    setTimeout(tick, 0);
    return {
      total,
      pause() { if (!dead && !over) ctx.suspend(); },
      resume() { if (!dead && !over) ctx.resume(); },
      stop() {
        if (dead) return;
        dead = true;
        clearInterval(timer); timer = null;
        live.forEach(s => { try { s.stop(); } catch (e) {} });
        live.clear();
        // a ride stopped mid-pause leaves the context suspended; wake it,
        // or the next ride's clock would never move
        if (ctx.state === 'suspended') ctx.resume();
      },
      time() { return Math.max(0, Math.min(ctx.currentTime - t0, total)); },
      get playing() { return !dead && !over && ctx.state === 'running'; },
      get ended() { return over; },
      get stopped() { return dead; },
    };
  }

  // ── the agreement test's door ───────────────────────────────────────
  // Render a plan through an OfflineAudioContext at the server's rate, with
  // the desk arithmetic and doc-shaped faders, and hand back the samples.
  // Same wire(), same lin() — the test proves the graph, not a copy of it.
  async function renderOffline(plan, channels, master, sr) {
    const total = +plan.total || 0;
    const oc = new OfflineAudioContext(1, Math.max(1, Math.ceil(total * sr)), sr);
    const gains = {};
    (channels || []).forEach(ch => { if (ch && ch.id) gains[String(ch.id)] = lin(ch); });
    const m = master || {};
    const mg = oc.createGain();
    mg.gain.value = m.mute ? 0
      : Math.max(0, Math.min(200, m.gain == null ? 100 : +m.gain)) / 100;
    mg.connect(oc.destination);
    const ch = {};
    const chOf = id => {
      if (id !== 'main' && !(id in gains)) id = 'main';
      if (!ch[id]) {
        ch[id] = oc.createGain();
        ch[id].gain.value = id in gains ? gains[id] : 1;
        ch[id].connect(mg);
      }
      return ch[id];
    };
    for (const ev of plan.events || []) {
      if (ev.kind === 'chime') {
        wire(oc, chimeBuf(oc, ev.dur), ev, ev.at, mg).src.start(ev.at);
        continue;
      }
      const ab = await fetch(ev.url).then(r => r.arrayBuffer());
      const buf = await oc.decodeAudioData(ab);
      wire(oc, buf, ev, ev.at, chOf(ev.chan || 'main')).src.start(ev.at);
    }
    const out = await oc.startRendering();
    return out.getChannelData(0);
  }

  return { ensure, resume, setMix, setMonitor, play, renderOffline,
           running: () => !!ctx && ctx.state === 'running' };
})();
