"""Out-of-runtime connectors.

The ``deferred`` subpackage holds providers that are intentionally NOT part of
the active v1 runtime (Massive market data, Benzinga news, web scraper). They
are kept as labelled seams so they are cheap to wire in later — but nothing in
the active runtime may import or call them.
"""
