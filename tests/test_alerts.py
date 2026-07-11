"""Alert sender (PR9): a thin ntfy.sh push-notification wrapper. Hermetic --
every test monkeypatches urllib.request.urlopen, so no real network call is
ever made. Covers: never raises regardless of failure mode, the unset-topic
no-op never attempts a network call, a real send failure logs a system_events
WARNING (and a failure in THAT logging still never escapes), and the request
carries the given title/message/priority.
"""

from __future__ import annotations

import urllib.error

from alphaos.util import alerts
from conftest import make_settings


def _settings(topic="test-topic"):
    return make_settings(NTFY_TOPIC=topic)


# --------------------------------------------------------------- unset topic
def test_unset_topic_is_a_silent_noop_and_never_calls_urlopen(monkeypatch):
    # A mutable call-recorder, not a raised exception -- send_alert's own
    # try/except would otherwise swallow a raise here and mask the bug this
    # test exists to catch (an early-return skip that never happens).
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append((a, k)))

    result = alerts.send_alert(_settings(topic=""), "title", "message")

    assert result is False
    assert calls == []  # never even attempted a network call


def test_unset_topic_noop_does_not_log_a_system_event(journal):
    """The unset-topic case is expected/normal (operator hasn't configured
    NTFY_TOPIC yet) -- it must NOT produce a system_events row, unlike a real
    send failure below."""
    result = alerts.send_alert(_settings(topic=""), "title", "message", journal=journal)

    assert result is False
    assert journal.count_rows("system_events") == 0


# ------------------------------------------------ conftest network backstop
def test_forgetting_to_monkeypatch_urlopen_is_blocked_by_the_autouse_fixture(journal):
    """Scope/safety audit LOW-2: the zero-network-leak guarantee previously
    rested only on mock-mode-default + unset-topic-default, with no hard
    stop for a test that configures a real topic and forgets to stub
    send_alert/urlopen. This test deliberately does NOT monkeypatch urlopen
    -- proving tests/conftest.py's autouse `_block_real_network_calls`
    fixture is what stands between a real topic + a missed stub and an
    actual outbound POST to ntfy.sh.

    Asserting `result is False` alone wouldn't distinguish "blocked by our
    fixture" from "a real urlopen call happened to fail for some unrelated
    reason" (e.g. no network in a sandboxed test runner) -- so this checks
    the specific guard message landed in system_events, proving OUR fixture,
    not incidental network absence, is what stopped the call."""
    result = alerts.send_alert(_settings(topic="a-real-topic"), "title", "message", journal=journal)

    assert result is False
    row = journal.one("SELECT * FROM system_events WHERE category = 'alerts'")
    assert "real urllib.request.urlopen() call to ntfy.sh" in row["detail_json"]


# ------------------------------------------------------------------ success
class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_successful_send_returns_true_and_carries_title_message_priority(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["title"] = req.get_header("Title")
        captured["priority"] = req.get_header("Priority")
        captured["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = alerts.send_alert(_settings(topic="my-topic"), "Job failed", "boom detail", priority="high")

    assert result is True
    assert captured["url"] == "https://ntfy.sh/my-topic"
    assert captured["data"] == b"boom detail"
    assert captured["title"] == "Job failed"
    assert captured["priority"] == "high"
    assert captured["timeout"] == 5


def test_successful_send_does_not_log_any_system_event(monkeypatch, journal):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResponse(200))

    result = alerts.send_alert(_settings(), "title", "message", journal=journal)

    assert result is True
    assert journal.count_rows("system_events") == 0


# -------------------------------------------------------------- HTTP error
def test_http_error_returns_false_logs_warning_never_raises(monkeypatch, journal):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://ntfy.sh/x", 500, "Internal Server Error", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)

    result = alerts.send_alert(_settings(), "Job failed", "boom detail", journal=journal)

    assert result is False
    row = journal.one("SELECT * FROM system_events WHERE category = 'alerts' ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert row["severity"] == "warning"
    assert "Job failed" in row["message"]


def test_network_error_returns_false_and_never_raises(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    result = alerts.send_alert(_settings(), "title", "message")  # no journal -- must still not raise

    assert result is False


def test_unexpected_exception_from_urlopen_never_raises(monkeypatch):
    """Belt: send_alert's own try/except must catch ANY exception type, not
    just urllib.error's -- e.g. a malformed title could raise ValueError from
    Request/add_header, or a monkeypatched test double could raise anything."""
    def boom(req, timeout=None):
        raise RuntimeError("something totally unexpected")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    result = alerts.send_alert(_settings(), "title", "message")

    assert result is False


def test_a_crash_in_journal_logging_itself_still_never_escapes(monkeypatch, journal):
    """Suspenders: if journal.log_system_event itself raises (e.g. DB locked),
    send_alert must still return False cleanly rather than propagate."""
    def boom(req, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr(journal, "log_system_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")))

    result = alerts.send_alert(_settings(), "title", "message", journal=journal)

    assert result is False  # did not raise despite the logging crash


# ------------------------------------------------------------- default arg
def test_journal_is_optional_and_defaults_to_no_logging(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    result = alerts.send_alert(_settings(), "title", "message")  # journal omitted entirely

    assert result is False


# --------------------------------------------------------- secret redaction
# Audit finding (HIGH): ntfy.sh is a NEW public egress channel for text that
# used to stay local (system_events). A configured secret VALUE must never
# reach the outbound POST body/headers, nor the local system_events log,
# regardless of which exception/reason string happens to carry it.
def test_a_configured_secret_value_is_redacted_from_the_outbound_request(monkeypatch):
    secret = "sk-supersecretvalue1234567890"
    s = make_settings(NTFY_TOPIC="topic", OPENAI_API_KEY=secret)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["title"] = req.get_header("Title")
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    alerts.send_alert(s, f"leaked in title: {secret}", f"leaked in body: {secret}")

    assert secret not in captured["data"].decode("utf-8")
    assert secret not in captured["title"]
    assert "REDACTED" in captured["title"]


def test_a_configured_secret_value_is_redacted_from_the_local_failure_log(monkeypatch, journal):
    secret = "alpaca-secret-abcdefghijklmnop"
    s = make_settings(NTFY_TOPIC="topic", ALPACA_SECRET_KEY=secret)

    def boom(req, timeout=None):
        raise urllib.error.URLError(f"connection failed for key {secret}")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    alerts.send_alert(s, "title", f"failure detail mentions {secret}", journal=journal)

    row = journal.one("SELECT * FROM system_events WHERE category = 'alerts' ORDER BY id DESC LIMIT 1")
    assert secret not in row["detail_json"]
    assert secret not in row["message"]


def test_a_short_secret_value_is_not_redacted_to_avoid_mangling_unrelated_text():
    """A trivially short/empty secret field must not trigger blanket
    substring replacement across ordinary alert text."""
    s = make_settings(NTFY_TOPIC="topic", OPENAI_API_KEY="ab")  # below the 6-char redaction floor

    from alphaos.util.alerts import _sanitize

    text = "this message happens to contain ab in the middle of a word"
    assert _sanitize(text, s) == text  # unchanged -- "ab" is too short to redact


def test_an_oversized_message_is_truncated_before_being_sent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    huge_message = "x" * 5000
    alerts.send_alert(_settings(), "title", huge_message)

    sent = captured["data"].decode("utf-8")
    assert len(sent) < 5000
    assert sent.endswith("...(truncated)")
