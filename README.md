# 🎙️ LiveFlow

Transcription de réunions **en direct**, **100 % locale**, dans Docker.
Capture du micro depuis un navigateur (PC ou téléphone), transcription par
**Qwen3-ASR** sur GPU NVIDIA, affichage du texte au fil de l'eau, historique
et export (TXT, Markdown, SRT, JSON).

Architecture détaillée : voir [ARCHITECTURE.md](ARCHITECTURE.md) (proposition 2 retenue,
sans reverse proxy : l'app sert directement le HTTPS).

```
Navigateur (micro, HTTPS/WebSocket)
   → app FastAPI (HTTPS auto-signé + VAD + segments + SQLite + interface web)
   → conteneur ASR Qwen3-ASR via vLLM (API compatible OpenAI)
```

## Prérequis

- Docker + Docker Compose
- GPU NVIDIA avec pilotes récents et le
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  (`nvidia-ctk runtime configure --runtime=docker`)
- ~6 Go de VRAM libres pour Qwen3-ASR-1.7B (ajuster `--gpu-memory-utilization`)

## Démarrage

```bash
docker compose up -d --build
```

Au premier lancement, le conteneur `asr` télécharge le modèle depuis
Hugging Face (quelques Go, mis en cache dans un volume). Suivre avec :

```bash
docker compose logs -f asr
```

Puis ouvrir **https://\<ip-du-serveur\>:8443** depuis un PC ou un téléphone du
réseau local, accepter l'avertissement de certificat, autoriser le micro, et
cliquer sur **Démarrer**.

> ⚠️ Le HTTPS est obligatoire : les navigateurs n'autorisent l'accès au micro
> (`getUserMedia`) qu'en HTTPS (ou sur `localhost`). L'app génère elle-même un
> certificat auto-signé au premier démarrage pour l'adresse `LIVEFLOW_HOST`
> (stocké dans le volume de données ; supprimez `data/certs/` pour le
> régénérer après un changement d'adresse).

## Unraid

Un compose dédié est fourni : [`docker-compose.unraid.yml`](docker-compose.unraid.yml)
(image préconstruite `ghcr.io/r0m1k3/liveflow`, GPU via `runtime: nvidia`,
port 8443). Prérequis : plugins **Nvidia Driver** et **Docker Compose Manager**.
Renseigner `LIVEFLOW_HOST` (IP du serveur) dans le compose, créer une stack
pointant sur ce fichier, puis ouvrir `https://<ip-unraid>:8443`.

## Mises à jour

À chaque push sur GitHub, une GitHub Action construit l'image de l'app et la
publie sur `ghcr.io/r0m1k3/liveflow:latest`. Pour mettre à jour l'app :

```bash
docker compose -f docker-compose.unraid.yml pull app && \
docker compose -f docker-compose.unraid.yml up -d app
```

> Première utilisation : le paquet ghcr.io doit être **public** (GitHub →
> page du dépôt → Packages → liveflow → Package settings → Change visibility).

## Authentification

L'interface est protégée par un identifiant/mot de passe (**admin / admin**
par défaut), définis par les variables `LIVEFLOW_USER` et `LIVEFLOW_PASSWORD`
du compose. La session dure 7 jours (cookie signé). **Changez le mot de passe
par défaut si l'application est accessible depuis internet.**

## Configuration

Variables d'environnement (fichier `.env` à la racine, voir `.env.example`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `ASR_LANGUAGE` | *(vide)* | Code langue forcé (`fr`, `en`…) ; vide = détection auto |
| `ASR_API_KEY` | `sk-local` | Clé envoyée au moteur ASR (inutile en local) |
| `HF_TOKEN` | *(vide)* | Jeton Hugging Face si nécessaire au téléchargement |

## Changer de moteur de transcription

L'app ne parle au moteur que via l'API OpenAI (`/v1/audio/transcriptions`).
Pour changer de moteur, remplacer le service `asr` dans `docker-compose.yml`
et ajuster `ASR_MODEL`. Exemples :

```yaml
# CPU uniquement — Parakeet-TDT-0.6B-v3 (rapide, 25 langues dont FR)
asr:
  image: ghcr.io/groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai:latest

# Écosystème Whisper — Speaches (faster-whisper, CPU/GPU)
asr:
  image: ghcr.io/speaches-ai/speaches:latest-cuda
```

## Récupérer une transcription

Depuis l'interface (boutons TXT / MD / SRT / JSON / Copier) ou en direct via l'API :

```bash
curl -k https://<ip>/api/meetings                      # liste des réunions
curl -k https://<ip>/api/meetings/1/export?format=txt  # export texte
```
