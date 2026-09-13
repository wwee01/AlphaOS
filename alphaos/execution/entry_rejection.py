"""Validate durable, request-bound broker rejection evidence before recovery."""
from __future__ import annotations

import math
import re


def validate_rejection_proof(row, proof):
    """A transport-captured rejection must still match the retained intent.

    Only the broker adapter creates this evidence. A stored message, HTTP status
    alone, or repeated client-ID absence cannot substitute for that capture.
    """
    if (not isinstance(proof, dict) or proof.get("type") != "alpaca_entry_broker_rejected"
            or type(proof.get("version")) is not int or proof["version"] != 1
            or type(proof.get("http_status")) is not int or proof["http_status"] not in {400, 403, 422}
            or type(proof.get("error_code")) is not int
            or proof["error_code"] != proof["http_status"] * 100000 + 10000
            or proof.get("classification") not in {"invalid_request", "wash_trade", "insufficient_buying_power",
                                                   "invalid_order_terms"}
            or proof.get("client_order_id") != row["client_order_id"] or proof.get("symbol") != row["symbol"]
            or not isinstance(proof.get("response_body_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof["response_body_sha256"])):
        raise ValueError("broker rejection proof is invalid or belongs to another entry")
    request = proof.get("request")
    if not isinstance(request, dict):
        raise ValueError("broker rejection proof lacks the submitted request")
    expected = {"client_order_id": row["client_order_id"], "symbol": row["symbol"],
                "side": "sell" if row["direction"] == "short" else "buy", "type": "limit",
                "order_class": "bracket", "time_in_force": row["time_in_force"]}
    if (set(request) != set(expected) | {"qty", "limit_price", "take_profit", "stop_loss"}
            or any(request.get(key) != value for key, value in expected.items())):
        raise ValueError("broker rejection request changed the authorized entry terms")
    allowed = {400: {"invalid_request"}, 403: {"wash_trade", "insufficient_buying_power"},
               422: {"invalid_order_terms"}}
    if proof["classification"] not in allowed[proof["http_status"]]:
        raise ValueError("broker rejection classification does not match its response")

    def same_number(actual, intended):
        try:
            value = float(actual)
            return not isinstance(actual, bool) and math.isfinite(value) and value > 0 and abs(value - intended) < 1e-8
        except (TypeError, ValueError):
            return False

    target, stop = request.get("take_profit"), request.get("stop_loss")
    if (not same_number(request.get("qty"), float(row["qty"]))
            or not same_number(request.get("limit_price"), round(float(row["limit_price"]), 2))
            or not isinstance(target, dict) or set(target) != {"limit_price"}
            or not same_number(target.get("limit_price"), round(float(row["take_profit_price"]), 2))
            or not isinstance(stop, dict) or set(stop) != {"stop_price"}
            or not same_number(stop.get("stop_price"), round(float(row["stop_loss_price"]), 2))):
        raise ValueError("broker rejection request changed the authorized quantities or prices")
    return proof
