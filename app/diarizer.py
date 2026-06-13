"""Identification des locuteurs par embeddings ECAPA-TDNN + clustering incrémental.

Chaque segment de parole (PCM 16 kHz mono) est projeté dans un espace de
représentation de dimension 192 via ECAPA-TDNN (SpeechBrain).  On maintient
un profil par locuteur (moyenne mobile exponentielle des embeddings) et on
attribue chaque nouveau segment au locuteur le plus proche par similarité
cosinus, ou on crée un nouveau locuteur si le score est sous le seuil.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

# Imports lourds (torch, speechbrain) chargés paresseusement dans load_model()
# pour ne pas pénaliser le démarrage quand la diarisation est désactivée.


@dataclass
class _SpeakerProfile:
    """Profil incrémental d'un locuteur au sein d'une session."""

    label: str
    embedding: np.ndarray            # centroïde courant (moyenne mobile)
    count: int = 0                   # nombre d'observations
    _ema_alpha: float = field(default=0.15, repr=False)

    def update(self, new_emb: np.ndarray) -> None:
        """Met à jour le centroïde via une moyenne mobile exponentielle.

        Alpha faible : le centroïde reste stable et ne dérive pas vers les
        variations d'un segment à l'autre (qui scinderaient un même locuteur).
        """
        self.count += 1
        if self.count == 1:
            self.embedding = new_emb.copy()
        else:
            self.embedding = (
                self._ema_alpha * new_emb
                + (1 - self._ema_alpha) * self.embedding
            )
        # Renormaliser pour que la similarité cosinus reste cohérente
        norm = np.linalg.norm(self.embedding)
        if norm > 0:
            self.embedding /= norm


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Similarité cosinus entre deux vecteurs unitaires."""
    return float(np.dot(a, b))


class EmbeddingModel:
    """Encapsule le modèle SpeechBrain ECAPA-TDNN (singleton partagé).

    Chargé une seule fois au démarrage de l'application, puis réutilisé
    par chaque session WebSocket via des instances de SpeakerDiarizer.
    """

    def __init__(self) -> None:
        import torch  # noqa: F811 — import local volontaire
        from speechbrain.inference.speaker import EncoderClassifier

        self._device = "cpu"
        self._model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir="/data/models/ecapa-tdnn",
            run_opts={"device": self._device},
        )
        self._torch = torch

    def extract(self, pcm: bytes, sample_rate: int = 16000) -> np.ndarray:
        """Extrait un embedding 192-d à partir d'un segment PCM 16-bit mono.

        Returns:
            np.ndarray de forme (192,), normalisé L2.

        Raises:
            ValueError: si le segment audio est trop court (< 200 ms).
        """
        n_samples = len(pcm) // 2
        min_samples = sample_rate // 5  # 200 ms minimum
        if n_samples < min_samples:
            raise ValueError(
                f"Segment trop court ({n_samples} samples, min {min_samples})"
            )

        # PCM 16-bit little-endian → float32 [-1, 1]
        samples = struct.unpack(f"<{n_samples}h", pcm)
        waveform = self._torch.tensor(samples, dtype=self._torch.float32) / 32768.0
        waveform = waveform.unsqueeze(0)  # (1, T)

        with self._torch.no_grad():
            embedding = self._model.encode_batch(waveform)

        emb = embedding.squeeze().cpu().numpy()  # (192,)
        # Normaliser L2
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb /= norm
        return emb


class SpeakerDiarizer:
    """Identifie le locuteur de chaque segment audio au sein d'une session.

    Chaque instance correspond à une réunion/session et maintient ses propres
    profils de locuteurs. Le modèle d'embedding est partagé (EmbeddingModel).
    """

    # Un nouveau locuteur n'est créé que si le meilleur score est NETTEMENT
    # sous le seuil (zone morte) ET que le segment est assez long pour donner
    # une empreinte fiable. Sinon le segment est rattaché au meilleur profil.
    # Cela évite qu'un même locuteur soit éclaté en Locuteur 3, 4, 5...
    NEW_SPEAKER_MARGIN = 0.20   # écart sous le seuil pour autoriser un nouveau
    NEW_SPEAKER_MIN_S = 1.2     # durée mini d'un segment pour créer un locuteur
    STICKY_MARGIN = 0.05        # adhérence au dernier locuteur si scores proches

    def __init__(
        self,
        model: EmbeddingModel,
        threshold: float = 0.35,
        max_speakers: int = 8,
    ) -> None:
        self._model = model
        self._threshold = threshold
        self._max_speakers = max_speakers
        self._profiles: list[_SpeakerProfile] = []
        self._last: _SpeakerProfile | None = None

    def identify(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """Identifie le locuteur d'un segment PCM.

        Returns:
            Label du locuteur ("Locuteur 1", "Locuteur 2"...) ou chaîne vide
            si le segment est trop court pour être analysé.
        """
        try:
            embedding = self._model.extract(pcm, sample_rate)
        except ValueError:
            return ""

        duration_s = (len(pcm) // 2) / sample_rate

        # Score de chaque profil existant
        scores = [(_cosine_similarity(embedding, p.embedding), p) for p in self._profiles]
        best_score, best_profile = max(scores, default=(-1.0, None), key=lambda x: x[0])

        # Adhérence : si le dernier locuteur est presque aussi proche que le
        # meilleur, on le garde (réduit le clignotement entre deux profils).
        if self._last is not None and best_profile is not self._last:
            last_score = next((s for s, p in scores if p is self._last), -1.0)
            if last_score >= best_score - self.STICKY_MARGIN:
                best_score, best_profile = last_score, self._last

        # Rattachement à un locuteur connu
        if best_profile is not None and best_score >= self._threshold:
            best_profile.update(embedding)
            self._last = best_profile
            return best_profile.label

        # Création d'un nouveau locuteur : conditions strictes
        can_create = (
            len(self._profiles) < self._max_speakers
            and duration_s >= self.NEW_SPEAKER_MIN_S
            and best_score < self._threshold - self.NEW_SPEAKER_MARGIN
        )
        if not can_create and best_profile is not None:
            # Zone ambiguë ou segment court : on rattache au plus proche
            best_profile.update(embedding)
            self._last = best_profile
            return best_profile.label

        new_label = f"Locuteur {len(self._profiles) + 1}"
        new_profile = _SpeakerProfile(label=new_label, embedding=embedding)
        new_profile.update(embedding)
        self._profiles.append(new_profile)
        self._last = new_profile
        return new_label

    def reset(self) -> None:
        """Réinitialise les profils pour une nouvelle session."""
        self._profiles.clear()
        self._last = None
