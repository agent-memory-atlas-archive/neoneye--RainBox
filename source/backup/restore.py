"""Reassemble a pg_dump directory from a backup manifest (see backup.store).

Decrypts the manifest with the offline age identity, then decrypts and
decompresses every part it names into a new directory, checking each file's
SHA-256 and size against the manifest. The result is an ordinary pg_dump
directory-format dump:

    python -m backup.restore <repo>/rainbox_database/<bucket>/<ts>Z.manifest.age \\
        -i backup-identity.txt -o restore-dump
    createdb rainbox_restore && pg_restore -d rainbox_restore restore-dump

The output directory holds the plaintext database; delete it when done.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from backup.pipeline import resolve_tool, run_pipeline
from backup.store import MANIFEST_FORMAT, SAFE_NAME


class RestoreError(RuntimeError):
    """The backup can't be reassembled faithfully: a part is missing, a file
    doesn't match the manifest, or the manifest itself is malformed."""


def read_manifest(manifest: str | os.PathLike[str], identity: str | os.PathLike[str]) -> dict:
    """Decrypt and parse a manifest, checking its shape."""
    age = resolve_tool("AGE", "age")
    cp = subprocess.run([age, "-d", "-i", str(identity), str(manifest)], capture_output=True)
    if cp.returncode != 0:
        raise RestoreError(f"age failed (exit {cp.returncode}): "
                           f"{cp.stderr.decode(errors='replace').strip()}")
    try:
        data = json.loads(cp.stdout)
    except ValueError as exc:
        raise RestoreError(f"{manifest} is not a backup manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT \
            or not isinstance(data.get("files"), list):
        raise RestoreError(f"{manifest} is not a {MANIFEST_FORMAT} manifest")
    return data


def default_repo(manifest: Path) -> Path:
    """The backup repo holding `manifest` (<repo>/rainbox_database/<bucket>/<file>)."""
    return manifest.resolve().parent.parent.parent


def assemble(
    manifest: str | os.PathLike[str],
    identity: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    repo: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Write every file the manifest names into the new directory `out_dir`
    and return their names. Raises RestoreError on a missing part, a digest or
    size mismatch, or a name/path that would leave `out_dir` or the repo."""
    manifest = Path(manifest)
    out_dir = Path(out_dir)
    repo = (Path(repo) if repo is not None else default_repo(manifest)).resolve()
    data = read_manifest(manifest, identity)
    if out_dir.exists():
        raise RestoreError(f"output directory {out_dir} already exists")

    age = resolve_tool("AGE", "age")
    zstd = resolve_tool("ZSTD", "zstd")
    out_dir.mkdir(parents=True, mode=0o700)
    names = []
    for entry in data["files"]:
        name, rel = entry.get("name"), entry.get("part")
        if not isinstance(name, str) or not SAFE_NAME.match(name) or not isinstance(rel, str):
            raise RestoreError(f"manifest entry {entry!r} is malformed")
        part = (repo / rel).resolve()
        if not part.is_relative_to(repo):
            raise RestoreError(f"part {rel} for {name} is outside the backup repo {repo}")
        if not part.is_file():
            raise RestoreError(f"missing part {rel} for {name}")
        dest = out_dir / name
        with open(dest, "wb") as out:
            run_pipeline([[age, "-d", "-i", str(identity), str(part)], [zstd, "-dc"]], out)
        h = hashlib.sha256()
        with open(dest, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != entry.get("sha256") or dest.stat().st_size != entry.get("bytes"):
            raise RestoreError(f"{name} from {rel} does not match the manifest")
        names.append(name)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m backup.restore",
        description="Reassemble a pg_dump directory from a rainbox backup manifest.",
    )
    parser.add_argument("manifest", help="the <ts>Z.manifest.age file")
    parser.add_argument("-i", "--identity", required=True,
                        help="the offline age identity (private key) file")
    parser.add_argument("-o", "--out", required=True,
                        help="directory to create for the reassembled dump")
    parser.add_argument("--repo", default=None,
                        help="backup repo holding the parts (default: the one holding the manifest)")
    args = parser.parse_args()
    try:
        names = assemble(args.manifest, args.identity, args.out, repo=args.repo)
    except RestoreError as exc:
        parser.exit(1, f"restore failed: {exc}\n")
    print(f"reassembled {len(names)} files into {args.out}")
    print(f"next: createdb <db> && pg_restore -d <db> {args.out}")


if __name__ == "__main__":
    sys.exit(main())
