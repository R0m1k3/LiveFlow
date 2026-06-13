const $ = (id) => document.getElementById(id);

const state = {
  recording: false,
  ws: null,
  audioContext: null,
  workletNode: null,
  mediaStream: null,
  sendBuffer: [],
  sendBufferSamples: 0,
  timerInterval: null,
  startedAt: null,
  currentMeetingId: null,
  lastLoudAt: 0,
  heardSound: false,
  silentWarned: false,
  sourceNode: null,
  paused: false,
  wakeLock: null,
};

// couleurs des étiquettes de locuteurs (classes .speaker-0 à .speaker-7)
const speakerColors = {};
function speakerColorIndex(speaker) {
  if (!(speaker in speakerColors)) {
    speakerColors[speaker] = Object.keys(speakerColors).length % 8;
  }
  return speakerColors[speaker];
}

const BATCH_SAMPLES = 4096; // ~256 ms de PCM 16 kHz par message WebSocket

// fetch avec redirection vers la page de connexion si la session a expiré
async function api(url, opts) {
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    location.href = '/login';
    throw new Error('session expirée');
  }
  return resp;
}

// -------------------------------------------------------------------- micro

async function listMics() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = devices.filter((d) => d.kind === 'audioinput' && d.deviceId);
    const sel = $('mic-select');
    const saved = localStorage.getItem('liveflow-mic') || '';
    sel.innerHTML = '<option value="">Micro par défaut</option>';
    for (const m of mics) {
      const opt = document.createElement('option');
      opt.value = m.deviceId;
      opt.textContent = m.label || `Micro ${sel.length}`;
      if (m.deviceId === saved) opt.selected = true;
      sel.appendChild(opt);
    }
  } catch (e) { /* énumération indisponible : on garde "Micro par défaut" */ }
}

function micConstraints(deviceId) {
  const c = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (deviceId) c.deviceId = { exact: deviceId };
  return c;
}

// Bascule sur un autre micro sans interrompre l'enregistrement.
async function switchMic(deviceId) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints(deviceId) });
  } catch (e) {
    return false;
  }
  if (state.sourceNode) state.sourceNode.disconnect();
  if (state.mediaStream) state.mediaStream.getTracks().forEach((t) => t.stop());
  state.mediaStream = stream;
  state.sourceNode = state.audioContext.createMediaStreamSource(stream);
  state.sourceNode.connect(state.workletNode);
  state.lastLoudAt = Date.now();
  return true;
}


// ----------------------------------------------------------- enregistrement

async function startRecording() {
  keepAwake();  // DANS le geste de clic, avant tout await (requis par iOS)
  const micId = localStorage.getItem('liveflow-mic');
  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints(micId) });
  } catch (firstErr) {
    // Micro choisi indisponible ou pilote qui refuse les options de
    // traitement audio : on retente avec la contrainte minimale.
    try {
      state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      const causes = {
        NotAllowedError: "Permission refusée. Autorisez le micro pour ce site (icône à gauche de l'adresse), et vérifiez que la page est servie en HTTPS.",
        NotReadableError: "Le micro est inaccessible au niveau du système : fermez les autres onglets ou applications qui l'utilisent (Teams, Discord, autre onglet LiveFlow...), redémarrez le navigateur, et vérifiez les paramètres de confidentialité micro de l'OS.",
        NotFoundError: "Aucun micro détecté. Branchez un micro ou choisissez le bon périphérique d'entrée.",
        OverconstrainedError: "Le micro ne supporte pas les réglages demandés.",
      };
      alert("Impossible de démarrer le micro.\n\n" + (causes[err.name] || "") + "\n\n(" + err + ")");
      allowSleep();  // échec micro : on relâche l'anti-veille
      return;
    }
  }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => state.ws.send(JSON.stringify({
    type: 'start',
    title: $('title').value,
    diarization: $('diarization-cb').checked,
  }));
  state.ws.onmessage = onServerMessage;
  state.ws.onclose = (e) => {
    if (e.code === 4401) { location.href = '/login'; return; }
    if (state.recording) stopRecording(true);
  };

  state.audioContext = new AudioContext();
  await state.audioContext.audioWorklet.addModule('worklet.js');
  state.workletNode = new AudioWorkletNode(state.audioContext, 'pcm-downsampler');
  state.workletNode.port.onmessage = (e) => queuePcm(new Int16Array(e.data));
  // L'amplification des micros faibles se fait côté serveur (AGC avant le
  // VAD) : le client envoie l'audio brut, sans gain qui se battrait avec.
  state.sourceNode = state.audioContext.createMediaStreamSource(state.mediaStream);
  state.sourceNode.connect(state.workletNode);

  // les libellés des micros ne sont disponibles qu'une fois la permission accordée
  listMics();

  state.recording = true;
  state.paused = false;
  state.startedAt = Date.now();
  state.lastLoudAt = Date.now();
  state.heardSound = false;
  state.silentWarned = false;
  state.timerInterval = setInterval(updateTimer, 500);
  $('record-btn').textContent = '■ Arrêter';
  $('record-btn').classList.add('recording');
  $('pause-btn').classList.remove('hidden', 'paused');
  $('pause-btn').textContent = '⏸ Pause';
  $('title').disabled = true;
  setStatus('rec', 'Enregistrement…');
  clearTranscript();
}

function queuePcm(samples) {
  if (state.paused) {
    state.lastLoudAt = Date.now(); // pas de chasse au micro pendant la pause
    return;
  }
  updateVu(samples);
  if (!state.recording || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.sendBuffer.push(samples);
  state.sendBufferSamples += samples.length;
  if (state.sendBufferSamples >= BATCH_SAMPLES) flushPcm();
}

function updateVu(samples) {
  let peak = 0;
  for (let i = 0; i < samples.length; i += 4) {
    const v = Math.abs(samples[i]);
    if (v > peak) peak = v;
  }
  // Affichage amplifié (x8) : le serveur amplifie le signal réel, le vumètre
  // reflète l'activité même avec un micro faible.
  $('vu-bar').style.width = Math.min(100, (peak / 32768) * 140 * 8) + '%';
  if (peak > 150) {
    state.lastLoudAt = Date.now();
    state.heardSound = true;
  }
}

function flushPcm() {
  if (!state.sendBufferSamples) return;
  const out = new Int16Array(state.sendBufferSamples);
  let off = 0;
  for (const chunk of state.sendBuffer) { out.set(chunk, off); off += chunk.length; }
  state.sendBuffer = [];
  state.sendBufferSamples = 0;
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(out.buffer);
}

function stopRecording(abrupt = false) {
  state.recording = false;
  clearInterval(state.timerInterval);
  allowSleep();
  if (state.sourceNode) state.sourceNode.disconnect();
  if (state.workletNode) state.workletNode.disconnect();
  if (state.mediaStream) state.mediaStream.getTracks().forEach((t) => t.stop());
  if (state.audioContext) state.audioContext.close();

  if (!abrupt && state.ws && state.ws.readyState === WebSocket.OPEN) {
    flushPcm();
    state.ws.send(JSON.stringify({ type: 'stop' }));
    setStatus('busy', 'Finalisation…');
  } else {
    setStatus('idle', 'Prêt');
  }

  $('record-btn').textContent = '● Démarrer';
  $('record-btn').classList.remove('recording');
  $('pause-btn').classList.add('hidden');
  state.paused = false;
  $('title').disabled = false;
  $('vu-bar').style.width = '0%';
}

function togglePause() {
  if (!state.recording || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.paused = !state.paused;
  const btn = $('pause-btn');
  if (state.paused) {
    flushPcm();
    state.ws.send(JSON.stringify({ type: 'pause' }));
    btn.textContent = '▶ Reprendre';
    btn.classList.add('paused');
    setStatus('busy', 'En pause');
    $('vu-bar').style.width = '0%';
  } else {
    state.ws.send(JSON.stringify({ type: 'resume' }));
    state.lastLoudAt = Date.now();
    btn.textContent = '⏸ Pause';
    btn.classList.remove('paused');
    setStatus('rec', 'Enregistrement…');
  }
}

function onServerMessage(event) {
  const msg = JSON.parse(event.data);
  if (msg.type === 'ready') {
    state.currentMeetingId = msg.meeting_id;
    $('transcript-title').textContent = msg.title;
    showExportBar(msg.meeting_id);
  } else if (msg.type === 'segment') {
    appendSegment(msg);
    if (state.recording) setStatus('rec', 'Enregistrement…');
  } else if (msg.type === 'error') {
    setStatus('error', 'Erreur ASR');
    console.error(msg.message);
  } else if (msg.type === 'done') {
    setStatus('idle', 'Terminé ✓');
    state.ws.close();
    state.ws = null;
    loadMeetings();
  }
}

// ----------------------------------------------------------------- affichage

function setStatus(cls, text) {
  const el = $('status');
  el.className = 'badge ' + cls;
  el.textContent = text;
}

function updateTimer() {
  const s = Math.floor((Date.now() - state.startedAt) / 1000);
  $('timer').textContent =
    String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');

  // Simple alerte tant qu'aucun son n'a jamais été capté : l'app ne change
  // JAMAIS de micro toute seule (le choix se fait dans la liste).
  if (!state.recording || state.paused) return;
  if (state.heardSound) {
    if (state.silentWarned) {
      state.silentWarned = false;
      setStatus('rec', 'Enregistrement…');
    }
    return;
  }
  if (!state.silentWarned && Date.now() - state.lastLoudAt > 5000) {
    state.silentWarned = true;
    setStatus('error', 'Aucun son capté — choisissez un autre micro dans la liste');
  }
}

function fmtTs(seconds) {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? String(h).padStart(2, '0') + ':' : '') +
    String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
}

function clearTranscript() {
  $('transcript').innerHTML = '';
}

function appendSegment(seg) {
  const div = document.createElement('div');
  div.className = 'segment';
  let speakerHtml = '';
  if (seg.speaker) {
    speakerHtml = `<span class="speaker speaker-${speakerColorIndex(seg.speaker)}"></span>`;
  }
  div.innerHTML = `<span class="ts">${fmtTs(seg.t0)}</span>${speakerHtml}<span class="text"></span>`;
  if (seg.speaker) div.querySelector('.speaker').textContent = seg.speaker;
  div.querySelector('.text').textContent = seg.text;
  $('transcript').appendChild(div);
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

function showExportBar(meetingId) {
  $('export-bar').classList.remove('hidden');
  for (const fmt of ['txt', 'md', 'srt', 'json']) {
    $('export-' + fmt).href = `/api/meetings/${meetingId}/export?format=${fmt}`;
  }
}

// ----------------------------------------------------------------- réunions

async function loadMeetings() {
  const meetings = await (await api('/api/meetings')).json();
  const ul = $('meeting-list');
  ul.innerHTML = '';
  for (const m of meetings) {
    const li = document.createElement('li');
    const date = new Date(m.created_at).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' });
    li.innerHTML = `<span class="m-title"></span><span class="m-meta">${date} · ${fmtTs(m.duration)}</span>`;
    li.querySelector('.m-title').textContent = m.title;
    li.onclick = () => openMeeting(m.id);
    if (m.id === state.currentMeetingId) li.classList.add('active');
    ul.appendChild(li);
  }
}

async function openMeeting(id) {
  if (state.recording) return;
  const meeting = await (await api(`/api/meetings/${id}`)).json();
  state.currentMeetingId = id;
  $('transcript-title').textContent = meeting.title;
  clearTranscript();
  if (meeting.segments.length === 0) {
    $('transcript').innerHTML = '<p class="placeholder">Aucun texte pour cette réunion.</p>';
  } else {
    meeting.segments.forEach(appendSegment);
  }
  showExportBar(id);
  loadMeetings();
}

async function deleteCurrentMeeting() {
  if (!state.currentMeetingId || state.recording) return;
  if (!confirm('Supprimer définitivement cette réunion et sa transcription ?')) return;
  await api(`/api/meetings/${state.currentMeetingId}`, { method: 'DELETE' });
  state.currentMeetingId = null;
  $('transcript-title').textContent = 'Transcription';
  clearTranscript();
  $('transcript').innerHTML = '<p class="placeholder">Réunion supprimée.</p>';
  $('export-bar').classList.add('hidden');
  loadMeetings();
}

async function copyTranscript() {
  const text = [...document.querySelectorAll('#transcript .segment')]
    .map((el) => {
      const speaker = el.querySelector('.speaker');
      const t = el.querySelector('.text').textContent;
      return speaker ? `[${speaker.textContent}] ${t}` : t;
    }).join('\n');
  await navigator.clipboard.writeText(text);
  $('copy-btn').textContent = '✓ Copié';
  setTimeout(() => ($('copy-btn').textContent = '📋 Copier'), 1500);
}

// --------------------------------------------------------------------- init

// --- Anti-veille (téléphone) ---
// Deux mécanismes combinés : l'API Wake Lock (iOS 16.4+/Android) ET, en repli,
// une micro-vidéo muette en boucle alimentée par un canvas (fonctionne sur les
// iOS plus anciens où Wake Lock est ignoré). À déclencher DANS le geste de clic.

async function acquireWakeLock() {
  try {
    if ('wakeLock' in navigator) {
      state.wakeLock = await navigator.wakeLock.request('screen');
    }
  } catch (e) { /* refusé / non supporté : le repli vidéo prend le relais */ }
}
function releaseWakeLock() {
  try { state.wakeLock && state.wakeLock.release(); } catch (e) {}
  state.wakeLock = null;
}

function startNoSleepVideo() {
  try {
    if (!state.noSleepVideo) {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 2;
      const ctx = canvas.getContext('2d');
      state.noSleepDraw = setInterval(() => {
        ctx.fillStyle = state._t ? '#000' : '#001';
        state._t = !state._t;
        ctx.fillRect(0, 0, 2, 2);
      }, 1000);
      const v = document.createElement('video');
      v.muted = true; v.setAttribute('muted', '');
      v.playsInline = true; v.setAttribute('playsinline', '');
      v.loop = true;
      v.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none;';
      v.srcObject = canvas.captureStream(2);
      document.body.appendChild(v);
      state.noSleepVideo = v;
    }
    const p = state.noSleepVideo.play();
    if (p) p.catch(() => {});
  } catch (e) { /* tant pis */ }
}
function stopNoSleepVideo() {
  try {
    if (state.noSleepVideo) state.noSleepVideo.pause();
    if (state.noSleepDraw) clearInterval(state.noSleepDraw);
    state.noSleepDraw = null;
  } catch (e) {}
}

function keepAwake() { acquireWakeLock(); startNoSleepVideo(); }
function allowSleep() { releaseWakeLock(); stopNoSleepVideo(); }

// iOS relâche le verrou quand l'onglet repasse au premier plan : on le reprend
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (state.recording) keepAwake();
    else loadMeetings();  // rafraîchit la liste en revenant sur l'onglet
  }
});
window.addEventListener('focus', () => { if (!state.recording) loadMeetings(); });
// rafraîchit régulièrement la liste (réunions faites depuis un autre appareil)
setInterval(() => { if (!state.recording) loadMeetings(); }, 20000);

$('record-btn').onclick = () => (state.recording ? stopRecording() : startRecording());
$('pause-btn').onclick = togglePause;
$('logout-btn').onclick = async () => { await fetch('/api/logout', { method: 'POST' }); location.href = '/login'; };
$('mic-select').onchange = async () => {
  const id = $('mic-select').value;
  localStorage.setItem('liveflow-mic', id);
  if (state.recording) {
    await switchMic(id || undefined);  // bascule à chaud, à la demande
    state.heardSound = false;
  }
};
listMics();
$('copy-btn').onclick = copyTranscript;
$('delete-btn').onclick = deleteCurrentMeeting;
loadMeetings();
