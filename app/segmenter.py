"""Découpage du flux micro en segments de parole via WebRTC VAD.

Le client envoie du PCM 16 bits mono 16 kHz. On analyse des trames de 30 ms :
un segment démarre quand la parole domine la fenêtre récente (avec un
pré-roll pour ne pas couper le début de phrase) et se termine après un
silence prolongé ou une durée maximale.
"""

from collections import deque
from dataclasses import dataclass

import webrtcvad

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 960 octets

VAD_AGGRESSIVENESS = 2
PREROLL_FRAMES = 10        # 300 ms conservées avant le déclenchement
TRIGGER_RATIO = 0.6        # part de trames "parole" du pré-roll pour démarrer
SILENCE_END_MS = 700       # silence qui clôt un segment
MIN_SPEECH_MS = 300        # en dessous, le segment est ignoré (bruit)
MAX_SEGMENT_S = 25         # coupe forcée pour garder une latence raisonnable


@dataclass
class Segment:
    pcm: bytes
    t0: float  # secondes depuis le début de la réunion
    t1: float


class SpeechSegmenter:
    def __init__(self):
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
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
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            seg = self._process_frame(frame)
            if seg is not None:
                segments.append(seg)
        return segments

    def flush(self) -> Segment | None:
        """Clôt le segment en cours (fin d'enregistrement)."""
        seg = self._finish_segment() if self._triggered else None
        self._ring.clear()
        self._pending.clear()
        return seg

    def _process_frame(self, frame: bytes) -> Segment | None:
        is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
        self._frame_index += 1

        if not self._triggered:
            self._ring.append((frame, is_speech))
            voiced = sum(1 for _, s in self._ring if s)
            if len(self._ring) == self._ring.maxlen and voiced >= TRIGGER_RATIO * self._ring.maxlen:
                self._triggered = True
                self._segment_start_frame = self._frame_index - len(self._ring)
                self._segment = bytearray(b"".join(f for f, _ in self._ring))
                self._speech_frames = voiced
                self._silence_frames = 0
                self._ring.clear()
            return None

        self._segment.extend(frame)
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
