"""Back up the rainbox Postgres database as deduplicated, public-key-encrypted parts.

Dumps postgres in pg_dump's directory format (one file per table's rows, plus
`toc.dat`) into a private temp directory, then hands it to backup.store, which
writes each file not already stored as a zstd-compressed part encrypted to a
recipient's *public* key with `age`, plus one encrypted manifest naming every
part (see notes/backup.md for the full operator guide):

    pg_dump --format=directory <dsn>        ->  toc.dat, <id>.dat, …
    each file not stored yet: zstd | age    ->  <ts>Z/<file>.zstd.age
    manifest (JSON) | age                   ->  <ts>Z.manifest.age

The rows of DERIVED_TABLES are left out; the nightly sync jobs rebuild them
after a restore.

Encryption is **mandatory and fail-closed**: a backup is only ever written
encrypted. rainbox holds only the recipient's public key, so a compromised host
can produce backups but cannot read them — the matching age *identity* (private
key) stays offline and is needed to restore (backup.restore reassembles the
dump directory; pg_restore loads it).

Generate a keypair once, offline, with `age-keygen -o identity.txt`; give
rainbox only the printed `age1…` public key (via RAINBOX_BACKUP_AGE_RECIPIENT or
--recipient) and keep identity.txt off the machine.

Usage:
    python -m backup.dump <backup-repo> -r age1...   # or set the env vars below
"""
import argparse
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backup import store
from backup.pipeline import resolve_tool, run_pipeline
from db.models import psycopg_dsn

logger = logging.getLogger(__name__)

DEFAULT_ZSTD_LEVEL = 19

# Tables whose rows are left out of every dump (their schema stays): data the
# nightly sync jobs rebuild from rows the dump does keep. The embedding tables
# are vector caches (vectors barely compress, and diary_embedding alone was
# most of a full dump); diary_word is the word index Diary sync rebuilds on
# every run, so its dump would never deduplicate.
DERIVED_TABLES = ("diary_embedding", "memory_embedding", "diary_word")

# Env vars for the recipient public key(s). RECIPIENT holds one or more inline
# `age1…`/`ssh-…` recipients (whitespace/comma separated); RECIPIENTS_FILE points
# at an age recipients file (one per line). Either or both may be set.
ENV_RECIPIENT = "RAINBOX_BACKUP_AGE_RECIPIENT"
ENV_RECIPIENTS_FILE = "RAINBOX_BACKUP_AGE_RECIPIENTS_FILE"


class NoRecipientError(RuntimeError):
    """No age recipient (public key) was configured. Backups are fail-closed:
    rather than write a plaintext dump, we refuse to back up at all."""


def split_recipients(value: str | None) -> list[str]:
    """Split an inline recipient string ("age1aaa, age1bbb") into a list,
    dropping empties. Whitespace- or comma-separated."""
    return [r for r in re.split(r"[\s,]+", (value or "").strip()) if r]


def resolve_recipients(
    recipients: list[str] | None = None,
    recipients_file: str | os.PathLike[str] | None = None,
) -> tuple[list[str], str | None]:
    """Resolve the age recipient public key(s) for this backup.

    Explicit arguments win; otherwise read RAINBOX_BACKUP_AGE_RECIPIENT (inline,
    whitespace/comma separated) and RAINBOX_BACKUP_AGE_RECIPIENTS_FILE. Returns
    (inline_recipients, recipients_file_or_None). Raises NoRecipientError if
    neither source yields anything — backups never silently fall back to
    plaintext."""
    if recipients is None:
        recipients = split_recipients(os.environ.get(ENV_RECIPIENT, ""))
    if recipients_file is None:
        recipients_file = os.environ.get(ENV_RECIPIENTS_FILE) or None
    if not recipients and not recipients_file:
        raise NoRecipientError(
            "no age recipient configured; set "
            f"{ENV_RECIPIENT} (an age1… public key) or {ENV_RECIPIENTS_FILE}, "
            "or pass --recipient. Backups are encrypt-only and will not run "
            "without a recipient public key."
        )
    return recipients, (str(recipients_file) if recipients_file else None)


def backup_database(
    backup_repo: str | os.PathLike[str],
    *,
    dsn: str | None = None,
    now: datetime | None = None,
    zstd_level: int = DEFAULT_ZSTD_LEVEL,
    recipients: list[str] | None = None,
    recipients_file: str | os.PathLike[str] | None = None,
) -> store.StoreResult:
    """Back up the database into `backup_repo` and return what was written.

    pg_dump writes its directory-format dump into a private (0700) temp
    directory that is deleted when this returns; backup.store then stores it
    deduplicated and encrypted. Encryption is mandatory: raises
    NoRecipientError before touching pg_dump if no recipient public key is
    configured.
    """
    recips, recips_file = resolve_recipients(recipients, recipients_file)

    dsn = psycopg_dsn() if dsn is None else dsn
    if now is None:
        now = datetime.now(timezone.utc)

    pg_dump = resolve_tool("PG_DUMP", "pg_dump")
    zstd = resolve_tool("ZSTD", "zstd")
    age = resolve_tool("AGE", "age")

    age_argv = [age]
    for r in recips:
        age_argv += ["-r", r]
    if recips_file:
        age_argv += ["-R", recips_file]

    logger.info("backing up %s -> %s (encrypted to %d recipient(s))",
                dsn, backup_repo, len(recips) + (1 if recips_file else 0))
    with tempfile.TemporaryDirectory(prefix="rainbox-backup-") as tmp:
        dump_dir = Path(tmp) / "dump"
        run_pipeline([[
            pg_dump, "--format=directory", "--compress=0", f"--file={dump_dir}",
            *(f"--exclude-table-data={t}" for t in DERIVED_TABLES), dsn,
        ]], subprocess.DEVNULL)
        result = store.store_dump(
            backup_repo, dump_dir, now=now,
            encrypt_argv=age_argv, zstd=zstd, zstd_level=zstd_level,
        )

    logger.info("backup complete: %s (%d new parts, %d reused, %d bytes written)",
                result.manifest, len(result.new_parts), result.reused, result.bytes_written)
    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="python -m backup.dump",
        description="Back up the rainbox database as deduplicated zstd+age-encrypted parts."
    )
    parser.add_argument(
        "backup_repo",
        nargs="?",
        default=os.environ.get("RAINBOX_BACKUP_REPO"),
        help="Backup-repo directory (or set RAINBOX_BACKUP_REPO). "
        "Backups land under <repo>/rainbox_database/.",
    )
    parser.add_argument(
        "-r", "--recipient",
        action="append",
        dest="recipients",
        metavar="age1...",
        help=f"age recipient public key (repeatable). Or set {ENV_RECIPIENT}.",
    )
    parser.add_argument(
        "--recipients-file",
        metavar="PATH",
        help=f"age recipients file (one per line). Or set {ENV_RECIPIENTS_FILE}.",
    )
    parser.add_argument(
        "--zstd-level",
        type=int,
        default=DEFAULT_ZSTD_LEVEL,
        help=f"zstd compression level (default {DEFAULT_ZSTD_LEVEL}).",
    )
    parser.add_argument(
        "--git-push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="After writing the backup, commit its files into the backup-repo git "
        "repo and push (default: RAINBOX_BACKUP_GIT_PUSH).",
    )
    args = parser.parse_args()

    if not args.backup_repo:
        parser.error("backup_repo is required (pass it or set RAINBOX_BACKUP_REPO)")

    try:
        result = backup_database(
            args.backup_repo,
            zstd_level=args.zstd_level,
            recipients=args.recipients,
            recipients_file=args.recipients_file,
        )
    except NoRecipientError as exc:
        parser.error(str(exc))
    print(result.manifest)

    from backup import remote as backup_remote

    push = backup_remote.git_push_enabled() if args.git_push is None else args.git_push
    if push:
        # The local backup already succeeded; an upload failure shouldn't discard
        # it, but it should still be a non-zero exit so a caller notices.
        try:
            logger.info("%s", backup_remote.git_push_backup(args.backup_repo, result.files()))
        except Exception as exc:  # noqa: BLE001
            logger.error("upload failed: %s", exc)
            sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
