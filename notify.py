#!/usr/bin/env python3
"""Watch LGSM Valheim console logs and post new join codes to a Discord webhook."""
import glob
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

logging.getLogger("urllib3").setLevel(logging.WARNING)

LOG_DIR = os.environ.get("LOG_DIR", "/logs")
STATE_FILE = os.environ.get("STATE_FILE", "/state/last.json")
WEBHOOK_FILE = os.environ.get("WEBHOOK_FILE", "/run/secrets/discord_webhook")
LOG_GLOB = "vhserver-console.log"
MAX_LINE = 4096
PATTERN = re.compile(r"join code (?P<code>\d{6})")
WEBHOOK_HOSTS = frozenset({
    "discord.com", "discordapp.com",
    "ptb.discord.com", "canary.discord.com",
})


class WebhookURL:
    """Wraps a credential-bearing URL so it cannot be logged by accident.

    The Discord webhook token is a path segment, not a header, so any code
    that prints the URL discloses the credential. Rendering this object
    always yields a placeholder; the real value comes out only through
    expose(), which is deliberately greppable for audit.
    """

    __slots__ = ("_url",)

    def __init__(self, url):
        self._url = url

    def __str__(self):
        return "<webhook redacted>"

    __repr__ = __str__

    def __format__(self, spec):
        return str(self)

    def expose(self):
        """Explicit unwrap. Only requests.get/post should call this."""
        return self._url


def load_webhook():
    try:
        with open(WEBHOOK_FILE) as f:
            url = f.read().strip()
    except OSError:
        raise SystemExit(f"webhook: cannot read {WEBHOOK_FILE}")
    # Nothing below prints `url` or `parts.path`; the token lives in the path.
    parts = urlparse(url)
    if parts.scheme != "https":
        raise SystemExit("webhook: scheme must be https")
    if parts.hostname not in WEBHOOK_HOSTS:
        raise SystemExit(f"webhook: host not allowed: {parts.hostname!r}")
    if not parts.path.startswith("/api/webhooks/"):
        raise SystemExit("webhook: path is not /api/webhooks/...")
    return WebhookURL(url)


def preflight(webhook, attempts=3):
    """A GET on a real webhook returns its JSON object. A wrong-but-live URL will not."""
    for n in range(1, attempts + 1):
        try:
            r = requests.get(webhook.expose(), timeout=10)
        except requests.RequestException as exc:
            # str(exc) embeds the full URL, token included. Class name only.
            print(f"preflight attempt {n}: {type(exc).__name__}", flush=True)
            time.sleep(2 ** n)
            continue
        if r.status_code != 200:
            raise SystemExit(f"preflight: HTTP {r.status_code}, check {WEBHOOK_FILE}")
        try:
            name = r.json()["name"]
        except (ValueError, KeyError):
            raise SystemExit("preflight: response was not a Discord webhook object")
        print(f"webhook ok: {name}", flush=True)
        return
    raise SystemExit("preflight: unreachable after retries")


def newest_log():
    files = glob.glob(os.path.join(LOG_DIR, LOG_GLOB))
    if not files:
        return None
    try:
        return max(files, key=os.path.getmtime)
    except FileNotFoundError:
        return None


def stat_ino(path):
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def load_last():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("code")
    except (OSError, ValueError):
        return None


def save_last(code):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"code": code, "ts": time.time()}, f)
    os.replace(tmp, STATE_FILE)


def notify(webhook, code):
    payload = {"content": f"Valheim server is up.\nJoin code: `{code}`"}
    for attempt in range(5):
        try:
            r = requests.post(webhook.expose(), json=payload, timeout=10)
            if r.status_code < 300:
                return True
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 5))
                except (ValueError, TypeError, AttributeError):
                    wait = 5.0
            else:
                wait = min(2 ** attempt, 60)
            reason = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            wait = min(2 ** attempt, 60)
            reason = type(exc).__name__
        print(
            f"notify failed (attempt {attempt + 1}): {reason}, retry in {wait:.0f}s",
            flush=True,
        )
        time.sleep(wait)
    print("notify gave up after 5 attempts", flush=True)
    return False


def main():
    webhook = load_webhook()
    preflight(webhook)
    last_code = load_last()
    current = None
    ino = None
    fh = None
    print(f"watching {LOG_DIR}", flush=True)
    while True:
        # Rotation check: a new inode means LGSM replaced the file, and a size
        # below our read offset means it was truncated in place. The old
        # os.path.exists check saw the path still present and kept reading a
        # dead handle forever.
        if fh:
            live = stat_ino(current)
            try:
                shrank = os.path.getsize(current) < fh.tell()
            except OSError:
                shrank = True
            if live != ino or shrank:
                print("log rotated, reopening", flush=True)
                fh.close()
                fh, current, ino = None, None, None
        candidate = newest_log()
        if candidate != current:
            if fh:
                fh.close()
            current = candidate
            if current:
                fh = open(current, "rb")
                ino = os.fstat(fh.fileno()).st_ino
                print(f"following {os.path.basename(current)}", flush=True)
            else:
                fh, ino = None, None
        if not fh:
            time.sleep(5)
            continue
        line = fh.readline().decode(errors="replace")
        if not line:
            time.sleep(1)
            continue
        match = PATTERN.search(line[:MAX_LINE])
        if not match:
            continue
        code = match.group("code")
        if code == last_code:
            continue
        if notify(webhook, code):
            last_code = code
            save_last(code)
            print("posted new join code", flush=True)


if __name__ == "__main__":
    main()
