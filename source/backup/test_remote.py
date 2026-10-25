"""Tests for backup.remote (git commit + push of a backup file).

No DB and no network: a local bare repo stands in for the remote, and a working
clone is the backup-repo. Everything lives under pytest's tmp_path.
"""
import subprocess

import pytest

from backup import remote as backup_remote


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def backup_repo(tmp_path):
    """A working git repo (the backup-repo) wired to a local bare 'remote'."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "README.md").write_text("backups\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "HEAD")
    return repo, remote


def test_git_push_enabled(monkeypatch):
    for v in ("1", "true", "YES", "On"):
        monkeypatch.setenv("RAINBOX_BACKUP_GIT_PUSH", v)
        assert backup_remote.git_push_enabled() is True
    for v in ("", "0", "no", "off"):
        monkeypatch.setenv("RAINBOX_BACKUP_GIT_PUSH", v)
        assert backup_remote.git_push_enabled() is False
    monkeypatch.delenv("RAINBOX_BACKUP_GIT_PUSH", raising=False)
    assert backup_remote.git_push_enabled() is False


def test_is_git_repo(backup_repo, tmp_path):
    repo, _ = backup_repo
    assert backup_remote.is_git_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert backup_remote.is_git_repo(plain) is False


def test_push_commits_and_uploads(backup_repo):
    repo, remote = backup_repo
    f = repo / "rainbox_database" / "2026-01-xx" / "2026-01-01T03-30-00Z.zstd.age"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"age-encryption.org/v1\nciphertext")

    note = backup_remote.git_push_backup(repo, [f])
    assert "pushed" in note and "2026-01-01T03-30-00Z.zstd.age" in note

    # The file is committed locally…
    log = _git(repo, "log", "--name-only", "--format=", "-1").stdout
    assert "2026-01-01T03-30-00Z.zstd.age" in log
    # …and present in the remote (bare repo) tree.
    tree = _git(remote, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "rainbox_database/2026-01-xx/2026-01-01T03-30-00Z.zstd.age" in tree


def test_push_same_file_twice_is_noop(backup_repo):
    repo, _ = backup_repo
    f = repo / "rainbox_database" / "2026-01-xx" / "x.zstd.age"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"data")
    backup_remote.git_push_backup(repo, [f])
    note = backup_remote.git_push_backup(repo, [f])
    assert "nothing to push" in note


def test_push_oversize_file_is_never_committed(backup_repo):
    repo, _ = backup_repo
    head = _git(repo, "rev-parse", "HEAD").stdout
    f = repo / "rainbox_database" / "1999-12-xx" / "1999-12-31T03-30-00Z.zstd.age"
    f.parent.mkdir(parents=True)
    with open(f, "wb") as fh:   # sparse: costs no disk
        fh.truncate(backup_remote.MAX_FILE_BYTES + 1)

    with pytest.raises(RuntimeError, match="file limit"):
        backup_remote.git_push_backup(repo, [f])

    # Nothing committed or staged, so later pushes are not blocked by it.
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_push_file_at_the_limit_is_pushed(backup_repo):
    repo, _ = backup_repo
    f = repo / "rainbox_database" / "1999-12-xx" / "1999-12-31T03-30-00Z.zstd.age"
    f.parent.mkdir(parents=True)
    with open(f, "wb") as fh:
        fh.truncate(backup_remote.MAX_FILE_BYTES)
    assert "pushed" in backup_remote.git_push_backup(repo, [f])


def test_push_several_files_as_one_commit_named_by_the_last(backup_repo):
    repo, remote = backup_repo
    bucket = repo / "rainbox_database" / "1999-12-xx"
    (bucket / "1999-12-31T03-30-00Z").mkdir(parents=True)
    part = bucket / "1999-12-31T03-30-00Z" / "toc.dat.zstd.age"
    manifest = bucket / "1999-12-31T03-30-00Z.manifest.age"
    part.write_bytes(b"part")
    manifest.write_bytes(b"manifest")

    note = backup_remote.git_push_backup(repo, [part, manifest])
    assert "pushed 2 files" in note and "1999-12-31T03-30-00Z.manifest.age" in note
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == \
        "backup rainbox_database/1999-12-xx/1999-12-31T03-30-00Z.manifest.age"
    tree = _git(remote, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "rainbox_database/1999-12-xx/1999-12-31T03-30-00Z/toc.dat.zstd.age" in tree
    assert "rainbox_database/1999-12-xx/1999-12-31T03-30-00Z.manifest.age" in tree


def test_one_oversize_file_keeps_the_whole_backup_uncommitted(backup_repo):
    repo, _ = backup_repo
    head = _git(repo, "rev-parse", "HEAD").stdout
    small = repo / "small.zstd.age"
    small.write_bytes(b"part")
    big = repo / "big.zstd.age"
    with open(big, "wb") as fh:
        fh.truncate(backup_remote.MAX_FILE_BYTES + 1)
    with pytest.raises(RuntimeError, match="file limit"):
        backup_remote.git_push_backup(repo, [small, big])
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_tracked_files(backup_repo, tmp_path):
    repo, _ = backup_repo
    f = repo / "rainbox_database" / "1999-12-xx" / "x.zstd.age"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"data")
    assert backup_remote.tracked_files(repo) == {"README.md"}
    backup_remote.git_push_backup(repo, [f])
    assert backup_remote.tracked_files(repo) == {
        "README.md", "rainbox_database/1999-12-xx/x.zstd.age"}
    plain = tmp_path / "plain"
    plain.mkdir()
    assert backup_remote.tracked_files(plain) is None


def test_push_file_outside_repo_raises(backup_repo, tmp_path):
    repo, _ = backup_repo
    outside = tmp_path / "elsewhere.zstd.age"
    outside.write_bytes(b"data")
    with pytest.raises(RuntimeError, match="not inside the git repo"):
        backup_remote.git_push_backup(repo, [outside])


def test_push_no_remote_raises(tmp_path):
    repo = tmp_path / "norem"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    f = repo / "b.zstd.age"
    f.write_bytes(b"data")
    with pytest.raises(RuntimeError, match="git push failed"):
        backup_remote.git_push_backup(repo, [f])
