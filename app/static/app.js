const $ = (id) => document.getElementById(id);

// Vérification du contexte sécurisé (requis pour le micro et les périphériques)
if (window.isSecureContext === false) {
  const warning = document.createElement('div');
  warning.style.cssText = 'background: #3a181a; color: #ff8589; padding: 12px; text-align: center; font-weight: bold; border-bottom: 1px solid #e5484d; font-size: 0.9rem; z-index: 9999; position: relative;';
  warning.innerHTML = '⚠️ Contexte non sécurisé (HTTP) détecté. Le navigateur bloquera l\'accès au micro. Veuillez utiliser HTTPS (https://...) ou localhost.';
  document.body.insertBefore(warning, document.body.firstChild);
}

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

// --- Diarisation : palette de couleurs par locuteur ---
const SPEAKER_COLORS = 8; // nombre de classes CSS .speaker-0 à .speaker-7
const speakerMap = {};     // "Speaker 1" → 0, "Speaker 2" → 1, ...
let speakerCounter = 0;

function getSpeakerColorIndex(speaker) {
  if (!(speaker in speakerMap)) {
    speakerMap[speaker] = speakerCounter % SPEAKER_COLORS;
    speakerCounter++;
  }
  return speakerMap[speaker];
}

// --------------------------------------------------------- sélection micro

async function populateMicList() {
  const sel = $('mic-select');
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    return;
  }
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = devices.filter((d) => d.kind === 'audioinput');
    // Garder la sélection courante si possible
    const current = sel.value;
    sel.innerHTML = '<option value="">Micro par défaut</option>';
    for (const mic of mics) {
      if (!mic.deviceId) continue; // Ignorer les périphériques sans ID (permission non accordée)
      const opt = document.createElement('option');
      opt.value = mic.deviceId;
      opt.textContent = mic.label || `Micro ${sel.options.length}`;
      sel.appendChild(opt);
    }
    if (current) sel.value = current;
  } catch (err) {
    console.warn('Impossible d\'énumérer les micros :', err);
  }
}

// ----------------------------------------------------------- enregistrement

async function startRecording() {
  const audioConstraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const selectedMic = $('mic-select').value;
  if (selectedMic) audioConstraints.deviceId = { exact: selectedMic };

  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    // Rafraîchir les étiquettes des micros une fois la permission accordée
    await populateMicList();
  } catch (err) {
    const causes = {
      NotAllowedError: "Permission refusée. Autorisez le micro pour ce site (icône à gauche de l'adresse), et vérifiez que la page est servie en HTTPS.",
      NotReadableError: "Le micro est inaccessible au niveau du système : fermez les applications qui l'utilisent (Teams, Discord...), vérifiez les paramètres de confidentialité micro de l'OS, et le périphérique d'entrée choisi par le navigateur.",
      NotFoundError: "Aucun micro détecté. Branchez un micro ou choisissez le bon périphérique d'entrée.",
      OverconstrainedError: "Le micro ne supporte pas les réglages demandés.",
    };
    alert("Impossible de démarrer le micro.\n\n" + (causes[err.name] || "") + "\n\n(" + err + ")");
    return;
  }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => state.ws.send(JSON.stringify({
    type: 'start',
    title: $('title').value,
    diarization: $('diarization-cb').checked,
  }));
  state.ws.onmessage = onServerMessage;
  state.ws.onerror = (err) => {
    console.error('Erreur WebSocket :', err);
    setStatus('error', 'Erreur Connexion');
  };
  state.ws.onclose = (e) => {
    console.log(`WebSocket fermé : code=${e.code}, raison=${e.reason}`);
    if (state.recording) {
      stopRecording(true);
      if (e.code !== 1000 && e.code !== 1001) {
        setStatus('error', `Déconnexion (${e.code})`);
      }
    }
  };

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
  if (!state.recording || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.sendBuffer.push(samples);
  state.sendBufferSamples += samples.length;
  if (state.sendBufferSamples >= BATCH_SAMPLES) flushPcm();
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
  
  // Toujours recharger les réunions pour synchroniser la liste (cas d'arrêt abrupt ou échec)
  loadMeetings();
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
  // Reset le mapping locuteurs pour chaque nouvelle session
  for (const key in speakerMap) delete speakerMap[key];
  speakerCounter = 0;
}

function appendSegment(seg) {
  const div = document.createElement('div');
  div.className = 'segment';
  let speakerHtml = '';
  if (seg.speaker) {
    const ci = getSpeakerColorIndex(seg.speaker);
    speakerHtml = `<span class="speaker speaker-${ci}">${seg.speaker}</span>`;
  }
  div.innerHTML = `<span class="ts">${fmtTs(seg.t0)}</span>${speakerHtml}<span class="text"></span>`;
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
  const lines = [...document.querySelectorAll('#transcript .segment')].map((el) => {
    const speaker = el.querySelector('.speaker');
    const text = el.querySelector('.text').textContent;
    return speaker ? `[${speaker.textContent}] ${text}` : text;
  });
  await navigator.clipboard.writeText(lines.join('\n'));
  $('copy-btn').textContent = '✓ Copié';
  setTimeout(() => ($('copy-btn').textContent = '📋 Copier'), 1500);
}

// --------------------------------------------------------------------- init

$('record-btn').onclick = () => (state.recording ? stopRecording() : startRecording());
$('copy-btn').onclick = copyTranscript;
$('delete-btn').onclick = deleteCurrentMeeting;
loadMeetings();

// Peupler la liste des micros au chargement et demander la permission si nécessaire
async function initMics() {
  await populateMicList();
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    try {
      // Demander l'accès au micro temporairement pour obtenir les autorisations et les noms réels
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // Relâcher immédiatement le micro pour éteindre le témoin d'enregistrement
      stream.getTracks().forEach((t) => t.stop());
      // Re-peupler la liste maintenant que la permission est acquise
      await populateMicList();
    } catch (err) {
      console.log('Permission initiale micro refusée ou non disponible :', err);
    }
  }
}

initMics();
navigator.mediaDevices?.addEventListener('devicechange', populateMicList);
