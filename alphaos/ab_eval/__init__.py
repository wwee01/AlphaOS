"""AB-EVAL-1: primary-evaluator A/B replay harness (shadow, read-only).

Replays IDENTICAL frozen ``openai_evaluations`` snapshots through two
models via the production ``OpenAIClient`` call path (only the model name
parameterized -- no forked prompt), to attribute the 2026-07-09+ propose
drought between INSTR-1 floor mechanics, model temperament, and market
conditions. See ``docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md``,
"## AB-EVAL-1".

Zero decision surface: nothing in this package is ever read by the live
scan/gate/risk/execution path -- same law as CANARY/EVAL-1.
"""
