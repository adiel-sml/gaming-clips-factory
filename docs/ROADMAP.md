# Roadmap

## Phase 1 : MVP ✅ (livré)
Vidéo locale ou URL autorisée → transcription → détection des moments → top N → clips avec contexte
→ 9:16 → sous-titres → export MP4. Inclut aussi la CLI complète, l'API REST, la file de tâches,
la déduplication, le contrôle qualité et les tests (dont un test end-to-end).

## Phase 2 : en grande partie livrée
- ✅ Suivi du visage (Haar) et 6 stratégies de cadrage, choix automatique
- ✅ Hooks fondés sur le contenu réel (citation, modèles par catégorie, LLM vérifié)
- ✅ Titres, captions et hashtags
- ✅ Dashboard web, import, cartes de moments, éditeur de clip
- ⏳ Détecteur de visage plus robuste (MediaPipe / YuNet, visages de profil, plusieurs personnes)
- ⏳ Détection automatique de la webcam sans visage (rectangle stable aux bords nets)
- ⏳ Prévisualisation du crop en direct dans l'éditeur (cadre déplaçable sur la vidéo source)

## Phase 3
- Classifieur audio des rires, cris et applaudissements (ex. YAMNet / PANNs) comme signaux supplémentaires
- Chat du stream (export légal ou API officielle) : pics de messages et d'emotes
- Analytics : saisie ou import des performances (vues, rétention), tableau de bord
- Amélioration automatique du scoring : régression des poids sur les performances réelles
  (`metadata.json` contient déjà les composantes)
- Stockage objet S3, file Redis + RQ, PostgreSQL par défaut en production

## Phase 4
- Multi-comptes et multi-niches (presets, jeux et hashtags par compte)
- Publication **uniquement via les API officielles** (YouTube Data API, TikTok Content Posting API,
  Instagram Graph API), lorsque leurs conditions le permettent, avec validation humaine
- Planification et tests A/B de hooks et de titres
