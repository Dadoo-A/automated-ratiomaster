# ratiomaster.py — émulateur d'announce BitTorrent

> Outil destiné uniquement à tester **votre propre tracker privé** (infrastructure personnelle).

Script Python 3 autonome (bibliothèque standard uniquement, aucun `pip install`) qui remplace
RatioMaster.NET : il annonce un torrent à un tracker HTTP/HTTPS comme le ferait un vrai
client (qBittorrent, uTorrent, Transmission), sans transférer de données.

Trois modes : **manuel** (un `.torrent` en argument), **dossier surveillé** (`watch\`) et
**source qBittorrent** (lit les torrents de la stack *ARR via l'API WebUI, idéal en conteneur Docker).

## Démarrage rapide

Prérequis : Python 3.10 ou plus récent.

```powershell
python ratiomaster.py "C:\chemin\mon.torrent"
```

Utilise les valeurs de `config.ini` (par défaut : qBittorrent 4.6.5, 500 KiB/s up, 2000 KiB/s down).
`Ctrl+C` arrête proprement la session (envoi de `event=stopped` au tracker).

Pour vérifier sans rien envoyer (affiche les URLs d'announce, temps simulé) :

```powershell
python ratiomaster.py "C:\chemin\mon.torrent" --dry-run
```

## Fonctionnement

1. Lecture du `.torrent` : `announce` + `announce-list` (tous les tiers), `info_hash`
   (SHA1 du dictionnaire `info`), taille totale.
2. Announce `started` → announces périodiques en respectant l'`interval` (et `min interval`)
   renvoyé par le tracker → `completed` quand le « téléchargement » atteint 100 % → `stopped`.
3. `uploaded` augmente à la vitesse d'upload, `downloaded` à la vitesse de download jusqu'à la
   taille totale, puis `left=0` (seed).
4. **Durée de session = taille / vitesse de download** (durée upload = durée DL), plus
   `seed_minutes` optionnelles.
   Exemple : 4 GiB à 2000 KiB/s → ~35 min, uploaded final ≈ 500 × 2097 s ≈ 1 GiB.
5. En cas d'échec, les trackers sont essayés dans l'ordre des tiers ; si tous échouent, nouvel
   essai après `retry_seconds`. Seuls les trackers `http://` / `https://` sont gérés (UDP ignoré).

## Mode 1 : manuel (CLI)

```powershell
python ratiomaster.py mon.torrent --up 500 --down 2000 --client qbittorrent
python ratiomaster.py mon.torrent --up 800 --down 3000 --seed-minutes 60 --jitter 10
python ratiomaster.py mon.torrent --seeder --up 300          # seed seul, illimité
python ratiomaster.py --list-clients
```

| Option | Rôle |
|---|---|
| `--up N` / `--down N` | vitesses en KiB/s (`--down 0` = déjà seed) |
| `--client NOM` | `qbittorrent`, `qbittorrent5`, `utorrent`, `transmission` |
| `--port N` | port annoncé (0 = aléatoire) |
| `--numwant N` | nombre de pairs demandés |
| `--seed-minutes N` | seed supplémentaire après la fin du DL |
| `--seeder` | démarre directement en seed (`left=0`) |
| `--jitter N` | variation aléatoire des vitesses (±N %) |
| `--max-interval N` | plafonne l'intervalle entre announces (s) |
| `--insecure` | ne vérifie pas le certificat HTTPS (auto-signé) |
| `--dry-run` | n'envoie rien, affiche les URLs, simule le temps |
| `--config FICHIER` | autre fichier de configuration |
| `--debug` | logs détaillés |

## Mode 2 : dossier surveillé

```powershell
python ratiomaster.py --watch
```

Scanne `watch\` toutes les `poll_seconds` secondes. Chaque nouveau `.torrent` déposé démarre
**sa propre session en parallèle** (un thread par torrent). Quand une session arrive au bout de
sa durée, le fichier est déplacé dans `watch\done\` ; un torrent illisible part dans
`watch\error\` (désactivable : `move_done = false`). `Ctrl+C` envoie `stopped` pour toutes les
sessions actives ; les torrents interrompus restent dans `watch\` et repartent de zéro au
prochain `--watch`. Un torrent rejeté par le tracker (`failure reason`) est réessayé toutes les
`retry_seconds` jusqu'à `Ctrl+C`.
Les options CLI (`--up`, `--client`…) s'appliquent à toutes les sessions du mode watch.

## Mode 3 : source qBittorrent (stack *ARR)

```powershell
python ratiomaster.py --mode qbit --qbit-url http://192.168.1.10:8080 --qbit-user admin --qbit-pass xxx --categories "tv-sonarr,radarr"
```

Interroge l'API WebUI de qBittorrent toutes les `poll_interval` secondes (30 par défaut) et
démarre une session d'émulation pour chaque torrent qui passe le filtre. Pas de `.torrent` à
lire : `info_hash`, nom, taille et URL d'announce viennent de l'API (`torrents/info`, et
`torrents/trackers` si le champ `tracker` est vide).

- **Une seule session par info_hash** (jamais de doublon).
- Torrent **supprimé**, **en pause/arrêté** ou **sorti du filtre** → `event=stopped` et fin de
  la session. Repris plus tard → nouvelle session (repart de zéro).
- Session arrivée au bout de sa durée → pas relancée pour ce torrent tant que l'outil tourne
  (redémarrer le conteneur pour relancer).
- Torrent sans tracker http(s) (DHT seul, UDP seul) → ignoré, réessayé à chaque poll.
- qBittorrent injoignable → nouvel essai avec délai croissant (max 5 min). Session expirée
  (HTTP 403) → reconnexion automatique.
- **L'émulateur annonce comme son propre pair** (peer_id et port à lui), distinct du vrai
  qBittorrent : le tracker voit donc **deux pairs** pour chaque torrent (le vrai qBit + l'émulateur).
  C'est attendu sur votre propre tracker.

**Filtres** (`categories`, `exclude_categories`, `tags`) :

| Valeur de `categories` | Effet |
|---|---|
| vide, `*` ou `all` | tous les torrents |
| `tv-sonarr,radarr,lidarr` | seulement ces catégories |

Les *ARR posent une catégorie sur leurs téléchargements, par défaut `tv-sonarr` (Sonarr),
`radarr` (Radarr), `lidarr` (Lidarr), `readarr` (Readarr) — vérifiez dans
*Settings → Download Clients* de chaque *ARR. Une catégorie configurée mais inconnue de qBit
est signalée dans les logs au démarrage. `exclude_categories` retire des catégories ;
`tags` (si renseigné) exige qu'au moins un des tags soit présent sur le torrent.

## Docker (ajout à la stack *ARR)

Fichiers : `Dockerfile`, `docker-compose.yml`, `.env.example`. Image `python:3.12-alpine`,
aucune dépendance. Dans le conteneur, la configuration passe par **variables d'environnement**
(le mode `qbit` est le mode par défaut de l'image, logs sur la sortie standard).

1. Copier le dossier (au minimum `Dockerfile`, `ratiomaster.py`, `config.ini`) sur la machine
   Docker, par ex. à côté du `docker-compose.yml` de la stack.
2. Copier `.env.example` en `.env` et mettre l'utilisateur / mot de passe WebUI de qBittorrent.
3. **Même réseau que qBittorrent** — c'est ce qui permet d'écrire `http://qbittorrent:8080`
   (le nom du service sert de nom DNS sur un réseau Docker partagé) :
   - *Option A (recommandée)* : coller le bloc `ratiomaster:` de `docker-compose.yml` sous
     `services:` dans le compose de la stack (même fichier = même réseau par défaut), ajuster
     `build:` vers le dossier du Dockerfile, décommenter `depends_on`.
   - *Option B* : garder un compose séparé et rejoindre le réseau existant (bloc `networks`
     en bas du fichier ; nom réel via `docker network ls`).
   - Si qBittorrent passe par un conteneur VPN (gluetun, `network_mode: service:gluetun`), le
     nom à utiliser est celui du service VPN : `QBIT_URL=http://gluetun:8080`.
4. Lancer :

```powershell
docker compose up -d --build ratiomaster
```
```powershell
docker compose logs -f ratiomaster
```

`docker compose stop ratiomaster` envoie `event=stopped` pour toutes les sessions
(`stop_grace_period: 60s` laisse le temps de le faire).

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `MODE` | `qbit` (image) | `cli`, `watch` ou `qbit` |
| `QBIT_URL` | `http://qbittorrent:8080` | URL WebUI de qBittorrent |
| `QBIT_USER` / `QBIT_PASS` | — | identifiants WebUI (vides si qBit autorise le sous-réseau sans login) |
| `POLL_INTERVAL` | `30` | secondes entre deux lectures de l'API |
| `CATEGORIES` | vide (= tout) | catégories à émuler, séparées par des virgules |
| `EXCLUDE_CATEGORIES` | vide | catégories ignorées |
| `TAGS` | vide | au moins un de ces tags requis |
| `CLIENT` | `qbittorrent` | client émulé (`qbittorrent5`, `utorrent`, `transmission`) |
| `UPLOAD_RATE` / `DOWNLOAD_RATE` | `500` / `2000` | vitesses en KiB/s |
| `RATE_JITTER` | `0` | variation aléatoire ± % |
| `SEED_AFTER` | `0` | minutes de seed après la durée de DL |
| `START_AS_SEEDER` | `false` | démarre en seed ; avec `SEED_AFTER=0` → tant que le torrent est actif dans qBit |
| `PORT` / `NUMWANT` | `0` / `200` | port annoncé (0 = aléatoire), pairs demandés |
| `MAX_INTERVAL` | `0` | plafond de l'intervalle d'announce (s) |
| `TIMEOUT` / `RETRY_SECONDS` | `20` / `60` | timeout HTTP, délai de nouvel essai tracker |
| `VERIFY_SSL` | `true` | `false` pour un certificat auto-signé (tracker et WebUI) |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / vide | niveau, fichier de log (vide = stdout) |

Hors Docker, ces mêmes variables marchent aussi (priorité : options CLI > variables
d'environnement > `config.ini`).

### Dépannage qBittorrent

- `login refuse` : identifiants WebUI faux.
- `IP bannie` : trop d'échecs de login ; attendre ou débannir dans qBit (*Options → Web UI*).
- `HTTP 401` / `403` persistants : dans *Options → Web UI*, vérifier la « validation de l'en-tête
  Host » (ajouter `qbittorrent` aux domaines autorisés ou la désactiver) et la protection CSRF.

## config.ini

| Section | Clé | Description |
|---|---|---|
| `[client]` | `client` | client émulé (peer_id + User-Agent + paramètres spécifiques) |
| | `port` | port annoncé, 0 = aléatoire |
| | `numwant` | nombre de pairs demandés |
| `[speed]` | `upload_kib` / `download_kib` | vitesses en KiB/s |
| | `rate_jitter_pct` | variation aléatoire ± % (0 = constant) |
| `[session]` | `seed_minutes` | seed après le DL (0 = arrêt à la fin du DL) |
| | `start_as_seeder` | démarre en seed ; avec `seed_minutes = 0` → illimité |
| | `max_interval` | plafond de l'intervalle entre announces, 0 = celui du tracker |
| `[network]` | `timeout` | timeout HTTP (s) |
| | `verify_ssl` | `false` pour un certificat auto-signé |
| | `retry_seconds` | attente avant nouvel essai si tous les trackers échouent |
| `[watch]` | `watch_dir` | dossier surveillé (relatif au script ou absolu) |
| | `poll_seconds` | fréquence de scan |
| | `move_done` | déplace vers `done\` / `error\` |
| `[log]` | `log_file` | fichier de log (vide = console seule) |
| | `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `[general]` | `mode` | mode par défaut sans argument : `cli`, `watch`, `qbit` |
| `[qbit]` | `url`, `username`, `password` | accès WebUI qBittorrent |
| | `poll_interval` | secondes entre deux lectures de l'API |
| | `categories`, `exclude_categories`, `tags` | filtres (voir mode 3) |

Priorité : option CLI > variables d'environnement > `config.ini` > valeur par défaut du script.

## Logs

Console + `ratiomaster.log` : horodatage, nom du torrent, tracker, event envoyé,
up/down/left courants, interval du tracker, seeders/leechers, erreurs.

```
2026-11-21 16:02:11 INFO    [Mon.Torrent] announce OK t=+0h30m00s tracker=tracker.exemple:443 event=(periodic) up=878.91 MiB down=3.43 GiB left=585.94 MiB interval=1800s seeders=3 leechers=1
```

## Ajouter un client

Dans `ratiomaster.py`, ajouter une entrée au dictionnaire `CLIENTS` (préfixe `peer_id`,
`User-Agent`, style de `key`, paramètres supplémentaires).

## Limites

- **Pas de détection freeleech** : le statut freeleech est une info du site/tracker, il n'est
  pas présent dans le `.torrent`. Le script annonce toujours `downloaded` normalement.
- Trackers UDP non gérés (HTTP/HTTPS uniquement).
- Torrents v2 purs (sans `length`/`files` v1) non gérés ; l'info_hash calculé est le hash v1 (SHA1).
- Le script ne contacte aucun pair et ne transfère aucune donnée : seules les statistiques
  annoncées au tracker changent.
