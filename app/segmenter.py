"""Découpage du flux micro en segments de parole via WebRTC VAD.

Le client envoie du PCM 16 bits mono 16 kHz. On analyse des trames de 30 ms :
un segment démarre quand la parole domine la fenêtre récente (avec un
pré-roll pour ne pas couper le début de phrase) et se termine après un
silence prolongé ou une durée maximale.
"""

from collections import deque
from dataclasses import dataclass

import audioop

import webrtcvad

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 960 octets

VAD_AGGRESSIVENESS = 1     # plus sensible : capte les attaques/fins douces
PREROLL_FRAMES = 25        # 750 ms gardées avant le déclenchement (début de phrase)
TRIGGER_RATIO = 0.35       # déclenche vite, sur peu de trames "parole"
SILENCE_END_MS = 950       # silence qui clôt un segment (capte les fins de mots)
MIN_SPEECH_MS = 240        # en dessous, le segment est ignoré (bruit)
MAX_SEGMENT_S = 15         # coupe forcée pour garder une latence raisonnable

# Contrôle de gain automatique appliqué AVANT la détection de parole : sans lui,
# un micro faible (casque...) reste sous le seuil du VAD et aucune phrase n'est
# détectée. On amène le niveau crête vers une cible exploitable.
AGC_TARGET_PEAK = 11000    # niveau crête visé (échelle int16)
AGC_NOISE_FLOOR = 120      # en dessous : du silence, on n'amplifie pas
AGC_MAX_GAIN = 60.0        # amplification maximale
AGC_ATTACK = 0.5           # vitesse de montée du gain (0-1)
AGC_RELEASE = 0.2          # vitesse de descente du gain (0-1)


@dataclass
class Segment:
    pcm: bytes
    t0: float  # secondes depuis le début de la réunion
    t1: float


class _AutoGain:
    """Amplification adaptative du flux pour fiabiliser la détection de parole."""

    def __init__(self):
        self.gain = 1.0

    def process(self, frame: bytes) -> bytes:
        # Toute erreur ici ne doit JAMAIS interrompre le découpage : on
        # renvoie la trame d'origine en cas de souci.
        try:
            peak = audioop.max(frame, 2)
            if peak >= AGC_NOISE_FLOOR:
                desired = min(AGC_MAX_GAIN, AGC_TARGET_PEAK / peak)
                rate = AGC_ATTACK if desired > self.gain else AGC_RELEASE
                self.gain += (desired - self.gain) * rate
                self.gain = max(1.0, min(AGC_MAX_GAIN, self.gain))
            if self.gain > 1.01:
                return audioop.mul(frame, 2, self.gain)  # audioop sature, ne déborde pas
        except Exception:
            return frame
        return frame



class SpeechSegmenter:
    def __init__(self):
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._agc = _AutoGain()
        self._pending = bytearray()
        self._ring: deque[tuple[bytes, bool]] = deque(maxlen=PREROLL_FRAMES)
        self._frame_index = 0
        self._triggered = False
        self._segment = bytearray()
        self._segment_start_frame = 0
        self._silence_frames = 0
        self._speech_frames = 0

    def feed(self, data: bytes) -> list[Segment]:
        """Ajoute de l'audio brut et renvoie les segments terminés."""
        self._pending.extend(data)
        segments = []
        while len(self._pending) >= FRAME_BYTES:
            raw = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            # Le VAD travaille sur l'audio amplifié (détection fiable même micro
            # faible) ; le segment stocke l'audio BRUT (transcription propre).
            amplified = self._agc.process(raw)
            seg = self._process_frame(raw, amplified)
            if seg is not None:
                segments.append(seg)
        return segments

    def flush(self) -> Segment | None:
        """Clôt le segment en cours (fin d'enregistrement)."""
        seg = self._finish_segment() if self._triggered else None
        self._ring.clear()
        self._pending.clear()
        return seg

    def _process_frame(self, raw: bytes, amplified: bytes) -> Segment | None:
        is_speech = self._vad.is_speech(amplified, SAMPLE_RATE)
        self._frame_index += 1

        if not self._triggered:
            self._ring.append((raw, is_speech))
            voiced = sum(1 for _, s in self._ring if s)
            if len(self._ring) == self._ring.maxlen and voiced >= TRIGGER_RATIO * self._ring.maxlen:
                self._triggered = True
                self._segment_start_frame = self._frame_index - len(self._ring)
                self._segment = bytearray(b"".join(f for f, _ in self._ring))
                self._speech_frames = voiced
                self._silence_frames = 0
                self._ring.clear()
            return None

        self._segment.extend(raw)
        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        too_long = len(self._segment) >= MAX_SEGMENT_S * SAMPLE_RATE * 2
        ended = self._silence_frames * FRAME_MS >= SILENCE_END_MS
        if ended or too_long:
            return self._finish_segment()
        return None

    def _finish_segment(self) -> Segment | None:
        self._triggered = False
        seg, self._segment = self._segment, bytearray()
        if self._speech_frames * FRAME_MS < MIN_SPEECH_MS:
            return None
        t0 = self._segment_start_frame * FRAME_MS / 1000
        t1 = self._frame_index * FRAME_MS / 1000
        return Segment(pcm=bytes(seg), t0=t0, t1=t1)
