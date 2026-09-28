"""No network; real source adapter with fake GET bodies and transport guards."""

from copy import deepcopy
import json
from pathlib import Path
from datetime import datetime
import urllib.request

import pytest

from alphaos.reports.economic_capture import EconomicSource, collect_economic_inputs
from alphaos.reports.economic_report import main, render_economics
from alphaos.reports.account_economics import measure_account_economics
from conftest import make_settings
from test_common_valuation import FakeSourceTransport, capture


def settings(**extra):
    return make_settings(
        ALPHAOS_MODE="paper",
        OFFLINE_MODE="false",
        ALPACA_API_KEY="secret-key",
        ALPACA_SECRET_KEY="secret-secret",
        **extra,
    )


class Transport(FakeSourceTransport):
    def get(self, base, path, params):
        if path == "/v1/corporate-actions":
            self.calls.append((base, path, deepcopy(params)))
            return {"corporate_actions": {}, "next_page_token": None}
        return super().get(base, path, params)


def test_real_source_contract_bounded_nine_gets_and_unchanged_start_receipt():
    start, _ = capture()
    saved = deepcopy(start)
    fake = Transport(
        calendar=[
            {"date": "2026-09-14", "open": "09:30", "close": "16:00"},
            {"date": "2026-09-15", "open": "09:30", "close": "16:00"},
        ],
        start="2026-09-15T21:00:00+00:00",
    )
    source = EconomicSource(
        settings(),
        activity_start=start["raw_evidence"]["activities_after"]["request"]["after"],
        transport=fake.get,
        clock=fake.now,
    )
    bundle = collect_economic_inputs(source, start)
    assert bundle["get_count"] == 9 and start == saved
    assert measure_account_economics(bundle)["comparison_eligible"] is True
    assert {path for _, path, _ in fake.calls} == {
        "/v2/calendar",
        "/v2/account",
        "/v2/positions",
        "/v2/account/activities",
        "/v2/stocks/bars",
        "/v1/corporate-actions",
    }
    assert "secret-key" not in str(bundle) and "private-account-id" not in str(bundle)
    req = bundle["corporate_actions"]["request"]
    assert req["data_quality"] == "all" and "types" not in req


@pytest.mark.parametrize(
    "params", [{"REAL_TRADING_ENABLED": "true"}, {"ALPACA_PAPER": "false"}]
)
def test_paper_guard_before_any_network(params):
    with pytest.raises(ValueError, match="paper-only"):
        EconomicSource(settings(**params), activity_start="2026-09-01T20:00:00Z")


def test_account_descriptions_redacted_and_partial_evidence_sanitized():
    fake = Transport()
    fake.activities = [
        {
            "id": "x",
            "activity_type": "FEE",
            "date": "2026-09-01",
            "net_amount": "-1",
            "description": "private-account-number",
            "account_id": "private-account-id",
        }
    ]
    source = EconomicSource(
        settings(),
        activity_start="2026-09-01T20:00:00Z",
        transport=fake.get,
        clock=fake.now,
    )
    source.account()
    source.activities(datetime.fromisoformat("2026-09-03T20:00:00+00:00"))
    assert "private-account-number" not in str(
        source.partial
    ) and "private-account-id" not in str(source.partial)
    assert source.partial["activities"][0]["rows"][0]["net_amount"] == "-1"


def test_real_http_route_allowlist_and_redirect_handler_refuses_credential_reroute(
    monkeypatch,
):
    source = EconomicSource(settings(), activity_start="2026-09-01T20:00:00Z")
    with pytest.raises(ValueError, match="allowlist"):
        source._http_get("https://evil.example", "/v2/account", {})
    with pytest.raises(ValueError, match="allowlist"):
        source._http_get("https://paper-api.alpaca.markets", "/v2/orders", {})
    seen = []

    def opener(*handlers):
        seen.extend(handlers)

        class Fake:
            def open(self, request, timeout):
                assert request.get_method() == "GET" and timeout == 15
                # Exercise the installed handler exactly where urllib would.
                return handlers[0].redirect_request(
                    request, None, 302, "", {}, "https://evil.example"
                )

        return Fake()

    monkeypatch.setattr(urllib.request, "build_opener", opener)
    with pytest.raises(ValueError, match="redacted"):
        source._http_get("https://paper-api.alpaca.markets", "/v2/account", {})
    assert len(seen) == 1


@pytest.mark.parametrize("failure", ["malformed", "repeated_token", "cap"])
def test_corporate_partial_or_repeated_pages_never_claim_exhaustion(failure):
    calls = []

    def transport(base, path, params):
        calls.append(params.copy())
        if failure == "malformed":
            return {"corporate_actions": []}
        return {
            "corporate_actions": {},
            "next_page_token": "same"
            if failure == "repeated_token"
            else str(len(calls)),
        }

    source = EconomicSource(
        settings(), activity_start="2026-09-01T20:00:00Z", transport=transport
    )
    with pytest.raises(ValueError):
        source.corporate_actions(["SPY"], "2025-09-01", "2027-09-30")
    assert len(calls) <= 10
    assert not source.partial.get("corporate_actions", {}).get(
        "pagination_exhausted", False
    )


def test_offline_report_actual_fixture_no_settings_no_journal_no_overwrites(tmp_path):
    path = Path(__file__).parent / "fixtures/economic_accounting/sep18_sep25_2026.json"
    out, md = tmp_path / "report.json", tmp_path / "report.md"
    rc = main(
        [
            "--input",
            str(path),
            "--output",
            str(out),
            "--markdown",
            str(md),
            "--monthly-overhead-usd",
            "150",
        ]
    )
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["account_gain_after_declared_overhead_usd"] == "704.16"
    assert "$739.16" in md.read_text() and "unrealized" in md.read_text()
    assert out.stat().st_mode & 0o777 == 0o600
    before = out.read_bytes()
    with pytest.raises(SystemExit):
        main(["--input", str(path), "--output", str(out)])
    assert out.read_bytes() == before
    assert "Research activation: **false**" in render_economics(result)
