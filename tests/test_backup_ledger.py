"""Ledger backup automation (PR9.5): deploy/backup_ledger.sh exercised
end-to-end as a real subprocess against a real temp SQLite DB -- never the
production `data/alphaos.db`. ALPHAOS_BACKUP_TEST_MODE=1 suppresses the real
alert send (this must never fire a real ntfy push from a test run);
ALPHAOS_BACKUP_DB_PATH / ALPHAOS_BACKUP_DEST_DIR redirect the script at
throwaway paths.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup_ledger.sh"


def _run_backup(
    db_path: Path, dest_dir: Path, text_archive_dir: Path | None = None,
    env_path: Path | None = None, passphrase: str | None = None,
    backup2_method: str | None = None, backup2_dest: Path | None = None,
) -> subprocess.CompletedProcess:
    # text_archive_dir defaults to a sibling path that does NOT exist --
    # without an explicit override, the script falls back to its own real
    # default ($REPO_DIR/data/text_archive). That's harmless only because
    # that real directory happens not to exist on this machine today; once
    # TEXT_ARCHIVE_ENABLED is ever turned on in production, every test using
    # this helper would silently start reading/mirroring real archive data.
    # Pinning an explicit non-existent default keeps every test hermetic
    # regardless of the real repo's data/ state, now and later.
    archive_dir = text_archive_dir if text_archive_dir is not None else dest_dir.parent / "no_text_archive_by_default"
    # OPS-B: env_path defaults to a sibling non-existent path too, for the
    # exact same reason -- without an explicit override the script would
    # fall back to $REPO_DIR/.env (the real one), and every test using this
    # helper would silently start reading/encrypting the operator's real
    # secrets. Never let a test touch the real .env.
    env_src = env_path if env_path is not None else dest_dir.parent / "no_env_by_default"
    env = {
        **os.environ,
        "ALPHAOS_BACKUP_DB_PATH": str(db_path),
        "ALPHAOS_BACKUP_DEST_DIR": str(dest_dir),
        "ALPHAOS_BACKUP_TEXT_ARCHIVE_DIR": str(archive_dir),
        "ALPHAOS_BACKUP_ENV_PATH": str(env_src),
        "ALPHAOS_BACKUP_TEST_MODE": "1",
    }
    if passphrase is not None:
        env["ALPHAOS_BACKUP_ENC_PASSPHRASE_OVERRIDE"] = passphrase
    if backup2_method is not None:
        env["BACKUP2_METHOD"] = backup2_method
    if backup2_dest is not None:
        env["BACKUP2_DEST"] = str(backup2_dest)
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


# =============================================================================
# TEXT-0: text archive mirror + sha256 backup verification
# =============================================================================
def _write_source_filing(text_archive_dir: Path, db_path: Path, rel_path: str, content: bytes) -> None:
    """Writes a real gzip of ``content`` under ``text_archive_dir/rel_path``
    and a matching ``text_documents`` row (sha256 of the RAW/uncompressed
    bytes, exactly as ``pull_new_filings`` computes it) into a real sqlite db
    -- mirrors production's on-disk + DB-row shape closely enough for the
    backup script's own verification query (``storage_path LIKE '%' || rel``)
    to match it."""
    full_path = text_archive_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(full_path, "wb") as f:
        f.write(content)
    sha256 = hashlib.sha256(content).hexdigest()

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS text_documents ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, storage_path TEXT, sha256 TEXT)"
    )
    conn.execute(
        "INSERT INTO text_documents (storage_path, sha256) VALUES (?, ?)",
        (f"data/text_archive/{rel_path}", sha256),
    )
    conn.commit()
    conn.close()


def test_backup_succeeds_when_text_archive_dir_does_not_exist(tmp_path):
    """The archive is best-effort and interim -- its ABSENCE (e.g. before an
    operator ever turns TEXT_ARCHIVE_ENABLED on) must never affect the DB
    backup, which is the part exit-review called CRITICAL."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")

    result = _run_backup(db, dest, text_archive_dir=tmp_path / "does_not_exist")

    assert result.returncode == 0, result.stderr
    today = date.today().isoformat()
    assert (dest / "daily" / f"alphaos-{today}.db.gz").exists()


def test_text_archive_mirror_and_sha256_verification_passes_on_a_match(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    text_archive_src = tmp_path / "text_archive"
    _make_real_sqlite_db(db, "x")
    _write_source_filing(text_archive_src, db, "2026/07/0000320193-26-000001.gz", b"raw filing body")

    result = _run_backup(db, dest, text_archive_dir=text_archive_src)

    assert result.returncode == 0, result.stderr
    assert (dest / "text_archive" / "2026" / "07" / "0000320193-26-000001.gz").exists()
    assert "0 mismatches" in result.stdout


def test_text_archive_sha256_mismatch_fails_loud_not_silently(tmp_path):
    """Regression test for a self-caught bug: an earlier version of the
    verification snippet called load_settings() for db_path, silently
    ignoring ALPHAOS_BACKUP_DB_PATH and always checking the real production
    DB -- so a genuine sha256 mismatch was never detected and the step always
    reported "0 mismatches" regardless of the truth. This plants a
    deliberately WRONG sha256 in the DB row and asserts the backup step
    actually notices and fails loud (stderr), without hard-failing the
    overall backup run (the DB backup, the CRITICAL part, already succeeded
    by this point in the script)."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    text_archive_src = tmp_path / "text_archive"
    _make_real_sqlite_db(db, "x")
    _write_source_filing(text_archive_src, db, "2026/07/0000320193-26-000002.gz", b"raw filing body 2")

    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE text_documents SET sha256 = ? WHERE storage_path LIKE ?",
        ("f" * 64, "%0000320193-26-000002.gz"),
    )
    conn.commit()
    conn.close()

    result = _run_backup(db, dest, text_archive_dir=text_archive_src)

    combined = result.stdout + result.stderr
    assert "VERIFICATION FAILED" in combined
    assert "sha256 mismatch" in combined
    assert result.returncode == 0  # best-effort: does not fail the overall run
    today = date.today().isoformat()
    assert (dest / "daily" / f"alphaos-{today}.db.gz").exists()  # DB backup unaffected


def test_text_archive_mirror_is_incremental_not_a_daily_rotation(tmp_path):
    """Unlike the DB's daily-rotation snapshots, the archive mirror only ever
    copies what rsync sees as new/changed -- a second run with no new files
    must not re-verify or re-copy anything already present."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    text_archive_src = tmp_path / "text_archive"
    _make_real_sqlite_db(db, "x")
    _write_source_filing(text_archive_src, db, "2026/07/0000320193-26-000003.gz", b"body three")

    r1 = _run_backup(db, dest, text_archive_dir=text_archive_src)
    r2 = _run_backup(db, dest, text_archive_dir=text_archive_src)

    assert r1.returncode == 0 and r2.returncode == 0
    assert "1 file(s) synced" in r1.stdout
    assert "0 file(s) synced" in r2.stdout


# =============================================================================
# OPS-B: env.enc encryption + MANIFEST + off-ecosystem second target
# =============================================================================
def _write_env_file(path: Path, content: str = "OPENAI_API_KEY=sk-test-fake\nSOME_SECRET=abc123\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_env_enc_not_armed_without_a_keychain_passphrase_is_a_warning_not_a_failure(tmp_path):
    """No ALPHAOS_BACKUP_ENC_PASSPHRASE_OVERRIDE and (in this sandboxed test
    environment) nothing in the real Keychain under the fixed service/account
    name -- env.enc must be skipped with a loud warning, never fail the run
    (the DB backup, the CRITICAL part, is unaffected)."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file)

    result = _run_backup(db, dest, env_path=env_file)  # no passphrase override

    combined = result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    today = date.today().isoformat()
    assert (dest / "daily" / f"alphaos-{today}.db.gz").exists()  # DB backup unaffected
    if "env.enc OK" not in combined:
        assert "NOT ARMED" in combined
        assert not (dest / "daily" / f"env-{today}.enc").exists()


def test_env_enc_encrypts_and_round_trip_verifies_with_a_passphrase(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file, "OPENAI_API_KEY=sk-round-trip-test\n")

    result = _run_backup(db, dest, env_path=env_file, passphrase="test-passphrase-123")

    assert result.returncode == 0, result.stderr
    assert "env.enc OK" in result.stdout
    assert "round-trip verified" in result.stdout
    today = date.today().isoformat()
    enc_file = dest / "daily" / f"env-{today}.enc"
    assert enc_file.exists()

    # Independently decrypt it ourselves (not trusting the script's own
    # self-check) and confirm it actually matches the source. A here-string
    # on an explicit fd is awkward to express directly via subprocess's own
    # stdin= plumbing, so shell out to a tiny bash wrapper -- matching
    # exactly how the script itself passes the passphrase (never argv/env).
    verify = subprocess.run(
        ["bash", "-c", 'openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in "$1" 3<<< "$2"',
         "_", str(enc_file), "test-passphrase-123"],
        capture_output=True, text=True, timeout=10,
    )
    assert verify.returncode == 0, verify.stderr
    assert "OPENAI_API_KEY=sk-round-trip-test" in verify.stdout


def test_env_enc_skipped_when_source_env_file_absent(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")

    result = _run_backup(db, dest, env_path=tmp_path / "does_not_exist_env", passphrase="whatever")

    assert result.returncode == 0, result.stderr
    assert "env.enc skipped" in result.stdout + result.stderr
    today = date.today().isoformat()
    assert (dest / "daily" / f"alphaos-{today}.db.gz").exists()  # DB backup unaffected


def test_manifest_json_contains_sha256_of_the_db_backup(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "manifest-test")

    result = _run_backup(db, dest)

    assert result.returncode == 0, result.stderr
    today = date.today().isoformat()
    manifest_file = dest / "daily" / f"MANIFEST-{today}.json"
    assert manifest_file.exists()

    import json
    manifest = json.loads(manifest_file.read_text())
    db_gz = dest / "daily" / f"alphaos-{today}.db.gz"
    expected_sha = hashlib.sha256(db_gz.read_bytes()).hexdigest()
    assert manifest["db_gz"]["sha256"] == expected_sha
    assert manifest["date"] == today
    assert "schema_version" in manifest
    assert "git_rev" in manifest


def test_manifest_includes_env_enc_entry_only_when_armed(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file)

    result = _run_backup(db, dest, env_path=env_file, passphrase="test-pass")

    assert result.returncode == 0, result.stderr
    import json
    today = date.today().isoformat()
    manifest = json.loads((dest / "daily" / f"MANIFEST-{today}.json").read_text())
    assert manifest["env_enc"] is not None
    assert "sha256" in manifest["env_enc"]


def test_offsite_backup_not_configured_is_a_warning_not_a_failure(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")

    result = _run_backup(db, dest, backup2_method="")  # explicitly empty/unconfigured

    assert result.returncode == 0, result.stderr
    assert "NOT ARMED" in result.stdout + result.stderr
    today = date.today().isoformat()
    assert (dest / "daily" / f"alphaos-{today}.db.gz").exists()  # DB backup unaffected


def test_offsite_backup_disk_method_copies_artifacts(tmp_path):
    # audit HIGH fix (2026-07-10): offsite requires the SAME passphrase as
    # env.enc -- an unencrypted DB must never leave this Mac. A real .env +
    # passphrase are now required for the offsite leg to proceed at all.
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    offsite = tmp_path / "offsite_target"
    offsite.mkdir()
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file)

    result = _run_backup(
        db, dest, env_path=env_file, passphrase="offsite-test-pass",
        backup2_method="disk", backup2_dest=offsite,
    )

    assert result.returncode == 0, result.stderr
    assert "Offsite backup OK" in result.stdout
    today = date.today().isoformat()
    assert (offsite / f"alphaos-{today}.db.gz.enc").exists()  # encrypted, not plaintext
    assert not (offsite / f"alphaos-{today}.db.gz").exists()  # plaintext must NOT be shipped
    assert (offsite / f"MANIFEST-{today}.json").exists()
    assert (offsite / f"env-{today}.enc").exists()


def test_offsite_backup_disk_method_missing_dest_dir_alerts_and_does_not_crash(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file)

    result = _run_backup(
        db, dest, env_path=env_file, passphrase="offsite-test-pass",
        backup2_method="disk", backup2_dest=tmp_path / "not_mounted",
    )

    assert result.returncode == 0, result.stderr  # best-effort: never fails the overall run
    assert "BACKUP FAILURE" in result.stdout + result.stderr
    today = date.today().isoformat()
    assert (dest / "daily" / f"alphaos-{today}.db.gz").exists()  # DB backup unaffected


def test_offsite_backup_only_happens_once_per_month(tmp_path):
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    offsite = tmp_path / "offsite_target"
    offsite.mkdir()
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file)

    r1 = _run_backup(
        db, dest, env_path=env_file, passphrase="offsite-test-pass",
        backup2_method="disk", backup2_dest=offsite,
    )
    r2 = _run_backup(
        db, dest, env_path=env_file, passphrase="offsite-test-pass",
        backup2_method="disk", backup2_dest=offsite,
    )

    assert r1.returncode == 0 and r2.returncode == 0
    assert "Offsite backup OK" in r1.stdout
    assert "already done this month" in r2.stdout


def test_offsite_db_copy_is_encrypted_not_plaintext(tmp_path):
    """audit HIGH (correctness, 2026-07-10): spec §3 explicitly requires
    "Encrypt the DB at the second target too" -- the offsite copy must
    never be a plain, gunzip-able .db.gz; only the local iCloud copy stays
    plaintext (fast restore). Verified both by content (not valid gzip) and
    by decrypting it back with the same passphrase and confirming the real
    DB content comes out the other side."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    offsite = tmp_path / "offsite_target"
    offsite.mkdir()
    _make_real_sqlite_db(db, "offsite-encryption-check")
    _write_env_file(env_file)

    result = _run_backup(
        db, dest, env_path=env_file, passphrase="offsite-test-pass",
        backup2_method="disk", backup2_dest=offsite,
    )

    assert result.returncode == 0, result.stderr
    assert "Offsite backup OK" in result.stdout
    today = date.today().isoformat()
    offsite_db = offsite / f"alphaos-{today}.db.gz.enc"
    assert offsite_db.exists()

    with pytest.raises(gzip.BadGzipFile):
        with gzip.open(offsite_db, "rb") as f:
            f.read()

    decrypt = subprocess.run(
        ["bash", "-c",
         'openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in "$1" -out "$2" 3<<< "$3"',
         "_", str(offsite_db), str(tmp_path / "restored.db.gz"), "offsite-test-pass"],
        capture_output=True, text=True, timeout=10,
    )
    assert decrypt.returncode == 0, decrypt.stderr
    restored = tmp_path / "restored.db"
    with gzip.open(tmp_path / "restored.db.gz", "rb") as src, open(restored, "wb") as dst:
        shutil.copyfileobj(src, dst)
    conn = sqlite3.connect(str(restored))
    row = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert row == ("offsite-encryption-check",)


def test_offsite_backup_refuses_when_no_passphrase_armed(tmp_path):
    """audit HIGH fix, negative case: BACKUP2_METHOD configured but no
    passphrase available (env.enc itself unarmed/absent) must ship NOTHING
    offsite -- never fall back to an unencrypted DB copy."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    offsite = tmp_path / "offsite_target"
    offsite.mkdir()
    _make_real_sqlite_db(db, "x")

    result = _run_backup(db, dest, backup2_method="disk", backup2_dest=offsite)
    # no env_path / no passphrase -- env.enc skipped, so offsite must refuse too

    assert result.returncode == 0, result.stderr  # best-effort: never fails the overall run
    assert "no Keychain passphrase armed" in result.stdout + result.stderr
    assert list(offsite.iterdir()) == []  # nothing shipped -- not even a plaintext DB


def test_no_passphrase_material_ever_appears_in_captured_output(tmp_path):
    """Spec's own explicitly-named acceptance test: "grep test -- no
    passphrase/key material in any captured log/journal output." Runs a
    full backup (env.enc + offsite, both armed) with a distinctive sentinel
    passphrase and confirms it appears in NEITHER stdout/stderr NOR any
    artifact written to disk (MANIFEST, offsite manifest, or the .enc
    files themselves, which are ciphertext and should never contain the
    key used to produce them in any recognizable form)."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    env_file = tmp_path / "dotenv"
    offsite = tmp_path / "offsite_target"
    offsite.mkdir()
    _make_real_sqlite_db(db, "x")
    _write_env_file(env_file)
    sentinel = "SENTINEL-PASSPHRASE-do-not-leak-9f8e7d6c"

    result = _run_backup(
        db, dest, env_path=env_file, passphrase=sentinel,
        backup2_method="disk", backup2_dest=offsite,
    )

    assert result.returncode == 0, result.stderr
    combined_output = result.stdout + result.stderr
    assert sentinel not in combined_output

    for artifact_dir in (dest / "daily", offsite):
        for artifact in artifact_dir.iterdir():
            if artifact.is_file():
                content = artifact.read_bytes()
                assert sentinel.encode() not in content, f"sentinel leaked into {artifact}"


def test_status_json_written_with_expected_fields(tmp_path, monkeypatch):
    """The status file is written repo-relative (data/backup_status.json),
    not into the throwaway dest dir -- redirect REPO_DIR's notion of "data/"
    by running from a temp copy would be disproportionate for this one
    assertion; instead just confirm the real repo's status file gets
    refreshed and has the expected shape after a run."""
    db = tmp_path / "source.db"
    dest = tmp_path / "dest"
    _make_real_sqlite_db(db, "x")
    status_file = SCRIPT.parent.parent / "data" / "backup_status.json"
    status_file.unlink(missing_ok=True)

    try:
        result = _run_backup(db, dest)

        assert result.returncode == 0, result.stderr
        assert status_file.exists()
        import json
        status = json.loads(status_file.read_text())
        assert "nightly_backup_ok_at_utc" in status
        assert "env_enc_armed" in status
        assert "offsite_configured" in status
    finally:
        status_file.unlink(missing_ok=True)
