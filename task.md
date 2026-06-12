# LiveFlow — Diarisation des locuteurs

## Context
LiveFlow est une application de transcription de réunions en temps réel. Nous y ajoutons la diarisation (identification automatique des locuteurs) via SpeechBrain ECAPA-TDNN et un clustering incrémental, ainsi qu'un sélecteur de microphone dans l'interface utilisateur.

## Current Focus
Vérification de la cohérence de tous les fichiers (backend et frontend) et tests d'intégration/fonctionnalité.

## Master Plan
- [x] Créer `app/diarizer.py` — module SpeakerDiarizer (embeddings + clustering)
- [x] Modifier `app/segmenter.py` — ajouter le champ `speaker` à la dataclass Segment
- [x] Modifier `app/main.py` — intégrer la diarisation dans le worker, le schéma de base de données, l'API et les exports
- [x] Modifier `app/static/app.js` — affichage des badges locuteurs, toggle, copie et intégration du sélecteur de micro
- [x] Modifier `app/static/index.html` — toggle diarisation et sélecteur de micro dans les contrôles
- [x] Modifier `app/static/style.css` — styles pour les badges locuteurs, le toggle et le sélecteur de micro
- [x] Modifier `app/requirements.txt` — ajouter speechbrain, torch, torchaudio
- [x] Modifier `app/Dockerfile` — installer PyTorch CPU-only
- [x] Modifier `.env.example` — variables DIARIZATION et DIARIZATION_THRESHOLD
- [x] Modifier `docker-compose.yml` — passer les variables d'environnement de diarisation
- [x] Vérification de la cohérence de tous les fichiers et validation
- [x] Correction de tout bug ou problème détecté

## Progress Log
- Création du module `diarizer.py` pour l'extraction des embeddings et le clustering.
- Modification de `segmenter.py` pour stocker le locuteur.
- Mise à jour de `main.py` pour la gestion de la session de diarisation, l'API et la persistance en base de données.
- Développement du frontend (styles, boutons, sélecteur de micro, badges).
- Validation et commit des modifications pour le sélecteur de micro et la diarisation.
- Amélioration du frontend pour rafraîchir les noms des micros après autorisation d'accès, commit et push sur main.
