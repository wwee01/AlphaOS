"""Inactive dedicated compact registry; unchanged v1 semantics, no runtime hook."""

from __future__ import annotations

from functools import lru_cache
import json
import sqlite3

from alphaos.research import hold10_prospective as v1
from alphaos.research import hold10_receipts as h

VERSION = "hold10_prospective_compact_dry_run_v2"
MAX_HISTORY_BYTES = v1.MAX_HISTORY_BYTES
MAX_CAPTURES = v1.MAX_CAPTURES
CANCELLATION_BYTES = v1.CANCELLATION_BYTES
MAX_EXPANDED_BYTES = v1.c.MAX_JSON_BYTES
MAX_NODES = 1000000
MAX_DEPTH = 80
OBJECT_THRESHOLD = 512
TABLES = (
    "research_hold10_compact_v2_registrations",
    "research_hold10_compact_v2_captures",
    "research_hold10_compact_v2_objects",
)
DDL = (
    f"CREATE TABLE {TABLES[0]} (registration_id TEXT PRIMARY KEY, family_id TEXT NOT NULL UNIQUE, {h._COMMON})",
    f"CREATE TABLE {TABLES[1]} (registration_id TEXT NOT NULL REFERENCES {TABLES[0]}(registration_id), sequence INTEGER NOT NULL CHECK(sequence>0), previous_receipt_sha256 TEXT, {h._COMMON}, PRIMARY KEY(registration_id,sequence))",
    f"CREATE TABLE {TABLES[2]} (object_sha256 TEXT PRIMARY KEY, node_json TEXT NOT NULL)",
) + tuple(
    f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable compact prospective registry'); END"
    for table in TABLES
    for action in ("UPDATE", "DELETE")
)


def _bytes(value):
    return len(h._json(value).encode())


def pack_observation(value):
    """Pure lossless representation, not a prospective registration or receipt."""
    value = v1._copy(value)
    objects = {}
    count = 0

    def encode(item, depth):
        nonlocal count
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise ValueError("expanded_structure_bound_exceeded")
        if isinstance(item, dict):
            node = ["d", [[key, encode(item[key], depth + 1)] for key in sorted(item)]]
        elif isinstance(item, list):
            node = ["l", [encode(x, depth + 1) for x in item]]
        else:
            node = ["v", item]
        if _bytes(node) >= OBJECT_THRESHOLD:
            key = v1.sha(node)
            objects[key] = node
            return ["r", key]
        return node

    root = encode(value, 0)
    return {
        "version": VERSION,
        "root": root,
        "expanded_sha256": v1.sha(value),
        "expanded_bytes": _bytes(value),
        "objects": objects,
    }


class _Decoder:
    """Verify the DAG and decoded cost before allocating expanded structures."""

    def __init__(self, objects):
        self.objects = objects
        self.memo = {}
        self.visiting = set()
        self.used = set()
        for key, node in objects.items():
            h._pin(node, key, "compact_object")

    def bounds(self, node, depth=0):
        if depth > MAX_DEPTH or not isinstance(node, list) or len(node) != 2:
            raise ValueError("invalid_compact_node_or_depth")
        tag, data = node
        if tag == "r":
            h._sha(data, "object_reference")
            if data not in self.objects or data in self.visiting:
                raise ValueError("missing_or_cyclic_compact_object")
            self.used.add(data)
            if data not in self.memo:
                self.visiting.add(data)
                self.memo[data] = self.bounds(self.objects[data], depth + 1)
                self.visiting.remove(data)
            size, count, height = self.memo[data]
            if depth + height > MAX_DEPTH:
                raise ValueError("expanded_structure_bound_exceeded")
            return size, count, height + 1
        if tag == "v":
            if isinstance(data, (list, dict)):
                raise ValueError("scalar_node_required")
            return _bytes(data), 1, 1
        if tag not in {"d", "l"} or not isinstance(data, list):
            raise ValueError("invalid_compact_container")
        size, count, height = 2 + max(0, len(data) - 1), 1, 1
        keys = []
        for entry in data:
            if tag == "d":
                if (
                    not isinstance(entry, list)
                    or len(entry) != 2
                    or not isinstance(entry[0], str)
                ):
                    raise ValueError("invalid_compact_mapping")
                keys.append(entry[0])
                size += _bytes(entry[0]) + 1
                entry = entry[1]
            nbytes, nodes, levels = self.bounds(entry, depth + 1)
            size += nbytes
            count += nodes
            height = max(height, levels + 1)
            if size > MAX_EXPANDED_BYTES or count > MAX_NODES:
                raise ValueError("expanded_structure_bound_exceeded")
        if keys != sorted(set(keys)):
            raise ValueError("canonical_unique_mapping_keys_required")
        return size, count, height

    def expand(self, node):
        # Caller must complete bounds() before this allocation.
        tag, data = node
        if tag == "r":
            return self.expand(self.objects[data])
        if tag == "v":
            return data
        if tag == "l":
            return [self.expand(x) for x in data]
        return {key: self.expand(value) for key, value in data}


def _expand(payload, decoder):
    v1._exact(
        payload,
        {"version", "root", "expanded_sha256", "expanded_bytes"},
        "exact_compact_payload_required",
    )
    if payload["version"] != VERSION:
        raise ValueError("compact_version_mismatch")
    size, _, _ = decoder.bounds(payload["root"])
    if (
        type(payload["expanded_bytes"]) is not int
        or size != payload["expanded_bytes"]
        or size > MAX_EXPANDED_BYTES
    ):
        raise ValueError("expanded_size_mismatch")
    value = decoder.expand(payload["root"])
    h._pin(value, payload["expanded_sha256"], "expanded_payload")
    return value


def expand_observation(packed):
    """Expand one bounded pure packet, verifying every reference and exact hash."""
    v1._exact(
        packed,
        {"version", "root", "expanded_sha256", "expanded_bytes", "objects"},
        "exact_packed_observation_required",
    )
    decoder = _Decoder(packed["objects"])
    value = _expand({k: v for k, v in packed.items() if k != "objects"}, decoder)
    if decoder.used != set(decoder.objects):
        raise ValueError("unreferenced_compact_objects")
    return value


@lru_cache(maxsize=1)
def _expected_schema():
    db = sqlite3.connect(":memory:")
    try:
        for sql in DDL:
            db.execute(sql)
        return v1._schema(db)
    finally:
        db.close()


def _verify(db):
    if v1._schema(db) != _expected_schema():
        raise ValueError("dedicated_compact_schema_required_no_mixed_registry")


def initialize_compact_registry(connection, *, purpose):
    if purpose != "inactive_manual_prospective_dry_run":
        raise ValueError("explicit_inactive_scratch_initialization_required")
    with h._atomic(connection):
        if not v1._schema(connection):
            for sql in DDL:
                connection.execute(sql)
        _verify(connection)


def _rows(db, table):
    cur = db.execute(f"SELECT * FROM {table} ORDER BY 1,2")
    return [dict(zip([x[0] for x in cur.description], row)) for row in cur]


def _receipt(raw):
    raw = dict(raw)
    text = raw.pop("payload_json")
    payload = json.loads(text)
    if text != h._json(payload):
        raise ValueError("noncanonical_stored_receipt")
    h._pin(payload, raw["payload_sha256"], "stored_payload")
    digest = raw.pop("receipt_sha256")
    h._pin(raw, digest, "stored_receipt")
    if raw["mode"] != "dry_run" or v1.c._dt(raw["recorded_at_utc"]) > v1._clock():
        raise ValueError("saved_mode_or_future_clock")
    return {**raw, "receipt_sha256": digest, "payload": payload}


def _logical(keys, payload, at):
    row = {
        **keys,
        "payload_sha256": v1.sha(payload),
        "recorded_at_utc": at,
        "mode": "dry_run",
    }
    return {**row, "receipt_sha256": v1.sha(row), "payload": payload}


class _History:
    def __init__(self, rows, decoder, rid):
        self.rows, self.decoder, self.rid = rows, decoder, rid
        self.metadata = []

    def __iter__(self):
        prior = None
        self.metadata = []
        for row in self.rows:
            payload = row["payload"]
            payload = (
                payload
                if payload.get("kind") == "cancel"
                else _expand(payload, self.decoder)
            )
            logical = _logical(
                {
                    "registration_id": self.rid,
                    "sequence": row["sequence"],
                    "previous_receipt_sha256": prior,
                },
                payload,
                row["recorded_at_utc"],
            )
            prior = logical["receipt_sha256"]
            self.metadata.append(
                {k: v for k, v in logical.items() if k != "payload"}
                | {
                    "kind": payload["kind"],
                    "storage_receipt_sha256": row["receipt_sha256"],
                    "expanded_bytes": _bytes(payload),
                }
            )
            yield logical


class CompactProspectiveHold10:
    def __init__(self, connection, *, dry_run=False):
        self.c, self.enabled = connection, dry_run is True

    def _scan(self, rid):
        _verify(self.c)
        # Bound raw reads before fetching payloads. Canonical envelope accounting
        # below is stricter and includes every table, not just capture payloads.
        raw_bytes = 0
        for table in TABLES:
            count = self.c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            limit = (
                1
                if table == TABLES[0]
                else MAX_CAPTURES + 1
                if table == TABLES[1]
                else MAX_NODES
            )
            if count > limit:
                raise ValueError("compact_storage_row_bound_exceeded")
            columns = [row[1] for row in self.c.execute(f"PRAGMA table_info({table})")]
            # Schema was matched first; these are fixed owned identifiers. Bound
            # every stored cell, including malformed oversized hash/ID metadata,
            # before Python materializes rows. Final canonical cost is stricter.
            expression = "+".join(
                f"COALESCE(length(CAST({column} AS BLOB)),0)" for column in columns
            )
            raw_bytes += self.c.execute(
                f"SELECT COALESCE(SUM({expression}),0) FROM {table}"
            ).fetchone()[0]
        if raw_bytes > MAX_HISTORY_BYTES + CANCELLATION_BYTES:
            raise ValueError("compact_storage_byte_cap_exceeded")
        registrations, captures, objects = [_rows(self.c, t) for t in TABLES]
        if len(registrations) != 1 or registrations[0]["registration_id"] != rid:
            raise ValueError("one_registered_local_family_required")
        if len(captures) > MAX_CAPTURES + 1:
            raise ValueError("bounded_capture_history_exceeded")
        object_map = {}
        for row in objects:
            node = json.loads(row["node_json"])
            if row["node_json"] != h._json(node):
                raise ValueError("noncanonical_stored_object")
            object_map[row["object_sha256"]] = node
        decoder = _Decoder(object_map)
        parsed = [_receipt(row) for row in captures]
        previous = None
        for index, row in enumerate(parsed, 1):
            if (
                row["registration_id"] != rid
                or row["sequence"] != index
                or row["previous_receipt_sha256"] != previous
            ):
                raise ValueError("compact_storage_chain_mismatch")
            previous = row["receipt_sha256"]
        cancels = [
            i for i, row in enumerate(parsed) if row["payload"].get("kind") == "cancel"
        ]
        if cancels and cancels != [len(parsed) - 1]:
            raise ValueError("cancellation_must_be_final")
        if len(parsed) - len(cancels) > MAX_CAPTURES:
            raise ValueError("bounded_capture_history_exceeded")
        sizes = [
            _bytes(row) for rows in (registrations, captures, objects) for row in rows
        ]
        total = sum(sizes)
        cancel_size = _bytes(captures[-1]) if cancels else 0
        if total - cancel_size > MAX_HISTORY_BYTES or cancel_size > CANCELLATION_BYTES:
            raise ValueError("compact_storage_byte_cap_exceeded")
        history = _History(parsed, decoder, rid)
        state = v1._view(_receipt(registrations[0]), history, v1._clock())
        if decoder.used != set(object_map):
            raise ValueError("unreferenced_compact_objects")
        state["captures"] = history.metadata
        state["storage"] = {
            "version": VERSION,
            "representation": "compact_capture_metadata_use_expand_capture_for_one",
            "canonical_stored_bytes": total,
            "unique_object_count": len(objects),
            "capture_count": len(parsed) - len(cancels),
            "cancel_count": len(cancels),
            "max_history_bytes": MAX_HISTORY_BYTES,
            "cancellation_reserve_bytes": CANCELLATION_BYTES,
            "physical_sqlite_bytes_included": False,
        }
        fingerprint = v1.sha([registrations, captures, objects])
        return state, fingerprint, parsed, decoder

    def read(self, registration_id):
        with v1._read_snapshot(self.c):
            return self._scan(registration_id)[0]

    def expand_capture(self, registration_id, sequence, *, expected_capture_sha256):
        with v1._read_snapshot(self.c):
            state, _, rows, decoder = self._scan(registration_id)
            if type(sequence) is not int or not 1 <= sequence <= len(rows):
                raise ValueError("valid_capture_sequence_required")
            metadata = state["captures"][sequence - 1]
            if metadata["receipt_sha256"] != expected_capture_sha256:
                raise ValueError("capture_pin_mismatch")
            payload = rows[sequence - 1]["payload"]
            payload = (
                payload
                if payload.get("kind") == "cancel"
                else _expand(payload, decoder)
            )
            return {
                k: v
                for k, v in metadata.items()
                if k not in {"kind", "storage_receipt_sha256", "expanded_bytes"}
            } | {"payload": payload}

    def register(self, contract, *, expected_contract_sha256):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        q = v1._contract(contract)
        h._pin(q, expected_contract_sha256, "contract")
        with h._atomic(self.c):
            _verify(self.c)
            existing = self.c.execute(
                f"SELECT registration_id FROM {TABLES[0]}"
            ).fetchall()
            if existing:
                if [row[0] for row in existing] != [q["registration_id"]]:
                    raise ValueError(
                        "one_local_family_registration_no_alias_or_restart"
                    )
                state = self._scan(q["registration_id"])[0]
                if state["registration"]["payload"]["contract"] != q:
                    raise ValueError("immutable_registration_conflict")
                return state
            at = v1._clock()
            if at >= v1.c._dt(q["start_at_utc"]):
                raise ValueError("registration_must_precede_future_interval")
            h._insert(
                self.c,
                TABLES[0],
                {"registration_id": q["registration_id"], "family_id": v1.FAMILY},
                {"contract": q, "schedule_slots": v1.schedule_slots(q)},
                at.isoformat(),
            )
            result = self._scan(q["registration_id"])[0]
            if v1._clock() >= v1.c._dt(q["start_at_utc"]):
                raise ValueError("registration_commit_boundary_crossed")
            return result

    def _append(self, state, rows, payload, now):
        if payload.get("kind") != "cancel":
            if state["storage"]["capture_count"] >= MAX_CAPTURES:
                raise ValueError("bounded_capture_history_exceeded")
            packed = pack_observation(payload)
            for key, node in packed.pop("objects").items():
                self.c.execute(
                    f"INSERT OR IGNORE INTO {TABLES[2]} VALUES(?,?)",
                    (key, h._json(node)),
                )
            payload = packed
        h._insert(
            self.c,
            TABLES[1],
            {
                "registration_id": state["registration"]["registration_id"],
                "sequence": len(rows) + 1,
                "previous_receipt_sha256": rows[-1]["receipt_sha256"] if rows else None,
            },
            payload,
            now.isoformat(),
        )

    def capture(
        self,
        snapshot,
        registration_id,
        *,
        expected_registration_sha256,
        expected_previous_capture_sha256,
    ):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        with v1._read_snapshot(self.c):
            before, fingerprint, _, _ = self._scan(registration_id)
        self._pins(
            before, expected_registration_sha256, expected_previous_capture_sha256
        )
        if before["state"] != "collecting":
            raise ValueError("capture_requires_open_registered_interval")
        observation = v1._copy(
            v1._observe(snapshot, before["registration"], v1._clock())
        )
        with h._atomic(self.c):
            state, current, rows, _ = self._scan(registration_id)
            if current != fingerprint:
                raise ValueError("concurrent_registry_change")
            if state["state"] != "collecting":
                raise ValueError("capture_deadline_or_cancellation_changed")
            snapshot.verify()
            now = v1._clock()
            for d in observation["dispositions"]:
                if d["status"].startswith("timely_") and now >= v1.c._dt(
                    d["deadline_utc"]
                ):
                    d["status"] = "late_at_commit"
            self._append(state, rows, observation, now)
            result = self._scan(registration_id)[0]
            snapshot.verify()
            # Packing and complete history validation can be expensive. Never
            # retain a pre-boundary timestamp after the boundary actually passed.
            final = v1._clock()
            contract = state["registration"]["payload"]["contract"]
            if (
                final < now
                or final >= v1.c._dt(contract["close_at_utc"])
                or any(
                    d["status"].startswith("timely_")
                    and final >= v1.c._dt(d["deadline_utc"])
                    for d in observation["dispositions"]
                )
            ):
                raise ValueError("capture_commit_boundary_crossed_retry")
            return result

    @staticmethod
    def _pins(state, registration, previous):
        if (
            state["registration"]["receipt_sha256"] != registration
            or state["latest_capture_sha256"] != previous
        ):
            raise ValueError("registration_or_latest_capture_pin_mismatch")

    def cancel(
        self,
        registration_id,
        *,
        expected_registration_sha256,
        expected_previous_capture_sha256,
    ):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        with h._atomic(self.c):
            state, _, rows, _ = self._scan(registration_id)
            self._pins(
                state, expected_registration_sha256, expected_previous_capture_sha256
            )
            if state["state"] == "cancelled":
                return state
            self._append(
                state,
                rows,
                {"kind": "cancel", "reason": "manual_withdrawal_no_deletion"},
                v1._clock(),
            )
            return self._scan(registration_id)[0]
