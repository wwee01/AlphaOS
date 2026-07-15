"""JournalStore — the SQLite source of truth.

Responsibilities:
* create/maintain the schema,
* stamp every row with UTC + SGT (+ market ET where the column exists),
* provide a small, safe insert/query surface,
* expose the counters the risk engine and approval path need
  (trades today, auto-approvals today, open positions, realized P&L today),
* optionally mirror append-only event streams to JSONL for durability
  (SQLite remains the source of truth).

History is never silently overwritten: state changes append to ``order_events``
even when ``paper_orders.state`` is also updated for fast reads.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Optional

from alphaos.constants import FailsafeReason, LabelSource, ProposalStatus, Severity
from alphaos.journal.schema import INDEXES, SCHEMA, SCHEMA_VERSION
from alphaos.lineage.hashing import stable_hash
from alphaos.util import timeutils
from alphaos.util.ids import new_id


class JournalStore:
    def __init__(
        self, db_path: str = "data/alphaos.db", jsonl_mirror: bool = False, read_only: bool = False,
    ):
        self.db_path = db_path
        self.jsonl_mirror = jsonl_mirror
        self.read_only = read_only
        self._columns: dict[str, set[str]] = {}
        if read_only:
            # ND-1 (console read-only API): SQLite's own `mode=ro` URI -- writes
            # are structurally impossible at the driver level (any INSERT/
            # UPDATE/DDL raises sqlite3.OperationalError), not merely absent by
            # caller discipline. Schema init/migration is deliberately skipped
            # here: a read-only connection must never attempt a DDL write, and
            # the writer process (scheduler/dashboard/CLI, which always opens
            # non-read-only) already owns keeping the schema current before any
            # read-only client connects.
            #
            # check_same_thread=False: FastAPI runs sync dependencies/route
            # handlers via Starlette's threadpool (anyio.to_thread.run_sync),
            # which does NOT guarantee a single request's dependency chain and
            # endpoint body execute on the same OS thread -- confirmed live
            # (boot-check against alphaos/api/__main__.py): a connection opened
            # in alphaos/api/deps.py's get_journal() was then used from a
            # different worker thread inside the SAME request, and sqlite3's
            # default check_same_thread=True raised ProgrammingError. Safe to
            # disable here specifically: each read_only connection is opened
            # fresh per request (see get_journal's generator-with-finally) and
            # never shared across concurrent requests, so this only relaxes an
            # over-strict same-thread check on a connection that is (a)
            # structurally incapable of writing and (b) used strictly
            # sequentially within one request's lifecycle, never genuinely
            # concurrently from two threads at once.
            uri = f"file:{os.path.abspath(db_path)}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            return
        if db_path not in (":memory:", "") and not db_path.startswith("file:"):
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        try:
            if db_path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.init_schema()

    # ------------------------------------------------------------------ setup
    def init_schema(self) -> None:
        cur = self.conn.cursor()
        for _name, ddl in SCHEMA:
            cur.execute(ddl)
        self.conn.commit()
        # Reconcile additive columns BEFORE building indexes: an index may target a
        # column that an older DB is missing until _migrate() adds it.
        self._migrate()
        cur = self.conn.cursor()
        for idx in INDEXES:
            cur.execute(idx)
        self.conn.commit()
        self._columns.clear()

    # ------------------------------------------------------------ migration
    def _migrate(self) -> None:
        """Forward-only, lightweight schema migration.

        Keeps the ledger/positions schema stable across pulls: any column added
        to SCHEMA is reconciled onto an existing DB automatically (additive,
        idempotent), so a ledger written by an older build keeps working. SQLite's
        ``PRAGMA user_version`` records the schema generation for diagnostics and
        as a hook for future destructive/transforming steps (which the additive
        reconciler cannot express and must be added here explicitly).
        """
        try:
            added = self._reconcile_columns()
        except sqlite3.Error as exc:  # surface loudly; never run on a half-migrated DB
            raise RuntimeError(f"schema migration failed: {exc}") from exc
        self.conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        self.conn.commit()
        if added:
            try:
                self.log_system_event(
                    Severity.WARNING,
                    "schema_migration",
                    f"Aligned DB to schema v{SCHEMA_VERSION}: added {len(added)} missing column(s).",
                    {"added": added},
                )
            except sqlite3.Error:  # pragma: no cover - audit log is best-effort
                pass

    def _reconcile_columns(self) -> list[str]:
        """Add any column present in SCHEMA but missing from the live DB.

        Additive only: existing rows are preserved and re-added columns are
        nullable (SQLite cannot add a NOT NULL column to a populated table without
        a default). Returns the ``table.column`` names that were added.
        """
        added: list[str] = []
        for table, coldefs in self._expected_columns().items():
            info = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:  # table just created by init_schema; defensive skip
                continue
            actual = {r["name"] for r in info}
            for name, coldef in coldefs.items():
                if name not in actual:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
                    added.append(f"{table}.{name}")
        return added

    @staticmethod
    def _expected_columns() -> dict[str, dict[str, str]]:
        """Columns each table should have, derived from SCHEMA (the source of
        truth), as ``{table: {column: "<add-column-ddl>"}}``."""
        probe = sqlite3.connect(":memory:")
        try:
            for _name, ddl in SCHEMA:
                probe.execute(ddl)
            expected: dict[str, dict[str, str]] = {}
            for (table,) in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall():
                cols: dict[str, str] = {}
                for _cid, name, ctype, _notnull, dflt, _pk in probe.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall():
                    coldef = f"{name} {ctype or 'TEXT'}"
                    if dflt is not None:
                        coldef += f" DEFAULT {dflt}"
                    cols[name] = coldef
                expected[table] = cols
            return expected
        finally:
            probe.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "JournalStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- internals
    def _cols(self, table: str) -> set[str]:
        if table not in self._columns:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            self._columns[table] = {r["name"] for r in rows}
            if not self._columns[table]:
                raise ValueError(f"unknown table: {table}")
        return self._columns[table]

    def insert(self, table: str, data: dict[str, Any], mirror: bool = False) -> int:
        """Insert ``data`` into ``table``, auto-stamping timestamp columns.

        Unknown columns raise (to catch typos early). JSON-serializable values
        for *_json columns may be passed as dict/list and are encoded here.
        """
        cols = self._cols(table)
        st = timeutils.stamp()
        row: dict[str, Any] = {}
        if "created_at_utc" in cols:
            row["created_at_utc"] = st.utc
        if "created_at_sgt" in cols:
            row["created_at_sgt"] = st.local_sgt
        if "created_at_market" in cols:
            row["created_at_market"] = st.market_et

        for key, value in data.items():
            if key not in cols:
                raise ValueError(f"{table}: unknown column {key!r}")
            if value is not None and (key.endswith("_json")) and not isinstance(value, str):
                value = json.dumps(value, default=str)
            if isinstance(value, bool):
                value = 1 if value else 0
            row[key] = value

        placeholders = ", ".join("?" for _ in row)
        columns = ", ".join(row.keys())
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
        cur = self.conn.execute(sql, list(row.values()))
        self.conn.commit()

        if mirror and self.jsonl_mirror:
            self._mirror_jsonl(table, row)
        return cur.lastrowid

    def _mirror_jsonl(self, table: str, row: dict[str, Any]) -> None:
        try:
            path = f"journal_{table}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"table": table, **row}, default=str) + "\n")
        except OSError:  # pragma: no cover - durability mirror is best-effort
            pass

    # --------------------------------------------------------------- queries
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------- system events
    def log_system_event(
        self,
        severity: Severity | str,
        category: str,
        message: str,
        detail: Optional[dict] = None,
    ) -> str:
        sev = severity.value if isinstance(severity, Severity) else str(severity)
        event_id = new_id("evt")
        self.insert(
            "system_events",
            {
                "event_id": event_id,
                "severity": sev,
                "category": category,
                "message": message,
                "detail_json": detail or {},
            },
            mirror=True,
        )
        return event_id

    # -------------------------------------------------------- config snapshot
    def record_config_version(self, settings) -> None:
        """Persist a redacted config snapshot. Secrets are never stored."""
        safe = {
            "mode": settings.mode.value,
            "approval_mode": settings.approval_mode.value,
            "real_trading_enabled_raw": settings.real_trading_enabled_raw,
            "alpaca_paper": settings.alpaca_paper,
            "alpaca_base_url": settings.alpaca_base_url,
            "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
            "max_paper_trades_per_day": settings.max_paper_trades_per_day,
            "max_open_positions": settings.max_open_positions,
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "paper_equity": settings.paper_equity,
            "max_auto_approvals_per_day": settings.max_auto_approvals_per_day,
            "unattended_approve_windows": settings.unattended_approve_windows,
            "max_unattended_approvals_per_day": settings.max_unattended_approvals_per_day,
            "max_spread_pct": settings.max_spread_pct,
            "min_dollar_volume": settings.min_dollar_volume,
            "max_data_age_seconds": settings.max_data_age_seconds,
            "has_openai_key": settings.has_openai_key,
            "has_anthropic_key": settings.has_anthropic_key,
            "has_massive_key": settings.has_massive_key,
            "has_benzinga_key": settings.has_benzinga_key,
            "has_alpaca_keys": settings.has_alpaca_keys,
            "allow_fixture_news": settings.allow_fixture_news,
            # HGEN-1 fixup (operator-directed, 2026-07-11): the snapshot
            # previously captured only has_openai_key/has_anthropic_key --
            # WHICH model those keys drive was invisible to config_hash, so
            # a same-day model switch (e.g. gpt-5.6-luna) moved zero bits of
            # the fingerprint. openai_primary_model/openai_review_model are
            # config, not secrets (unlike the API key itself), so they
            # belong in the safe snapshot alongside every other non-secret
            # setting above. Also captures the new HGEN flags/caps this
            # build ships, for the same reason every other feature's own
            # flags/caps already appear here (debate_max_calls_per_day,
            # canary_tier2_label_diff_pct, etc. -- see the fields above).
            "openai_primary_model": settings.openai_primary_model,
            "openai_review_model": settings.openai_review_model,
            "hypothesis_gen_shadow_enabled": settings.hypothesis_gen_shadow_enabled,
            "hypothesis_gen_recurring_enabled": settings.hypothesis_gen_recurring_enabled,
            "hypothesis_gen_max_calls_per_day": settings.hypothesis_gen_max_calls_per_day,
            "hypothesis_gen_max_proposals_per_run": settings.hypothesis_gen_max_proposals_per_run,
        }
        payload = json.dumps(safe, sort_keys=True, default=str)
        # PR9.5 fix: builtin hash() on a str is PYTHONHASHSEED-randomized (a
        # security default since Python 3.3) -- the SAME config produced a
        # DIFFERENT config_hash on every process restart, defeating the whole
        # point of a config-change fingerprint. stable_hash is the codebase's
        # own established deterministic-hash convention (lineage/hashing.py),
        # already used correctly by build_config_hashes() for the lineage
        # snapshot's own config_hash -- this was the one place that missed it.
        config_hash = stable_hash(safe)
        self.insert(
            "config_versions",
            {
                "config_hash": config_hash,
                "mode": settings.mode.value,
                "approval_mode": settings.approval_mode.value,
                "real_trading_enabled_raw": settings.real_trading_enabled_raw,
                "config_json": payload,
            },
        )

    # ----------------------------------------------------------- counters/day
    def start_of_trading_day_utc(self, now: Optional[datetime] = None) -> str:
        """UTC ISO timestamp for the start of the current US-market day.

        Daily limits (trades/day, auto-approvals/day, daily loss) are scoped to
        the trading day, not the UTC calendar day.
        """
        now = now or timeutils.now_utc()
        md = timeutils.market_date(now)
        # Midnight ET on the market date, expressed in UTC.
        from datetime import datetime as _dt, time as _t, timezone as _tz

        try:
            from zoneinfo import ZoneInfo

            et = ZoneInfo("America/New_York")
            start_et = _dt.combine(md, _t(0, 0), tzinfo=et)
            # Normalize to UTC: created_at_utc is stored as +00:00 and the daily-cap
            # queries compare it as a string, so the boundary must also be +00:00.
            # A bare .astimezone() would use the host's local zone and break the
            # lexical comparison for part of each UTC day.
            return timeutils.to_iso(start_et.astimezone(_tz.utc))
        except Exception:  # pragma: no cover
            return timeutils.to_iso(_dt.combine(md, _t(0, 0)))

    def count_rows(self, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.scalar(sql, params) or 0)

    def count_auto_approvals_today(self) -> int:
        """The SHARED daily cap (settings.max_auto_approvals_per_day) counts
        BOTH global-auto (AUTO_APPROVED) and unattended-window
        (UNATTENDED_APPROVED, 2026-07-11) rows together -- one budget across
        both mechanisms, so raising one door's own cap can never silently
        raise the other's effective ceiling."""
        return self.count_rows(
            "approvals",
            "label IN ('AUTO_APPROVED', 'UNATTENDED_APPROVED') AND created_at_utc >= ?",
            (self.start_of_trading_day_utc(),),
        )

    def count_unattended_approvals_today(self) -> int:
        """The unattended-window mechanism's OWN cap
        (settings.max_unattended_approvals_per_day) -- UNATTENDED_APPROVED
        rows only, checked in ADDITION to (not instead of) the shared cap
        above."""
        return self.count_rows(
            "approvals",
            "label = ? AND created_at_utc >= ?",
            ("UNATTENDED_APPROVED", self.start_of_trading_day_utc()),
        )

    def count_paper_orders_today(self, strategy: Optional[str] = None) -> int:
        start = self.start_of_trading_day_utc()
        if strategy:
            return self.count_rows(
                "paper_orders",
                "created_at_utc >= ? AND strategy = ?",
                (start, strategy),
            )
        return self.count_rows("paper_orders", "created_at_utc >= ?", (start,))

    def count_open_positions(self, strategy: Optional[str] = None) -> int:
        if strategy:
            return self.count_rows(
                "positions", "status = 'open' AND strategy = ?", (strategy,)
            )
        return self.count_rows("positions", "status = 'open'")

    def realized_pnl_today(self) -> float:
        start = self.start_of_trading_day_utc()
        val = self.scalar(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM trade_outcomes WHERE created_at_utc >= ?",
            (start,),
        )
        return float(val or 0.0)

    # ----------------------------------------------------- dashboard helpers
    def open_positions(self) -> list[dict]:
        return self.query("SELECT * FROM positions WHERE status = 'open' ORDER BY id DESC")

    def closed_outcomes(self, limit: int = 200) -> list[dict]:
        return self.query(
            "SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT ?", (limit,)
        )

    def recent_candidates(self, limit: int = 200, include_shadow: bool = False) -> list[dict]:
        """EXP-1 mechanism 9(c)/(f): defaults to EXCLUDING shadow-tier rows.

        This is the generic "recent candidates" feed behind the dashboard's
        Candidates/Trade-Packet tabs (streamlit_app.py) and the console
        API's ``/system`` route -- exactly the kind of surface mechanism 9
        names as the single biggest EXP-1 build-session risk: a shadow-tier
        candidate now carries a REAL AI label (including a real 'propose'
        label_decision) indistinguishable from an actionable one unless a
        surface explicitly filters. ``include_shadow=True`` is available for
        a future dedicated shadow-aware audit view that stamps rows
        NON-ACTIONABLE/SHADOW inline -- no such view exists yet, so the safe
        default stays exclusion (mirrors relabel/eval_corpus_build's own
        default-exclude-shadow posture).
        """
        if include_shadow:
            return self.query("SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,))
        return self.query(
            "SELECT * FROM candidates WHERE shadow_tier = 0 ORDER BY id DESC LIMIT ?", (limit,),
        )

    def recent_proposals(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM trade_proposals ORDER BY id DESC LIMIT ?", (limit,))

    def open_proposals(self, limit: int = 200) -> list[dict]:
        """Proposals still awaiting an explicit approve/reject decision.

        This is the actionable approval queue: a proposal leaves it once it is
        approved (filled), rejected, or blocked. Read-only — newest first.
        """
        return self.query(
            "SELECT * FROM trade_proposals WHERE status IN (?, ?) "
            "ORDER BY id DESC LIMIT ?",
            (*ProposalStatus.approvable(), limit),
        )

    def latest_freshness_for_symbol(self, symbol: str) -> Optional[dict]:
        """Most recent stored data-freshness for a symbol (read-only; does NOT
        fetch live data). Used to show last-known freshness in the approval queue
        without writing a snapshot on render."""
        return self.one(
            "SELECT freshness_status, is_usable, source_timestamp "
            "FROM price_snapshots WHERE symbol = ? ORDER BY id DESC LIMIT 1",
            (symbol,),
        )

    # --------------------------------------- 2.3 candidate-flow / label views
    def candidates_by_status(self, status: str, limit: int = 200) -> list[dict]:
        return self.query(
            "SELECT * FROM candidates WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        )

    def proposed_candidates(self, limit: int = 200) -> list[dict]:
        return self.candidates_by_status("proposed", limit)

    def watch_candidates(self, limit: int = 200) -> list[dict]:
        return self.candidates_by_status("watch", limit)

    def rejected_candidates_recent(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM rejected_candidates ORDER BY id DESC LIMIT ?", (limit,))

    def blocked_proposals(self, limit: int = 200) -> list[dict]:
        return self.query(
            "SELECT * FROM trade_proposals WHERE status = 'blocked' ORDER BY id DESC LIMIT ?", (limit,)
        )

    def label_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM candidate_labels WHERE candidate_id = ? ORDER BY id DESC LIMIT 1", (candidate_id,)
        )

    def packet_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM candidate_packets WHERE candidate_id = ? ORDER BY id DESC LIMIT 1", (candidate_id,)
        )

    def recent_candidate_labels(self, limit: int = 200, include_shadow: bool = False) -> list[dict]:
        """EXP-1 mechanism 9: defaults to EXCLUDING shadow-tier rows -- this
        table's ``symbol``/label columns are exactly the shape mechanism 9
        warns about; unused today (no caller wires this into a surface yet)
        but fixed defensively so a future caller inherits the safe default
        rather than needing to remember it."""
        if include_shadow:
            return self.query("SELECT * FROM candidate_labels ORDER BY id DESC LIMIT ?", (limit,))
        return self.query(
            "SELECT * FROM candidate_labels WHERE shadow_tier = 0 ORDER BY id DESC LIMIT ?", (limit,),
        )

    def label_summary(self) -> dict:
        """Counts by primary_label and by advisory label_decision (read-only).

        EXP-1: excludes ``shadow_tier = 1`` rows -- this is the operator
        console's own label-summary tile (``/api/v1/decisions``); pooling
        shadow-tier label volume into it would both mislead an operator
        reading "what is the core book's AI actually saying" and violate the
        same never-pool-shadow-with-core law mechanism 11's statistics
        follow (a counts-only aggregate is not a symbol-name leak, but it is
        still a live-aggregate that must stay core-only)."""
        return {
            "by_primary_label": self.query(
                "SELECT primary_label AS label, COUNT(*) AS n FROM candidate_labels "
                "WHERE shadow_tier = 0 GROUP BY primary_label ORDER BY n DESC"
            ),
            "by_label_decision": self.query(
                "SELECT label_decision AS decision, COUNT(*) AS n FROM candidate_labels "
                "WHERE shadow_tier = 0 GROUP BY label_decision ORDER BY n DESC"
            ),
        }

    # ---------------------------------------------- 2.4 catalyst enrichment views
    def catalyst_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM candidate_catalysts WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        )

    def recent_catalysts(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM candidate_catalysts ORDER BY id DESC LIMIT ?", (limit,))

    def catalyst_summary(self) -> dict:
        """Counts by catalyst_status and catalyst_type (read-only)."""
        return {
            "by_status": self.query(
                "SELECT catalyst_status AS status, COUNT(*) AS n FROM candidate_catalysts "
                "GROUP BY catalyst_status ORDER BY n DESC"
            ),
            "by_type": self.query(
                "SELECT catalyst_type AS type, COUNT(*) AS n FROM candidate_catalysts "
                "GROUP BY catalyst_type ORDER BY n DESC"
            ),
        }

    # ---------------------------------------------- 2.5 last30days research views
    def last30days_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM candidate_last30days WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        )

    def recent_last30days(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM candidate_last30days ORDER BY id DESC LIMIT ?", (limit,))

    def last30days_summary(self) -> dict:
        """Counts by last30days_status (read-only). The status set explicitly keeps
        ``skipped_budget_cap`` distinct from ``none_found`` / ``unavailable`` /
        ``error`` so the dashboard never mislabels a budget-skipped candidate."""
        return {
            "by_status": self.query(
                "SELECT last30days_status AS status, COUNT(*) AS n FROM candidate_last30days "
                "GROUP BY last30days_status ORDER BY n DESC"
            ),
        }

    # ------------------------------------------- 2.6 decision-adjustment views
    def recent_decision_adjustments(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM decision_adjustments ORDER BY id DESC LIMIT ?", (limit,))

    def decision_adjustment_summary(self) -> dict:
        """Counts by adjustment direction (read-only): upgraded / downgraded /
        unchanged. Lets the dashboard show how often narrative moved the call."""
        return {
            "by_adjustment": self.query(
                "SELECT adjustment, COUNT(*) AS n FROM decision_adjustments "
                "GROUP BY adjustment ORDER BY n DESC"
            ),
        }

    # ----------------------------------------------- 2.7 last30days polarity views
    def recent_polarity(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM last30days_polarity ORDER BY id DESC LIMIT ?", (limit,))

    def polarity_summary(self) -> dict:
        """Counts by sentiment, narrative driver type, and arming classification."""
        return {
            "by_sentiment": self.query(
                "SELECT sentiment_label AS label, COUNT(*) AS n FROM last30days_polarity "
                "GROUP BY sentiment_label ORDER BY n DESC"
            ),
            "by_driver": self.query(
                "SELECT narrative_driver_type AS driver, COUNT(*) AS n FROM last30days_polarity "
                "GROUP BY narrative_driver_type ORDER BY n DESC"
            ),
            "by_arming": self.query(
                "SELECT arming_classification AS arming, COUNT(*) AS n FROM last30days_polarity "
                "GROUP BY arming_classification ORDER BY n DESC"
            ),
        }

    # -------------------------------------------- 2.8 armed watch + user override
    def armed_watches(self, limit: int = 200) -> list[dict]:
        """Near-action watchlist: armed but stayed WATCH (NOT rejects)."""
        return self.query(
            "SELECT * FROM decision_adjustments WHERE armed_watch = 1 ORDER BY id DESC LIMIT ?", (limit,))

    def armed_watch_summary(self) -> dict:
        return {
            "by_arming": self.query(
                "SELECT arming_classification AS arming, COUNT(*) AS n FROM decision_adjustments "
                "WHERE armed_watch = 1 GROUP BY arming_classification ORDER BY n DESC"
            ),
        }

    def recent_user_overrides(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM user_decision_overrides ORDER BY id DESC LIMIT ?", (limit,))

    def override_by_id(self, override_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM user_decision_overrides WHERE override_id = ?", (override_id,))

    def user_override_summary(self) -> dict:
        return {
            "by_action": self.query(
                "SELECT user_override_action AS action, COUNT(*) AS n FROM user_decision_overrides "
                "GROUP BY user_override_action ORDER BY n DESC"),
            "by_attribution": self.query(
                "SELECT attribution_result AS attribution, COUNT(*) AS n FROM user_decision_overrides "
                "GROUP BY attribution_result ORDER BY n DESC"),
            "executed": self.count_rows("user_decision_overrides", "execution_allowed = 1"),
            "blocked": self.count_rows("user_decision_overrides", "blocked_reason IS NOT NULL"),
            "pending": self.count_rows("user_decision_overrides", "outcome_status = 'pending'"),
            "nightdesk": self.count_rows("user_decision_overrides", "nightdesk_research_candidate = 1"),
        }

    def labeller_source_summary(self, limit: int = 100) -> dict:
        """Breakdown of the most recent labels by source (openai / mock / fail_safe)
        + the fail-safe rate + fail-safe reasons. PURE READ, for VISIBILITY only.
        A failing labeller looks like a conservative reject (label_source=fail_safe
        floors the decision), so a spike here is the alarm — it never changes any
        decision, gate, or approval. Reasons come from ``validation_status`` on the
        fail-safe rows (live_exception / truncated_output / parse_error / ...)."""
        rows = self.query(
            "SELECT label_source, validation_status FROM candidate_labels ORDER BY id DESC LIMIT ?",
            (limit,))
        by_source: dict[str, int] = {}
        by_failsafe_reason: dict[str, int] = {}
        for r in rows:
            src = r.get("label_source") or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
            if src == LabelSource.FAIL_SAFE.value:
                reason = r.get("validation_status") or FailsafeReason.UNKNOWN.value
                by_failsafe_reason[reason] = by_failsafe_reason.get(reason, 0) + 1
        total = len(rows)
        fail_safe = by_source.get(LabelSource.FAIL_SAFE.value, 0)
        return {
            "total": total,
            "openai": by_source.get(LabelSource.OPENAI.value, 0),
            "mock": by_source.get(LabelSource.MOCK.value, 0),
            "fail_safe": fail_safe,
            "by_source": by_source,
            "fail_safe_rate": round(fail_safe / total, 3) if total else 0.0,
            "by_failsafe_reason": by_failsafe_reason,
        }

    def recent_system_events(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,))

    def evaluation_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM openai_evaluations WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        )

    def claude_review_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM claude_reviews WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        )

    def proposal_by_id(self, proposal_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM trade_proposals WHERE proposal_id = ?", (proposal_id,)
        )

    def approval_for_proposal(self, proposal_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM approvals WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        )

    # ----------------------------------------------- trade-packet lookups
    def candidate_by_id(self, candidate_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))

    def position_by_id(self, position_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM positions WHERE position_id = ?", (position_id,))

    def order_by_id(self, order_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM paper_orders WHERE order_id = ?", (order_id,))

    def orders_for_proposal(self, proposal_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM paper_orders WHERE proposal_id = ? ORDER BY id ASC", (proposal_id,)
        )

    def fills_for_order(self, order_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM paper_fills WHERE order_id = ? ORDER BY id ASC", (order_id,)
        )

    def order_events_for_order(self, order_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id ASC", (order_id,)
        )

    def exits_for_position(self, position_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM exits WHERE position_id = ? ORDER BY id ASC", (position_id,)
        )

    def outcome_for_position(self, position_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM trade_outcomes WHERE position_id = ? ORDER BY id DESC LIMIT 1",
            (position_id,),
        )

    def risk_check_for_proposal(self, proposal_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM risk_checks WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        )

    def monitoring_snapshots_for_position(self, position_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM monitoring_snapshots WHERE position_id = ? ORDER BY id ASC",
            (position_id,),
        )

    def baseline_for_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM baseline_outcomes WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        )

    def rejections_for_candidate(self, candidate_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM rejected_candidates WHERE candidate_id = ? ORDER BY id ASC",
            (candidate_id,),
        )

    def scan_batch_by_id(self, scan_batch_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM scan_batches WHERE scan_batch_id = ?", (scan_batch_id,)
        )

    def recent_scan_batches(self, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM scan_batches ORDER BY id DESC LIMIT ?", (limit,))

    def recent_scheduler_runs(self, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM scheduler_runs ORDER BY id DESC LIMIT ?", (limit,))

    def position_for_trade(self, trade_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM positions WHERE trade_id = ? ORDER BY id DESC LIMIT 1", (trade_id,)
        )

    def outcome_for_trade(self, trade_id: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM trade_outcomes WHERE trade_id = ? ORDER BY id DESC LIMIT 1", (trade_id,)
        )

    def attribution_by_candidate(self, candidate_ids: list[str]) -> dict[str, dict]:
        """Batch lookup (one query, not N+1): candidate_id -> its latest
        attribution_records row. A candidate_id with NO entry in the returned
        dict means no attribution row exists yet -- the UI-PR-A hindsight
        column must render that as "pending", never as a fabricated zero
        delta_r (unknown-never-zero, same posture as position_health.py)."""
        if not candidate_ids:
            return {}
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.query(
            f"SELECT * FROM attribution_records WHERE candidate_id IN ({placeholders}) "
            f"ORDER BY created_at_utc DESC",
            tuple(candidate_ids),
        )
        out: dict[str, dict] = {}
        for r in rows:
            cid = r.get("candidate_id")
            if cid and cid not in out:  # first row per id wins (DESC = latest)
                out[cid] = r
        return out
