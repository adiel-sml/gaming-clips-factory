# Architecture et choix techniques

## Principes

1. **Un MVP qui fonctionne réellement** plutôt qu'une architecture impossible à tester. Chaque service
   est une classe autonome, testée indépendamment, que l'on peut remplacer derrière une interface.
2. **Hybride, pas « tout LLM ».** Le traitement lourd passe par FFmpeg, OpenCV et numpy. La
   transcription passe par Whisper. Le scoring passe par des règles explicables. Le LLM est
   optionnel et n'intervient que là où il apporte de la valeur : compréhension du contexte,
   classification fine, hooks et captions.
3. **Idempotent et reprenable.** Chaque étape coûteuse est mise en cache sur disque
   (`analysis.json`, `transcript.json`). Relancer une analyse après une erreur repart du cache, et
   les tâches sont persistées en base.
4. **Tout ce qui change est configurable** (`config/*.yaml|json`, surcharge par variables
   d'environnement).

## Stack retenue

| Besoin | Choix | Pourquoi | Remplaçable par |
|---|---|---|---|
| API | FastAPI | typage, OpenAPI auto (/docs), simple | — |
| Données | SQLModel / SQLAlchemy, SQLite | zéro installation pour le MVP ; `DATABASE_URL` pour PostgreSQL | PostgreSQL |
| File de tâches | table `job` + worker (polling, claim atomique) | pas de Redis à installer ; reprise après crash, retries | Redis + RQ/Celery (seule `JobQueue` change) |
| Vidéo | FFmpeg (subprocess) | référence de l'industrie ; crop dynamique via expressions, libass, loudnorm, NVENC | — |
| Signaux visuels | décodage FFmpeg basse résolution (160 px, 4 i/s) + numpy | ~2 s pour 2 min de vidéo | PySceneDetect, modèles vidéo |
| Visage | YuNet (OpenCV, modèle de 230 Ko inclus, licence MIT) ; repli Haar | fiable sur les petites webcams, CPU | MediaPipe, YOLO (interface `FaceDetector`) |
| Transcription | faster-whisper (CTranslate2) | 4× plus rapide que Whisper, int8 CPU, GPU auto, mots horodatés | API Whisper, Deepgram… (interface `Transcriber`) |
| LLM | SDK Anthropic, sortie JSON contrainte par schéma | fiable à parser ; repli automatique sur les règles | autre fournisseur (interface `LLMProvider`) |
| Frontend | Next.js + TypeScript, CSS sans framework | léger, aucune dépendance UI | — |
| Stockage | système de fichiers (`data/`, `output/`) | MVP | stockage objet S3 (prochaine étape) |

## Détection des moments : détail

Signaux calculés tous les 0,5 s :

- **Audio** : niveau RMS normalisé de façon robuste (médiane/MAD), pic soudain par rapport aux 8 s
  précédentes, et détecteur « silence puis explosion » (base calme de −6 dB sous la médiane, puis
  saut de plus de 12 dB).
- **Vidéo** : intensité de mouvement, densité de changements de plan, variabilité du mouvement,
  centre horizontal du mouvement (utilisé ensuite pour le cadrage).
- **Texte** : lexique FR/EN normalisé (surprise, humour, colère, victoire, défaite, action, rareté),
  exclamations, répétitions (« non non non »), débit de parole, mentions de jeux.

Les indices textuels sont étalés par un noyau gaussien et additionnés : plusieurs indices proches
donnent un signal plus fort. Une courbe d'activation lissée sur 2 s fournit les pics, séparés d'au
moins `peak_min_distance`. Autour de chaque pic, on calcule le début de la montée (onset) et la fin
de la réaction, puis les 9 composantes, le score, la catégorie, une raison lisible construite à
partir des preuves, et une confiance qui dépend de l'accord entre modalités (audio, texte, image).

## Contexte du clip

- **Début** : le début naturel le plus tardif (début de phrase, pause de plus de 0,55 s ou
  changement de plan) situé entre `onset − 10 s` et `onset − 3 s`. À défaut, on recule plus si
  quelqu'un parle avant l'événement.
- **Fin** : la première fin de phrase ou pause entre `fin de réaction + 2 s` et `+ 8 s`.
- Les durées min/max sont imposées en s'appuyant d'abord sur les débuts de phrase. Le résultat n'est
  jamais coupé au milieu d'un mot.

## Rendu

Un seul appel FFmpeg par clip : `-ss/-t` (découpe), filtergraph de composition (crop dont la
position est une expression linéaire par morceaux de `t`, pour un suivi fluide sans ré-encodage
intermédiaire), `ass` (sous-titres et hook), `fps`, `loudnorm` (−14 LUFS), puis libx264 ou NVENC,
AAC 48 kHz et `+faststart`. Le graphe est passé directement à `-filter_complex` (sans shell, donc sans souci d'échappement). L'option
`-filter_complex_script`, supprimée dans FFmpeg 9, n'est pas utilisée ; un graphe géant passe par
`-/filter_complex fichier`.

## Facecam et effets

- **Visage du streamer** : YuNet sur 10 images du passage. On garde le visage qui revient au même
  endroit (les personnages du jeu bougent, la webcam non).
- **Cadre de la webcam** : on mesure la variation de chaque pixel sur 8 images. La zone immobile d'un
  seul tenant qui contient le visage est la webcam. Le split écran utilise ce cadre exact, avec une
  estimation autour du visage en repli.
- **Effets** (bloc `effects` de la config) : zoom avant avec tremblement et flash au moment fort,
  plus une barre de progression en haut. Ce sont des expressions FFmpeg évaluées image par image
  dans le même encodage (environ 10 % de temps de rendu en plus).

## Purge automatique

`retention.hours` (48 h par défaut) : une vidéo sans activité depuis ce délai est supprimée avec ses
extraits, son analyse, ses clips et ses fiches en base. Le worker vérifie toutes les heures. Les
fichiers en cache trop vieux (`data/sources`, `data/uploads`, `output/ready`) sont aussi effacés.
Rien n'est touché pendant un traitement. Commande manuelle : `clips purge --dry-run`.

## Modèle de données

`Video` 1─n `Highlight` 1─n `Clip` (une ligne par variante) ; `Job` pour la file. Les champs
JSON stockent les composantes du score, les signaux, le résultat du contrôle qualité, le plan de
cadrage et le bloc `performance` (vues, likes…) pour la boucle d'amélioration du scoring (phase 3).

## Limites connues du MVP

- La détection de rires repose sur la transcription (« haha », « mdr »). Il n'y a pas encore de
  classifieur audio : voir la roadmap.
- La webcam est retrouvée à partir du visage du streamer (visage qui reste au même endroit, puis
  zone immobile autour de lui). Une webcam sans visage visible n'est pas détectée : on peut fixer la
  zone dans la configuration ou par clip.
- Les emojis en couleur ne sont pas possibles dans les sous-titres (libass ne gère pas les polices
  emoji couleur).
- La file en base convient à quelques workers. Au-delà, il faut passer à Redis + RQ.
- Le chat du stream n'est pas encore exploité. Il faudra l'importer légalement, via les API
  officielles ou un fichier fourni.
