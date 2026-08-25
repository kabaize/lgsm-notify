# lgsm-notify

A sidecar container that watches a [LinuxGSM](https://linuxgsm.com/) game server's
console log, extracts the crossplay join code, and posts it to a private Discord
channel via webhook.

Valheim's crossplay join code routes through PlayFab, so friends can connect
without anyone sharing a public IP. The code is regenerated on every server
restart, which on a home connection with a dynamic ISP address is exactly when
people need it most. This automates the announcement.

Currently supports Valheim. The name is `lgsm-notify` because the log-watching
approach generalizes to any LinuxGSM-managed server; see [Roadmap](#roadmap).

## How it works

There is no network path between the notifier and the game server. No Docker
socket, no `exec`, no API. The sole coupling is a shared host directory:

```
game server ──writes──> ${LGSM_LOG_DIR} <──reads(ro)── lgsm-notify ──https──> Discord
```

The notifier tails the live console log, matches a fixed
machine-generated line, and posts on any code it has not already sent. State
persists in a named volume so a container restart does not repost.

`depends_on` is cosmetic ordering only; the notifier tolerates a missing or
absent log directory and waits.

## Quick start

```bash
git clone https://github.com/<you>/lgsm-notify
cd lgsm-notify

cp .env.example .env
$EDITOR .env                      # set LGSM_LOG_DIR

mkdir -p secrets
printf '%s' 'https://discord.com/api/webhooks/ID/TOKEN' > secrets/discord_webhook.txt
chmod 600 secrets/discord_webhook.txt

docker compose -f compose.example.yaml up -d --build
docker compose -f compose.example.yaml logs -f
```

Expected startup output:

```
webhook ok: my-webhook-name
watching /logs
following vhserver-console.log
```

`webhook ok:` means a `GET` on the configured URL returned a real Discord
webhook object. If it does not appear, the container exits with a specific
reason rather than running silently.

Requires `-crossplay` in the LinuxGSM start parameters. Without it no join code
is generated and there is nothing to match.

## Configuration

| Variable       | Default                        | Purpose                                     |
| -------------- | ------------------------------ | ------------------------------------------- |
| `LGSM_LOG_DIR` | none, required                 | Host path to the LGSM console log directory |
| `LOG_DIR`      | `/logs`                        | Mount point inside the container            |
| `STATE_FILE`   | `/state/last.json`             | Last-posted code, on a named volume         |
| `WEBHOOK_FILE` | `/run/secrets/discord_webhook` | Path to the webhook secret                  |
| `TZ`           | `UTC`                          | Affects container-local timestamps only     |

`WEBHOOK_FILE` is a **path**, not a URL. The webhook itself is never passed as
an environment variable; see [Threat model](#threat-model).

## Threat model

Two classes of secret pass through this program:

1. **The Discord webhook token.** It is a URL _path segment_, not a header. Any
   code that prints the URL discloses a write credential for the channel.
2. **The join code.** It grants access to the game server for as long as the
   session lives.

Container stdout is routinely shipped to log aggregators, so **information
disclosure through logs is the primary risk here**, not remote code execution
or privilege escalation. The controls below follow from that.

### Controls

**Neither secret is ever written to stdout.** Enforced by tests, not by
convention. See `tests/test_no_token_leak.py`.

**The webhook is wrapped in a `WebhookURL` type** whose `__str__`, `__repr__`,
and `__format__` all render `<webhook redacted>`. An f-string, a `%s`, a
`print()`, or a repr in a traceback cannot leak it. The real value comes out
only via `.expose()`, which is deliberately greppable:

```bash
grep -n 'expose()' notify.py   # Every hit should be a requests call
```

Same idea as Rust's `secrecy::Secret` and Pydantic's `SecretStr`.

**`raise_for_status()` and `print(exc)` are banned.** `requests` embeds the full
request URL in its exception messages, which is precisely how the token would
escape. Failures log the HTTP status code and the exception class name, never
the URL. A Semgrep rule in `.semgrep/` fails CI if any of these patterns return.

**The webhook is delivered as a file secret, not an environment variable.**
Environment variables are copied into container config on disk
(`docker inspect`, `/var/lib/docker/containers/*/config.v2.json`), rendered by
`docker compose config`, readable at `/proc/1/environ` by anything in the
container, and inherited by child processes. That on-disk config persists for
the container's lifetime and gets swept into filesystem-level backups, which
for a restic or similar snapshot means the credential cannot be unwritten. A
file has a path, a mode, and an owner, which are the three things access control
is built from.

**The URL is validated on load**, not merely prefix-checked: scheme must be
`https`, host must be a known Discord host, path must begin with
`/api/webhooks/`. This stops a typo or a hostile config file from redirecting
posts to an arbitrary endpoint. Failures name the rejected _hostname_ only.

**Startup preflight.** A wrong-but-live URL that returns 2xx on `POST` would
otherwise cause the program to mark codes as delivered and persist state for
messages nobody received. A `GET` on a genuine webhook returns its JSON object;
anything else is a hard exit.

### Container hardening

`read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, non-root
`1000:1000`, `tmpfs` for `/tmp`, log mount is `:ro`, and no `networks:` key so
the container lands on the project default bridge with egress to Discord and no
route to the game server network.

### Not addressed

- `readline()` can return a partial line if the writer is mid-write. A
  tell/seek-back guard is planned.
- A file switch discards any unread tail; a `drain()` before close is planned.
- The whole file is read from byte 0. Fine for a single-session log, fragile if
  LGSM log configuration changes.
- The Python base image is pinned by tag, not digest. A tag can
  be repointed upstream without notice. Digest-pinning
  is the stronger supply-chain guarantee and is planned.
- `requirements.txt` pins exact versions but not hashes. `pip install --require-hashes`
  would catch a compromised package mirror serving a tampered artifact under
  the same version number; not yet in place.
- No `SECURITY.md` / vulnerability disclosure policy. Planned, low effort.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
pip install semgrep && semgrep scan --config .semgrep/ --error --metrics=off .
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect --source /repo --redact -v
docker run --rm -v "$PWD:/repo" aquasec/trivy:latest fs --scanners vuln,secret,misconfig /repo
```

All four run in CI on every push-to-main, PRs into main/develop, workflow_dispatch, or cron schedule. See `.github/workflows/cicd.yml`.

### Never commit

`secrets/`, any real webhook URL (including in screenshots or pasted log
output), real console logs as test fixtures (they contain live join codes,
player Steam IDs, player names, and the host's public IP), `.env`, and
`last.json`. `.gitignore` covers all of these.

## Notes on running LinuxGSM in Docker

Do not use LGSM `start` / `stop` / `restart` / `monitor` inside the container;
use `docker restart <container>` and the restart policy. A helper for the
remaining LGSM commands:

```bash
vh() { docker exec -u linuxgsm -w /app -it valheimserver ./vhserver "$@"; }
```

On restart, LGSM replaces the live log file. The watcher detects this by inode
change and by truncation, not by path existence: a path-existence check sees
the path still present, keeps a dead file handle, and silently reads nothing
forever.

## Roadmap

- Pattern packs for other LinuxGSM-supported servers
- Notifier plugins beyond Discord
- ReDoS guardrails on user-supplied patterns
- PII default-deny on captured groups

## License

MIT
