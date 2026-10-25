"""Tests for backup.store (deduplicated parts, manifest, local index) and
backup.restore (reassembling a dump directory from a manifest).

No DB: a fake pg_dump directory (a few small files) stands in for a real dump.
Real `zstd` and `age` run, with a throwaway keypair; everything lives under
pytest's tmp_path.
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backup import restore, store
from backup.pipeline import resolve_tool

T1 = datetime(1999, 12, 30, 3, 30, 0, tzinfo=timezone.utc)
T2 = datetime(1999, 12, 31, 3, 30, 0, tzinfo=timezone.utc)
T3 = datetime(1999, 12, 31, 4, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def age_keypair(tmp_path):
    """A throwaway age keypair. Returns (recipient_pubkey, identity_path)."""
    identity = tmp_path / "identity.txt"
    out = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    m = re.search(r"(age1[0-9a-z]+)", out.stderr + out.stdout)
    assert m, f"could not parse age public key from: {out.stderr!r}"
    return m.group(1), identity


def _dump(root: Path, files: dict[str, bytes]) -> Path:
    d = root
    d.mkdir(parents=True)
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


def _store(repo, dump_dir, now, recipient):
    return store.store_dump(
        repo, dump_dir, now=now,
        encrypt_argv=[resolve_tool("AGE", "age"), "-r", recipient],
        zstd=resolve_tool("ZSTD", "zstd"), zstd_level=1,
    )


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo, result):
    rels = [p.relative_to(repo).as_posix() for p in result.files()]
    _git(repo, "add", "--", *rels)
    _git(repo, "commit", "-m", "backup", "--", *rels)


DUMP_A = {"toc.dat": b"schema v1\n", "4872.dat": b"diary rows\n", "4874.dat": b"chat rows 1\n"}
DUMP_B = {"toc.dat": b"schema v1\n", "4872.dat": b"diary rows\n", "4874.dat": b"chat rows 2\n"}


def test_manifest_and_part_layout():
    assert store.manifest_relative_path(T2).as_posix() == \
        "rainbox_database/1999-12-xx/1999-12-31T03-30-00Z.manifest.age"
    assert store.part_directory(T2).as_posix() == \
        "rainbox_database/1999-12-xx/1999-12-31T03-30-00Z"


def test_manifest_path_converts_aware_to_utc():
    from datetime import timedelta

    local = datetime(2000, 1, 1, 0, 30, 0, tzinfo=timezone(timedelta(hours=2)))
    assert store.manifest_relative_path(local).as_posix() == \
        "rainbox_database/1999-12-xx/1999-12-31T22-30-00Z.manifest.age"


def test_first_backup_stores_every_file_and_restores(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)

    assert res.manifest == repo / store.manifest_relative_path(T1)
    assert res.reused == 0
    assert sorted(p.name for p in res.new_parts) == \
        ["4872.dat.zstd.age", "4874.dat.zstd.age", "toc.dat.zstd.age"]
    assert all(p.parent == repo / store.part_directory(T1) for p in res.new_parts)
    for p in res.files():
        assert p.read_bytes().startswith(b"age-encryption.org/")
    assert res.bytes_written == sum(p.stat().st_size for p in res.files())
    # No temp files left next to the parts or the manifest.
    assert not list(repo.rglob("*.part"))

    out = tmp_path / "out"
    names = restore.assemble(res.manifest, identity, out)
    assert sorted(names) == sorted(DUMP_A)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == DUMP_A


def test_unchanged_files_reuse_earlier_parts(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    first = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    second = _store(repo, _dump(tmp_path / "d2", DUMP_B), T2, recipient)

    assert [p.name for p in second.new_parts] == ["4874.dat.zstd.age"]
    assert second.reused == 2

    manifest = restore.read_manifest(second.manifest, identity)
    parts = {f["name"]: f["part"] for f in manifest["files"]}
    first_dir = store.part_directory(T1).as_posix()
    assert parts["toc.dat"] == f"{first_dir}/toc.dat.zstd.age"
    assert parts["4872.dat"] == f"{first_dir}/4872.dat.zstd.age"
    assert parts["4874.dat"] == f"{store.part_directory(T2).as_posix()}/4874.dat.zstd.age"
    assert first.manifest.exists()

    out = tmp_path / "out"
    restore.assemble(second.manifest, identity, out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == DUMP_B


def test_identical_files_in_one_dump_share_a_part(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    empty = b"\\.\n\n"
    res = _store(repo, _dump(tmp_path / "d1", {"toc.dat": b"s\n", "1.dat": empty, "2.dat": empty}),
                 T1, recipient)
    assert len(res.new_parts) == 2
    files = {f["name"]: f["part"] for f in restore.read_manifest(res.manifest, identity)["files"]}
    assert files["1.dat"] == files["2.dat"]


def test_manifest_lists_every_file_sorted_with_plaintext_digest(tmp_path, age_keypair):
    import hashlib

    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    manifest = restore.read_manifest(res.manifest, identity)
    assert manifest["format"] == store.MANIFEST_FORMAT
    assert manifest["created_at"] == "1999-12-30T03:30:00Z"
    assert [f["name"] for f in manifest["files"]] == sorted(DUMP_A)
    for f in manifest["files"]:
        assert f["sha256"] == hashlib.sha256(DUMP_A[f["name"]]).hexdigest()
        assert f["bytes"] == len(DUMP_A[f["name"]])


def test_in_a_git_repo_only_committed_parts_are_reused(git_repo, tmp_path, age_keypair):
    recipient, _ = age_keypair
    first = _store(git_repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    # Never committed (e.g. the upload was refused): nothing to build on.
    second = _store(git_repo, _dump(tmp_path / "d2", DUMP_A), T2, recipient)
    assert second.reused == 0 and len(second.new_parts) == 3

    _commit(git_repo, second)
    third = _store(git_repo, _dump(tmp_path / "d3", DUMP_A), T3, recipient)
    assert third.reused == 3 and third.new_parts == []
    assert first.manifest.exists()


def test_a_missing_part_is_stored_again(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    first = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    (repo / store.part_directory(T1) / "4872.dat.zstd.age").unlink()

    second = _store(repo, _dump(tmp_path / "d2", DUMP_A), T2, recipient)
    assert [p.name for p in second.new_parts] == ["4872.dat.zstd.age"]
    out = tmp_path / "out"
    restore.assemble(second.manifest, identity, out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == DUMP_A
    assert first.manifest.exists()


def test_index_stays_out_of_the_files_to_commit(git_repo, tmp_path, age_keypair):
    recipient, _ = age_keypair
    res = _store(git_repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    index = store.index_path(git_repo)
    assert index == git_repo / ".git" / "rainbox-backup-index.json"
    assert index.is_file() and index not in res.files()
    data = json.loads(index.read_text())
    assert data["format"] == store.INDEX_FORMAT
    assert sorted(data["parts"].values()) == sorted(
        p.relative_to(git_repo).as_posix() for p in res.new_parts)


def test_index_in_a_plain_directory(tmp_path):
    assert store.index_path(tmp_path) == tmp_path / ".rainbox-backup-index.json"


def test_corrupt_index_means_a_full_backup(tmp_path, age_keypair):
    recipient, _ = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    store.index_path(repo).write_text("{not json")
    second = _store(repo, _dump(tmp_path / "d2", DUMP_A), T2, recipient)
    assert second.reused == 0 and len(second.new_parts) == 3


def test_failed_run_leaves_no_manifest_parts_or_index(tmp_path, age_keypair):
    recipient, _ = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RuntimeError, match="age"):
        _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, "age1notavalidrecipient")
    assert not (repo / store.manifest_relative_path(T1)).exists()
    assert not (repo / store.part_directory(T1)).exists()
    assert not store.index_path(repo).exists()


def test_unsafe_dump_file_name_is_refused(tmp_path, age_keypair):
    recipient, _ = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="dump file name"):
        _store(repo, _dump(tmp_path / "d1", {".hidden": b"x"}), T1, recipient)


def test_restore_detects_a_swapped_part(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    parts = repo / store.part_directory(T1)
    (parts / "4872.dat.zstd.age").write_bytes((parts / "4874.dat.zstd.age").read_bytes())
    with pytest.raises(restore.RestoreError, match="4872.dat"):
        restore.assemble(res.manifest, identity, tmp_path / "out")


def test_restore_reports_a_missing_part(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    (repo / store.part_directory(T1) / "toc.dat.zstd.age").unlink()
    with pytest.raises(restore.RestoreError, match="missing part"):
        restore.assemble(res.manifest, identity, tmp_path / "out")


def test_restore_refuses_an_existing_output_directory(tmp_path, age_keypair):
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _store(repo, _dump(tmp_path / "d1", DUMP_A), T1, recipient)
    (tmp_path / "out").mkdir()
    with pytest.raises(restore.RestoreError, match="already exists"):
        restore.assemble(res.manifest, identity, tmp_path / "out")


def test_restore_refuses_a_part_outside_the_repo(tmp_path, age_keypair):
    """A manifest naming `../` paths must not read or write outside its places."""
    recipient, identity = age_keypair
    repo = tmp_path / "repo"
    (repo / "rainbox_database" / "1999-12-xx").mkdir(parents=True)
    manifest = {"format": store.MANIFEST_FORMAT, "created_at": "1999-12-31T03:30:00Z",
                "files": [{"name": "toc.dat", "part": "../elsewhere.zstd.age",
                           "sha256": "0" * 64, "bytes": 1}]}
    path = repo / store.manifest_relative_path(T2)
    subprocess.run([resolve_tool("AGE", "age"), "-r", recipient, "-o", str(path)],
                   input=json.dumps(manifest).encode(), check=True)
    with pytest.raises(restore.RestoreError, match="outside the backup repo"):
        restore.assemble(path, identity, tmp_path / "out")
