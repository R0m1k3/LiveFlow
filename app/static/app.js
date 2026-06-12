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
};

const BATCH_SAMPLES = 4096; // ~256 ms de PCM 16 kHz par message WebSocket

// ----------------------------------------------------------- enregistrement

async function startRecording() {
  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (firstErr) {
    // Certains pilotes échouent avec les options de traitement audio :
    // on retente avec la contrainte minimale avant d'abandonner.
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
  state.ws.onclose = () => { if (state.recording) stopRecording(true); };

  state.audioContext = new AudioContext();
  await state.audioContext.audioWorklet.addModule('worklet.js');
  const source = state.audioContext.createMediaStreamSource(state.mediaStream);
  state.workletNode = new AudioWorkletNode(state.audioContext, 'pcm-downsampler');
  state.workletNode.port.onmessage = (e) => queuePcm(new Int16Array(e.data));
  source.connect(state.workletNode);

  state.recording = true;
  state.startedAt = Date.now();
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
  const meetings = await (await fetch('/api/meetings')).json();
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
  const meeting = await (await fetch(`/api/meetings/${id}`)).json();
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
  await fetch(`/api/meetings/${state.currentMeetingId}`, { method: 'DELETE' });
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
$('copy-btn').onclick = copyTranscript;
$('delete-btn').onclick = deleteCurrentMeeting;
loadMeetings();
