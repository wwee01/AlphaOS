"""Import-graph guard: active runtime modules must NOT import the deferred
Massive or Benzinga connectors (Change Prompt §4, §9)."""

from __future__ import annotations

import pathlib

ALPHAOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "alphaos"

FORBIDDEN = (
    "connectors.deferred.massive",
    "connectors.deferred.benzinga",
    "connectors.deferred.web_news",
    "connectors.deferred",
)


def _runtime_py_files():
    return [p for p in ALPHAOS_DIR.rglob("*.py")]


def test_active_runtime_does_not_import_deferred_connectors():
    offenders = []
    for path in _runtime_py_files():
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append((str(path.relative_to(ALPHAOS_DIR.parent)), needle))
    assert not offenders, f"active runtime imports deferred connectors: {offenders}"


def test_active_runtime_does_not_import_connectors_package_at_all():
    offenders = []
    for path in _runtime_py_files():
        text = path.read_text(encoding="utf-8")
        if "import connectors" in text or "from connectors" in text:
            offenders.append(str(path.relative_to(ALPHAOS_DIR.parent)))
    assert not offenders, f"active runtime imports the connectors package: {offenders}"
