#!/usr/bin/env python3
"""
ratiomaster.py - standalone BitTorrent announce emulator (pure stdlib).

Intended for testing YOUR OWN tracker: it announces a torrent to an HTTP(S)
tracker as if a real client were downloading/seeding it, without transferring
any data.

Usage:
    python ratiomaster.py file.torrent [--up 500 --down 2000 --client qbittorrent]
    python ratiomaster.py --watch
    python ratiomaster.py --mode qbit --qbit-url http://qbittorrent:8080 --categories radarr
    python ratiomaster.py --list-clients

Config priority: CLI args > environment variables (see ENV_MAP) > config.ini > defaults.
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import http.cookiejar
import json
import logging
import os
import random
import shutil
import signal
import ssl
import string
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.ini"

log = logging.getLogger("ratiomaster")

# Global stop flag, set by Ctrl+C. Every session watches it.
STOP = threading.Event()


# --------------------------------------------------------------------------
# Bencode
# --------------------------------------------------------------------------

class BencodeError(ValueError):
    pass


def _bdecode_at(data: bytes, i: int, depth: int = 0) -> tuple[Any, int]:
    """Decode one bencoded value starting at data[i]. Returns (value, next_index)."""
    if depth > 200:
        raise BencodeError("nesting too deep")
    if i >= len(data):
        raise BencodeError("unexpected end of data")
    c = data[i:i + 1]
    if c == b"i":
        end = data.find(b"e", i + 1)
        if end == -1:
            raise BencodeError("unterminated integer")
        raw = data[i + 1:end]
        try:
            if not raw or raw.startswith(b"-0") or (raw.startswith(b"0") and len(raw) > 1):
                raise ValueError
            return int(raw), end + 1
        except ValueError:
            raise BencodeError(f"invalid integer {raw!r}") from None
    if c == b"l":
        i += 1
        out = []
        while True:
            if i >= len(data):
                raise BencodeError("unterminated list")
            if data[i:i + 1] == b"e":
                return out, i + 1
            v, i = _bdecode_at(data, i, depth + 1)
            out.append(v)
    if c == b"d":
        i += 1
        d: dict[bytes, Any] = {}
        while True:
            if i >= len(data):
                raise BencodeError("unterminated dict")
            if data[i:i + 1] == b"e":
                return d, i + 1
            k, i = _bdecode_at(data, i, depth + 1)
            if not isinstance(k, bytes):
                raise BencodeError("dict key is not a string")
            v, i = _bdecode_at(data, i, depth + 1)
            d[k] = v
    if c.isdigit():
        colon = data.find(b":", i)
        if colon == -1:
            raise BencodeError("invalid string length")
        try:
            n = int(data[i:colon])
        except ValueError:
            raise BencodeError("invalid string length") from None
        start = colon + 1
        if start + n > len(data):
            raise BencodeError("string runs past end of data")
        return data[start:start + n], start + n
    raise BencodeError(f"unexpected byte {c!r} at offset {i}")


def bdecode(data: bytes) -> Any:
    v, end = _bdecode_at(data, 0)
    if end != len(data):
        # Some trackers append garbage/newlines; tolerate trailing whitespace.
        if data[end:].strip():
            raise BencodeError("trailing data after bencoded value")
    return v


def bencode(v: Any) -> bytes:
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode("utf-8")
    if isinstance(v, (bytes, bytearray)):
        return b"%d:%s" % (len(v), bytes(v))
    if isinstance(v, (list, tuple)):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(bencode(k) + bencode(x) for k, x in items) + b"e"
    raise TypeError(f"cannot bencode {type(v).__name__}")


# --------------------------------------------------------------------------
# Torrent parsing
# --------------------------------------------------------------------------

class TorrentError(ValueError):
    pass


@dataclass
class Torrent:
    path: Path | None  # None when the torrent comes from the qBittorrent API
    name: str
    info_hash: bytes
    total_size: int
    tiers: list[list[str]]  # announce tiers, each a list of URLs

    @property
    def info_hash_hex(self) -> str:
        return self.info_hash.hex()


def _txt(b: Any) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return str(b)


def parse_torrent(path: Path) -> Torrent:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise TorrentError(f"cannot read file: {e}") from None
    if not data.startswith(b"d"):
        raise TorrentError("not a bencoded dictionary")

    # Walk the top-level dict by hand so we keep the exact byte span of 'info'
    # (info_hash must be the SHA1 of the original bytes, not a re-encoding).
    top: dict[bytes, Any] = {}
    info_span = None
    try:
        i = 1
        while True:
            if i >= len(data):
                raise BencodeError("unterminated top-level dict")
            if data[i:i + 1] == b"e":
                break
            k, i = _bdecode_at(data, i)
            if not isinstance(k, bytes):
                raise BencodeError("dict key is not a string")
            start = i
            v, i = _bdecode_at(data, i)
            top[k] = v
            if k == b"info":
                info_span = (start, i)
    except (BencodeError, RecursionError) as e:
        raise TorrentError(f"malformed torrent: {e}") from None

    info = top.get(b"info")
    if not isinstance(info, dict) or info_span is None:
        raise TorrentError("missing 'info' dictionary")
    info_hash = hashlib.sha1(data[info_span[0]:info_span[1]]).digest()

    name = _txt(info.get(b"name.utf-8") or info.get(b"name") or Path(path).stem)

    if isinstance(info.get(b"length"), int):
        total = info[b"length"]
    elif isinstance(info.get(b"files"), list):
        total = 0
        for f in info[b"files"]:
            if not isinstance(f, dict) or not isinstance(f.get(b"length"), int):
                raise TorrentError("invalid entry in 'files'")
            total += f[b"length"]
    else:
        raise TorrentError("no 'length' or 'files' in info (v2-only torrent?)")
    if total < 0:
        raise TorrentError("negative size")

    tiers: list[list[str]] = []
    al = top.get(b"announce-list")
    if isinstance(al, list):
        for tier in al:
            if isinstance(tier, list):
                urls = [_txt(u).strip() for u in tier if isinstance(u, bytes) and u.strip()]
                if urls:
                    tiers.append(urls)
    ann = top.get(b"announce")
    if isinstance(ann, bytes) and ann.strip():
        url = _txt(ann).strip()
        if not any(url in t for t in tiers):
            # BEP12: with an announce-list, 'announce' is ignored by clients; we keep
            # it as a last-resort tier anyway.
            tiers.append([url])
    if not tiers:
        raise TorrentError("no tracker URL (announce / announce-list)")

    return Torrent(Path(path), name, info_hash, total, tiers)


# --------------------------------------------------------------------------
# Client profiles
# --------------------------------------------------------------------------

ALNUM = string.ascii_letters + string.digits


@dataclass(frozen=True)
class ClientProfile:
    name: str
    peer_id_prefix: bytes
    user_agent: str
    peer_id_random: str  # "alnum" or "bytes"
    key_style: str  # "hex8" (uppercase hex, 8 chars) or "int" (decimal)
    # Extra static query params the real client sends, appended after the core ones.
    extra_params: tuple[tuple[str, str], ...] = ()
    gzip: bool = True

    def make_peer_id(self) -> bytes:
        n = 20 - len(self.peer_id_prefix)
        if self.peer_id_random == "bytes":
            tail = os.urandom(n)
        else:
            tail = "".join(random.choice(ALNUM) for _ in range(n)).encode()
        return self.peer_id_prefix + tail

    def make_key(self) -> str:
        if self.key_style == "int":
            return str(random.randint(0, 2**31 - 1))
        return "%08X" % random.getrandbits(32)


# Add new clients here. Key = name used in config/CLI.
CLIENTS: dict[str, ClientProfile] = {
    "qbittorrent": ClientProfile(
        name="qBittorrent 4.6.5",
        peer_id_prefix=b"-qB4650-",
        user_agent="qBittorrent/4.6.5",
        peer_id_random="alnum",
        key_style="hex8",
        extra_params=(("supportcrypto", "1"), ("redundant", "0")),
    ),
    "qbittorrent5": ClientProfile(
        name="qBittorrent 5.0.2",
        peer_id_prefix=b"-qB5020-",
        user_agent="qBittorrent/5.0.2",
        peer_id_random="alnum",
        key_style="hex8",
        extra_params=(("supportcrypto", "1"), ("redundant", "0")),
    ),
    "utorrent": ClientProfile(
        name="uTorrent 3.5.5",
        peer_id_prefix=b"-UT355W-",
        user_agent="uTorrent/355(46206)",
        peer_id_random="bytes",
        key_style="hex8",
        extra_params=(("corrupt", "0"),),
    ),
    "transmission": ClientProfile(
        name="Transmission 4.0.5",
        peer_id_prefix=b"-TR4050-",
        user_agent="Transmission/4.0.5",
        peer_id_random="alnum",
        key_style="hex8",
        extra_params=(("supportcrypto", "1"), ("requirecrypto", "0")),
        gzip=False,
    ),
}


# --------------------------------------------------------------------------
# Tracker announce
# --------------------------------------------------------------------------

class TrackerError(Exception):
    pass


@dataclass
class AnnounceResult:
    interval: int
    min_interval: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    warning: str | None = None


def _q(b: bytes) -> str:
    # Raw-bytes percent encoding; keep unreserved chars literal like real clients.
    return urllib.parse.quote_from_bytes(b, safe="-._~")


def build_announce_url(base: str, *, info_hash: bytes, peer_id: bytes, port: int,
                       uploaded: int, downloaded: int, left: int, event: str,
                       numwant: int, key: str, client: ClientProfile) -> str:
    params = [
        ("info_hash", _q(info_hash)),
        ("peer_id", _q(peer_id)),
        ("port", str(port)),
        ("uploaded", str(uploaded)),
        ("downloaded", str(downloaded)),
        ("left", str(left)),
        ("key", key),
    ]
    if event:
        params.append(("event", event))
    params += [("numwant", str(numwant)), ("compact", "1"), ("no_peer_id", "1")]
    params += list(client.extra_params)
    qs = "&".join(f"{k}={v}" for k, v in params)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{qs}"


def http_get(url: str, client: ClientProfile, timeout: float, verify_ssl: bool) -> bytes:
    headers = {"User-Agent": client.user_agent, "Connection": "close"}
    if client.gzip:
        headers["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, headers=headers)
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read(4 * 1024 * 1024)
            enc = (r.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as e:
        raise TrackerError(f"HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise TrackerError(f"network error: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise TrackerError(f"network error: {e}") from None
    try:
        if enc == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
    except (OSError, zlib.error) as e:
        raise TrackerError(f"bad compressed response: {e}") from None
    return body


def parse_announce_response(body: bytes) -> AnnounceResult:
    try:
        resp = bdecode(body)
    except (BencodeError, RecursionError) as e:
        snippet = body[:120].decode("utf-8", errors="replace")
        raise TrackerError(f"invalid tracker response ({e}): {snippet!r}") from None
    if not isinstance(resp, dict):
        raise TrackerError("tracker response is not a dictionary")
    if b"failure reason" in resp:
        raise TrackerError(f"tracker failure: {_txt(resp[b'failure reason'])}")
    interval = resp.get(b"interval")
    if not isinstance(interval, int) or interval <= 0:
        interval = 1800
    mi = resp.get(b"min interval")
    return AnnounceResult(
        interval=interval,
        min_interval=mi if isinstance(mi, int) and mi > 0 else None,
        seeders=resp.get(b"complete") if isinstance(resp.get(b"complete"), int) else None,
        leechers=resp.get(b"incomplete") if isinstance(resp.get(b"incomplete"), int) else None,
        warning=_txt(resp[b"warning message"]) if b"warning message" in resp else None,
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass
class Settings:
    mode: str = ""  # "", "watch" or "qbit" ("" = CLI with a .torrent argument)
    client: str = "qbittorrent"
    upload_kib: float = 500.0
    download_kib: float = 2000.0
    rate_jitter_pct: float = 0.0
    port: int = 0  # 0 = random
    numwant: int = 200
    seed_minutes: float = 0.0
    start_as_seeder: bool = False
    timeout: float = 20.0
    verify_ssl: bool = True
    retry_seconds: int = 60
    max_interval: int = 0  # 0 = always obey tracker interval
    dry_run: bool = False
    watch_dir: Path = field(default_factory=lambda: SCRIPT_DIR / "watch")
    poll_seconds: float = 5.0
    move_done: bool = True
    log_file: str = "ratiomaster.log"
    log_level: str = "INFO"
    # qBittorrent API source mode
    qbit_url: str = "http://qbittorrent:8080"
    qbit_user: str = ""
    qbit_pass: str = ""
    qbit_poll: float = 30.0
    categories: str = ""  # "" / "*" / "all" = every category
    exclude_categories: str = ""
    tags: str = ""  # "" = no tag filter; else torrent must carry one of these tags


def _bool(v: str) -> bool:
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on", "oui"):
        return True
    if v in ("0", "false", "no", "off", "non"):
        return False
    raise ValueError(v)


# Environment variable -> (Settings attribute, converter). Env overrides config.ini,
# CLI overrides env. Primary configuration path for the Docker container.
ENV_MAP: dict[str, tuple[str, Any]] = {
    "MODE": ("mode", str),
    "QBIT_URL": ("qbit_url", str),
    "QBIT_USER": ("qbit_user", str),
    "QBIT_PASS": ("qbit_pass", str),
    "POLL_INTERVAL": ("qbit_poll", float),
    "CATEGORIES": ("categories", str),
    "EXCLUDE_CATEGORIES": ("exclude_categories", str),
    "TAGS": ("tags", str),
    "UPLOAD_RATE": ("upload_kib", float),
    "DOWNLOAD_RATE": ("download_kib", float),
    "RATE_JITTER": ("rate_jitter_pct", float),
    "CLIENT": ("client", str),
    "PORT": ("port", int),
    "NUMWANT": ("numwant", int),
    "SEED_AFTER": ("seed_minutes", float),
    "START_AS_SEEDER": ("start_as_seeder", _bool),
    "MAX_INTERVAL": ("max_interval", int),
    "TIMEOUT": ("timeout", float),
    "VERIFY_SSL": ("verify_ssl", _bool),
    "RETRY_SECONDS": ("retry_seconds", int),
    "WATCH_DIR": ("watch_dir", Path),
    "LOG_FILE": ("log_file", str),
    "LOG_LEVEL": ("log_level", str),
}
# These may legitimately be set to an empty string (e.g. LOG_FILE="" = console only).
_ENV_EMPTY_OK = {"LOG_FILE", "CATEGORIES", "EXCLUDE_CATEGORIES", "TAGS", "QBIT_USER", "QBIT_PASS"}


def apply_env(s: Settings, env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    for var, (attr, conv) in ENV_MAP.items():
        if var not in env:
            continue
        raw = env[var].strip()
        if raw == "" and var not in _ENV_EMPTY_OK:
            continue
        try:
            setattr(s, attr, conv(raw))
        except ValueError:
            print(f"[!] invalid environment variable {var}={raw!r}, ignored", file=sys.stderr)
    s.mode = s.mode.lower()
    s.client = s.client.lower()
    s.log_level = s.log_level.upper()
    return s


def load_config(path: Path) -> Settings:
    s = Settings()
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if path.is_file():
        try:
            cp.read(path, encoding="utf-8")
        except configparser.Error as e:
            print(f"[!] invalid config.ini, using defaults: {e}", file=sys.stderr)
            return s

    def get(section, key, conv, default):
        if not cp.has_option(section, key):
            return default
        raw = cp.get(section, key).strip()
        if raw == "":
            return default
        try:
            return conv(raw)
        except ValueError:
            print(f"[!] config.ini [{section}] {key}={raw!r} invalid, default={default!r}",
                  file=sys.stderr)
            return default

    b = _bool
    s.mode = get("general", "mode", str, s.mode).lower()
    s.client = get("client", "client", str, s.client).lower()
    s.port = get("client", "port", int, s.port)
    s.numwant = get("client", "numwant", int, s.numwant)
    s.upload_kib = get("speed", "upload_kib", float, s.upload_kib)
    s.download_kib = get("speed", "download_kib", float, s.download_kib)
    s.rate_jitter_pct = get("speed", "rate_jitter_pct", float, s.rate_jitter_pct)
    s.seed_minutes = get("session", "seed_minutes", float, s.seed_minutes)
    s.start_as_seeder = get("session", "start_as_seeder", b, s.start_as_seeder)
    s.max_interval = get("session", "max_interval", int, s.max_interval)
    s.timeout = get("network", "timeout", float, s.timeout)
    s.verify_ssl = get("network", "verify_ssl", b, s.verify_ssl)
    s.retry_seconds = get("network", "retry_seconds", int, s.retry_seconds)
    wd = get("watch", "watch_dir", str, None)
    if wd:
        p = Path(wd)
        s.watch_dir = p if p.is_absolute() else SCRIPT_DIR / p
    s.poll_seconds = get("watch", "poll_seconds", float, s.poll_seconds)
    s.move_done = get("watch", "move_done", b, s.move_done)
    s.log_file = get("log", "log_file", str, s.log_file)
    s.log_level = get("log", "level", str, s.log_level).upper()
    s.qbit_url = get("qbit", "url", str, s.qbit_url)
    s.qbit_user = get("qbit", "username", str, s.qbit_user)
    s.qbit_pass = get("qbit", "password", str, s.qbit_pass)
    s.qbit_poll = get("qbit", "poll_interval", float, s.qbit_poll)
    s.categories = get("qbit", "categories", str, s.categories)
    s.exclude_categories = get("qbit", "exclude_categories", str, s.exclude_categories)
    s.tags = get("qbit", "tags", str, s.tags)
    return s


# --------------------------------------------------------------------------
# Emulation session
# --------------------------------------------------------------------------

def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m{s:02d}s"


class Session:
    """One emulated client announcing one torrent."""

    def __init__(self, torrent: Torrent, cfg: Settings, stop: threading.Event = STOP):
        self.t = torrent
        self.cfg = cfg
        self.stop = stop
        if cfg.client not in CLIENTS:
            raise ValueError(f"unknown client '{cfg.client}' (choices: {', '.join(CLIENTS)})")
        self.client = CLIENTS[cfg.client]
        self.peer_id = self.client.make_peer_id()
        self.key = self.client.make_key()
        self.port = cfg.port or random.randint(20000, 60000)
        self.tiers = [list(t) for t in torrent.tiers]
        self.log = logging.LoggerAdapter(log, {"torrent": torrent.name[:40]})

        self.up_rate = max(0.0, cfg.upload_kib) * 1024
        self.down_rate = max(0.0, cfg.download_kib) * 1024
        self.uploaded = 0.0
        self.seeding = cfg.start_as_seeder or self.down_rate <= 0 or torrent.total_size == 0
        self.downloaded = 0.0
        if self.seeding:
            self.dl_seconds = 0.0
        else:
            self.dl_seconds = torrent.total_size / self.down_rate
        self.total_seconds = self.dl_seconds + max(0.0, cfg.seed_minutes) * 60
        self.infinite = self.seeding and cfg.seed_minutes <= 0
        self.completed_sent = self.seeding
        self.ok = False  # session ran and sent 'stopped'
        self.finished = False  # planned duration fully reached (not interrupted)
        self._offset = 0.0  # simulated clock offset (dry-run)
        self._start = None

    # -- clock (real, or simulated in dry-run) ----------------------------
    def _now(self) -> float:
        return time.monotonic() + self._offset

    def _sleep(self, sec: float) -> bool:
        """Sleep sec seconds; returns True if a stop was requested."""
        if self.cfg.dry_run:
            self._offset += sec
            return self.stop.is_set()
        return self.stop.wait(sec)

    def _elapsed(self) -> str:
        return "t=+" + fmt_dur(self._now() - self._start) if self._start is not None else "t=0"

    # -- counters ---------------------------------------------------------
    def _jitter(self, rate: float) -> float:
        j = self.cfg.rate_jitter_pct
        if j <= 0:
            return rate
        return max(0.0, rate * (1 + random.uniform(-j, j) / 100.0))

    def _advance(self, dt: float) -> None:
        if dt <= 0:
            return
        self.uploaded += self._jitter(self.up_rate) * dt
        if not self.seeding:
            self.downloaded = min(float(self.t.total_size),
                                  self.downloaded + self._jitter(self.down_rate) * dt)

    @property
    def left(self) -> int:
        if self.seeding:
            return 0
        return max(0, self.t.total_size - int(self.downloaded))

    # -- announce ---------------------------------------------------------
    def announce(self, event: str) -> AnnounceResult | None:
        """Try every tracker tier in order (BEP12). Returns result or None on total failure."""
        up, down, left = int(self.uploaded), int(self.downloaded), self.left
        last_err = None
        for tier in self.tiers:
            for idx, url in enumerate(list(tier)):
                if not url.lower().startswith(("http://", "https://")):
                    self.log.debug("skip unsupported tracker %s", url)
                    last_err = f"unsupported scheme: {url.split(':', 1)[0]}"
                    continue
                full = build_announce_url(
                    url, info_hash=self.t.info_hash, peer_id=self.peer_id, port=self.port,
                    uploaded=up, downloaded=down, left=left, event=event,
                    numwant=0 if event == "stopped" else self.cfg.numwant,
                    key=self.key, client=self.client)
                host = urllib.parse.urlsplit(url).netloc
                if self.cfg.dry_run:
                    self.log.info("[DRY-RUN] %s event=%s up=%s down=%s left=%s -> %s",
                                  self._elapsed(), event or "(periodic)",
                                  fmt_bytes(up), fmt_bytes(down),
                                  fmt_bytes(left), full)
                    return AnnounceResult(interval=1800)
                try:
                    res = parse_announce_response(
                        http_get(full, self.client, self.cfg.timeout, self.cfg.verify_ssl))
                except TrackerError as e:
                    last_err = f"{host}: {e}"
                    self.log.warning("announce event=%s to %s failed: %s",
                                     event or "(periodic)", host, e)
                    continue
                # BEP12: move working tracker to front of its tier.
                if idx:
                    tier.insert(0, tier.pop(idx))
                self.log.info(
                    "announce OK %s tracker=%s event=%s up=%s down=%s left=%s interval=%ss%s%s",
                    self._elapsed(), host, event or "(periodic)", fmt_bytes(up), fmt_bytes(down),
                    fmt_bytes(left), res.interval,
                    f" seeders={res.seeders} leechers={res.leechers}"
                    if res.seeders is not None else "",
                    f" WARNING={res.warning}" if res.warning else "")
                return res
        self.log.error("all trackers failed (event=%s): %s", event or "(periodic)", last_err)
        return None

    # -- main loop ----------------------------------------------------------
    def run(self) -> bool:
        t = self.t
        self.log.info(
            "session start: client=%s peer_id=%s port=%d size=%s up=%.0f KiB/s down=%.0f KiB/s "
            "info_hash=%s",
            self.client.name, self.peer_id[:8].decode(errors="replace"), self.port,
            fmt_bytes(t.total_size), self.up_rate / 1024, self.down_rate / 1024, t.info_hash_hex)
        if self.infinite:
            self.log.info("seed mode, unlimited duration (Ctrl+C to stop)")
        else:
            self.log.info("planned duration: %s (download %s + seed %s)", fmt_dur(self.total_seconds),
                          fmt_dur(self.dl_seconds), fmt_dur(self.total_seconds - self.dl_seconds))

        # 'started' announce, retried until success or stop.
        res = None
        while not self.stop.is_set():
            res = self.announce("started")
            if res:
                break
            if self.stop.wait(self.cfg.retry_seconds):
                return False
        if res is None:
            return False

        self._start = start = last = self._now()
        next_announce = start + self._wait_for(res)
        announces = 1
        while True:
            now = self._now()
            # Wake up at whichever comes first: next announce, download completion, end.
            targets = [next_announce]
            if not self.seeding and self.down_rate > 0:
                targets.append(now + max(0.5, (t.total_size - self.downloaded) / self.down_rate))
            if not self.infinite:
                targets.append(start + self.total_seconds)
            stopped = self._sleep(max(0.0, min(targets) - now))
            now = self._now()
            self._advance(now - last)
            last = now

            if stopped:
                self.log.info("stop requested -> event=stopped")
                break

            event = ""
            # Download done when bytes reach the size, or the planned DL time elapsed
            # (avoids ending at left=8 B because of float rounding / jitter).
            if not self.seeding and (self.downloaded >= t.total_size - 1
                                     or now - start >= self.dl_seconds - 0.5):
                self.downloaded = float(t.total_size)
                self.seeding = True
                if not self.completed_sent:
                    self.completed_sent = True
                    event = "completed"
                    self.log.info("download complete (%s) -> seeding", fmt_bytes(t.total_size))

            end_reached = not self.infinite and now - start >= self.total_seconds - 0.5

            if event or (not end_reached and now >= next_announce - 0.5):
                r = self.announce(event)
                announces += 1
                if r:
                    next_announce = now + self._wait_for(r)
                else:
                    next_announce = now + self.cfg.retry_seconds
                    if event == "completed":
                        self.completed_sent = False  # retry 'completed' next time

            if end_reached:
                self.finished = True
                self.log.info("session duration reached (%s) -> event=stopped",
                              fmt_dur(now - start))
                break

            if self.cfg.dry_run and self.infinite and announces >= 5:
                self.log.info("[DRY-RUN] unlimited seed: stopping after 5 simulated announces")
                break

        self.announce("stopped")
        ratio = self.uploaded / self.downloaded if self.downloaded else float("inf")
        self.log.info("session finished: up=%s down=%s ratio=%s", fmt_bytes(self.uploaded),
                      fmt_bytes(self.downloaded),
                      "inf" if ratio == float("inf") else f"{ratio:.2f}")
        self.ok = True
        return True

    def _wait_for(self, res: AnnounceResult) -> float:
        w = res.interval
        if res.min_interval:
            w = max(w, res.min_interval)
        if self.cfg.max_interval > 0:
            w = min(w, max(self.cfg.max_interval, res.min_interval or 0))
        return float(w)


# --------------------------------------------------------------------------
# Run modes
# --------------------------------------------------------------------------

def run_single(path: Path, cfg: Settings) -> int:
    try:
        t = parse_torrent(path)
    except TorrentError as e:
        log.error("invalid torrent %s: %s", path, e, extra={"torrent": path.name})
        return 2
    log.info("torrent: %s | size %s | info_hash %s | trackers %s", t.name,
             fmt_bytes(t.total_size), t.info_hash_hex,
             " ; ".join(",".join(tier) for tier in t.tiers), extra={"torrent": t.name[:40]})
    try:
        sess = Session(t, cfg)
    except ValueError as e:
        log.error("%s", e, extra={"torrent": t.name[:40]})
        return 2
    th = threading.Thread(target=sess.run, name="session", daemon=True)
    th.start()
    _join_interruptible([th])
    return 0 if sess.ok else 1


def _move(path: Path, sub: str) -> None:
    dest_dir = path.parent / sub
    try:
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / path.name
        if dest.exists():
            dest = dest_dir / f"{path.stem}.{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(dest))
    except OSError as e:
        log.warning("cannot move %s to %s/: %s", path.name, sub, e, extra={"torrent": "watch"})


def run_watch(cfg: Settings) -> int:
    wd = cfg.watch_dir
    wd.mkdir(parents=True, exist_ok=True)
    wlog = logging.LoggerAdapter(log, {"torrent": "watch"})
    wlog.info("watching %s (every %.0fs). Ctrl+C to stop.", wd, cfg.poll_seconds)
    active: dict[Path, tuple[threading.Thread, Session]] = {}
    seen: dict[Path, float] = {}  # path -> mtime of a file already handled/rejected

    def reap() -> None:
        for p, (th, sess) in list(active.items()):
            if not th.is_alive():
                del active[p]
                wlog.info("session ended: %s (%s)", p.name,
                          "completed" if sess.finished else "interrupted")
                # Only fully completed sessions go to done/; interrupted ones stay
                # in watch/ and restart from zero on the next --watch run.
                if cfg.move_done and sess.finished and p.exists():
                    _move(p, "done")
                    seen.pop(p, None)

    while not STOP.is_set():
        reap()
        try:
            files = sorted(x for x in wd.iterdir()
                           if x.is_file() and x.suffix.lower() == ".torrent")
        except OSError as e:
            wlog.error("cannot list %s: %s", wd, e)
            files = []
        for p in files:
            if p in active:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if seen.get(p) == st.st_mtime:
                continue
            if time.time() - st.st_mtime < 1.0:
                continue  # still being written; pick it up next poll
            seen[p] = st.st_mtime
            try:
                t = parse_torrent(p)
                sess = Session(t, cfg)
            except (TorrentError, ValueError) as e:
                wlog.error("torrent rejected %s: %s", p.name, e)
                if cfg.move_done:
                    _move(p, "error")
                    seen.pop(p, None)
                continue
            th = threading.Thread(target=sess.run, name=f"s-{p.stem[:20]}", daemon=True)
            th.start()
            active[p] = (th, sess)
            wlog.info("new session: %s (%d active)", p.name, len(active))
        STOP.wait(cfg.poll_seconds)

    wlog.info("stopping: sending event=stopped for %d session(s)...", len(active))
    _join_interruptible([th for th, _ in active.values()], grace=cfg.timeout + 10)
    reap()
    return 0


def _join_interruptible(threads: list[threading.Thread], grace: float = 60.0) -> None:
    """Join threads while still reacting to Ctrl+C (Windows-friendly)."""
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        STOP.set()
    deadline = time.monotonic() + grace
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))


# --------------------------------------------------------------------------
# qBittorrent WebUI API source mode
# --------------------------------------------------------------------------

class QbitError(Exception):
    pass


class QbitClient:
    """Minimal qBittorrent WebUI API v2 client (cookie session, lazy login)."""

    def __init__(self, base: str, user: str, password: str, timeout: float,
                 verify_ssl: bool = True):
        self.base = base.rstrip("/")
        self.user, self.password, self.timeout = user, password, timeout
        self.jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base.lower().startswith("https") and not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def _req(self, path: str, params: dict | None = None,
             data: dict | None = None) -> tuple[int, bytes]:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        # Referer/Origin must match the WebUI origin, or qBit rejects the request (CSRF).
        req = urllib.request.Request(url, data=body, headers={
            "Referer": self.base, "Origin": self.base,
            "User-Agent": f"ratiomaster/{VERSION}"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read(32 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except urllib.error.URLError as e:
            raise QbitError(f"qBittorrent unreachable ({self.base}): {e.reason}") from None
        except (TimeoutError, OSError) as e:
            raise QbitError(f"qBittorrent unreachable ({self.base}): {e}") from None

    def login(self) -> None:
        if not self.user:
            raise QbitError("authentication required (HTTP 403) but QBIT_USER is empty")
        self.jar.clear()
        code, body = self._req("/api/v2/auth/login",
                               data={"username": self.user, "password": self.password})
        if code == 403:
            raise QbitError("IP banned by qBittorrent (too many failed logins)")
        if code not in (200, 204) or body.strip() == b"Fails.":
            raise QbitError(f"login rejected (HTTP {code}, response {body[:40]!r})")
        log.info("connected to qBittorrent %s", self.base, extra={"torrent": "qbit"})

    def get_json(self, path: str, params: dict | None = None) -> Any:
        # Try without logging in first (qBit may whitelist the subnet); on 403 = no/expired
        # SID -> log in once and retry.
        for attempt in (0, 1):
            code, body = self._req(path, params)
            if code == 403 and attempt == 0:
                self.login()
                continue
            if code != 200:
                raise QbitError(f"{path}: HTTP {code}")
            try:
                return json.loads(body)
            except ValueError:
                raise QbitError(f"{path}: invalid JSON response") from None
        raise QbitError(f"{path}: HTTP 403 after login")

    def logout(self) -> None:
        try:
            self._req("/api/v2/auth/logout", data={})
        except QbitError:
            pass


# Paused/stopped (qBit 4.x "paused*", 5.x "stopped*") or broken torrents are not emulated.
QBIT_INACTIVE = {"pausedup", "pauseddl", "stoppedup", "stoppeddl", "error", "missingfiles",
                 "unknown"}


def _csv_set(s: str) -> set[str]:
    return {x.strip().lower() for x in (s or "").split(",") if x.strip()}


class QbitFilter:
    def __init__(self, cfg: Settings):
        cats = _csv_set(cfg.categories)
        self.all_categories = not cats or bool(cats & {"*", "all"})
        self.categories = cats
        self.exclude = _csv_set(cfg.exclude_categories)
        self.tags = _csv_set(cfg.tags)

    def match(self, t: dict) -> str | None:
        """Returns None if the torrent matches, else a short reason."""
        cat = str(t.get("category") or "").lower()
        if cat in self.exclude:
            return f"excluded category '{cat}'"
        if not self.all_categories and cat not in self.categories:
            return f"category '{cat}' not in filter"
        if self.tags and not (_csv_set(str(t.get("tags") or "")) & self.tags):
            return "tag not in filter"
        return None

    def describe(self) -> str:
        parts = ["categories=" + ("all" if self.all_categories
                                  else ",".join(sorted(self.categories)))]
        if self.exclude:
            parts.append("excluded=" + ",".join(sorted(self.exclude)))
        if self.tags:
            parts.append("tags=" + ",".join(sorted(self.tags)))
        return " ".join(parts)


def qbit_hash(t: dict) -> str | None:
    h = str(t.get("infohash_v1") or t.get("hash") or "").strip().lower()
    if len(h) == 40 and all(c in "0123456789abcdef" for c in h):
        return h
    return None


def qbit_tiers(qc: QbitClient, t: dict, h: str) -> list[list[str]]:
    """Announce tiers for a qBit torrent: working tracker first, then the others by tier."""
    def http_ok(u: str) -> bool:
        return u.lower().startswith(("http://", "https://"))

    tiers: list[list[str]] = []
    cur = str(t.get("tracker") or "").strip()
    if http_ok(cur):
        tiers.append([cur])
    try:
        trackers = qc.get_json("/api/v2/torrents/trackers", {"hash": h})
    except QbitError as e:
        if tiers:
            return tiers
        raise
    by_tier: dict[int, list[tuple[int, str]]] = {}
    for tr in trackers if isinstance(trackers, list) else []:
        if not isinstance(tr, dict):
            continue
        url = str(tr.get("url") or "").strip()
        if not http_ok(url) or url == cur:  # skips "** [DHT] **" pseudo-entries too
            continue
        tier = tr.get("tier") if isinstance(tr.get("tier"), int) else 999
        working = 0 if tr.get("status") == 2 else 1
        by_tier.setdefault(tier, []).append((working, url))
    for tier in sorted(by_tier):
        tiers.append([u for _, u in sorted(by_tier[tier])])
    return tiers


def run_qbit(cfg: Settings) -> int:
    qlog = logging.LoggerAdapter(log, {"torrent": "qbit"})
    qc = QbitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, cfg.timeout, cfg.verify_ssl)
    flt = QbitFilter(cfg)
    qlog.info("qBittorrent source %s, poll %.0fs, %s. Ctrl+C to stop.",
              qc.base, cfg.qbit_poll, flt.describe())

    # info_hash hex -> (thread, session, per-session stop event)
    active: dict[str, tuple[threading.Thread, Session, threading.Event]] = {}
    finished: set[str] = set()  # completed sessions are not restarted (until restart of tool)
    warned: set[str] = set()  # hashes already reported as skipped (avoid log spam)
    categories_checked = False
    failures = 0

    def reap() -> None:
        for h, (th, sess, _) in list(active.items()):
            if not th.is_alive():
                del active[h]
                if sess.finished:
                    finished.add(h)
                qlog.info("session ended: %s (%s)", sess.t.name,
                          "completed" if sess.finished else "interrupted")

    while not STOP.is_set():
        reap()
        try:
            if not categories_checked and not flt.all_categories:
                cats = qc.get_json("/api/v2/torrents/categories")
                known = {str(c).lower() for c in (cats if isinstance(cats, dict) else {})}
                for c in sorted(flt.categories - known):
                    qlog.warning("category '%s' unknown to qBittorrent (known: %s)",
                                 c, ", ".join(sorted(known)) or "none")
                categories_checked = True
            items = qc.get_json("/api/v2/torrents/info", {"filter": "all"})
            if not isinstance(items, list):
                raise QbitError("/api/v2/torrents/info: expected a list")
        except QbitError as e:
            failures += 1
            wait = min(cfg.qbit_poll * 2 ** min(failures - 1, 6), 300.0)
            qlog.warning("%s -> retrying in %.0fs", e, wait)
            STOP.wait(wait)
            continue
        if failures:
            qlog.info("qBittorrent reachable again")
        failures = 0

        current: dict[str, dict] = {}
        why_not: dict[str, str] = {}
        for t in items:
            if not isinstance(t, dict):
                continue
            h = qbit_hash(t)
            if not h:
                continue
            state = str(t.get("state") or "").lower()
            reason = flt.match(t)
            if reason is None and state in QBIT_INACTIVE:
                reason = f"state {t.get('state')}"
            if reason is None:
                current[h] = t
            else:
                why_not[h] = reason

        # Stop sessions whose torrent was removed, paused or no longer matches the filter.
        for h, (th, sess, ev) in active.items():
            if h not in current and not ev.is_set():
                qlog.info("stopping %s: %s", sess.t.name,
                          why_not.get(h, "removed from qBittorrent"))
                ev.set()

        for h, t in current.items():
            if h in active or h in finished:
                continue
            name = str(t.get("name") or h)
            size = t.get("total_size") if isinstance(t.get("total_size"), int) else t.get("size")
            if not isinstance(size, int) or size < 0:
                if h not in warned:
                    qlog.warning("%s: unknown size, skipped", name)
                    warned.add(h)
                continue
            try:
                tiers = qbit_tiers(qc, t, h)
            except QbitError as e:
                qlog.warning("%s: cannot read trackers (%s), retrying next poll",
                             name, e)
                continue
            if not tiers:
                if h not in warned:
                    qlog.warning("%s: no http(s) tracker, skipped (rechecked every poll)",
                                 name)
                    warned.add(h)
                continue
            warned.discard(h)
            torrent = Torrent(None, name, bytes.fromhex(h), size, tiers)
            ev = threading.Event()
            try:
                sess = Session(torrent, cfg, stop=ev)
            except ValueError as e:
                qlog.error("%s", e)
                return 2
            th = threading.Thread(target=sess.run, name=f"q-{h[:8]}", daemon=True)
            th.start()
            active[h] = (th, sess, ev)
            qlog.info("new session: %s [cat=%s] (%d active)", name,
                      t.get("category") or "-", len(active))
        STOP.wait(cfg.qbit_poll)

    qlog.info("stopping: sending event=stopped for %d session(s)...", len(active))
    for _, _, ev in active.values():
        ev.set()
    _join_interruptible([th for th, _, _ in active.values()], grace=cfg.timeout + 10)
    reap()
    qc.logout()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class _CtxFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "torrent"):
            record.torrent = "-"
        return True


def setup_logging(cfg: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(torrent)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    log.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(_CtxFilter())
    log.addHandler(sh)
    if cfg.log_file:
        p = Path(cfg.log_file)
        if not p.is_absolute():
            p = SCRIPT_DIR / p
        try:
            fh = logging.FileHandler(p, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(_CtxFilter())
            log.addHandler(fh)
        except OSError as e:
            print(f"[!] log file {p} unavailable: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="BitTorrent announce emulator (for testing YOUR OWN tracker).")
    ap.add_argument("torrent", nargs="?", help=".torrent file (manual mode)")
    ap.add_argument("--mode", choices=["cli", "watch", "qbit"],
                    help="cli (default with a .torrent), watch (folder), qbit (qBittorrent API)")
    ap.add_argument("--watch", action="store_true", help="shortcut for --mode watch")
    ap.add_argument("--qbit-url", help="qBittorrent WebUI URL (e.g. http://qbittorrent:8080)")
    ap.add_argument("--qbit-user")
    ap.add_argument("--qbit-pass")
    ap.add_argument("--poll", type=float, help="qBittorrent API poll interval (s)")
    ap.add_argument("--categories", help="qBit categories to emulate (comma-separated; empty/*/all = all)")
    ap.add_argument("--exclude-categories")
    ap.add_argument("--tags", help="qBit tags (comma-separated); any one matches")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.ini")
    ap.add_argument("--up", type=float, help="upload rate in KiB/s")
    ap.add_argument("--down", type=float, help="download rate in KiB/s (0 = already seeding)")
    ap.add_argument("--client", help=f"emulated client: {', '.join(CLIENTS)}")
    ap.add_argument("--port", type=int, help="announced port (0 = random)")
    ap.add_argument("--numwant", type=int)
    ap.add_argument("--seed-minutes", type=float, help="extra seeding minutes after the download")
    ap.add_argument("--seeder", action="store_true", help="start directly as a seeder (left=0)")
    ap.add_argument("--jitter", type=float, help="random rate variation in %%")
    ap.add_argument("--max-interval", type=int,
                    help="cap (s) on the interval between announces (0 = tracker's interval)")
    ap.add_argument("--watch-dir", help="watched folder")
    ap.add_argument("--insecure", action="store_true", help="do not verify the HTTPS certificate")
    ap.add_argument("--dry-run", action="store_true",
                    help="print announce URLs without sending anything (simulated time)")
    ap.add_argument("--list-clients", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(argv)

    if a.list_clients:
        for k, c in CLIENTS.items():
            print(f"{k:14} {c.name:22} peer_id={c.peer_id_prefix.decode()}  UA={c.user_agent}")
        return 0

    cfg = apply_env(load_config(Path(a.config)))
    for attr, val in (("upload_kib", a.up), ("download_kib", a.down), ("port", a.port),
                      ("numwant", a.numwant), ("seed_minutes", a.seed_minutes),
                      ("rate_jitter_pct", a.jitter), ("max_interval", a.max_interval),
                      ("qbit_url", a.qbit_url), ("qbit_user", a.qbit_user),
                      ("qbit_pass", a.qbit_pass), ("qbit_poll", a.poll),
                      ("categories", a.categories),
                      ("exclude_categories", a.exclude_categories), ("tags", a.tags)):
        if val is not None:
            setattr(cfg, attr, val)
    if a.client:
        cfg.client = a.client.lower()
    if a.seeder:
        cfg.start_as_seeder = True
    if a.insecure:
        cfg.verify_ssl = False
    if a.dry_run:
        cfg.dry_run = True
    if a.debug:
        cfg.log_level = "DEBUG"
    if a.watch_dir:
        cfg.watch_dir = Path(a.watch_dir)

    # Mode: CLI flag > a .torrent argument (= cli) > MODE env / config.ini.
    if a.watch:
        mode = "watch"
    elif a.mode:
        mode = a.mode
    elif a.torrent:
        mode = "cli"
    else:
        mode = cfg.mode or "cli"

    if cfg.client not in CLIENTS:
        ap.error(f"unknown client '{cfg.client}' (choices: {', '.join(CLIENTS)})")
    if mode not in ("cli", "watch", "qbit"):
        ap.error(f"unknown mode '{mode}' (cli, watch, qbit)")
    if mode == "cli" and not a.torrent:
        ap.error("give a .torrent file, --watch or --mode qbit")
    if a.dry_run and mode != "cli":
        ap.error("--dry-run is only supported with a .torrent file")

    setup_logging(cfg)
    # Ctrl+C / docker stop -> global stop flag; sessions then send event=stopped.
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: STOP.set())

    if mode == "watch":
        return run_watch(cfg)
    if mode == "qbit":
        return run_qbit(cfg)
    return run_single(Path(a.torrent), cfg)


if __name__ == "__main__":
    sys.exit(main())
