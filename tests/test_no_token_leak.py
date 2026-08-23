"""Assert the negative: no code path writes a credential to stdout, stderr, or a logging record.

Two classes of secret pass through this program.
  1. The webhook token, which is a path segment of the webhook URL.
  2. The join code, which grants access to the game server.

requests embeds the request URL in its exception messages, so `print(exc)` or
`raise_for_status()` leaks the token. These tests fail the build if that
regresses.
"""
import contextlib
import io
import logging

import pytest
import requests

import notify

FAKE = "https://discord.com/api/webhooks/1234567890/SUPERSECRETTOKENVALUE"
TOKEN = "SUPERSECRETTOKENVALUE"
CODE = "654321"


def raiser(exc):
    def _f(*args, **kwargs):
        raise exc

    return _f


def run_capture(fn, *args, **kwargs):
    """Run fn, returning everything it wrote to stdout, stderr, or SystemExit."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            fn(*args, **kwargs)
        except SystemExit as exc:
            buf.write(str(exc))
    return buf.getvalue()


@pytest.fixture
def hook():
    return notify.WebhookURL(FAKE)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)


# --- the wrapper itself -----------------------------------------------------

def test_webhookurl_str_is_redacted(hook):
    assert TOKEN not in str(hook)
    assert TOKEN not in repr(hook)
    assert TOKEN not in f"{hook}"
    assert TOKEN not in "%s" % (hook,)
    assert TOKEN not in "{}".format(hook)


def test_webhookurl_expose_returns_real_value(hook):
    assert hook.expose() == FAKE


def test_webhookurl_has_no_dict(hook):
    """__slots__ prevents a stray attribute from carrying the value elsewhere."""
    with pytest.raises(AttributeError):
        hook.url_copy = FAKE


# --- urllib3 debug logging --------------------------------------------------

def test_urllib3_logger_forced_above_debug():
    """Set at import time in notify.py; must survive a later root logging config."""
    assert logging.getLogger("urllib3").getEffectiveLevel() > logging.DEBUG


def test_urllib3_connectionpool_debug_record_is_suppressed(caplog):
    """urllib3.connectionpool is where the token-bearing request line would be
    logged. Even if a future `logging.basicConfig(level=logging.DEBUG)` sets
    the root logger to DEBUG, this child logger's effective level must stay
    above DEBUG, so the record is never even constructed.
    """
    caplog.set_level(logging.DEBUG)
    logging.getLogger("urllib3.connectionpool").debug(
        "POST /api/webhooks/1234567890/%s HTTP/1.1", TOKEN
    )
    assert TOKEN not in caplog.text


# --- notify() ---------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    requests.ConnectionError(f"failed to establish a connection to {FAKE}"),
    requests.Timeout(f"HTTPSConnectionPool: read timed out for url: {FAKE}"),
    requests.TooManyRedirects(f"exceeded 30 redirects for {FAKE}"),
])
def test_notify_never_prints_token_on_exception(monkeypatch, hook, exc):
    monkeypatch.setattr(notify.requests, "post", raiser(exc))
    out = run_capture(notify.notify, hook, CODE)
    assert TOKEN not in out


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_notify_never_prints_token_on_http_error(monkeypatch, hook, status):
    class Resp:
        status_code = status
        url = FAKE

        def json(self):
            return {}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Resp())
    out = run_capture(notify.notify, hook, CODE)
    assert TOKEN not in out
    assert str(status) in out  # the status IS logged; only the URL is not


def test_notify_never_prints_join_code_on_failure(monkeypatch, hook):
    monkeypatch.setattr(
        notify.requests, "post", raiser(requests.ConnectionError("boom"))
    )
    assert CODE not in run_capture(notify.notify, hook, CODE)


def test_notify_survives_non_json_429_body(monkeypatch, hook):
    """A 429 from a proxy may not be JSON. Parsing it must not crash the loop."""

    class Resp:
        status_code = 429
        url = FAKE

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Resp())
    out = run_capture(notify.notify, hook, CODE)
    assert "gave up" in out
    assert TOKEN not in out


# --- preflight() ------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 404, 500])
def test_preflight_never_prints_token_on_http_error(monkeypatch, hook, status):
    class Resp:
        status_code = status

        def json(self):
            return {}

    monkeypatch.setattr(notify.requests, "get", lambda *a, **k: Resp())
    assert TOKEN not in run_capture(notify.preflight, hook)


def test_preflight_never_prints_token_on_exception(monkeypatch, hook):
    monkeypatch.setattr(
        notify.requests, "get", raiser(requests.ConnectionError(f"no route to {FAKE}"))
    )
    assert TOKEN not in run_capture(notify.preflight, hook, attempts=2)


def test_preflight_rejects_non_webhook_json(monkeypatch, hook):
    class Resp:
        status_code = 200

        def json(self):
            return {"unexpected": "shape"}

    monkeypatch.setattr(notify.requests, "get", lambda *a, **k: Resp())
    assert "not a Discord webhook object" in run_capture(notify.preflight, hook)


# --- load_webhook() ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://discord.com/api/webhooks/1/TOKENVALUE",       # not https
    "https://evil.example.com/api/webhooks/1/TOKENVALUE",  # wrong host
    "https://discord.com/api/oauth2/authorize",            # wrong path
    "https://discord.com.evil.example/api/webhooks/1/x",   # suffix confusion
    "",                                                    # empty file
])
def test_load_webhook_rejects_bad_urls(monkeypatch, tmp_path, url):
    path = tmp_path / "discord_webhook.txt"
    path.write_text(url)
    monkeypatch.setattr(notify, "WEBHOOK_FILE", str(path))
    with pytest.raises(SystemExit):
        notify.load_webhook()


def test_load_webhook_accepts_valid_url(monkeypatch, tmp_path):
    path = tmp_path / "discord_webhook.txt"
    path.write_text(FAKE + "\n")  # trailing newline must be stripped
    monkeypatch.setattr(notify, "WEBHOOK_FILE", str(path))
    hook = notify.load_webhook()
    assert hook.expose() == FAKE


def test_load_webhook_error_omits_url(monkeypatch, tmp_path):
    path = tmp_path / "discord_webhook.txt"
    path.write_text("https://evil.example.com/api/webhooks/1/" + TOKEN)
    monkeypatch.setattr(notify, "WEBHOOK_FILE", str(path))
    assert TOKEN not in run_capture(notify.load_webhook)


# --- main() -----------------------------------------------------------------

def test_join_code_never_printed_on_success(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "vhserver-console.log").write_text(
        '08/22/2026 15:05:12: Session "testserver" with join code '
        f'{CODE} and IP 203.0.113.7:2456 is active with 0 player(s).\n'
    )
    monkeypatch.setattr(notify, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(notify, "STATE_FILE", str(tmp_path / "state" / "last.json"))
    monkeypatch.setattr(notify, "load_webhook", lambda: notify.WebhookURL(FAKE))
    monkeypatch.setattr(notify, "preflight", lambda hook, **kw: None)
    monkeypatch.setattr(notify, "notify", lambda hook, code: True)

    class Done(Exception):
        pass

    monkeypatch.setattr(notify, "save_last", raiser(Done()))
    with pytest.raises(Done):
        notify.main()

    out = capsys.readouterr().out
    assert CODE not in out
    assert TOKEN not in out
    assert "posted new join code" not in out  # save_last aborts before the print
