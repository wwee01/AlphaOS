"""Append-only turn receipts for cooperative research work; no outcome evidence."""

import json
from datetime import datetime

from alphaos.util import timeutils
from alphaos.util.ids import new_id

VERSION = "outcomes_work_turn_v1"
DOMAINS = {
    "outer": ("candidate", "attribution_discover", "attribution_resolve", "baseline"),
    "baseline": ("v1", "hold10"),
}


def _read(journal, domain):
    if domain not in DOMAINS:
        raise ValueError("unknown outcome work domain")
    row = journal.one(
        "SELECT * FROM system_events WHERE category=? ORDER BY id DESC LIMIT 1",
        (f"outcomes_work_turn_{domain}",),
    )
    if not row:
        return DOMAINS[domain][0], None, 0
    value = json.loads(row["detail_json"])
    at = datetime.fromisoformat(row["created_at_utc"].replace("Z", "+00:00"))
    if (
        set(value) != {"version", "domain", "stage", "next_stage", "revision"}
        or value["version"] != VERSION
        or value["domain"] != domain
        or value["stage"] not in DOMAINS[domain]
        or value["next_stage"] not in DOMAINS[domain]
        or value["next_stage"]
        != DOMAINS[domain][
            (DOMAINS[domain].index(value["stage"]) + 1) % len(DOMAINS[domain])
        ]
        or type(value["revision"]) is not int
        or value["revision"] < 1
        or row["message"] != VERSION
        or at.tzinfo is None
        or at.utcoffset() is None
        or at > timeutils.now_utc()
    ):
        raise ValueError("invalid outcome continuation receipt")
    return value["next_stage"], row["id"], value["revision"]


def turn(journal, domain):
    return _read(journal, domain)


def claim_turn(journal, domain, stage, expected_id):
    if journal.conn.in_transaction:
        raise RuntimeError("outcome turn requires its own transaction")
    journal.conn.execute("BEGIN IMMEDIATE")
    try:
        selected, row_id, revision = _read(journal, domain)
        if selected != stage or row_id != expected_id:
            raise RuntimeError("outcome continuation changed during processing")
        stages = DOMAINS[domain]
        value = {
            "version": VERSION,
            "domain": domain,
            "stage": stage,
            "next_stage": stages[(stages.index(stage) + 1) % len(stages)],
            "revision": revision + 1,
        }
        st = timeutils.stamp()
        cursor = journal.conn.execute(
            "INSERT INTO system_events(event_id,severity,category,message,detail_json,"
            "created_at_utc,created_at_sgt,created_at_market) VALUES(?,?,?,?,?,?,?,?)",
            (
                new_id("outturn"),
                "info",
                f"outcomes_work_turn_{domain}",
                VERSION,
                json.dumps(value, sort_keys=True),
                st.utc,
                st.local_sgt,
                st.market_et,
            ),
        )
        journal.conn.commit()
        return value["next_stage"], cursor.lastrowid
    except BaseException:
        journal.conn.rollback()
        raise
