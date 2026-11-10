/* mediacast web portal — vanilla JS, talks only to /api/v1 (see docs/api-v1.md). */
(() => {
  'use strict';

  const CFG = Object.assign({ token: '', pairUrls: [], version: 'dev' }, window.MEDIACAST || {});
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- helpers

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function fmtTime(s) {
    if (s == null || !isFinite(s) || s < 0) s = 0;
    s = Math.floor(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (x) => String(x).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  }

  function fmtRuntime(s) {
    if (!s) return '';
    const m = Math.round(s / 60);
    return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
  }

  function relTime(iso) {
    const t = Date.parse(iso);
    if (!isFinite(t)) return '';
    const d = (Date.now() - t) / 1000;
    if (d < 60) return 'just now';
    const units = [[60, 'min'], [24, 'h'], [7, 'd'], [4.35, 'w'], [12, 'mo'], [Infinity, 'y']];
    let v = d / 60;
    for (let i = 0; i < units.length; i++) {
      const [div, name] = units[i];
      if (v < div) return `${Math.floor(v)}${name === 'min' ? ' min' : name} ago`;
      v /= div;
    }
    return '';
  }

  function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
  };

  // ---------------------------------------------------------------- toast

  let toastTimer;
  function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.toggle('err', !!isErr);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 5000 : 2500);
  }

  // ---------------------------------------------------------------- API

  class ApiError extends Error {
    constructor(msg, status, code) { super(msg); this.status = status; this.code = code; }
  }

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (CFG.token) h.Authorization = `Bearer ${CFG.token}`;
    return h;
  }

  async function api(method, path, body) {
    let res;
    try {
      res = await fetch(`/api/v1${path}`, {
        method,
        headers: authHeaders(body !== undefined ? { 'Content-Type': 'application/json' } : null),
        body: body !== undefined ? JSON.stringify(body) : undefined,
        cache: 'no-store',
      });
    } catch (e) {
      throw new ApiError('Server unreachable', 0, 'network');
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok) {
      const msg = (data && typeof data.error === 'string' && data.error) || `${res.status} ${res.statusText || 'error'}`;
      throw new ApiError(msg, res.status, data && data.code);
    }
    return data || {};
  }

  // Fire an action and report the outcome in the toast.
  async function act(method, path, body, okMsg) {
    try {
      await api(method, path, body);
      if (okMsg) toast(okMsg);
      return true;
    } catch (e) {
      toast(e.message, true);
      return false;
    }
  }

  const control = (action, value) => act('POST', '/control', value === undefined ? { action } : { action, value });

  // ---------------------------------------------------------------- state

  let state = null;
  let stateAt = 0;          // local ms when `state` arrived
  let seeking = false;      // user is dragging the progress bar
  let volTouchedAt = 0;     // ignore server volume briefly after local changes
  let lastThemesKey = '';

  function castPosition() {
    if (!state || state.position_s == null) return null;
    let p = state.position_s;
    if (state.activity === 'playing') p += (Date.now() - stateAt) / 1000;
    if (state.duration_s) p = Math.min(p, state.duration_s);
    return p;
  }

  const ACTIVITY = { idle: 'Idle', playing: 'Playing', paused: 'Paused', screensaver: 'Screensaver' };
  const SOURCE = { youtube: 'YouTube', jellyfin: 'mrpflix', url: 'Link', screensaver: 'Screensaver' };

  function applyState(s) {
    if (!s || typeof s !== 'object') return;
    state = s;
    stateAt = Date.now();
    render();
  }

  function render() {
    const s = state;
    if (!s) return;

    // Power banner
    const asleep = s.power === 'sleeping', screenOff = s.screen === 'off';
    $('power-banner').classList.toggle('hidden', !(asleep || screenOff));
    $('power-text').textContent = asleep ? 'Projector is asleep' : 'Screen is off';

    renderNight();

    // Now playing
    const idle = s.activity === 'idle' || !s.activity;
    $('now-activity').textContent = ACTIVITY[s.activity] || s.activity || '';
    const src = $('now-source');
    src.hidden = !s.source;
    src.textContent = SOURCE[s.source] || s.source || '';
    $('now-title').textContent = s.title || (idle ? 'Nothing playing' : (ACTIVITY[s.activity] || ''));
    $('now-sub').textContent = s.subtitle || '';
    const art = $('now-art');
    if (s.artwork_url) {
      if (art.getAttribute('src') !== s.artwork_url) art.src = s.artwork_url;
      art.hidden = false;
    } else {
      art.hidden = true;
      art.removeAttribute('src');
    }
    $('now-art-wrap').classList.toggle('has-img', !!s.artwork_url);

    const showTransport = !idle && s.activity !== 'screensaver';
    $('transport').hidden = !showTransport;
    const tb = $('toggle-btn');
    tb.textContent = s.activity === 'playing' ? '❚❚' : '▶';
    tb.setAttribute('aria-label', s.activity === 'playing' ? 'Pause' : 'Play');
    $('progress').parentElement.hidden = !s.duration_s;
    renderProgress();

    // Volume
    if (Date.now() - volTouchedAt > 1500 && typeof s.volume === 'number') {
      $('vol').value = s.volume;
      $('vol-val').textContent = s.muted ? 'muted' : `${Math.round(s.volume)}%`;
    }

    renderTracks();
    renderSpotify();
    renderScreensaver();
    syncMediaSession();
  }

  function renderProgress(overridePos) {
    if (!state) return;
    const dur = state.duration_s || 0;
    const pos = overridePos != null ? overridePos : (castPosition() || 0);
    const pct = dur ? Math.max(0, Math.min(100, (pos / dur) * 100)) : 0;
    $('progress-fill').style.width = `${pct}%`;
    $('progress-knob').style.left = `${pct}%`;
    $('pos').textContent = fmtTime(pos);
    $('dur').textContent = fmtTime(dur);
    const p = $('progress');
    p.setAttribute('aria-valuemax', String(Math.round(dur)));
    p.setAttribute('aria-valuenow', String(Math.round(pos)));
    p.setAttribute('aria-valuetext', `${fmtTime(pos)} of ${fmtTime(dur)}`);
  }

  function spotifyPosition(sp) {
    if (sp.position_s == null) return null;
    let p = sp.position_s;
    if (sp.playing && sp.updated_at) p += Math.max(0, (Date.now() - sp.updated_at) / 1000);
    if (sp.duration_s) p = Math.min(p, sp.duration_s);
    return p;
  }

  function renderSpotify() {
    const sp = state && state.spotify;
    const show = !!(sp && (sp.active || sp.title));
    $('spotify').classList.toggle('hidden', !show);
    if (!show) return;
    $('sp-title').textContent = sp.title || 'Spotify';
    $('sp-sub').textContent = [sp.artist, sp.album].filter(Boolean).join(' · ');
    $('sp-state').textContent = sp.playing ? 'Playing' : (sp.active ? 'Paused' : 'Inactive');
    const ctl = !!sp.controllable;
    $('spotify').classList.toggle('controllable', ctl);
    $('sp-controls').hidden = !ctl;
    $('sp-hint').hidden = ctl;
    $('sp-knob').hidden = !ctl;
    const bar = $('sp-progress');
    bar.classList.toggle('seekable', ctl);
    if (ctl) { bar.setAttribute('role', 'slider'); bar.tabIndex = 0; } else { bar.removeAttribute('role'); bar.removeAttribute('tabindex'); }
    const tg = $('sp-toggle');
    tg.textContent = sp.playing ? '❚❚' : '▶';
    tg.setAttribute('aria-label', sp.playing ? 'Pause' : 'Play');
    if (Date.now() - spVolTouchedAt > 1500) {
      const known = typeof sp.volume === 'number';
      if (known) $('sp-vol').value = sp.volume;
      $('sp-vol-val').textContent = known ? `${Math.round(sp.volume)}%` : '—';
    }
    const img = $('sp-art');
    if (sp.artwork_url) {
      if (img.getAttribute('src') !== sp.artwork_url) img.src = sp.artwork_url;
      img.hidden = false;
    } else {
      img.hidden = true;
    }
    tickSpotify();
  }

  function tickSpotify(overridePos) {
    const sp = state && state.spotify;
    if (!sp) return;
    const pos = overridePos != null ? overridePos : spotifyPosition(sp);
    const dur = sp.duration_s || 0;
    const pct = dur && pos != null ? `${Math.max(0, Math.min(100, (pos / dur) * 100))}%` : '0%';
    $('sp-fill').style.width = pct;
    $('sp-knob').style.left = pct;
    $('sp-time').textContent = dur ? `${fmtTime(pos)} / ${fmtTime(dur)}` : '';
    const bar = $('sp-progress');
    bar.setAttribute('aria-valuemax', String(Math.round(dur)));
    bar.setAttribute('aria-valuenow', String(Math.round(pos || 0)));
  }

  // Spotify (librespot) transport — only shown when state.spotify.controllable.
  let spVolTouchedAt = 0;
  let spSeeking = false;
  const spControl = (action, value) =>
    act('POST', '/spotify/control', value === undefined ? { action } : { action, value });
  $('sp-prev').addEventListener('click', () => spControl('previous'));
  $('sp-next').addEventListener('click', () => spControl('next'));
  $('sp-toggle').addEventListener('click', () => spControl('toggle'));
  (() => {
    const bar = $('sp-progress');
    let pendingPos = null;
    const dur = () => (state && state.spotify && state.spotify.duration_s) || 0;
    const posFromEvent = (e) => {
      const r = bar.getBoundingClientRect();
      return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur();
    };
    bar.addEventListener('pointerdown', (e) => {
      const sp = state && state.spotify;
      if (!sp || !sp.controllable || !dur()) return;
      spSeeking = true;
      bar.setPointerCapture(e.pointerId);
      pendingPos = posFromEvent(e);
      tickSpotify(pendingPos);
    });
    bar.addEventListener('pointermove', (e) => {
      if (!spSeeking) return;
      pendingPos = posFromEvent(e);
      tickSpotify(pendingPos);
    });
    const finish = (commit) => {
      if (!spSeeking) return;
      spSeeking = false;
      if (commit && pendingPos != null) {
        const sp = state.spotify;   // optimistic until librespot's `seeked` event
        sp.position_s = pendingPos; sp.updated_at = Date.now();
        spControl('seek_to', Math.round(pendingPos));
      }
      pendingPos = null;
      tickSpotify();
    };
    bar.addEventListener('pointerup', () => finish(true));
    bar.addEventListener('pointercancel', () => finish(false));
    bar.addEventListener('keydown', (e) => {
      if (!(state && state.spotify && state.spotify.controllable)) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); spControl('seek', -10); }
      if (e.key === 'ArrowRight') { e.preventDefault(); spControl('seek', 10); }
    });
    const vol = $('sp-vol');
    const send = debounce((v) => spControl('volume', v), 250);
    vol.addEventListener('input', () => {
      spVolTouchedAt = Date.now();
      $('sp-vol-val').textContent = `${vol.value}%`;
      send(Number(vol.value));
    });
  })();

  // ---------------------------------------------------------------- night deep sleep

  // `state.night` is null/absent on older hosts; tolerate partial objects too.
  function nightInfo() {
    const n = state && state.night;
    return n && typeof n === 'object' ? n : null;
  }

  function fmtClock(ms) {
    const d = new Date(ms);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  function renderNight() {
    const n = nightInfo();
    const chip = $('night-chip');
    if (n && n.enabled) {
      let t = `🌙 Deep sleep ${n.start || '?'}–${n.end || '?'}`;
      if (n.pending && n.in_window) {
        t += ' · pending';
        if (typeof n.next_check_at === 'number' && n.next_check_at > Date.now()) t += ` · next check ${fmtClock(n.next_check_at)}`;
      }
      chip.textContent = t;
      chip.hidden = false;
    } else {
      chip.hidden = true;
    }
    renderBedtime();
  }

  // Countdown banner; ticked locally every second while it runs.
  function renderBedtime() {
    const n = nightInfo();
    const until = n && typeof n.countdown_until === 'number' ? n.countdown_until : 0;
    const left = Math.ceil((until - Date.now()) / 1000);
    const on = left > 0;
    $('bedtime-banner').classList.toggle('hidden', !on);
    if (on) $('bedtime-text').textContent = `🌙 Going to sleep in ${left}s`;
  }
  setInterval(() => { if (state) renderBedtime(); }, 1000);

  $('bedtime-cancel').addEventListener('click', () => act('POST', '/power', { action: 'bedtime_cancel' }, 'Bedtime postponed'));

  // Local tick between pushes (no polling — just re-render the clock).
  setInterval(() => {
    if (document.hidden || !state) return;
    if (state.activity === 'playing' && !seeking) renderProgress();
    if (state.spotify && state.spotify.playing && !spSeeking) tickSpotify();
  }, 500);

  // ---------------------------------------------------------------- transport

  document.querySelectorAll('[data-seek]').forEach((b) => {
    b.addEventListener('click', () => control('seek', Number(b.dataset.seek)));
  });
  $('toggle-btn').addEventListener('click', () => control('toggle'));
  $('stop-btn').addEventListener('click', () => control('stop'));

  // Seek bar: tap or drag, commit on release.
  (() => {
    const bar = $('progress');
    let pendingPos = null;
    const posFromEvent = (e) => {
      const r = bar.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
      return f * ((state && state.duration_s) || 0);
    };
    bar.addEventListener('pointerdown', (e) => {
      if (!state || !state.duration_s) return;
      seeking = true;
      bar.setPointerCapture(e.pointerId);
      pendingPos = posFromEvent(e);
      renderProgress(pendingPos);
    });
    bar.addEventListener('pointermove', (e) => {
      if (!seeking) return;
      pendingPos = posFromEvent(e);
      renderProgress(pendingPos);
    });
    const finish = (commit) => {
      if (!seeking) return;
      seeking = false;
      if (commit && pendingPos != null) {
        // Optimistic: pretend the server is already there.
        if (state) { state.position_s = pendingPos; stateAt = Date.now(); }
        control('seek_to', Math.round(pendingPos));
      }
      pendingPos = null;
      renderProgress();
    };
    bar.addEventListener('pointerup', () => finish(true));
    bar.addEventListener('pointercancel', () => finish(false));
    bar.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft') { e.preventDefault(); control('seek', -10); }
      if (e.key === 'ArrowRight') { e.preventDefault(); control('seek', 10); }
    });
  })();

  // Volume
  (() => {
    const vol = $('vol');
    const send = debounce((v) => control('volume', v), 250);
    vol.addEventListener('input', () => {
      volTouchedAt = Date.now();
      $('vol-val').textContent = `${vol.value}%`;
      send(Number(vol.value));
    });
    const step = (d) => {
      volTouchedAt = Date.now();
      const v = Math.max(0, Math.min(100, Number(vol.value) + d));
      vol.value = v;
      $('vol-val').textContent = `${v}%`;
      control('volume_delta', d);
    };
    $('vol-down').addEventListener('click', () => step(-5));
    $('vol-up').addEventListener('click', () => step(5));
  })();

  // ---------------------------------------------------------------- phone media notification
  //
  // Media Session for what is casting on the projector (YouTube / mrpflix / link — never Spotify,
  // whose own app already has a Connect notification). Limitations:
  // - Chrome on Android only shows the notification while this page itself plays media of >= 5 s,
  //   so we loop a generated 10 s WAV of digital silence at normal volume (a muted or volume-0
  //   element doesn't count). It has to start from a tap (autoplay policy); after the first tap on
  //   the page later starts are allowed, so it then follows the cast by itself.
  // - Media Session isn't [SecureContext]-gated, so this works on plain http://192.168.0.22.
  // - Such media takes full Android audio focus: starting it pauses whatever the phone itself is
  //   playing (e.g. Spotify playing locally). We only hold it while a cast is actually playing,
  //   release it on pause/stop, and once the system pauses it (another app took focus) we don't
  //   take focus back until the user touches a cast control. The toggle switches it off entirely.
  // - The page must stay open in a tab; closing it or Android killing the browser ends it.
  // - No queue on the projector, so there are no previous/next track buttons.

  const MS = 'mediaSession' in navigator;
  let msAudio = null;
  let msActivated = false;   // the page has had a user gesture, so play() is allowed
  let msYielded = false;     // another app took audio focus; wait for a cast-control tap
  let msOurPause = false;    // the element pause in flight is ours, not the system's
  let msMetaKey = '';
  let msHandlersOn = false;
  let msEnabled = store.get('ms_notify', true);

  function silentWavUrl() {
    // 16-bit mono PCM (the most widely decodable WAV) carrying +-1 LSB of noise (~ -90 dBFS,
    // inaudible) rather than digital zero, so no browser can treat it as a silent player.
    const rate = 22050, secs = 10, n = rate * secs;
    const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE');
    str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, 'data'); v.setUint32(40, n * 2, true);
    const pcm = new Int16Array(buf, 44, n);
    for (let i = 0; i < n; i++) pcm[i] = (i & 1) ? 1 : -1;
    return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  }

  // Visible reason next to the toggle: browsers fail this silently, so say what happened.
  let msStatusText = '';
  function msStatus(t) {
    if (t === msStatusText) return;
    msStatusText = t;
    const el = $('ms-status');
    if (el) el.textContent = t ? `· ${t}` : '';
  }

  function castActive() {
    const s = state;
    return !!(s && (s.activity === 'playing' || s.activity === 'paused') && s.source && s.source !== 'screensaver');
  }

  function msPlay() {
    if (!msAudio) {
      msAudio = new Audio();
      msAudio.loop = true;
      msAudio.preload = 'auto';
      msAudio.addEventListener('pause', () => {
        // Paused by the system (audio focus lost to another app) rather than by us.
        if (!msOurPause && castActive()) msYielded = true;
        msOurPause = false;
      });
      msAudio.addEventListener('playing', () => msStatus('active'));
      msAudio.addEventListener('error', () => {
        const e = msAudio.error;
        if (msAudio.getAttribute('src')) msStatus(`audio error ${e ? e.code : '?'}${e && e.message ? ': ' + e.message : ''}`);
      });
    }
    if (!msAudio.getAttribute('src')) msAudio.src = silentWavUrl();
    if (!msAudio.paused) return;
    msAudio.play().catch((e) => {   // NotAllowedError until the next tap
      msStatus(e && e.name === 'NotAllowedError' ? 'blocked by browser - tap the page' : `can't play: ${e && e.name}`);
    });
  }

  function msPause(release) {
    if (!msAudio) return;
    if (!msAudio.paused) { msOurPause = true; msAudio.pause(); }
    if (release && msAudio.getAttribute('src')) {
      const u = msAudio.src;
      msAudio.removeAttribute('src');
      msAudio.load();               // drop the player so Chrome removes the notification
      URL.revokeObjectURL(u);
    }
  }

  function msSetHandlers(on) {
    if (on === msHandlersOn) return;
    msHandlersOn = on;
    const optimistic = (pos) => { if (state && pos != null) { state.position_s = pos; stateAt = Date.now(); } };
    const h = {
      play: () => { msYielded = false; msPlay(); control('play'); },
      pause: () => { control('pause'); },
      stop: () => { control('stop'); },
      seekbackward: (d) => { const o = (d && d.seekOffset) || 10; optimistic(Math.max(0, (castPosition() || 0) - o)); control('seek', -o); },
      seekforward: (d) => { const o = (d && d.seekOffset) || 10; optimistic((castPosition() || 0) + o); control('seek', o); },
      seekto: (d) => {
        if (!d || d.seekTime == null) return;
        optimistic(d.seekTime);
        control('seek_to', Math.round(d.seekTime));
      },
      previoustrack: null,
      nexttrack: null,
    };
    Object.keys(h).forEach((k) => {
      try { navigator.mediaSession.setActionHandler(k, on ? h[k] : null); } catch (_) { /* unsupported action */ }
    });
  }

  function syncMediaSession() {
    if (!MS) return;
    const active = castActive();
    $('ms-opt').hidden = !active;
    const ms = navigator.mediaSession;
    if (!active || !msEnabled) {
      msYielded = false;
      msPause(true);
      msSetHandlers(false);
      if (msMetaKey) { ms.metadata = null; msMetaKey = ''; }
      ms.playbackState = 'none';
      msStatus(msEnabled ? '' : 'off');
      return;
    }
    const s = state;
    const art = s.artwork_url ? new URL(s.artwork_url, location.href).href : new URL('/static/icon-512.png', location.href).href;
    const key = JSON.stringify([s.title, s.subtitle, art, s.source]);
    if (key !== msMetaKey && 'MediaMetadata' in window) {
      msMetaKey = key;
      ms.metadata = new MediaMetadata({
        title: s.title || SOURCE[s.source] || 'Projector',
        artist: s.subtitle || '',
        album: `Projector · ${SOURCE[s.source] || s.source}`,
        artwork: [{ src: art }],
      });
    }
    msSetHandlers(true);
    const playing = s.activity === 'playing';
    ms.playbackState = playing ? 'playing' : 'paused';
    if (s.duration_s > 0 && ms.setPositionState) {
      try {
        ms.setPositionState({ duration: s.duration_s, position: Math.max(0, Math.min(castPosition() || 0, s.duration_s)), playbackRate: 1 });
      } catch (_) { /* inconsistent values */ }
    }
    if (playing && msActivated && !msYielded) msPlay();
    else if (!playing) { msPause(false); msStatus('paused'); }
    if (playing && !msActivated) msStatus('tap the page to enable');
    else if (playing && msYielded) msStatus('paused by another app - tap a cast control');
  }

  if (MS) {
    const tg = $('ms-toggle');
    tg.checked = msEnabled;
    tg.addEventListener('change', () => {
      msEnabled = tg.checked;
      store.set('ms_notify', msEnabled);
      msYielded = false;
      syncMediaSession();
    });
    // Gestures that may start the silent track. Touching a cast control also reclaims audio
    // focus after another app took it; a tap elsewhere on the page doesn't.
    const CAST_UI = '#now, #cast-form, #yt-grid, #jf-grid, #sheet';
    const onGesture = (e) => {
      msActivated = true;
      if (e.target && e.target.closest && e.target.closest(CAST_UI)) msYielded = false;
      syncMediaSession();
    };
    ['click', 'keydown', 'touchend'].forEach((t) => document.addEventListener(t, onGesture, true));
  }

  // ---------------------------------------------------------------- power

  const POWER_MSG = { sleep: 'Going to sleep', wake: 'Waking up', screen_off: 'Screen off', deep_sleep: 'Server going to deep sleep', poweroff: 'Server shutting down' };
  document.querySelectorAll('[data-power]').forEach((b) => {
    b.addEventListener('click', () => {
      const action = b.dataset.power;
      if (action === 'poweroff' && !confirm(
        'Power off the WHOLE SERVER?\n\nThis shuts down the machine, not just the projector: ' +
        'mrpflix, Home Assistant, music and this page all go down. ' +
        'Only pressing the physical power button brings it back.')) return;
      if (action === 'deep_sleep') {
        const n = nightInfo();
        if (!confirm('Put the server into deep sleep? Spotify and this app will be unreachable until you move the mouse, ' +
          `press the power button, or ${(n && n.end) || '08:00'}.`)) return;
      }
      act('POST', '/power', { action }, POWER_MSG[action]);
    });
  });
  $('wake-btn').addEventListener('click', () => act('POST', '/power', { action: 'wake' }, 'Waking up'));

  // ---------------------------------------------------------------- screensaver

  function renderScreensaver() {
    const ss = state && state.screensaver;
    const box = $('ss-themes');
    if (!ss) return;
    const themes = Array.isArray(ss.themes) ? ss.themes : [];
    const key = JSON.stringify(themes);
    if (key !== lastThemesKey) {
      lastThemesKey = key;
      box.replaceChildren();
      themes.forEach((t) => {
        const b = el('button', 'btn', t.name || t.id);
        b.type = 'button';
        b.dataset.theme = t.id;
        b.addEventListener('click', () => act('POST', '/screensaver', { theme: t.id }, `Screensaver: ${t.name || t.id}`));
        box.appendChild(b);
      });
      const off = el('button', 'btn', 'Off');
      off.type = 'button';
      off.dataset.theme = '';
      off.addEventListener('click', () => act('POST', '/screensaver', { theme: null }, 'Screensaver off'));
      box.appendChild(off);
    }
    box.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.theme ? (ss.active && ss.theme === b.dataset.theme) : !ss.active;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', String(on));
    });
    if (ss.idle_minutes) {
      $('ss-note').textContent = `Starts on its own after ${ss.idle_minutes} min with nothing playing, or pick one now.`;
    }
  }

  // ---------------------------------------------------------------- live events (SSE over fetch)

  let esAbort = null;
  let esBackoff = 1000;
  let esTimer = null;
  let esConnected = false;

  function setConn(live) {
    esConnected = live;
    const c = $('conn');
    c.classList.toggle('live', live);
    c.textContent = live ? 'live' : 'offline';
  }

  async function refreshState() {
    try { applyState(await api('GET', '/state')); } catch (e) {
      if (e.status === 401) toast('Not authorised — reload the page from the server', true);
    }
  }

  function scheduleReconnect() {
    clearTimeout(esTimer);
    esTimer = setTimeout(connectEvents, esBackoff);
    esBackoff = Math.min(esBackoff * 2, 15000);
  }

  function handleFrame(frame) {
    let event = 'message';
    const data = [];
    frame.split(/\r?\n/).forEach((line) => {
      if (!line || line[0] === ':') return;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      let val = i < 0 ? '' : line.slice(i + 1);
      if (val[0] === ' ') val = val.slice(1);
      if (field === 'event') event = val;
      else if (field === 'data') data.push(val);
    });
    if (!data.length) return;
    if (event === 'state') {
      try { applyState(JSON.parse(data.join('\n'))); } catch (_) { /* malformed */ }
    } else if (event === 'feed') {
      try { ytOnFeedEvent(JSON.parse(data.join('\n'))); } catch (_) { /* malformed */ }
    }
  }

  async function connectEvents() {
    clearTimeout(esTimer);
    if (esAbort) esAbort.abort();
    const ctrl = new AbortController();
    esAbort = ctrl;
    try {
      const res = await fetch('/api/v1/events', {
        headers: authHeaders({ Accept: 'text/event-stream' }),
        cache: 'no-store',
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) {
        if (res.status === 401) esBackoff = 15000;
        throw new Error(`events ${res.status}`);
      }
      setConn(true);
      esBackoff = 1000;
      refreshState();
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let m;
        while ((m = /\r?\n\r?\n/.exec(buf))) {
          const frame = buf.slice(0, m.index);
          buf = buf.slice(m.index + m[0].length);
          handleFrame(frame);
        }
      }
      throw new Error('stream ended');
    } catch (_) {
      if (esAbort !== ctrl) return; // superseded by a newer connection
      setConn(false);
      scheduleReconnect();
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    refreshState();
    if (!esConnected) { esBackoff = 1000; connectEvents(); }
  });
  window.addEventListener('online', () => { if (!esConnected) { esBackoff = 1000; connectEvents(); } });

  // ---------------------------------------------------------------- cast form

  (() => {
    const input = $('cast-url');
    const q = new URLSearchParams(location.search);
    let pre = q.get('url') || '';
    if (!pre) {
      const m = /https?:\/\/\S+/.exec(q.get('text') || '');
      if (m) pre = m[0];
    }
    if (pre) {
      input.value = pre;
      try { history.replaceState(null, '', location.pathname); } catch (_) { /* ignore */ }
      setTimeout(() => input.focus(), 0);
    }

    async function cast(url) {
      url = (url || '').trim();
      if (!url) return;
      if (await act('POST', '/cast', { url }, 'Casting…')) input.value = '';
    }

    $('cast-form').addEventListener('submit', (e) => { e.preventDefault(); cast(input.value); });
    $('paste-btn').addEventListener('click', async () => {
      try {
        const txt = (await navigator.clipboard.readText()).trim();
        if (!txt) { toast('Clipboard is empty', true); return; }
        const m = /https?:\/\/\S+/.exec(txt);
        input.value = m ? m[0] : txt;
        cast(input.value);
      } catch (_) {
        toast('Clipboard not accessible here — long-press the field to paste', true);
        input.focus();
      }
    });
  })();

  // ---------------------------------------------------------------- tiles

  function thumb(src, landscape) {
    const box = el('div', landscape ? 'thumb landscape' : 'thumb');
    if (src) {
      const img = el('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.src = src;
      img.addEventListener('error', () => { img.remove(); box.classList.add('noimg'); }, { once: true });
      box.appendChild(img);
    } else {
      box.classList.add('noimg');
    }
    box.setAttribute('aria-hidden', 'true');
    return box;
  }

  function tile(thumbEl, title, sub, onClick) {
    const b = el('button', 'tile');
    b.type = 'button';
    b.appendChild(thumbEl);
    b.appendChild(el('div', 'title', title));
    if (sub) b.appendChild(el('div', 'sub', sub));
    b.addEventListener('click', onClick);
    return b;
  }

  // ---------------------------------------------------------------- YouTube

  const yt = {
    items: [], shown: 0, page: 24, hideShorts: !!store.get('mc.yt.hideShorts', false), loading: false,
    changedAt: 0,      // feed `changed_at` we hold; SSE `event: feed` past it → silent refetch
    pendingAt: 0,      // a feed event that arrived mid-load
    fresh: new Set(),  // ids that arrived via a silent refetch, not yet seen
  };

  function ytVisible() { return yt.items.filter((v) => !(yt.hideShorts && v.is_short)); }

  function ytRender(reset) {
    const grid = $('yt-grid');
    const list = ytVisible();
    if (reset) { grid.replaceChildren(); yt.shown = 0; }
    const next = list.slice(yt.shown, yt.shown + yt.page);
    next.forEach((v) => {
      const th = thumb(v.thumbnail, true);
      if (v.is_short) th.appendChild(el('span', 'pill short', 'Shorts'));
      if (v.duration_s) th.appendChild(el('span', 'pill dur', fmtTime(v.duration_s)));
      const sub = [v.channel, relTime(v.published)].filter(Boolean).join(' · ');
      const t = tile(th, v.title || v.url, sub, () => act('POST', '/cast', { url: v.url }, `Casting: ${v.title || v.url}`));
      t.dataset.vid = v.id;
      grid.appendChild(t);
    });
    yt.shown += next.length;
    $('yt-more').hidden = yt.shown >= list.length;
    const empty = $('yt-empty');
    empty.hidden = list.length > 0;
    if (!list.length) empty.textContent = yt.items.length ? 'Only Shorts in the feed.' : 'No videos.';
  }

  // First tile at/below the top bar, and its viewport offset — the anchor
  // a silent refetch restores so new videos landing above don't shove the
  // page.
  function ytAnchor() {
    const topBar = document.querySelector('.top');
    const minY = topBar ? topBar.getBoundingClientRect().bottom : 0;
    for (const t of $('yt-grid').children) {
      const r = t.getBoundingClientRect();
      if (r.bottom > minY) return r.top > window.innerHeight ? null : { vid: t.dataset.vid, top: r.top };
    }
    return null;
  }

  function ytNewPill() {
    let pill = $('yt-new');
    const n = yt.fresh.size;
    const gridTop = $('yt-grid').getBoundingClientRect().top;
    // Only worth a pill when the newest tiles are scrolled out of view.
    if (!n || gridTop > 0) {
      yt.fresh.clear();
      if (pill) pill.hidden = true;
      return;
    }
    if (!pill) {
      pill = el('button', 'new-pill');
      pill.id = 'yt-new';
      pill.type = 'button';
      pill.addEventListener('click', () => {
        yt.fresh.clear();
        pill.hidden = true;
        $('yt').scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      document.body.appendChild(pill);
    }
    pill.textContent = `↑ ${n} new video${n === 1 ? '' : 's'}`;
    pill.hidden = false;
  }
  window.addEventListener('scroll', () => { if (yt.fresh.size) ytNewPill(); }, { passive: true });

  function ytOnFeedEvent(ev) {
    const at = Number(ev && ev.updated_at) || 0;
    if (!at || at <= yt.changedAt || $('yt').classList.contains('hidden')) return;
    if (yt.loading) { yt.pendingAt = Math.max(yt.pendingAt, at); return; }
    ytLoad(false, true);
  }

  async function ytLoad(refresh, silent) {
    if (yt.loading) return;
    yt.loading = true;
    yt.pendingAt = 0;
    const btn = $('yt-refresh');
    if (!silent) {
      btn.disabled = true;
      btn.textContent = refresh ? 'Refreshing…' : 'Loading…';
    }
    try {
      const data = await api('GET', `/youtube/feed?refresh=${refresh ? 1 : 0}&shorts=1`);
      $('yt').classList.remove('hidden');
      const items = Array.isArray(data.items) ? data.items : [];
      yt.changedAt = Math.max(yt.changedAt, Number(data.changed_at) || 0);
      if (silent && yt.items.length) {
        const before = new Set(yt.items.map((v) => v.id));
        const anchor = ytAnchor();
        const shown = yt.shown;
        yt.items = items;
        items.forEach((v) => { if (!before.has(v.id)) yt.fresh.add(v.id); });
        for (const id of [...yt.fresh]) if (!items.some((v) => v.id === id)) yt.fresh.delete(id);
        ytRender(true);
        while (yt.shown < shown && yt.shown < ytVisible().length) ytRender(false);
        const t = anchor && $('yt-grid').querySelector(`[data-vid="${CSS.escape(anchor.vid)}"]`);
        if (t) window.scrollBy(0, t.getBoundingClientRect().top - anchor.top);
        ytNewPill();
      } else {
        yt.items = items;
        yt.fresh.clear();
        ytRender(true);
        ytNewPill();
      }
      if (refresh) toast('Feed refreshed');
    } catch (e) {
      if (silent) return;
      if (e.status === 404) { $('yt').classList.add('hidden'); return; }
      $('yt').classList.remove('hidden');
      if (!yt.items.length) {
        $('yt-grid').replaceChildren();
        $('yt-empty').hidden = false;
        $('yt-empty').textContent = `Couldn't load feed: ${e.message}`;
      } else {
        toast(e.message, true);
      }
    } finally {
      yt.loading = false;
      if (!silent) {
        btn.disabled = false;
        btn.textContent = 'Refresh';
      }
      if (yt.pendingAt > yt.changedAt) ytLoad(false, true);
    }
  }

  $('yt-hide-shorts').checked = yt.hideShorts;
  $('yt-hide-shorts').addEventListener('change', (e) => {
    yt.hideShorts = e.target.checked;
    store.set('mc.yt.hideShorts', yt.hideShorts);
    ytRender(true);
  });
  $('yt-refresh').addEventListener('click', () => ytLoad(true));
  $('yt-more').addEventListener('click', () => ytRender(false));

  // ---------------------------------------------------------------- Jellyfin

  const PLAYABLE = new Set(['Movie', 'Episode', 'Video', 'MusicVideo', 'Trailer']);
  const DRILL_ITEM = new Set(['Series', 'Season']);
  const JF_PAGE = 60;
  const TAB_TITLES = { resume: 'Continue watching', nextup: 'Next up', views: 'Libraries' };

  const jf = {
    tab: store.get('mc.jf.tab', 'resume'),
    stack: [],          // frames: {kind, title, parentId?, id?, q?, items, total, start}
    lowQ: !!store.get('mc.jf.lowq', false),
    seq: 0,             // guards against out-of-order responses
  };
  if (!TAB_TITLES[jf.tab]) jf.tab = 'resume';

  function epCode(it) {
    if (it.season == null && it.episode == null) return '';
    return `S${it.season ?? '?'}E${it.episode ?? '?'}`;
  }

  function jfSub(it) {
    switch (it.type) {
      case 'Episode': return [it.series_name, epCode(it)].filter(Boolean).join(' · ');
      case 'Series': return [it.year, it.child_count ? `${it.child_count} seasons` : ''].filter(Boolean).join(' · ');
      case 'Season': return it.child_count ? `${it.child_count} episodes` : (it.series_name || '');
      default: return it.year ? String(it.year) : '';
    }
  }

  function jfTile(it) {
    const th = thumb(it.image_url, it.type === 'Episode' || it.type === 'CollectionFolder' || it.type === 'UserView');
    if (it.played) th.appendChild(el('span', 'pill done', '✓'));
    else if (it.played_pct > 0) {
      const bar = el('div', 'bar');
      const fill = el('div');
      fill.style.width = `${Math.min(100, it.played_pct)}%`;
      bar.appendChild(fill);
      th.appendChild(bar);
    }
    return tile(th, it.name || '(untitled)', jfSub(it), () => jfOpen(it));
  }

  function jfCurrent() { return jf.stack[jf.stack.length - 1]; }

  function jfRenderFrame() {
    const f = jfCurrent();
    const grid = $('jf-grid');
    grid.replaceChildren();
    if (!f) return;
    // Tabs / crumbs
    document.querySelectorAll('#jf-tabs .chip').forEach((c) => {
      const on = c.dataset.tab === jf.tab && f.kind !== 'search';
      c.classList.toggle('active', on);
      c.setAttribute('aria-selected', String(on));
    });
    const deep = jf.stack.length > 1;
    $('jf-crumbs').hidden = !deep;
    $('jf-crumb').textContent = jf.stack.slice(1).map((x) => x.title).join(' › ');
    // Grid
    const items = f.items || [];
    items.forEach((it) => grid.appendChild(jfTile(it)));
    const empty = $('jf-empty');
    empty.hidden = !!items.length || f.loading;
    if (f.error) { empty.hidden = false; empty.textContent = f.error; }
    else if (!items.length && !f.loading) {
      empty.textContent = f.kind === 'resume' ? 'Nothing to continue.' : f.kind === 'nextup' ? 'Nothing up next.' : f.kind === 'search' ? 'No results.' : 'Empty.';
    }
    $('jf-more').hidden = !(f.total != null && items.length < f.total);
    if (f.loading && !items.length) { empty.hidden = false; empty.textContent = 'Loading…'; }
  }

  function jfPath(f, start) {
    switch (f.kind) {
      case 'resume': return '/jellyfin/resume';
      case 'nextup': return '/jellyfin/nextup';
      case 'views': return '/jellyfin/views';
      case 'item': return `/jellyfin/item/${encodeURIComponent(f.id)}`;
      case 'items': return `/jellyfin/items?parent_id=${encodeURIComponent(f.parentId)}&start=${start}&limit=${JF_PAGE}`;
      case 'search': return `/jellyfin/items?search=${encodeURIComponent(f.q)}&types=Movie,Series,Episode&start=${start}&limit=${JF_PAGE}`;
      default: return null;
    }
  }

  async function jfFetch(f, append) {
    const seq = ++jf.seq;
    f.loading = true;
    f.error = null;
    const start = append ? (f.items || []).length : 0;
    if (!append) f.items = [];
    jfRenderFrame();
    try {
      const data = await api('GET', jfPath(f, start));
      if (seq !== jf.seq) return;
      $('jf').classList.remove('hidden');
      let items;
      if (f.kind === 'item') {
        items = Array.isArray(data.children) ? data.children : [];
        // Fall back to a plain child listing if the backend gave no children.
        if (!items.length) {
          f.kind = 'items';
          f.parentId = f.id;
          f.loading = false;
          return jfFetch(f, false);
        }
      } else {
        items = Array.isArray(data.items) ? data.items : [];
      }
      f.items = append ? (f.items || []).concat(items) : items;
      f.total = typeof data.total === 'number' ? data.total : null;
    } catch (e) {
      if (seq !== jf.seq) return;
      if (e.status === 404 && jf.stack.length <= 1 && f.kind !== 'search') {
        $('jf').classList.add('hidden');
        return;
      }
      // Configured but unreachable: keep the section visible with the error
      // instead of silently hiding it.
      $('jf').classList.remove('hidden');
      const msg = e.status === 502 ? 'mrpflix is unreachable right now' : e.message;
      if (append) toast(msg, true); else f.error = `Couldn't load: ${msg}`;
    } finally {
      if (seq === jf.seq) { f.loading = false; jfRenderFrame(); }
    }
  }

  function jfSetRoot(kind, extra) {
    jf.stack = [Object.assign({ kind, title: TAB_TITLES[kind] || '' }, extra || {})];
    jfFetch(jf.stack[0], false);
  }

  function jfPush(frame) {
    jf.stack.push(frame);
    jfFetch(frame, false);
  }

  function jfOpen(it) {
    if (PLAYABLE.has(it.type)) { openSheet(it); return; }
    if (DRILL_ITEM.has(it.type)) {
      jfPush({ kind: 'item', id: it.id, title: it.type === 'Season' && it.series_name ? `${it.series_name} · ${it.name}` : it.name });
    } else {
      jfPush({ kind: 'items', parentId: it.id, title: it.name });
    }
    $('jf').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  document.querySelectorAll('#jf-tabs .chip').forEach((c) => {
    c.addEventListener('click', () => {
      jf.tab = c.dataset.tab;
      store.set('mc.jf.tab', jf.tab);
      $('jf-search').value = '';
      jfSetRoot(jf.tab);
    });
  });
  $('jf-back').addEventListener('click', () => {
    if (jf.stack.length > 1) { jf.stack.pop(); jf.seq++; jfRenderFrame(); }
  });
  $('jf-more').addEventListener('click', () => { const f = jfCurrent(); if (f) jfFetch(f, true); });
  $('jf-search').addEventListener('input', debounce(() => {
    const q = $('jf-search').value.trim();
    if (q) jfSetRoot('search', { q, title: `Search “${q}”` });
    else jfSetRoot(jf.tab);
  }, 350));
  $('jf-lowq').checked = jf.lowQ;
  $('jf-lowq').addEventListener('change', (e) => { jf.lowQ = e.target.checked; store.set('mc.jf.lowq', jf.lowQ); });

  // ---------------------------------------------------------------- audio / subtitle tracks
  // Shape: docs/api-v1.md JfTracks (GET /jellyfin/item/{id}/tracks, State.tracks).

  // Fill one <select> with tracks; subtitles get an "Off" (-1) entry first.
  // Returns whether there is anything to choose between.
  function fillTrackSelect(sel, list, current, withOff) {
    sel.replaceChildren();
    if (withOff) sel.append(new Option('Off', '-1'));
    for (const t of list || []) sel.append(new Option(t.label, String(t.index)));
    const cur = current == null ? (withOff ? -1 : null) : current;
    if (cur != null) sel.value = String(cur);
    return (list || []).length > (withOff ? 0 : 1);
  }

  // Lay out an audio + subtitle picker pair; hidden when there is no choice.
  function fillTracks(prefix, tr) {
    const hasA = fillTrackSelect($(`${prefix}-audio`), tr.audio, tr.audio_index, false);
    const hasS = fillTrackSelect($(`${prefix}-subs`), tr.subtitles, tr.subtitle_index, true);
    $(`${prefix}-audio-f`).hidden = !hasA;
    $(`${prefix}-subs-f`).hidden = !hasS;
    $(`${prefix}-tracks`).hidden = !(hasA || hasS);
  }

  let nowTracksKey = '';
  let trackBusy = false;

  function renderTracks() {
    const s = state;
    const tr = s && s.activity !== 'idle' && s.source === 'jellyfin' ? s.tracks : null;
    if (!tr) { $('now-tracks').hidden = true; nowTracksKey = ''; return; }
    const key = JSON.stringify([s.item && s.item.id, tr.audio, tr.subtitles, tr.audio_index, tr.subtitle_index]);
    // Don't rebuild under an open picker or while a switch is in flight.
    const focused = document.activeElement === $('now-audio') || document.activeElement === $('now-subs');
    if (key === nowTracksKey || focused || trackBusy) return;
    nowTracksKey = key;
    fillTracks('now', tr);
  }

  async function switchTrack(body) {
    trackBusy = true;
    $('now-audio').disabled = $('now-subs').disabled = true;
    try {
      const r = await api('POST', '/jellyfin/tracks', body);
      toast(r.restarted ? 'Restarting the stream with the new track…' : 'Track switched');
    } catch (e) {
      toast(e.message, true);
    } finally {
      trackBusy = false;
      $('now-audio').disabled = $('now-subs').disabled = false;
      nowTracksKey = '';   // re-sync the pickers with the server's selection
      renderTracks();
    }
  }

  $('now-audio').addEventListener('change', (e) => switchTrack({ audio_index: Number(e.target.value) }));
  $('now-subs').addEventListener('change', (e) => switchTrack({ subtitle_index: Number(e.target.value) }));

  // Detail sheet
  let sheetItem = null;
  let sheetTracks = null;   // { id, tracks } once loaded for the open item

  function fillSheet(it) {
    const title = it.type === 'Episode' && it.series_name ? `${it.series_name} — ${it.name}` : (it.name || '');
    $('sheet-title').textContent = title;
    $('sheet-meta').textContent = [
      it.type === 'Episode' ? epCode(it) : '',
      it.year, fmtRuntime(it.runtime_s), it.played ? 'Watched' : '',
    ].filter(Boolean).join(' · ');
    $('sheet-overview').textContent = it.overview || '';
    const img = $('sheet-img');
    if (it.image_url) { img.src = it.image_url; img.hidden = false; } else img.hidden = true;
    const rb = $('sheet-resume');
    if (it.resume_s > 0) { rb.hidden = false; rb.textContent = `Resume from ${fmtTime(it.resume_s)}`; }
    else rb.hidden = true;
    $('sheet-start').classList.toggle('primary', !(it.resume_s > 0));
  }

  async function openSheet(it) {
    sheetItem = it;
    sheetTracks = null;
    $('sheet-tracks').hidden = true;
    fillSheet(it);
    loadSheetTracks(it.id);
    $('sheet').hidden = false;
    (it.resume_s > 0 ? $('sheet-resume') : $('sheet-start')).focus();
    try {
      const full = await api('GET', `/jellyfin/item/${encodeURIComponent(it.id)}`);
      if (sheetItem && sheetItem.id === it.id) { sheetItem = Object.assign({}, it, full, { id: it.id }); fillSheet(sheetItem); }
    } catch (_) { /* list data is enough */ }
  }

  async function loadSheetTracks(id) {
    try {
      const tr = await api('GET', `/jellyfin/item/${encodeURIComponent(id)}/tracks`);
      if (!sheetItem || sheetItem.id !== id) return;
      sheetTracks = { id, tracks: tr };
      fillTracks('sheet', tr);
    } catch (_) { /* play still works with the server's defaults */ }
  }

  function closeSheet() { $('sheet').hidden = true; sheetItem = null; sheetTracks = null; }

  async function playSheet(resume) {
    if (!sheetItem) return;
    const body = { id: sheetItem.id, resume };
    if (sheetTracks && sheetTracks.id === sheetItem.id) {
      if (sheetTracks.tracks.audio.length) body.audio_index = Number($('sheet-audio').value);
      body.subtitle_index = Number($('sheet-subs').value);
    }
    if (jf.lowQ) body.low_quality = true;
    const name = sheetItem.name;
    closeSheet();
    await act('POST', '/jellyfin/play', body, `Playing: ${name}`);
  }

  $('sheet-resume').addEventListener('click', () => playSheet(true));
  $('sheet-start').addEventListener('click', () => playSheet(false));
  $('sheet-close').addEventListener('click', closeSheet);
  $('sheet').addEventListener('click', (e) => { if (e.target === $('sheet')) closeSheet(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('sheet').hidden) closeSheet(); });

  // ---------------------------------------------------------------- pairing

  let tokenShown = false;

  function pairBases() {
    const seen = new Set();
    const out = [];
    [location.origin].concat(CFG.pairUrls || []).forEach((u) => {
      if (!u || typeof u !== 'string' || !/^https?:\/\//.test(u)) return;
      const v = u.replace(/\/+$/, '');
      if (!seen.has(v)) { seen.add(v); out.push(v); }
    });
    return out;
  }

  function qrSvg(text) {
    const qr = qrcode(0, 'M');
    qr.addData(text);
    qr.make();
    const n = qr.getModuleCount(), quiet = 2, size = n + quiet * 2;
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
    svg.setAttribute('shape-rendering', 'crispEdges');
    svg.setAttribute('role', 'img');
    let d = '';
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c + quiet} ${r + quiet}h1v1h-1z`;
    }
    const bg = document.createElementNS(NS, 'rect');
    bg.setAttribute('width', String(size)); bg.setAttribute('height', String(size)); bg.setAttribute('fill', '#fff');
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', d); path.setAttribute('fill', '#000');
    svg.append(bg, path);
    return svg;
  }

  function renderPair() {
    const sel = $('pair-base');
    const base = sel.value;
    $('pair-url').textContent = base;
    const box = $('pair-qr');
    box.replaceChildren();
    if (!CFG.token) { box.appendChild(el('p', 'muted small', 'No token configured — open this page from the server.')); return; }
    if (typeof qrcode !== 'function') { box.appendChild(el('p', 'muted small', 'QR library failed to load.')); return; }
    const uri = `mediacast://pair?url=${encodeURIComponent(base)}&token=${encodeURIComponent(CFG.token)}`;
    try { box.appendChild(qrSvg(uri)); } catch (e) { box.appendChild(el('p', 'muted small', `QR error: ${e.message || e}`)); }
  }

  (() => {
    const sel = $('pair-base');
    pairBases().forEach((u) => {
      const o = el('option', null, u);
      o.value = u;
      sel.appendChild(o);
    });
    // Prefer a LAN address over e.g. localhost when the page is opened locally.
    const lan = (CFG.pairUrls || []).find((u) => typeof u === 'string');
    if (/^https?:\/\/(localhost|127\.)/.test(location.origin) && lan) sel.value = lan.replace(/\/+$/, '');
    sel.addEventListener('change', renderPair);
    $('pair').addEventListener('toggle', () => { if ($('pair').open) renderPair(); });
    $('pair-token').textContent = CFG.token || '(none)';
    $('token-show').addEventListener('click', () => {
      tokenShown = !tokenShown;
      $('pair-token').hidden = !tokenShown;
      $('token-show').textContent = tokenShown ? 'Hide token' : 'Show token';
    });
    $('token-copy').addEventListener('click', async () => {
      if (!CFG.token) { toast('No token configured', true); return; }
      let ok = false;
      try { await navigator.clipboard.writeText(CFG.token); ok = true; } catch (_) {
        // Clipboard API needs a secure context; fall back for plain http.
        const ta = el('textarea');
        ta.value = CFG.token;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        ta.remove();
      }
      toast(ok ? 'Token copied' : 'Copy failed — use “Show token”', !ok);
    });
  })();

  // ---------------------------------------------------------------- boot

  $('foot').textContent = `mediacast ${CFG.version}`;
  if (!window.MEDIACAST) toast('No server config injected — running in offline preview', true);

  refreshState();
  connectEvents();
  ytLoad(false);
  jfSetRoot(jf.tab);

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => { /* needs https or localhost */ });
    });
  }
})();
