"""Alpaca paper-trading connector — guarded stub.

Strict paper-only guardrails (non-negotiable):
* ``submit_order`` refuses unless ``REAL_TRADING_ENABLED`` is exactly 'false',
  ``ALPACA_PAPER=true``, and ``ALPACA_BASE_URL`` is the paper endpoint.
* There is no live-money endpoint anywhere in this class.

v1 is a stub: it implements the preflight guardrails and a capability probe, but
it does not place real orders. After guardrails pass it raises
``AlpacaNotConnected`` so the OrderManager falls back to clearly-labelled
simulated execution. Wiring the real alpaca-py calls is the documented next step.
"""

from __future__ import annotations

from alphaos.config.settings import PAPER_BASE_URL, Settings
from alphaos.constants import REAL_TRADING_REQUIRED_VALUE


class AlpacaSafetyError(Exception):
    """Raised when a submission would violate the paper-only guardrails."""


class AlpacaNotConnected(Exception):
    """Raised by the v1 stub after guardrails pass (no live connection yet)."""


class AlpacaClient:
    def __init__(self, settings: Settings, journal=None):
        self.settings = settings
        self.journal = journal

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

    # ---------------------------------------------------------- capabilities
    def capabilities(self) -> dict:
        """Capability probe. A real impl queries the Alpaca API; the stub
        reports the documented Alpaca paper equity capabilities and degrades
        gracefully. Order-type support is always confirmed before use."""
        return {
            "bracket": True,   # Alpaca supports bracket (entry + TP + SL, OCO)
            "oco": True,
            "short": True,     # paper short supported; margin gate handled upstream
            "fractional": False,
        }

    # --------------------------------------------------------------- submit
    def submit_order(self, order_request: dict) -> dict:
        """Submit a PAPER order. Enforces guardrails, then (v1 stub) raises
        AlpacaNotConnected so execution falls back to simulation."""
        self.preflight()  # never bypassed
        if self.journal is not None:
            self.journal.log_system_event(
                "info",
                "broker",
                "Alpaca paper connector is a v1 stub; guardrails passed, "
                "falling back to simulated paper execution.",
                {"symbol": order_request.get("symbol")},
            )
        raise AlpacaNotConnected("v1 Alpaca connector is a stub (no live submission).")
