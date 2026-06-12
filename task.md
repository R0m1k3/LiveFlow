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
- [x] Rendre configurable le nombre maximal de locuteurs (DIARIZATION_MAX_SPEAKERS, défaut 8) dans main.py, .env.example et docker-compose.yml
- [x] Créer l'afficheur de volume micro en temps réel (visualiseur audio) dans index.html, style.css et app.js
- [x] Mettre en place la capture et l'affichage des erreurs JS non gérées à l'écran pour faciliter le diagnostic
- [x] Modifier `app/static/index.html` pour intégrer le bouton de pause
- [x] Modifier `app/static/style.css` pour styliser le bouton de pause
- [x] Modifier `app/static/app.js` pour gérer l'état de pause (timer, flux audio et boutons)
- [x] Modifier `app/main.py` pour traiter les commandes WebSocket `pause` et `resume` (flush VAD)
- [x] Résoudre le problème d'apparition des sigles chinois en forçant le français (ASR_LANGUAGE=fr) par défaut dans le code, le docker-compose et l'environnement.

## Progress Log
- Création du module `diarizer.py` pour l'extraction des embeddings et le clustering.
- Modification de `segmenter.py` pour stocker le locuteur.
- Mise à jour de `main.py` pour la gestion de la session de diarisation, l'API et la persistance en base de données.
- Développement du frontend (styles, boutons, sélecteur de micro, badges).
- Validation et commit des modifications pour le sélecteur de micro et la diarisation.
- Amélioration du frontend pour rafraîchir les noms des micros après autorisation d'accès, commit et push sur main.
- Correction de l'affichage de la liste des micros : demande initiale d'autorisation au chargement de la page et filtrage des périphériques sans identifiant.
- Ajout de la variable d'environnement DIARIZATION_MAX_SPEAKERS (défaut 8) et intégration dans l'instanciation de SpeakerDiarizer.
- Résolution des problèmes de synchronisation de la liste des réunions (rechargement systématique dans stopRecording), détection des contextes non sécurisés (affichage d'un bandeau d'erreur si HTTP) et gestion des erreurs de connexion WebSocket.
- Sécurisation globale de startRecording() et stopRecording() (bloc try/catch global, nettoyage complet et guards pour éléments null) et changement de contrainte de micro de 'exact' à 'ideal' pour éviter de bloquer la captation.
- Création d'un indicateur visuel de volume micro en temps réel calculé par RMS.
- Intégration d'un système de capture d'erreurs JavaScript globales affichées à l'écran pour le diagnostic.
- Ajout de paramètres de version (cache-busting ?v=2) dans index.html pour forcer le rechargement de style.css et app.js par le navigateur.



