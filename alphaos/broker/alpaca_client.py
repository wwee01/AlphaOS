"""Alpaca paper-trading connector.

Strict paper-only guardrails (non-negotiable):
* ``submit_*`` refuse unless ``REAL_TRADING_ENABLED`` is exactly 'false',
  ``ALPACA_PAPER=true``, and ``ALPACA_BASE_URL`` is the paper endpoint.
* The TradingClient is always constructed with ``paper=True``. There is no
  live-money endpoint anywhere in this class.

Two modes:
* If ``EXECUTION_PROVIDER=alpaca_paper`` (and paper safety holds), this places
  REAL orders against the Alpaca **paper** API via alpaca-py (lazy import).
* Otherwise execution stays simulated internally and this connector is unused.

A ``trading_client`` may be injected (tests use a fake) so the order lifecycle
is exercised hermetically without the SDK or network. All broker objects are
normalized to SDK-agnostic dicts (see ``order_mapping``).
"""

from __future__ import annotations

from typing import Optional

from alphaos.broker import order_mapping
from alphaos.config.settings import PAPER_BASE_URL, Settings
from alphaos.constants import REAL_TRADING_REQUIRED_VALUE, Severity, TradeDirection


class AlpacaSafetyError(Exception):
    """Raised when a submission would violate the paper-only guardrails."""


class AlpacaNotConnected(Exception):
    """Raised when a real paper client cannot be constructed (e.g. SDK missing)."""


class AlpacaClient:
    def __init__(self, settings: Settings, journal=None, trading_client=None):
        self.settings = settings
        self.journal = journal
        self._client = trading_client  # injectable; real one built lazily

    # --------------------------------------------------------------- guards
    def preflight(self) -> None:
        """Raise AlpacaSafetyError unless every paper-only condition holds."""
        s = self.settings
        if s.real_trading_enabled_raw != REAL_TRADING_REQUIRED_VALUE:
            raise AlpacaSafetyError(
                f"REAL_TRADING_ENABLED must be 'false' (got {s.real_trading_enabled_raw!r})."
            )
        if not s.alpaca_paper:
            raise AlpacaSafetyError("ALPACA_PAPER must be true.")
        if s.alpaca_base_url.rstrip("/") != PAPER_BASE_URL:
            raise AlpacaSafetyError(f"ALPACA_BASE_URL must be {PAPER_BASE_URL}.")
        if not s.has_alpaca_keys:
            raise AlpacaSafetyError("Alpaca API key and secret are required.")

    @property
    def is_safe_paper(self) -> bool:
        try:
            self.preflight()
            return True
        except AlpacaSafetyError:
            return False

    def capabilities(self) -> dict:
        return {"bracket": True, "oco": True, "short": True, "fractional": False}

    # ---------------------------------------------------------- client build
    def _trading_client(self):
        if self._client is not None:
            return self._client
        self.preflight()  # never build a client unless paper-safe
        try:  # pragma: no cover - exercised only with the live SDK + creds
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(
                api_key=self.settings.alpaca_api_key,
                secret_key=self.settings.alpaca_secret_key,
                paper=True,  # hard-wired: paper only
            )
            return self._client
        except ImportError as exc:  # pragma: no cover
            raise AlpacaNotConnected(f"alpaca-py not installed: {exc}")

    # --------------------------------------------------------------- submit
    def submit_bracket(self, proposal) -> dict:
        """Submit a broker-native bracket (entry + take-profit + stop-loss, OCO)
        to the Alpaca PAPER API. Returns a normalized order dict."""
        self.preflight()  # never bypassed
        client = self._trading_client()
        spec = {
            "symbol": proposal.symbol,
            "qty": int(proposal.qty),
            "side": "sell" if proposal.direction == TradeDirection.SHORT.value else "buy",
            "entry": round(float(proposal.entry), 2),
            "target": round(float(proposal.target), 2),
            "stop": round(float(proposal.stop), 2),
            "tif": "day",
            "client_order_id": proposal.proposal_id,
        }
        order = self._submit(client, spec)
        normalized = order_mapping.normalize_order(order)
        if self.journal is not None:
            self.journal.log_system_event(
                Severity.INFO, "broker",
                f"Alpaca PAPER bracket submitted for {proposal.symbol} "
                f"(broker_order_id={normalized['broker_order_id']}, status={normalized['status']}).",
            )
        return normalized

    def _submit(self, client, spec: dict):
        # Fakes (tests) consume the SDK-agnostic spec directly, so the SDK is
        # only imported on the real branch (CI has no alpaca-py installed).
        if getattr(client, "FAKE", False):
            return client.submit(spec)
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest  # pragma: no cover
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce  # pragma: no cover

        side = OrderSide.SELL if spec["side"] == "sell" else OrderSide.BUY  # pragma: no cover
        request = LimitOrderRequest(  # pragma: no cover
            symbol=spec["symbol"], qty=spec["qty"], side=side, time_in_force=TimeInForce.DAY,
            limit_price=spec["entry"], order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=spec["target"]),
            stop_loss=StopLossRequest(stop_price=spec["stop"]),
            client_order_id=spec["client_order_id"],
        )
        return client.submit_order(request)  # pragma: no cover

    # ----------------------------------------------------- reconciliation
    def get_order(self, broker_order_id: str) -> dict:
        client = self._trading_client()
        order = client.get_order_by_id(broker_order_id)
        return order_mapping.normalize_order(order)

    def list_positions(self) -> list[dict]:
        client = self._trading_client()
        return [order_mapping.normalize_position(p) for p in client.get_all_positions()]

    def cancel_order(self, broker_order_id: str) -> None:
        self._trading_client().cancel_order_by_id(broker_order_id)

    def get_account(self) -> dict:
        client = self._trading_client()
        acct = client.get_account()
        return {
            "account_number": getattr(acct, "account_number", None),
            "status": order_mapping._s(getattr(acct, "status", None)),
            "cash": order_mapping._f(getattr(acct, "cash", None)),
            "equity": order_mapping._f(getattr(acct, "equity", None)),
            "trading_blocked": getattr(acct, "trading_blocked", None),
            # Confirm we're talking to a paper account, never live.
            "pattern_day_trader": getattr(acct, "pattern_day_trader", None),
        }

    # legacy stub name kept for the simulated path's guardrail probe
    def submit_order(self, order_request: dict) -> dict:  # pragma: no cover
        self.preflight()
        raise AlpacaNotConnected("use submit_bracket for real paper execution; simulated path otherwise.")
