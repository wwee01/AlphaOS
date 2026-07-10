"""CANARY: model-drift canary. Weekly replay of a frozen prompt set through
the CURRENT playbook classifier -- never a reimplementation -- to detect
silent upstream OpenAI model changes before they contaminate weeks of ledger
data. Distinct from EVAL-1 (which answers "is this prompt better?"); CANARY
answers only "did the configured model change under us?". Zero decision
surface: never read by any gate/eval/labeller/risk/execution path.
"""

from alphaos.canary.corpus import DEFAULT_CORPUS_DIR
from alphaos.canary.run import get_baseline_run, pin_baseline, run_canary

__all__ = ["DEFAULT_CORPUS_DIR", "run_canary", "pin_baseline", "get_baseline_run"]
