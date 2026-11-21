# ratiomaster.py — BitTorrent announce emulator

> A tool meant only for testing **your own private tracker** (personal infrastructure).

## ⚠️ Disclaimer & legal notice

- This software is provided **"AS IS", without warranty of any kind**, and **without liability** on the author's part — see [LICENSE](LICENSE) (MIT).
- It is intended **exclusively** for testing **your own tracker / your own infrastructure**, for which you are authorized.
- Faking the statistics (ratio, upload) of a **third-party tracker** without permission **violates its terms of service** and may constitute **fraud, which is illegal** depending on your jurisdiction.
- **You are solely responsible** for how you use it. The author and contributors accept no liability for any damage or consequence.
- This repository **does not redistribute** the third-party `RatioMaster.NET.exe` binary.

*This is not legal advice.*

A standalone Python 3 script (standard library only, no `pip install`) that replaces
RatioMaster.NET: it announces a torrent to an HTTP/HTTPS tracker the way a real client
would (qBittorrent, uTorrent, Transmission), without transferring any data.

Three modes: **manual** (a `.torrent` passed as an argument), **watched folder** (`watch\`) and
**qBittorrent source** (reads torrents from an *ARR stack through the WebUI API, ideal in a Docker container).

## Quick start

Requirement: Python 3.10 or newer.

```powershell
python ratiomaster.py "C:\path\my.torrent"
```

Uses the values from `config.ini` (defaults: qBittorrent 4.6.5, 500 KiB/s up, 2000 KiB/s down).
`Ctrl+C` stops the session cleanly (sends `event=stopped` to the tracker).

To check without sending anything (prints the announce URLs, simulated time):

```powershell
python ratiomaster.py "C:\path\my.torrent" --dry-run
```

## How it works

1. Reads the `.torrent`: `announce` + `announce-list` (every tier), `info_hash`
   (SHA1 of the `info` dictionary), total size.
2. `started` announce → periodic announces obeying the `interval` (and `min interval`)
   returned by the tracker → `completed` when the "download" reaches 100% → `stopped`.
3. `uploaded` grows at the upload rate, `downloaded` at the download rate up to the
   total size, then `left=0` (seeding).
4. **Session duration = size / download rate** (upload duration = download duration), plus
   optional `seed_minutes`.
   Example: 4 GiB at 2000 KiB/s → ~35 min, final uploaded ≈ 500 × 2097 s ≈ 1 GiB.
5. On failure, trackers are tried in tier order; if they all fail, it retries after
   `retry_seconds`. Only `http://` / `https://` trackers are supported (UDP is skipped).

## Mode 1: manual (CLI)

```powershell
python ratiomaster.py my.torrent --up 500 --down 2000 --client qbittorrent
python ratiomaster.py my.torrent --up 800 --down 3000 --seed-minutes 60 --jitter 10
python ratiomaster.py my.torrent --seeder --up 300          # seed only, unlimited
python ratiomaster.py --list-clients
```

| Option | Purpose |
|---|---|
| `--up N` / `--down N` | rates in KiB/s (`--down 0` = already seeding) |
| `--client NAME` | `qbittorrent`, `qbittorrent5`, `utorrent`, `transmission` |
| `--port N` | announced port (0 = random) |
| `--numwant N` | number of peers requested |
| `--seed-minutes N` | extra seeding after the download ends |
| `--seeder` | start directly as a seeder (`left=0`) |
| `--jitter N` | random rate variation (±N %) |
| `--max-interval N` | cap on the interval between announces (s) |
| `--insecure` | do not verify the HTTPS certificate (self-signed) |
| `--dry-run` | send nothing, print the URLs, simulate time |
| `--config FILE` | use another configuration file |
| `--debug` | verbose logs |

## Mode 2: watched folder

```powershell
python ratiomaster.py --watch
```

Scans `watch\` every `poll_seconds` seconds. Each new `.torrent` dropped there starts
**its own session in parallel** (one thread per torrent). When a session reaches the end of
its duration, the file is moved to `watch\done\`; an unreadable torrent goes to
`watch\error\` (can be disabled: `move_done = false`). `Ctrl+C` sends `stopped` for every
active session; interrupted torrents stay in `watch\` and start again from zero on the
next `--watch`. A torrent rejected by the tracker (`failure reason`) is retried every
`retry_seconds` until `Ctrl+C`.
CLI options (`--up`, `--client`…) apply to every session in watch mode.

## Mode 3: qBittorrent source (*ARR stack)

```powershell
python ratiomaster.py --mode qbit --qbit-url http://192.168.1.10:8080 --qbit-user admin --qbit-pass xxx --categories "tv-sonarr,radarr"
```

Polls the qBittorrent WebUI API every `poll_interval` seconds (30 by default) and
starts an emulation session for each torrent that passes the filter. No `.torrent` to
read: `info_hash`, name, size and announce URL come from the API (`torrents/info`, and
`torrents/trackers` when the `tracker` field is empty).

- **One session per info_hash** (never duplicated).
- Torrent **removed**, **paused/stopped** or **no longer matching the filter** → `event=stopped` and
  the session ends. Resumed later → new session (starts from zero).
- A session that reached the end of its duration → not restarted for that torrent while the tool
  is running (restart the container to run it again).
- Torrent with no http(s) tracker (DHT only, UDP only) → skipped, rechecked on every poll.
- qBittorrent unreachable → retried with an increasing delay (max 5 min). Expired session
  (HTTP 403) → automatic re-login.
- **The emulator announces as its own peer** (its own peer_id and port), separate from the real
  qBittorrent: the tracker therefore sees **two peers** for each torrent (the real qBit + the emulator).
  This is expected on your own tracker.

**Filters** (`categories`, `exclude_categories`, `tags`):

| `categories` value | Effect |
|---|---|
| empty, `*` or `all` | every torrent |
| `tv-sonarr,radarr,lidarr` | only these categories |

The *ARR apps set a category on their downloads, by default `tv-sonarr` (Sonarr),
`radarr` (Radarr), `lidarr` (Lidarr), `readarr` (Readarr) — check
*Settings → Download Clients* in each *ARR. A configured category that qBit doesn't know
is reported in the logs at startup. `exclude_categories` removes categories;
`tags` (if set) requires at least one of the tags to be present on the torrent.

## Standalone (no Docker)

Docker is optional — it is only needed for the *ARR-stack integration. `ratiomaster.py` runs
anywhere Python 3.10+ is installed, with nothing else to install.

### Desktop qBittorrent on your PC

Enable the WebUI in qBittorrent (*Tools → Options → Web UI*), then:

```powershell
python ratiomaster.py --mode qbit --qbit-url http://localhost:8080 --qbit-user admin --qbit-pass yourpass
```

Or put it in `config.ini` once and just run `python ratiomaster.py`:

```ini
[general]
mode = qbit

[qbit]
url = http://localhost:8080
username = admin
password = yourpass
```

If `localhost` is slow to connect on Windows, use `http://127.0.0.1:8080`.

### Launchers

| File | Platform | Usage |
|---|---|---|
| `run.cmd` | Windows | `run.cmd [options]` forwards every option to `ratiomaster.py`. **Drag & drop a `.torrent` onto `run.cmd`** to emulate it in CLI mode. Double-click with no file → uses `mode` from `config.ini` (set it to `watch` or `qbit`). Shows a clear message with the python.org link if Python is missing. The window waits for a key at the end (set `RM_NOPAUSE=1` to skip). |
| `run.sh` | Linux / macOS | `./run.sh [options]`, same options as `ratiomaster.py`. |

Git on Windows may not keep the executable bit, so on Linux/macOS run once:

```bash
chmod +x run.sh
```

### Standalone `.exe` (no Python needed)

On a Windows machine with Python, build a single-file executable:

```powershell
python -m pip install pyinstaller
```
```powershell
.\build_exe.cmd
```

The result is `dist\ratiomaster.exe`. It **runs on PCs without Python installed** and takes the
same options (`ratiomaster.exe my.torrent --up 500`, `ratiomaster.exe --mode qbit ...`).
Put `config.ini` next to the `.exe` to change the defaults; `watch\` and `ratiomaster.log`
are created next to it too. Some antivirus tools flag PyInstaller executables as suspicious
(false positive); building it yourself from the source avoids trusting a third-party binary.

## Docker (adding it to the *ARR stack)

Files: `Dockerfile`, `docker-compose.yml`, `.env.example`. `python:3.12-alpine` image,
no dependencies. Inside the container, configuration goes through **environment variables**
(`qbit` is the image's default mode, logs go to standard output).

1. Copy the folder (at least `Dockerfile`, `ratiomaster.py`, `config.ini`) to the Docker
   host, e.g. next to the stack's `docker-compose.yml`.
2. Copy `.env.example` to `.env` and fill in the qBittorrent WebUI username / password.
3. **Same network as qBittorrent** — this is what lets you write `http://qbittorrent:8080`
   (the service name acts as a DNS name on a shared Docker network):
   - *Option A (recommended)*: paste the `ratiomaster:` block from `docker-compose.yml` under
     `services:` in the stack's compose file (same file = same default network), point
     `build:` at the Dockerfile's folder, uncomment `depends_on`.
   - *Option B*: keep a separate compose file and join the existing network (`networks` block
     at the bottom of the file; real name via `docker network ls`).
   - If qBittorrent runs behind a VPN container (gluetun, `network_mode: service:gluetun`), the
     name to use is the VPN service's: `QBIT_URL=http://gluetun:8080`.
4. Start it:

```powershell
docker compose up -d --build ratiomaster
```
```powershell
docker compose logs -f ratiomaster
```

`docker compose stop ratiomaster` sends `event=stopped` for every session
(`stop_grace_period: 60s` leaves time to do it).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MODE` | `qbit` (image) | `cli`, `watch` or `qbit` |
| `QBIT_URL` | `http://qbittorrent:8080` | qBittorrent WebUI URL |
| `QBIT_USER` / `QBIT_PASS` | — | WebUI credentials (empty if qBit allows the subnet without login) |
| `POLL_INTERVAL` | `30` | seconds between two API reads |
| `CATEGORIES` | empty (= all) | categories to emulate, comma-separated |
| `EXCLUDE_CATEGORIES` | empty | ignored categories |
| `TAGS` | empty | at least one of these tags required |
| `CLIENT` | `qbittorrent` | emulated client (`qbittorrent5`, `utorrent`, `transmission`) |
| `UPLOAD_RATE` / `DOWNLOAD_RATE` | `500` / `2000` | rates in KiB/s |
| `RATE_JITTER` | `0` | random variation ± % |
| `SEED_AFTER` | `0` | seeding minutes after the download duration |
| `START_AS_SEEDER` | `false` | start as a seeder; with `SEED_AFTER=0` → for as long as the torrent is active in qBit |
| `PORT` / `NUMWANT` | `0` / `200` | announced port (0 = random), peers requested |
| `MAX_INTERVAL` | `0` | cap on the announce interval (s) |
| `TIMEOUT` / `RETRY_SECONDS` | `20` / `60` | HTTP timeout, tracker retry delay |
| `VERIFY_SSL` | `true` | `false` for a self-signed certificate (tracker and WebUI) |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / empty | level, log file (empty = stdout) |

Outside Docker these same variables also work (priority: CLI options > environment
variables > `config.ini`).

### qBittorrent troubleshooting

- `login rejected`: wrong WebUI credentials.
- `IP banned`: too many failed logins; wait, or unban it in qBit (*Options → Web UI*).
- Persistent `HTTP 401` / `403`: in *Options → Web UI*, check "Host header validation"
  (add `qbittorrent` to the allowed domains or disable it) and CSRF protection.

## config.ini

| Section | Key | Description |
|---|---|---|
| `[client]` | `client` | emulated client (peer_id + User-Agent + client-specific parameters) |
| | `port` | announced port, 0 = random |
| | `numwant` | number of peers requested |
| `[speed]` | `upload_kib` / `download_kib` | rates in KiB/s |
| | `rate_jitter_pct` | random variation ± % (0 = constant) |
| `[session]` | `seed_minutes` | seeding after the download (0 = stop when the download ends) |
| | `start_as_seeder` | start as a seeder; with `seed_minutes = 0` → unlimited |
| | `max_interval` | cap on the interval between announces, 0 = the tracker's |
| `[network]` | `timeout` | HTTP timeout (s) |
| | `verify_ssl` | `false` for a self-signed certificate |
| | `retry_seconds` | wait before retrying when every tracker fails |
| `[watch]` | `watch_dir` | watched folder (relative to the script, or absolute) |
| | `poll_seconds` | scan frequency |
| | `move_done` | move to `done\` / `error\` |
| `[log]` | `log_file` | log file (empty = console only) |
| | `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `[general]` | `mode` | default mode when no argument is given: `cli`, `watch`, `qbit` |
| `[qbit]` | `url`, `username`, `password` | qBittorrent WebUI access |
| | `poll_interval` | seconds between two API reads |
| | `categories`, `exclude_categories`, `tags` | filters (see mode 3) |

Priority: CLI option > environment variables > `config.ini` > script default.

## Logs

Console + `ratiomaster.log`: timestamp, torrent name, tracker, event sent,
current up/down/left, tracker interval, seeders/leechers, errors.

```
2026-11-21 16:02:11 INFO    [My.Torrent] announce OK t=+0h30m00s tracker=tracker.example:443 event=(periodic) up=878.91 MiB down=3.43 GiB left=585.94 MiB interval=1800s seeders=3 leechers=1
```

## Adding a client

In `ratiomaster.py`, add an entry to the `CLIENTS` dictionary (`peer_id` prefix,
`User-Agent`, `key` style, extra parameters).

## Limitations

- **No freeleech detection**: freeleech status is site/tracker information and is not
  stored in the `.torrent`. The script always announces `downloaded` normally.
- UDP trackers are not supported (HTTP/HTTPS only).
- Pure v2 torrents (without v1 `length`/`files`) are not supported; the computed info_hash is the v1 hash (SHA1).
- The script contacts no peers and transfers no data: only the statistics announced to the
  tracker change.
