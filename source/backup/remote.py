"""Upload a finished backup to a remote by committing it into a git repo.

The backup-repo directory (where `backup.dump` writes `…Z.zstd.age` files) can
itself be a git repo with a remote — e.g.
`git@github.com:Username/rainbox-backup.git`. "Upload to remote" then
means: stage just the files the new backup wrote (its new parts and its
manifest), commit them, and `git push`. Because the
files are already public-key-encrypted ciphertext (see `backup.dump`), pushing
them to untrusted storage like GitHub is safe.

This is opt-in: enabled by `RAINBOX_BACKUP_GIT_PUSH` (truthy) or the
`--git-push` CLI flag. Pushing needs the running process to have credentials for
the remote (e.g. an SSH agent/key for an `git@github.com:` remote).
"""
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

# How long to allow each git invocation (the push talks to the network).
GIT_TIMEOUT = 180

# GitHub rejects a push containing any file over 100 MiB, and an unpushed
# commit holding one blocks every later push too — so such a file is never
# committed; it stays on disk as a local-only backup.
MAX_FILE_BYTES = 100 * 1024 * 1024

_TRUTHY = {"1", "true", "yes", "on"}


def git_push_enabled() -> bool:
    """Whether backups should be pushed, from RAINBOX_BACKUP_GIT_PUSH."""
    return os.environ.get("RAINBOX_BACKUP_GIT_PUSH", "").strip().lower() in _TRUTHY


def is_git_repo(path: str | os.PathLike[str]) -> bool:
    git = shutil.which(os.environ.get("GIT", "git"))
    if not git:
        return False
    return subprocess.run(
        [git, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    ).returncode == 0


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    git = shutil.which(os.environ.get("GIT", "git"))
    if not git:
        raise FileNotFoundError("'git' not found on PATH; set GIT to its full path")
    return subprocess.run(
        [git, "-C", str(repo), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )


def _check(cp: subprocess.CompletedProcess, what: str) -> None:
    if cp.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {cp.returncode}): {cp.stderr.strip()}")


def tracked_files(repo: str | os.PathLike[str]) -> set[str] | None:
    """Paths git tracks (committed or staged) under `repo`, relative to it, or
    None when `repo` is not a git work tree."""
    if not is_git_repo(repo):
        return None
    cp = _git(Path(repo), ["ls-files", "-z"])
    _check(cp, "git ls-files")
    return {p for p in cp.stdout.split("\0") if p}


def git_push_backup(
    repo: str | os.PathLike[str],
    files: Sequence[str | os.PathLike[str]],
    *,
    remote: str = "origin",
) -> str:
    """Commit `files` (all under `repo`) as one commit and push to `remote`.

    Stages only those files (so unrelated working-tree changes are left alone),
    commits them as `backup <last file>` — callers pass the manifest last — and
    pushes the current branch. Returns a short human summary. Raises
    RuntimeError on any git failure (no remote, auth, non-fast-forward,
    network/timeout), and before staging anything when any file is over
    MAX_FILE_BYTES."""
    repo = Path(repo).resolve()
    rels = []
    for f in files:
        path = Path(f).resolve()
        try:
            rel = path.relative_to(repo).as_posix()
        except ValueError as e:
            raise RuntimeError(
                f"backup file {path} is not inside the git repo {repo}"
            ) from e
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RuntimeError(
                f"{rel} is {size} bytes, over the remote's {MAX_FILE_BYTES}-byte file "
                "limit; not committed (the local backup is kept)"
            )
        rels.append(rel)
    if not rels:
        raise ValueError("no backup files to push")

    if not is_git_repo(repo):
        raise RuntimeError(f"{repo} is not a git repository")

    _check(_git(repo, ["add", "--", *rels]), "git add")

    # Commit only if staging produced a change (re-pushing identical paths is a
    # no-op rather than an error). `diff --cached --quiet` exits 1 when staged.
    name = rels[-1]
    if _git(repo, ["diff", "--cached", "--quiet", "--", *rels]).returncode == 0:
        return f"{name}: already committed, nothing to push"

    _check(_git(repo, ["commit", "-m", f"backup {name}", "--", *rels]), "git commit")
    _check(_git(repo, ["push", remote, "HEAD"]), "git push")
    count = f"{len(rels)} files" if len(rels) > 1 else "1 file"
    return f"pushed {count} for {name} to {remote}"
