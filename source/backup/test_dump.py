"""Tests for backup.dump.

The round-trip tests hit the live local Postgres (db.psycopg_dsn(), which the
conftest points at rainbox_claude) but write only into pytest's tmp_path, so
they leave no artifacts behind. Backups are encrypt-only (age), so they
generate a throwaway keypair, encrypt to its public key, then reassemble the
dump with the private identity (backup.restore) and read it back with
`pg_restore -f -`.
"""
import re
import subprocess
from datetime import datetime, timezone

import pytest

from backup import dump as backup_db
from backup import restore, store
from backup.pipeline import resolve_tool


@pytest.fixture
def age_keypair(tmp_path):
    """A throwaway age keypair. Returns (recipient_pubkey, identity_path)."""
    identity = tmp_path / "identity.txt"
    out = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    # age-keygen prints "Public key: age1..." to stderr.
    m = re.search(r"(age1[0-9a-z]+)", out.stderr + out.stdout)
    assert m, f"could not parse age public key from: {out.stderr!r}"
    return m.group(1), identity


def _restored_sql(manifest, identity, tmp_path) -> bytes:
    out = tmp_path / "restored"
    restore.assemble(manifest, identity, out)
    return subprocess.run(
        [resolve_tool("PG_RESTORE", "pg_restore"), "-f", "-", str(out)],
        capture_output=True, check=True,
    ).stdout


def test_resolve_recipients_inline_and_env(monkeypatch):
    # Explicit argument wins.
    recips, rfile = backup_db.resolve_recipients(["age1aaa", "age1bbb"])
    assert recips == ["age1aaa", "age1bbb"] and rfile is None
    # Env var, whitespace/comma separated.
    monkeypatch.setenv(backup_db.ENV_RECIPIENT, "age1aaa, age1bbb\nage1ccc")
    recips, _ = backup_db.resolve_recipients()
    assert recips == ["age1aaa", "age1bbb", "age1ccc"]


def test_resolve_recipients_missing_raises(monkeypatch):
    monkeypatch.delenv(backup_db.ENV_RECIPIENT, raising=False)
    monkeypatch.delenv(backup_db.ENV_RECIPIENTS_FILE, raising=False)
    with pytest.raises(backup_db.NoRecipientError):
        backup_db.resolve_recipients()


def test_backup_database_roundtrip(tmp_path, age_keypair):
    """Take a real encrypted backup, reassemble it, and read it back as a
    valid pg_dump; derived tables keep their schema but not their rows."""
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    now = datetime(1999, 12, 31, 3, 30, 0, tzinfo=timezone.utc)
    res = backup_db.backup_database(repo, now=now, zstd_level=1, recipients=[recipient])

    assert res.manifest == repo / "rainbox_database/1999-12-xx/1999-12-31T03-30-00Z.manifest.age"
    assert res.manifest.read_bytes().startswith(b"age-encryption.org/")
    assert any(p.name == "toc.dat.zstd.age" for p in res.new_parts)

    plain = _restored_sql(res.manifest, identity, tmp_path)
    assert b"PostgreSQL database dump" in plain
    for table in backup_db.DERIVED_TABLES:
        assert f"CREATE TABLE public.{table} ".encode() in plain
        assert f"COPY public.{table} ".encode() not in plain
    assert b"COPY public.chat_message " in plain


def test_second_backup_of_an_unchanged_database_reuses_parts(tmp_path, age_keypair):
    recipient, _ = age_keypair
    repo = tmp_path / "repo"
    first = backup_db.backup_database(
        repo, now=datetime(1999, 12, 30, 3, 30, tzinfo=timezone.utc),
        zstd_level=1, recipients=[recipient])
    second = backup_db.backup_database(
        repo, now=datetime(1999, 12, 31, 3, 30, tzinfo=timezone.utc),
        zstd_level=1, recipients=[recipient])
    # The sandbox can change between the two dumps, but nearly all of it can't.
    assert second.reused >= len(first.new_parts) - 3
    assert len(second.new_parts) <= 3


def test_backup_database_writes_no_plaintext_into_the_repo(tmp_path, age_keypair):
    recipient, _ = age_keypair
    repo = tmp_path / "repo"
    backup_db.backup_database(
        repo, now=datetime(1999, 12, 31, 3, 30, tzinfo=timezone.utc),
        zstd_level=1, recipients=[recipient])
    for p in repo.rglob("*"):
        if p.is_file() and p != store.index_path(repo):
            assert p.read_bytes().startswith(b"age-encryption.org/"), p


def test_backup_database_without_recipient_raises(tmp_path, monkeypatch):
    """Fail-closed: no recipient configured -> refuse to back up (no plaintext)."""
    monkeypatch.delenv(backup_db.ENV_RECIPIENT, raising=False)
    monkeypatch.delenv(backup_db.ENV_RECIPIENTS_FILE, raising=False)
    now = datetime(1999, 12, 31, 3, 30, 0, tzinfo=timezone.utc)
    with pytest.raises(backup_db.NoRecipientError):
        backup_db.backup_database(tmp_path, now=now)
    # Nothing was written.
    assert not (tmp_path / "rainbox_database").exists()


def test_backup_database_missing_tool_raises(tmp_path, monkeypatch, age_keypair):
    recipient, _ = age_keypair
    monkeypatch.setenv("PG_DUMP", "/nonexistent/definitely-not-pg_dump-binary")
    now = datetime(1999, 12, 31, 3, 30, 0, tzinfo=timezone.utc)
    with pytest.raises(Exception):
        backup_db.backup_database(tmp_path, now=now, recipients=[recipient])
    # A failed backup leaves nothing behind.
    bucket = tmp_path / "rainbox_database/1999-12-xx"
    assert not bucket.exists() or list(bucket.iterdir()) == []
