# LiveFlow — Propositions d'architecture

Application de transcription de réunions en direct, 100 % locale, hébergée dans Docker.

**Besoins couverts :**
- Capturer l'audio en live depuis un micro (PC ou téléphone)
- Transcrire en texte, en local, sans aucun service cloud
- Afficher le texte en temps réel dans une interface web
- Récupérer / exporter la transcription (TXT, Markdown, SRT…)

**Contrainte technique commune importante :** pour accéder au micro depuis un
navigateur (surtout sur téléphone), l'API `getUserMedia` exige du **HTTPS**
(sauf sur `localhost`). Toutes les propositions incluent donc un reverse proxy
TLS (Caddy ou Traefik) avec certificat auto-signé ou `mkcert` sur le réseau local.

---

## Proposition 1 — Monolithe simple (1 conteneur)

> *La plus simple à mettre en place et à maintenir. Idéale pour un usage perso
> ou une petite équipe, sur une machine unique (avec ou sans GPU).*

```
[Navigateur PC / Téléphone]
   │  micro → MediaRecorder / Web Audio
   │  WebSocket (audio chunks PCM/Opus)
   ▼
┌──────────────────────────────────────────┐
│  Conteneur "liveflow"                    │
│  ┌────────────────────────────────────┐  │
│  │ FastAPI (Python)                   │  │
│  │  • sert le frontend (HTML/JS)      │  │
│  │  • WebSocket /transcribe           │  │
│  │  • VAD Silero (découpe la parole)  │  │
│  │  • faster-whisper (ASR embarqué)   │  │
│  │  • SQLite (réunions + segments)    │  │
│  └────────────────────────────────────┘  │
└──────────────────────────────────────────┘
   ▲
Caddy (HTTPS local) — peut être dans le même compose
```

**Stack :** Python / FastAPI · faster-whisper (modèles Whisper `small` → `large-v3`,
CPU ou GPU) · Silero VAD · SQLite · frontend vanilla JS ou Svelte/Vue léger.

**Fonctionnement :** le navigateur capture le micro, envoie l'audio par
WebSocket ; le VAD détecte les fins de phrases ; chaque segment est transcrit
et renvoyé au navigateur (texte qui s'affiche au fil de l'eau) ; tout est
stocké en SQLite ; bouton « Exporter » → TXT/MD/SRT.

| ✅ Avantages | ❌ Limites |
|---|---|
| 1 seul conteneur, un `docker compose up` et c'est fini | Modèle ASR couplé à l'app (changer de moteur = modifier le code) |
| Aucune dépendance réseau, très peu de pièces mobiles | Une seule réunion simultanée confortable (selon machine) |
| Fonctionne sur CPU (modèle `small`/`medium`) | Pas de diarisation (qui parle) |

---

## Proposition 2 — App + moteur ASR séparé (2-3 conteneurs) ⭐ Recommandée

> *Le meilleur compromis simple / puissant / évolutif. On réutilise un conteneur
> de transcription existant exposant une API standard, comme demandé.*

```
[Navigateur PC / Téléphone]
   │ HTTPS + WebSocket
   ▼
┌─────────────┐     ┌──────────────────────────────┐
│ Caddy (TLS) │ ──▶ │ Conteneur "app" (FastAPI)    │
└─────────────┘     │  • frontend + API + WebSocket│
                    │  • VAD + découpage           │
                    │  • SQLite/Postgres           │
                    └──────────┬───────────────────┘
                               │ HTTP (API style OpenAI /v1/audio/transcriptions)
                               ▼
                    ┌──────────────────────────────┐
                    │ Conteneur ASR (interchangeable)│
                    │  ex. Speaches (faster-whisper),│
                    │  whisper.cpp server, ou un     │
                    │  modèle Qwen audio via vLLM    │
                    └──────────────────────────────┘
```

**Stack :** docker-compose à 3 services : `caddy` + `app` + `asr`.
Pour le service ASR, des images Docker prêtes à l'emploi existent :
- **Speaches** (ex *faster-whisper-server*) : API compatible OpenAI, streaming, CPU/GPU
- **whisper.cpp server** : très léger, idéal CPU
- **vLLM / autre runtime** servant un modèle audio de la famille Qwen si vous voulez rester sur Qwen

L'app ne connaît que l'URL `ASR_BASE_URL` : **changer de moteur = changer une
ligne dans le compose**, zéro modification de code.

| ✅ Avantages | ❌ Limites |
|---|---|
| Moteur ASR interchangeable (Whisper aujourd'hui, Qwen demain) | 2-3 conteneurs à orchestrer (reste trivial avec compose) |
| On réutilise des images Docker existantes et maintenues | Légère latence en plus (HTTP entre app et ASR) |
| Le GPU est isolé dans le conteneur ASR ; l'app reste légère | |
| Évolutif : on peut répliquer le service ASR plus tard | |

---

## Proposition 3 — Pipeline temps réel complet (le plus puissant)

> *Pour plusieurs réunions simultanées, plusieurs participants, diarisation
> (« qui a dit quoi ») et résumé automatique par LLM local. Plus de pièces,
> mais chaque besoin avancé est couvert.*

```
[PC / Téléphones des participants]
   │ WebRTC (latence < 200 ms, gestion réseau mobile)
   ▼
┌────────────────────────────────────────────────────────────┐
│ docker-compose                                             │
│                                                            │
│  Traefik/Caddy (TLS) ── LiveKit (SFU WebRTC auto-hébergé)  │
│                              │ pistes audio par participant│
│                              ▼                             │
│  Worker(s) de transcription (Python)                       │
│   • VAD Silero + streaming ASR (faster-whisper/WhisperX)   │
│   • Diarisation pyannote OU 1 piste = 1 participant via    │
│     WebRTC (diarisation "gratuite")                        │
│                              │                             │
│  Redis (pub/sub live) ── Postgres (réunions, segments)     │
│                              │                             │
│  Ollama (Qwen3 / LLM local) ◀ résumé, comptes-rendus,      │
│                               points d'action en fin de    │
│                               réunion                      │
│                              │                             │
│  Frontend (React/Svelte) : texte live par locuteur,        │
│  historique, recherche, export TXT/MD/SRT/JSON             │
└────────────────────────────────────────────────────────────┘
```

**Stack :** LiveKit (self-hosted) · workers Python (faster-whisper/WhisperX +
pyannote) · Redis · Postgres · Ollama avec Qwen3 pour le résumé · frontend SPA.

| ✅ Avantages | ❌ Limites |
|---|---|
| Multi-réunions, multi-participants, latence minimale | 6-7 conteneurs : nettement plus complexe à opérer |
| Diarisation : transcription attribuée à chaque locuteur | GPU quasi indispensable (ASR + diarisation + LLM) |
| Résumé / compte-rendu automatique par LLM local (Qwen3) | Surdimensionné pour un usage solo |
| Toujours 100 % local | |

---

## Comparatif et recommandation

| | P1 Monolithe | P2 App + ASR ⭐ | P3 Pipeline complet |
|---|---|---|---|
| Conteneurs | 1 (+TLS) | 3 | 6-7 |
| Complexité | ★ | ★★ | ★★★★ |
| Réutilise des images ASR existantes | Non | **Oui** | Oui |
| Moteur interchangeable (Whisper ↔ Qwen…) | Non | **Oui** | Oui |
| Diarisation (qui parle) | Non | Optionnelle plus tard | Oui |
| Résumé LLM (Qwen3 via Ollama) | Non | Facile à ajouter | Oui |
| GPU requis | Non (CPU ok) | Non (CPU ok) | Recommandé |
| Multi-réunions simultanées | Limité | Moyen | Oui |

**Recommandation : Proposition 2.** Elle reste « simple, efficace et
puissante » : un compose de 3 services, l'interface fait tout
(transcrire → afficher → exporter), et elle répond exactement à votre idée de
« dépendre de dockers qui font déjà de la transcription » — le moteur ASR est
un conteneur sur étagère qu'on peut remplacer à tout moment. Le jour où il
faut la diarisation ou le résumé automatique, on ajoute un service au compose
et on migre en douceur vers la proposition 3, sans rien jeter.
