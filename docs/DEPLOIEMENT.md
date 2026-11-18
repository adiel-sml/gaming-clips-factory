# Déploiement : frontend sur Vercel, backend sur un serveur

```
Navigateur ──► Vercel : site Next.js (dossier frontend/)
                 │  appels API directs (jeton d'accès)
                 ▼
             Serveur Docker : API + worker + disque (dossier backend/, docker/backend.Dockerfile)
```

**Pourquoi le backend ne va pas sur Vercel :** les fonctions Vercel sont limitées en durée (quelques
minutes), n'ont pas de disque permanent, acceptent des envois d'environ 4,5 Mo maximum et ne peuvent
pas faire tourner de worker en continu. L'analyse vidéo a besoin de tout ça : FFmpeg, Whisper,
fichiers de plusieurs Go, traitements longs.

---

## 1. Backend (à faire en premier)

Le backend tient dans **un seul conteneur**. L'API traite elle-même la file de tâches grâce au
worker embarqué. Il lui faut :

| Variable | Valeur |
|---|---|
| `GCF_API_TOKEN` | un long secret aléatoire (`openssl rand -hex 32`). **Obligatoire en ligne** : sans lui, n'importe qui peut utiliser ton serveur |
| `GCF_CORS_ORIGINS` | l'URL de ton site Vercel, ex. `https://gaming-clips-factory.vercel.app` (plusieurs URL séparées par des virgules) |
| `GCF_DATA_DIR` / `GCF_OUTPUT_DIR` | dossiers sur le disque persistant, ex. `/data/data` et `/data/output` |
| `ANTHROPIC_API_KEY` | optionnel (LLM) |
| `DATABASE_URL` | optionnel : vide = SQLite sur le disque persistant (suffisant pour démarrer) |

Ressources conseillées : **2 vCPU / 4 Go de RAM minimum**, 20 Go de disque ou plus selon le volume
de vidéos. Sur CPU, compte environ 1 minute de traitement pour 5 à 10 minutes de vidéo (modèle
Whisper `base`).

### Option A : Railway (le plus simple)
1. railway.com → **New Project → Deploy from GitHub repo** → `gaming-clips-factory`.
2. **Settings → Build** : Dockerfile path = `docker/backend.Dockerfile`, root directory = `/` (racine).
3. **Volumes** → ajouter un volume monté sur `/data`.
4. **Variables** : celles du tableau ci-dessus (`GCF_DATA_DIR=/data/data`, `GCF_OUTPUT_DIR=/data/output`).
5. **Networking → Generate Domain** → note l'URL, ex. `https://gcf-api.up.railway.app`.
6. Vérifie : `https://…/health` doit répondre `{"status":"ok"}`.

### Option B : VPS (le moins cher pour la puissance, ex. Hetzner, OVH)
```bash
# sur le serveur (Ubuntu), avec Docker installé
git clone https://github.com/adiel-sml/gaming-clips-factory && cd gaming-clips-factory
cp .env.example .env    # remplir GCF_API_TOKEN, GCF_CORS_ORIGINS…
docker compose up -d --build api worker
```
Mets ensuite un reverse proxy HTTPS devant le port 8000 (Caddy : `ton-domaine.fr { reverse_proxy localhost:8000 }`).
Le site Vercel est en HTTPS : l'API doit l'être aussi, sinon le navigateur bloque les appels.

## 2. Frontend sur Vercel
1. vercel.com → **Add New → Project** → importer `gaming-clips-factory`.
2. **Root Directory** : `frontend` (Vercel détecte Next.js automatiquement).
3. **Environment Variables** : `NEXT_PUBLIC_API_URL` = l'URL du backend (étape 1.5), sans `/` final.
4. **Deploy**. À la première visite, le site demande le jeton d'accès (`GCF_API_TOKEN`). Il est
   gardé dans ton navigateur, jamais dans le code.
5. Si ton URL Vercel change (domaine perso), mets à jour `GCF_CORS_ORIGINS` côté backend.

## 3. Vérifications
- `https://<backend>/health` → `ok` ; `https://<backend>/stats` sans jeton → `401` (protégé).
- Le site affiche l'écran « Accès », puis le dashboard une fois le jeton saisi.
- Importe une petite vidéo par l'interface (« fichier vidéo ») et lance l'analyse.

## Limites à connaître
- L'import d'URL YouTube depuis un serveur cloud est souvent refusé par YouTube. Envoie plutôt
  tes fichiers.
- L'envoi de très gros fichiers (plusieurs Go) par le navigateur dépend de l'hébergeur (délais,
  taille max). Pour les longs streams, la CLI sur le serveur est plus fiable.
- Le jeton unique protège un usage personnel. Pour plusieurs utilisateurs, il faudra de vrais
  comptes (phase 4).
