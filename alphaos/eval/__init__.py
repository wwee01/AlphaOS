"""EVAL-1: offline AI-eval harness (punch #13). Replays the frozen golden
corpus through the CURRENT playbook labeller -- never a reimplementation --
so a prompt/model change can be measured in days via replay, instead of
waiting months for enough real ledger data. Zero decision surface: never
read by any gate/eval/labeller/risk/execution path.
"""

from alphaos.eval.corpus import DEFAULT_CORPUS_DIR
from alphaos.eval.harness import run_eval

__all__ = ["DEFAULT_CORPUS_DIR", "run_eval"]
