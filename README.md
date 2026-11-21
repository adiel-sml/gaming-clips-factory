# GamingClips Factory

Transforme automatiquement des vidéos et streams gaming **dont vous avez les droits** en clips verticaux
(9:16, 1080×1920) prêts pour TikTok, YouTube Shorts et Instagram Reels : détection des moments forts,
cadrage intelligent, sous-titres dynamiques, hook, titres, hashtags, contrôle qualité et export.

```
URL / fichier → import → analyse audio/vidéo → transcription → scoring des moments → top N
→ bornes avec contexte → déduplication → cadrage 9:16 → sous-titres + hook → titres/hashtags
→ contrôle qualité → export MP4 + metadata.json + .srt
```

> **Cadre légal.** Le système ne traite que des contenus autorisés : il exige une confirmation des
> droits à l'import, n'accepte que des domaines listés, et télécharge via yt-dlp **sans** cookies ni
> contournement de protection (DRM, connexion, âge). Si une plateforme refuse, il s'arrête. Il ne publie
> rien automatiquement. La publication (phase 4) ne passera que par les API officielles.

---

## Sommaire
1. [Installation](#1-installation)
2. [Configuration](#2-configuration)
3. [Variables d'environnement](#3-variables-denvironnement)
4. [Installation FFmpeg](#4-installation-ffmpeg)
5. [Installation GPU](#5-installation-gpu)
6. [Lancer le backend](#6-lancer-le-backend)
7. [Lancer le frontend](#7-lancer-le-frontend)
8. [Lancer le worker](#8-lancer-le-worker)
9. [Utilisation CLI](#9-utilisation-cli)
10. [Utilisation API](#10-utilisation-api)
11. [Pipeline complet](#11-pipeline-complet)
12. [Tests](#12-tests)
13. [Troubleshooting](#13-troubleshooting)
14. [Architecture](#14-architecture)

---

## 1. Installation

Prérequis : Python ≥ 3.10, FFmpeg ≥ 5 (avec libass), Node.js ≥ 20 (pour l'interface web).

```bash
git clone <votre-repo> gaming-clips-factory && cd gaming-clips-factory
bash scripts/setup.sh            # venv + dépendances Python + npm install + .env
source .venv/bin/activate
```

Installation manuelle équivalente :

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev,url,llm]"     # url = yt-dlp, llm = SDK Anthropic (optionnels)
cp .env.example .env
cd frontend && npm install
```

Avec Docker (API + worker + interface) :

```bash
cp .env.example .env
docker compose up --build        # UI http://localhost:3000 · API http://localhost:8000/docs
```

> Le `docker-compose.yml` a été validé (`docker compose config`). En revanche, les images n'ont pas pu
> être construites dans l'environnement de développement, faute de démon Docker. Testez-le chez vous
> avant de vous appuyer dessus.

Au premier lancement, le modèle Whisper choisi est téléchargé depuis Hugging Face (≈ 150 Mo pour
`base`, ≈ 3 Go pour `large-v3`).

## 2. Configuration

Tout ce qui peut changer est dans `config/`. Aucune modification de code n'est nécessaire.

| Fichier | Contenu |
|---|---|
| `config/config.yaml` | durées des clips, poids du scoring, seuils, contexte avant/après, cadrage, variantes, encodage, LLM, file d'attente |
| `config/subtitle_presets.yaml` | presets de sous-titres (`clean`, `gaming`, `high_energy`…) : police, tailles, couleurs, mot surligné, animation, position |
| `config/games.json` | jeux détectés (GTA 6, Fortnite…) : mots-clés, variantes de transcription, hashtags |

Exemples :

```yaml
# config.yaml
video:   {min_clip_duration: 15, max_clip_duration: 60, clips_per_video: 10}
scoring:
  aggressiveness: normal           # conservative | normal | aggressive
  weights: {surprise: 2.0, action_intensity: 1.8, emotional_intensity: 1.5, humor: 1.2}
  context_before: [3, 10]          # secondes de contexte avant l'événement (min, max)
  context_after: [2, 8]
composition: {layout: auto}        # auto | gameplay | streamer | split | webcam_overlay | tracking | blur_fit
variants:
  count: 2
  definitions:
    - {name: main}
    - {name: alt, subtitle_style: high_energy, layout: blur_fit, hook_index: 1}
output: {resolution: 1080x1920, fps: auto, video_codec: auto}   # auto = nvenc si GPU
```

Ajouter un jeu : ajoutez un bloc dans `games.json`.

```json
"rocketleague": {"name": "Rocket League", "keywords": ["Rocket League", "RL"], "hashtags": ["#rocketleague"]}
```

Ajouter un style de sous-titres : ajoutez un bloc dans `subtitle_presets.yaml`. Il apparaît
automatiquement dans l'API, la CLI et l'interface.

## 3. Variables d'environnement

| Variable | Rôle |
|---|---|
| `GCF_API_TOKEN` | jeton exigé par l'API (en-tête `Authorization: Bearer …` ou `?token=`). **Obligatoire en ligne** ; vide = API ouverte (usage local) |
| `GCF_CORS_ORIGINS` | sites autorisés à appeler l'API, séparés par des virgules (ex. ton URL Vercel) |
| `ANTHROPIC_API_KEY` | active le LLM (optionnel) pour affiner le scoring, les hooks, titres et captions. Sans clé : règles uniquement |
| `DATABASE_URL` | vide = SQLite `data/gcf.db` ; ex. `postgresql+psycopg://user:pass@host/db` (installer `backend[postgres]`) |
| `GCF_DATA_DIR`, `GCF_OUTPUT_DIR` | dossiers de travail et de sortie (défaut : `./data`, `./output`) |
| `GCF_CONFIG`, `GCF_CONFIG_DIR` | autre fichier/dossier de configuration |
| `GCF__SECTION__CLE` | surcharge n'importe quelle clé de `config.yaml`, ex. `GCF__TRANSCRIPTION__MODEL=small` |
| `HF_TOKEN` | téléchargements Hugging Face plus rapides |
| `NEXT_PUBLIC_API_URL` | URL de l'API vue par le navigateur (défaut `http://localhost:8000`) |

## 4. Installation FFmpeg

FFmpeg fait tout le travail lourd : décodage, découpe, crop, incrustation des sous-titres (libass),
encodage H.264/AAC.

```bash
sudo apt install ffmpeg fonts-inter    # Debian / Ubuntu
brew install ffmpeg                    # macOS
winget install Gyan.FFmpeg             # Windows
ffmpeg -filters | grep -E " ass | subtitles "   # doit afficher les filtres ass/subtitles
```

La police **Inter** donne le rendu prévu des sous-titres. Sans elle, libass utilise une police de
remplacement.

## 5. Installation GPU

Le matériel est détecté automatiquement : `clips hw` affiche le CPU, le GPU, la VRAM, le modèle Whisper
choisi et l'encodeur.

| Matériel | Whisper (`model: auto`) | Encodage |
|---|---|---|
| CPU < 8 cœurs | `base` int8 | libx264 |
| CPU ≥ 8 cœurs | `small` int8 | libx264 |
| GPU < 5 Go | `small` int8_float16 | h264_nvenc |
| GPU 5–10 Go | `medium` float16 | h264_nvenc |
| GPU ≥ 10 Go | `large-v3` float16 | h264_nvenc |

Pour le GPU NVIDIA : pilote récent + CUDA 12 + cuDNN 9 (requis par CTranslate2/faster-whisper), puis
un FFmpeg compilé avec NVENC. En Docker : installez `nvidia-container-toolkit` et décommentez le bloc
`deploy` du service `worker`. Vous pouvez forcer un choix avec `GCF__TRANSCRIPTION__MODEL=large-v3`
ou `GCF__OUTPUT__VIDEO_CODEC=libx264`.

## 6. Lancer le backend

```bash
cd backend
python -m app serve              # ou : clips serve   → http://localhost:8000/docs
```

Par défaut, l'API traite elle-même la file de tâches (`queue.embedded_worker: true`). C'est le mode
MVP « tout-en-un ».

## 7. Lancer le frontend

```bash
cd frontend
npm run dev                      # développement → http://localhost:3000
npm run build && npm start       # production
```

Pages : **Dashboard** (statistiques, meilleurs moments, erreurs) · **Importer** (URL ou fichier,
options) · **Vidéo** (cartes des moments : preview, générer, rejeter, éditer) · **Éditeur de clip**
(début/fin, cadrage, position du crop, style, hook, titre, hashtags, rendu, export, explication du
score) · **Clips** (filtres par statut).

## 8. Lancer le worker

Pour traiter la file dans un processus séparé (production, ou plusieurs workers) :

```bash
GCF__QUEUE__EMBEDDED_WORKER=false clips serve    # API sans traitement
clips worker                                      # un ou plusieurs workers
clips worker --once                               # vide la file puis s'arrête
```

Les tâches sont persistées en base. Après un crash, les tâches « running » trop anciennes sont
remises en file au démarrage du worker. Les échecs transitoires sont retentés
(`queue.max_attempts`).

## 9. Utilisation CLI

Depuis `backend/` : `python -m app <commande>`, ou `clips <commande>` partout une fois le paquet
installé.

```bash
clips hw                                             # matériel et modèles choisis
clips import partie.mp4 --confirm-rights             # importe (dédupliqué par empreinte)
clips import "https://youtu.be/…" --confirm-rights --rights-note "ma chaîne"
clips analyze <video_id|fichier|URL> --confirm-rights [--clips 10 --aggressiveness aggressive]
clips highlights <video_id> [--json]                 # meilleurs moments
clips generate <highlight_id> [--variant main]       # rendu des variantes
clips pipeline partie.mp4 --confirm-rights --top 5   # tout en une commande
clips pipeline "https://youtu.be/…" --confirm-rights --from 6:00:00 --to 7:00:00
                                                     # long stream : seule la plage 6h-7h est téléchargée
clips batch videos.txt --confirm-rights [--now]      # lot : « source [début] [fin] » par ligne
clips export <clip_id> [--force]                     # → output/ready/
clips clips --status needs_review                    # liste des clips
clips status                                         # vue d'ensemble
clips worker | clips serve
```

Exemple de sortie de `clips analyze` :

```
#1  hl_277d265c15  score= 88.6  00:00:45.49 → 00:01:01.71 (16s)  [funny] conf=0.97
     Moment drôle : pic sonore soudain (~+10 dB) ; réaction verbale « ha ha ha ha », « mort de rire » ; forte action à l'écran ; rires détectés.
```

## 10. Utilisation API

Documentation interactive générée automatiquement : `http://localhost:8000/docs` (Swagger) et `/redoc`.

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/videos` | import URL/chemin + options ; `start`/`end` (ex. `"6:00:00"`, `"7h"`) pour un extrait ; `auto`: `none`/`analyze`/`pipeline` |
| POST | `/videos/upload` | envoi d'un fichier (multipart) |
| GET | `/videos`, `/videos/{id}` | liste / détail (+ nb de moments, de clips, dernière tâche) |
| GET | `/videos/{id}/source` | vidéo source (preview) |
| POST | `/videos/{id}/analyze` | analyse (file d'attente, ou `?sync=true`) |
| POST | `/videos/{id}/pipeline` | pipeline complet |
| GET | `/videos/{id}/highlights` | moments triés par score |
| POST | `/highlights/{id}/generate` | génère les clips du moment |
| POST | `/highlights/{id}/reject` | rejette un moment |
| GET | `/clips`, `/clips/{id}` | liste (filtres `status`, `video_id`) / détail |
| PATCH | `/clips/{id}` | édite bornes, cadrage, style, hook, titre, statut, performances |
| POST | `/clips/{id}/render` | re-rendu après édition |
| POST | `/clips/{id}/export` | export vers `output/ready/` (`force` pour un clip `needs_review`) |
| GET | `/jobs`, `/jobs/{id}` | suivi des tâches |
| GET | `/stats`, `/system`, `/health` | dashboard, presets/layouts/jeux, santé |

```bash
curl -X POST localhost:8000/videos -H 'Content-Type: application/json' \
  -d '{"source": "/videos/partie.mp4", "rights_confirmed": true, "options": {"clips_per_video": 5}, "auto": "pipeline"}'
curl localhost:8000/videos/<id>/highlights
curl -X PATCH localhost:8000/clips/<clip_id> -H 'Content-Type: application/json' \
  -d '{"hook_text": "ATTENDS LA FIN…", "crop": {"x": 0.4}}'
curl -X POST localhost:8000/clips/<clip_id>/render
curl -X POST localhost:8000/clips/<clip_id>/export -d '{}' -H 'Content-Type: application/json'
```

**Liens : analyse légère, HD à la demande** (`ingestion.url_mode: proxy`, par défaut). Pour une URL, on
télécharge une version 360p avec l'audio (environ 10× plus légère), qui sert à l'analyse et à l'aperçu.
Au rendu, seul le passage du clip (avec 5 s de marge) est téléchargé en HD, puis gardé en cache pour les
re-rendus. Si la HD est indisponible, le clip est rendu depuis la version légère, avec un avertissement.
`url_mode: full` rétablit le téléchargement complet en HD.

**Longs streams : importer un extrait.** Indiquez une plage début/fin (formats `6:00:00`, `6h`, `6h30`,
`90m`, `45s` ou en secondes). Pour une URL, yt-dlp ne télécharge que cette plage. Pour un fichier,
l'extrait est copié sans ré-encodage. Les moments affichent aussi leur heure dans la vidéo d'origine
(`source_start_ts`). Une vidéo plus longue que `ingestion.max_video_duration` (4 h par défaut) sans
plage est refusée avec un message explicite.

## 11. Pipeline complet

| Étape | Service | Ce qui se passe |
|---|---|---|
| 1 | `VideoIngestionService` | droits, domaine autorisé, empreinte (pas de double import), yt-dlp sans contournement |
| 2 | `VideoAnalysisService` | audio 16 kHz ; niveau sonore, mouvement, changements de plan, luminosité, hash perceptuel (pas de 0,5 s). Résultat en cache dans `analysis.json` |
| 3 | `TranscriptionService` | faster-whisper, horodatage au mot, VAD, vocabulaire des jeux. Résultat en cache dans `transcript.json` |
| 4 | `HighlightDetectionService` | signaux audio, vidéo et texte → pics → 9 composantes → `viral_score`, catégorie, raison, confiance |
| 5 | LLM (optionnel) | reclasse les 15 meilleurs candidats ; le score final mêle règles (70 %) et LLM (30 %) |
| 6 | `ClipSelectionService` | contexte dynamique avant/après, découpe sur début/fin de phrase, jamais au milieu d'un mot |
| 7 | `DeduplicationService` | recouvrement temporel, transcription similaire, hash perceptuel → fusion ou `duplicate_of` |
| 8 | `VideoCompositionService` | stratégie de cadrage, suivi du visage ou du mouvement, filtergraph FFmpeg |
| 9 | `SubtitleService` | ASS mot par mot, mots-clés agrandis, style émotionnel, zone sûre |
| 10 | `HookGenerationService` | citation réelle ou modèle par catégorie ; hooks LLM vérifiés (pas de fait inventé) |
| 11 | `MetadataGenerationService` | 3 titres, 3 captions, 5–10 hashtags liés au contenu |
| 12 | `RenderService` | un seul appel FFmpeg : découpe, crop, sous-titres, hook, loudnorm −14 LUFS |
| 13 | `QualityControlService` | durée, résolution, codecs, audio, images noires, visage coupé, texte hors écran, synchro, fichier valide → `ready` ou `needs_review` + raisons |
| 14 | Export | `output/<video>/<clip>/clip.mp4`, `thumbnail.jpg`, `subtitles.ass/.srt`, `metadata.json` ; `output/ready/` pour les clips exportés |

**Score viral.** Chaque composante vaut entre 0 et 1, avec des poids configurables :

```
raw   = Σ poids_i × composante_i / Σ poids_i
score = 100 × (0,55 × raw + 0,45 × moyenne des 3 meilleures composantes)  (+ bonus jeu populaire)
```

Les composantes sont : `emotional_intensity`, `surprise`, `action_intensity`, `humor`,
`context_independence`, `replayability`, `quotability`, `audio_intensity` et `visual_interest`.
La moyenne des meilleures composantes valorise un moment très fort sur un seul axe, comme une
explosion de rire.

**Catégories :** `unexpected_reaction`, `funny`, `rage`, `victory_clutch`, `fail`, `action`, `quote`,
`rare_event`, `tension_release`. Une preuve verbale explicite (« game over », « let's go ») prime sur
l'intensité générique.

**Stratégies de cadrage :**
- `gameplay` : crop vertical qui suit le mouvement, attiré vers le centre (viseur, HUD).
- `split` : webcam en haut, gameplay en bas. Choisi automatiquement si un petit visage stable est détecté.
- `streamer` : webcam dominante (60 %).
- `webcam_overlay` : gameplay plein écran avec la webcam en incrustation.
- `tracking` : suit le visage, pour un streamer plein cadre ou de l'IRL.
- `blur_fit` : image entière sur fond flouté. Ne coupe jamais rien.

La zone webcam peut être forcée dans `composition.webcam_region` ou par clip
(`crop.webcam_region`).

**Métadonnées pour l'analyse des performances.** `metadata.json` contient la source, les bornes, le
score et ses composantes, la raison, la transcription, le cadrage, le hook, les titres, les hashtags,
le résultat du contrôle qualité et un bloc `performance`, que l'on remplit via
`PATCH /clips/{id}` avec `{"performance": {"views": …}}`.

## 12. Tests

```bash
cd backend
pytest                    # 76 tests : lexique, scoring, sélection, contexte, dédup, sous-titres,
                          # 6 cadrages rendus, QC, ingestion, file d'attente, API, LLM simulé, end-to-end,
                          # + vraie transcription faster-whisper (modèle tiny, ignorée si absente)
pytest -m "not slow"      # sans le test qui télécharge un modèle Whisper
python ../scripts/make_test_video.py   # vidéo de test libre de droits (faux stream « GTA 6 » commenté)
clips pipeline ../data/test/test_stream_gta6.mp4 --confirm-rights
```

Le test end-to-end enchaîne vidéo → transcription → highlight → clip → sous-titres → export sur une
vidéo synthétique, et vérifie que l'événement est dans le clip, le contexte avant, le contrôle
qualité, les fichiers et les métadonnées.

## 13. Troubleshooting

| Symptôme | Cause / solution |
|---|---|
| `FFmpeg/FFprobe introuvables` | installer FFmpeg (§4) et vérifier le `PATH` |
| `No such filter: 'ass'` | FFmpeg sans libass : installez la version de votre distribution ou un build « full » |
| Sous-titres dans une autre police | installer Inter (`fonts-inter`) ou changer `font` dans le preset |
| Emojis absents des incrustations | libass ne rend pas les emojis couleur : ils sont retirés de l'image mais conservés dans titres et captions |
| `Téléchargement impossible` (YouTube) | la plateforme exige une connexion ou bloque l'accès ; le système ne contourne pas. Importez votre fichier source (ex. export YouTube Studio) |
| `Confirmez que vous avez le droit…` | ajouter `--confirm-rights` (CLI) ou `rights_confirmed: true` (API) |
| Transcription lente | modèle plus petit (`GCF__TRANSCRIPTION__MODEL=base`) ou GPU (§5) |
| Mauvais mots (ex. « GT6 ») | ajouter la variante dans `games.json` (`keywords`, `asr_hints`) ou utiliser un modèle plus grand |
| Peu ou pas de moments | `aggressiveness: aggressive`, baisser `scoring.min_score`, vérifier qu'il y a de la voix |
| Clip `needs_review` | voir `qc.reasons` (API/UI) ; corriger dans l'éditeur, puis re-rendre, ou exporter avec `force` |
| Mauvais cadrage webcam | choisir `layout: split` et définir `crop.webcam_region: [x, y, w, h]` (valeurs 0-1) |
| `database is locked` | SQLite avec plusieurs workers : passer à PostgreSQL (`DATABASE_URL`) |
| L'UI affiche « API injoignable » | lancer l'API et vérifier `NEXT_PUBLIC_API_URL` et `api.cors_origins` |

## 14. Architecture

Mise en ligne (frontend sur Vercel, backend sur Railway ou un VPS) : [`docs/DEPLOIEMENT.md`](docs/DEPLOIEMENT.md).

Voir [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) pour les choix techniques et
[`docs/ROADMAP.md`](docs/ROADMAP.md) pour les phases suivantes.

```
gaming-clips-factory/
├── backend/
│   ├── app/
│   │   ├── api/            FastAPI (main.py, schemas.py)
│   │   ├── core/           config, base de données, détection matérielle, utilitaires
│   │   ├── models/         entités SQLModel : Video, Highlight, Clip, Job
│   │   ├── video/          wrappers FFmpeg, VideoAnalysisService
│   │   ├── transcription/  interface Transcriber + faster-whisper
│   │   ├── scoring/        lexique FR/EN, détection de jeu, HighlightDetectionService
│   │   ├── clips/          sélection + contexte, déduplication, suivi visage, composition 9:16
│   │   ├── subtitles/      SubtitleService (ASS/SRT)
│   │   ├── captions/       LLM optionnel, hooks, titres/captions/hashtags
│   │   ├── services/       ingestion, pipeline, rendu, contrôle qualité
│   │   ├── workers/        file de tâches persistée + worker
│   │   └── cli.py          CLI Typer (python -m app / clips)
│   └── tests/
├── frontend/               Next.js + TypeScript (app/, components/, hooks/, lib/)
├── config/                 config.yaml, subtitle_presets.yaml, games.json
├── docker/                 Dockerfiles backend et frontend
├── scripts/                setup.sh, make_test_video.py, videos.example.txt
├── docs/                   architecture et roadmap
├── data/                   (généré) vidéos, caches d'analyse, base SQLite, logs
└── output/                 (généré) clips rendus et exports
```
