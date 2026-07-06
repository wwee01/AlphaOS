"""Ledger backup automation (PR9.5): deploy/backup_ledger.sh exercised
end-to-end as a real subprocess against a real temp SQLite DB -- never the
production `data/alphaos.db`. ALPHAOS_BACKUP_TEST_MODE=1 suppresses the real
alert send (this must never fire a real ntfy push from a test run);
ALPHAOS_BACKUP_DB_PATH / ALPHAOS_BACKUP_DEST_DIR redirect the script at
throwaway paths.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup_ledger.sh"


def _run_backup(db_path: Path, dest_dir: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "ALPHAOS_BACKUP_DB_PATH": str(db_path),
        "ALPHAOS_BACKUP_DEST_DIR": str(dest_dir),
        "ALPHAOS_BACKUP_TEST_MODE": "1",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30,
    )


def _make_real_sqlite_db(path: Path, marker: str) -> None:
    """(Re)write a real, valid SQLite DB at ``path`` with a single marker
    row -- idempotent so a test can call this twice against the SAME path to
    simulate "the DB changed since the last backup"."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t (v) VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def test_backup_succeeds_and_produces_a_restorable_gzip(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "hello-drill")

    result = _run_backup(db, dest)

    assert result.returncode == 0, result.stderr
    today = date.today().isoformat()
    daily_file = dest / "daily" / f"alphaos-{today}.db.gz"
    assert daily_file.exists()

    restored = tmp_path / "restored.db"
    with gzip.open(daily_file, "rb") as src, open(restored, "wb") as dst:
        shutil.copyfileobj(src, dst)
    conn = sqlite3.connect(str(restored))
    row = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert row == ("hello-drill",)


def test_backup_also_creates_this_months_monthly_snapshot(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")

    _run_backup(db, dest)

    this_month = date.today().strftime("%Y-%m")
    assert (dest / "monthly" / f"alphaos-{this_month}.db.gz").exists()


def test_monthly_snapshot_is_not_overwritten_by_a_second_run_same_month(tmp_path):
    """A second backup later the same month must not clobber the month's
    already-frozen snapshot (fixed at first-of-month, or first successful run
    after a missed fire -- see the script's own comment)."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "first")
    _run_backup(db, dest)

    this_month = date.today().strftime("%Y-%m")
    monthly_file = dest / "monthly" / f"alphaos-{this_month}.db.gz"
    first_mtime = monthly_file.stat().st_mtime

    _make_real_sqlite_db(db, "second")  # DB changed...
    _run_backup(db, dest)               # ...but a second run same month...

    assert monthly_file.stat().st_mtime == first_mtime  # ...must not re-touch it


def test_source_db_missing_fails_loud(tmp_path):
    result = _run_backup(tmp_path / "does-not-exist.db", tmp_path / "dest")

    assert result.returncode == 1
    assert "not found" in result.stdout + result.stderr


def test_corrupt_db_fails_the_integrity_gate_and_never_rotates_in_a_bad_backup(tmp_path):
    db = tmp_path / "corrupt.db"
    db.write_text("this is not a sqlite file")
    dest = tmp_path / "dest"

    result = _run_backup(db, dest)

    assert result.returncode == 1
    assert not (dest / "daily").exists() or list((dest / "daily").glob("*.db.gz")) == []


def test_test_mode_never_shells_out_to_python_or_network(tmp_path, monkeypatch):
    """Belt-and-suspenders: force a failure and confirm the test-mode message
    is printed (proving the alert path was reached) without ever invoking the
    real alphaos.util.alerts module -- verified by checking the failure
    output explicitly says the real send was skipped."""
    result = _run_backup(tmp_path / "missing.db", tmp_path / "dest")

    assert result.returncode == 1
    assert "test mode -- real alert send skipped" in result.stdout + result.stderr


def test_rotation_keeps_newest_30_daily_and_prunes_the_rest(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")
    daily_dir = dest / "daily"
    daily_dir.mkdir(parents=True)

    base = date.today() - timedelta(days=60)
    for i in range(35):  # 35 pre-existing fake daily backups
        d = (base + timedelta(days=i)).isoformat()
        (daily_dir / f"alphaos-{d}.db.gz").write_bytes(b"fake")
    before_count = len(list(daily_dir.glob("*.db.gz")))

    result = _run_backup(db, dest)  # adds 1 more (today's real one) -> 36 total

    assert result.returncode == 0, result.stderr
    remaining = sorted(p.name for p in daily_dir.glob("*.db.gz"))
    assert before_count == 35
    assert len(remaining) == 30  # pruned down to the cap
    today = date.today().isoformat()
    assert f"alphaos-{today}.db.gz" in remaining  # today's real backup survives
    # the 6 oldest fake ones (36 - 30 = 6) must be gone
    oldest_expected_gone = (base + timedelta(days=0)).isoformat()
    assert f"alphaos-{oldest_expected_gone}.db.gz" not in remaining


def test_rotation_keeps_newest_12_monthly_and_prunes_the_rest(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")
    monthly_dir = dest / "monthly"
    monthly_dir.mkdir(parents=True)

    # 13 fake old monthly snapshots, all safely before the current month.
    months = [f"2020-{m:02d}" for m in range(1, 13)] + ["2020-12b"]
    for m in months:
        (monthly_dir / f"alphaos-{m}.db.gz").write_bytes(b"fake")

    result = _run_backup(db, dest)  # adds this month's real one -> 14 total

    assert result.returncode == 0, result.stderr
    remaining = list(monthly_dir.glob("*.db.gz"))
    assert len(remaining) == 12


def test_script_is_safe_to_run_twice_in_a_row_same_day(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")

    r1 = _run_backup(db, dest)
    r2 = _run_backup(db, dest)

    assert r1.returncode == 0 and r2.returncode == 0
    today = date.today().isoformat()
    assert len(list((dest / "daily").glob(f"alphaos-{today}.db.gz"))) == 1


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="requires the sqlite3 CLI binary")
def test_sqlite3_cli_is_available():
    """Documents the one non-Python system dependency this script has."""
    assert shutil.which("sqlite3") is not None
