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
  silentWarned: false,
};

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

// ----------------------------------------------------------- enregistrement

async function startRecording() {
  const constraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const micId = localStorage.getItem('liveflow-mic');
  if (micId) constraints.deviceId = { exact: micId };
  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
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
      return;
    }
  }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => state.ws.send(JSON.stringify({ type: 'start', title: $('title').value }));
  state.ws.onmessage = onServerMessage;
  state.ws.onclose = (e) => {
    if (e.code === 4401) { location.href = '/login'; return; }
    if (state.recording) stopRecording(true);
  };

  state.audioContext = new AudioContext();
  await state.audioContext.audioWorklet.addModule('worklet.js');
  const source = state.audioContext.createMediaStreamSource(state.mediaStream);
  state.workletNode = new AudioWorkletNode(state.audioContext, 'pcm-downsampler');
  state.workletNode.port.onmessage = (e) => queuePcm(new Int16Array(e.data));
  source.connect(state.workletNode);

  // les libellés des micros ne sont disponibles qu'une fois la permission accordée
  listMics();

  state.recording = true;
  state.startedAt = Date.now();
  state.lastLoudAt = Date.now();
  state.silentWarned = false;
  state.timerInterval = setInterval(updateTimer, 500);
  $('record-btn').textContent = '■ Arrêter';
  $('record-btn').classList.add('recording');
  $('title').disabled = true;
  setStatus('rec', 'Enregistrement…');
  clearTranscript();
}

function queuePcm(samples) {
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
  $('vu-bar').style.width = Math.min(100, (peak / 32768) * 140) + '%';
  if (peak > 1500) state.lastLoudAt = Date.now();
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
  $('title').disabled = false;
  $('vu-bar').style.width = '0%';
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

  // alerte si le micro ne capte plus rien depuis 5 s
  if (!state.recording) return;
  const silent = Date.now() - state.lastLoudAt > 5000;
  if (silent && !state.silentWarned) {
    state.silentWarned = true;
    setStatus('error', 'Aucun son capté — changez de micro dans la liste');
  } else if (!silent && state.silentWarned) {
    state.silentWarned = false;
    setStatus('rec', 'Enregistrement…');
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
  div.innerHTML = `<span class="ts">${fmtTs(seg.t0)}</span><span class="text"></span>`;
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
  const text = [...document.querySelectorAll('#transcript .segment .text')]
    .map((el) => el.textContent).join('\n');
  await navigator.clipboard.writeText(text);
  $('copy-btn').textContent = '✓ Copié';
  setTimeout(() => ($('copy-btn').textContent = '📋 Copier'), 1500);
}

// --------------------------------------------------------------------- init

$('record-btn').onclick = () => (state.recording ? stopRecording() : startRecording());
$('logout-btn').onclick = async () => { await fetch('/api/logout', { method: 'POST' }); location.href = '/login'; };
$('mic-select').onchange = () => localStorage.setItem('liveflow-mic', $('mic-select').value);
listMics();
$('copy-btn').onclick = copyTranscript;
$('delete-btn').onclick = deleteCurrentMeeting;
loadMeetings();
