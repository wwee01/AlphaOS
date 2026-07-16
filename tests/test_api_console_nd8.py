"""ND-8 Tailscale console access contract tests (docs/roadmap/
console-migration-nd.md §3's security model, extended to be env-driven
rather than hardcoded).

Covers the ND-8 build-doc test matrix:

* Default (no CONSOLE_* env set) -> console_bind_host/console_port resolve
  to the ND-1 hardcoded values (127.0.0.1 / 8601), and the allowlist behaves
  exactly as it did before this build: loopback passes, a disallowed origin
  -> 403.
* `CONSOLE_ALLOWED_ORIGINS` set to a tailnet-shaped origin -> that origin
  passes, a DIFFERENT non-listed origin still -> 403, loopback still passes.
* Missing `X-AlphaOS-Console` header -> 403 even from an allowed
  (tailnet) origin -- the header gate is independent of the origin gate.
* Write endpoints still fail closed (503, no PIN configured) regardless of
  which allowed origin the request carries.
* `CONSOLE_BIND_HOST`/`CONSOLE_PORT` are parsed onto `Settings` correctly,
  including a malformed `CONSOLE_PORT` falling back to the default rather
  than raising.
* `CONSOLE_ALLOWED_ORIGINS` parsing is robust: blank entries and surrounding
  whitespace are dropped, never a crash.

Reuses test_api_console.py's `_seed`/`_client`/`HEADERS` fixtures (same
file-based, seeded-journal pattern every other console test file in this
suite reuses -- see test_api_console_nd2.py's own precedent) for the
read-path tests, and mirrors test_api_console_nd3.py's write-client fixture
for the PIN-still-required test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from alphaos.api.app import create_app
from alphaos.api.deps import get_kill_switch, get_nonce_store, get_pin_store, get_rate_limiter, get_settings
from alphaos.api.nonce import NonceStore
from alphaos.api.pin import PinRateLimiter, PinStore
from alphaos.api.security import loopback_origins
from alphaos.safety import KillSwitch
from conftest import make_settings
from test_api_console import HEADERS, _client, _seed

TAILNET_ORIGIN = "http://100.64.1.2:8601"
OTHER_NON_LISTED_ORIGIN = "http://100.99.9.9:8601"


def _write_client(settings, tmp_path) -> TestClient:
    """Minimal write-capable client for the PIN-still-required test --
    mirrors test_api_console_nd3.py's `_client`, but this file never
    configures a PIN (the point of the test is that no origin can bypass the
    503 fail-closed guard)."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_pin_store] = lambda: PinStore(path=str(tmp_path / "unset_pin.hash"))
    app.dependency_overrides[get_rate_limiter] = lambda: PinRateLimiter()
    app.dependency_overrides[get_nonce_store] = lambda: NonceStore()
    app.dependency_overrides[get_kill_switch] = lambda: KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    return TestClient(app)


# ------------------------------------------------------- Settings parsing


def test_default_env_console_bind_host_and_port_unchanged():
    """No CONSOLE_* env set at all -> byte-identical to ND-1's hardcoded
    values, and zero extra allowed origins."""
    settings = make_settings()
    assert settings.console_bind_host == "127.0.0.1"
    assert settings.console_port == 8601
    assert settings.console_allowed_origins == ""
    assert settings.console_allowed_origins_list == ()


def test_console_bind_host_reads_operator_value():
    settings = make_settings(CONSOLE_BIND_HOST="100.64.1.2")
    assert settings.console_bind_host == "100.64.1.2"


def test_console_bind_host_0_0_0_0_is_allowed_not_rejected():
    """0.0.0.0 (whole-LAN exposure) is the operator's explicit choice to
    honor, not something this build refuses -- see alphaos/api/__main__.py's
    docstring."""
    settings = make_settings(CONSOLE_BIND_HOST="0.0.0.0")
    assert settings.console_bind_host == "0.0.0.0"


@pytest.mark.parametrize("blank_value", ["", "   ", "\t"])
def test_console_bind_host_present_but_blank_falls_back_to_loopback(blank_value):
    """Audit-fixup (scope/safety MED): CONSOLE_BIND_HOST= (present, empty --
    as distinct from unset) previously fell through to `uvicorn.run(host="")`,
    which binds ALL interfaces (IPv4 AND IPv6) -- broader than even the
    documented, explicit 0.0.0.0 opt-in, and the exact opposite of what
    .env.example told the operator "blank" would do. A blank/whitespace-only
    value must resolve to the same safe loopback default as leaving the key
    unset entirely."""
    settings = make_settings(CONSOLE_BIND_HOST=blank_value)
    assert settings.console_bind_host == "127.0.0.1"


def test_console_port_reads_operator_value():
    settings = make_settings(CONSOLE_PORT="9000")
    assert settings.console_port == 9000


def test_console_port_malformed_falls_back_to_default_not_raise():
    """Matches `_get_int`'s existing generic malformed-value behavior
    (settings.py) -- a bad CONSOLE_PORT must not crash settings loading."""
    settings = make_settings(CONSOLE_PORT="not-a-number")
    assert settings.console_port == 8601


def test_console_allowed_origins_blank_and_whitespace_entries_ignored():
    settings = make_settings(CONSOLE_ALLOWED_ORIGINS=" , http://100.64.1.2:8601 , , ,http://mac-mini.tailnet.ts.net:8601 ,")
    assert settings.console_allowed_origins_list == (
        "http://100.64.1.2:8601",
        "http://mac-mini.tailnet.ts.net:8601",
    )


def test_console_allowed_origins_empty_string_yields_empty_tuple():
    settings = make_settings(CONSOLE_ALLOWED_ORIGINS="   ")
    assert settings.console_allowed_origins_list == ()


def test_loopback_origins_helper_matches_nd1_hardcoded_default():
    assert loopback_origins(8601) == frozenset({"http://localhost:8601", "http://127.0.0.1:8601"})


# ---------------------------------------------- default allowlist (no env)


def test_default_allowlist_disallowed_origin_still_403(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    journal.close()


def test_default_allowlist_loopback_still_passes(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": "http://localhost:8601"},
    )
    assert r.status_code == 200
    journal.close()


def test_default_allowlist_tailnet_origin_not_allowed_without_env(tmp_path):
    """With CONSOLE_ALLOWED_ORIGINS unset, a tailnet-shaped origin is just
    another disallowed origin -> 403 (proves the extra allowlist is
    opt-in, not silently permissive)."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": TAILNET_ORIGIN},
    )
    assert r.status_code == 403
    journal.close()


# ------------------------------------------ CONSOLE_ALLOWED_ORIGINS opt-in


def test_tailnet_origin_passes_when_allowlisted(tmp_path):
    _, journal, db_path = _seed(tmp_path)
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=TAILNET_ORIGIN)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": TAILNET_ORIGIN},
    )
    assert r.status_code == 200
    journal.close()


def test_different_non_listed_origin_still_403_when_one_tailnet_origin_allowed(tmp_path):
    _, journal, db_path = _seed(tmp_path)
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=TAILNET_ORIGIN)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": OTHER_NON_LISTED_ORIGIN},
    )
    assert r.status_code == 403
    journal.close()


def test_loopback_still_passes_when_tailnet_origin_allowlisted(tmp_path):
    _, journal, db_path = _seed(tmp_path)
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=TAILNET_ORIGIN)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": "http://127.0.0.1:8601"},
    )
    assert r.status_code == 200
    journal.close()


def test_magicdns_style_origin_passes_when_allowlisted(tmp_path):
    magicdns_origin = "http://mac-mini.tailnet-name.ts.net:8601"
    _, journal, db_path = _seed(tmp_path)
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=magicdns_origin)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": magicdns_origin},
    )
    assert r.status_code == 200
    journal.close()


def test_missing_console_header_403_even_from_allowed_tailnet_origin(tmp_path):
    _, journal, db_path = _seed(tmp_path)
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=TAILNET_ORIGIN)
    r = _client(settings).get("/api/v1/health", headers={"Origin": TAILNET_ORIGIN})  # no X-AlphaOS-Console
    assert r.status_code == 403
    journal.close()


# --------------------------------------------------- writes still gated


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8601", TAILNET_ORIGIN])
def test_write_endpoint_503_no_pin_regardless_of_allowed_origin(tmp_path, origin):
    """Origin allowlisting and PIN authorization are independent gates
    (docs/roadmap/console-migration-nd.md §3): being an allowed origin (even
    a newly-added tailnet one) must never substitute for a configured PIN."""
    db_path = str(tmp_path / "nd8_write_test.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=TAILNET_ORIGIN)
    client = _write_client(settings, tmp_path)
    headers = dict(HEADERS)
    if origin is not None:
        headers["Origin"] = origin
    r = client.post(
        "/api/v1/actions/scan", json={"pin": "4242", "nonce": "nd8-n1"}, headers=headers,
    )
    assert r.status_code == 503
    assert "set-pin" in r.json()["detail"]


def test_write_endpoint_still_403_for_a_non_allowed_origin(tmp_path):
    """The header/PIN gates only ever run AFTER the origin gate passes --
    confirm a genuinely disallowed origin is still turned away before PIN
    logic is ever reached."""
    db_path = str(tmp_path / "nd8_write_test2.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path, CONSOLE_ALLOWED_ORIGINS=TAILNET_ORIGIN)
    client = _write_client(settings, tmp_path)
    r = client.post(
        "/api/v1/actions/scan",
        json={"pin": "4242", "nonce": "nd8-n2"},
        headers={**HEADERS, "Origin": "http://evil.example"},
    )
    assert r.status_code == 403
