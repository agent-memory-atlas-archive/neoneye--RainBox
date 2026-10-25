"""Store a pg_dump directory as deduplicated, encrypted parts plus a manifest.

Each file of a directory-format dump (`toc.dat`, one `<dump id>.dat` per
table's rows) becomes a *part* — zstd-compressed, then age-encrypted — unless an
earlier backup already stored that exact plaintext, in which case the earlier
part is named again. One age-encrypted JSON *manifest* per backup maps every
dump file to its part, with the plaintext's SHA-256 and size (see
notes/backup.md, "File layout"):

    <repo>/rainbox_database/<yyyy>-<mm>-xx/<ts>Z.manifest.age
    <repo>/rainbox_database/<yyyy>-<mm>-xx/<ts>Z/<dump file>.zstd.age

Which SHA-256 is stored in which part is remembered in a local index that never
leaves the host (`.git/rainbox-backup-index.json` in a git repo). A part is
reused only while its file exists and, in a git repo, is committed.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backup import remote
from backup.pipeline import run_pipeline

logger = logging.getLogger(__name__)

DB_SUBDIR = "rainbox_database"
MANIFEST_SUFFIX = ".manifest.age"
PART_SUFFIX = ".zstd.age"
MANIFEST_FORMAT = "rainbox-backup-manifest/1"
INDEX_FORMAT = "rainbox-backup-index/1"
INDEX_NAME = "rainbox-backup-index.json"

# What pg_dump's directory format names its files: toc.dat, <id>.dat,
# blobs_<id>.toc, blob_<oid>.dat. Anything else is refused rather than
# written under a name the restore would have to trust.
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


@dataclass
class StoreResult:
    """One stored backup: its manifest, the parts this run wrote, and how many
    dump files were served by parts an earlier backup wrote."""

    manifest: Path
    new_parts: list[Path] = field(default_factory=list)
    reused: int = 0

    def files(self) -> list[Path]:
        """Everything this backup wrote, the manifest last (it names the commit)."""
        return [*self.new_parts, self.manifest]

    @property
    def bytes_written(self) -> int:
        return sum(p.stat().st_size for p in self.files())


def _utc(now: datetime) -> datetime:
    """Naive datetimes are assumed to already be UTC; aware ones are converted."""
    return now.astimezone(timezone.utc) if now.tzinfo is not None else now


def _stamp(now: datetime) -> str:
    return _utc(now).strftime("%Y-%m-%dT%H-%M-%SZ")


def _bucket(now: datetime) -> Path:
    return Path(DB_SUBDIR) / _utc(now).strftime("%Y-%m-xx")


def manifest_relative_path(now: datetime) -> Path:
    """The manifest of a backup taken at `now`, relative to the backup repo."""
    return _bucket(now) / f"{_stamp(now)}{MANIFEST_SUFFIX}"


def part_directory(now: datetime) -> Path:
    """Where a backup taken at `now` writes the parts it is first to store."""
    return _bucket(now) / _stamp(now)


def index_path(repo: str | os.PathLike[str]) -> Path:
    """The local index: inside `.git/` when the repo has one (git never pushes
    it), else a dotfile at the repo root."""
    repo = Path(repo)
    git_dir = repo / ".git"
    return git_dir / INDEX_NAME if git_dir.is_dir() else repo / f".{INDEX_NAME}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_index(repo: Path) -> dict[str, str]:
    path = index_path(repo)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable backup index %s: %s", path, exc)
        return {}
    if not isinstance(data, dict) or data.get("format") != INDEX_FORMAT:
        logger.warning("ignoring backup index %s with unknown format", path)
        return {}
    parts = data.get("parts")
    return {k: v for k, v in parts.items() if isinstance(v, str)} if isinstance(parts, dict) else {}


def _write_atomic(dest: Path, write) -> None:
    """Call `write(fileobj)` into a temp file next to `dest`, then rename it
    into place; on any failure the temp file is removed."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            write(out)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def store_dump(
    repo: str | os.PathLike[str],
    dump_dir: str | os.PathLike[str],
    *,
    now: datetime,
    encrypt_argv: list[str],
    zstd: str,
    zstd_level: int,
) -> StoreResult:
    """Store the pg_dump directory `dump_dir` under `repo` as a backup taken
    at `now`, and return what was written.

    `encrypt_argv` is the full `age -r …` command (reads stdin, writes
    ciphertext to stdout). The manifest is written last and the index updated
    after it, so a failure leaves no manifest; the parts this run wrote are
    removed again on failure."""
    repo = Path(repo)
    dump_dir = Path(dump_dir)
    names = sorted(p.name for p in dump_dir.iterdir())
    for name in names:
        if not SAFE_NAME.match(name) or not (dump_dir / name).is_file():
            raise ValueError(f"unexpected dump file name {name!r} in {dump_dir}")

    index = _load_index(repo)
    tracked = remote.tracked_files(repo)

    def usable(rel: str) -> bool:
        return (repo / rel).is_file() and (tracked is None or rel in tracked)

    manifest_rel = manifest_relative_path(now)
    parts_rel = part_directory(now)
    result = StoreResult(manifest=repo / manifest_rel)
    written: dict[str, str] = {}   # sha256 -> part rel, for parts this run wrote
    entries = []
    try:
        for name in names:
            src = dump_dir / name
            digest = _sha256(src)
            part = written.get(digest)
            if part is None and digest in index and usable(index[digest]):
                part = index[digest]
                result.reused += 1
            if part is None:
                part = (parts_rel / f"{name}{PART_SUFFIX}").as_posix()
                dest = repo / part
                dest.parent.mkdir(parents=True, exist_ok=True)
                _write_atomic(dest, lambda out, src=src: run_pipeline(
                    [[zstd, f"-{zstd_level}", "-q", "-c", str(src)], encrypt_argv], out))
                written[digest] = part
                result.new_parts.append(dest)
            entries.append({"name": name, "part": part, "sha256": digest,
                            "bytes": src.stat().st_size})

        manifest = {
            "format": MANIFEST_FORMAT,
            "created_at": _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": entries,
        }
        result.manifest.parent.mkdir(parents=True, exist_ok=True)

        def write_manifest(out) -> None:
            cp = subprocess.run(encrypt_argv, input=json.dumps(manifest, indent=1).encode(),
                                stdout=out, stderr=subprocess.PIPE)
            if cp.returncode != 0:
                err = cp.stderr.decode(errors="replace").strip()
                raise RuntimeError(f"{encrypt_argv[0]} failed (exit {cp.returncode}): {err}")

        _write_atomic(result.manifest, write_manifest)
    except BaseException:
        for p in result.new_parts:
            p.unlink(missing_ok=True)
        try:
            (repo / parts_rel).rmdir()
        except OSError:
            pass
        raise

    index.update(written)
    _write_atomic(index_path(repo), lambda out: out.write(
        json.dumps({"format": INDEX_FORMAT, "parts": index}, indent=1, sort_keys=True).encode()))
    logger.info("stored %s: %d new parts, %d reused", result.manifest,
                len(result.new_parts), result.reused)
    return result
