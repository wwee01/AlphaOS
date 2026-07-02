"""Measurement / counterfactual-learning layer.

Turns every scanned candidate — proposed, rejected, armed-watch, or
user-overridden — into learnable forward-outcome data, whether or not it ever
became a real trade. PURE MEASUREMENT: this package is never read by the
scan/eval/labeller/risk/execution/approval path; it only reads already-recorded
decisions and observes bars that came after them. Not a de-novo historical
backtest — it only replays decisions AlphaOS actually made.
"""
