"""Shell-less subprocess pipelines for the backup tools (pg_dump, zstd, age).

Shared by backup.dump, backup.store and backup.restore; no app or database
imports, so a restore can run on a machine without the app configured.
"""
import os
import shutil
import subprocess


def resolve_tool(env_var: str, name: str) -> str:
    """Locate an external tool, preferring an explicit env override."""
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            f"{name!r} not found on PATH; set {env_var} to its full path"
        )
    return found


def run_pipeline(stages: list[list[str]], out) -> None:
    """Run `stages` as a shell-less pipeline (stage N's stdout → stage N+1's
    stdin), the last writing to the open file `out`. Raises RuntimeError naming
    the first failing stage and its stderr."""
    procs: list[subprocess.Popen] = []
    for i, argv in enumerate(stages):
        is_last = i == len(stages) - 1
        p = subprocess.Popen(
            argv,
            stdin=procs[-1].stdout if procs else None,
            stdout=out if is_last else subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if procs:
            # Close the parent's copy of the upstream pipe so the upstream stage
            # gets SIGPIPE if a downstream stage dies early.
            assert procs[-1].stdout is not None
            procs[-1].stdout.close()
        procs.append(p)

    # communicate() downstream-first; each call collects that stage's stderr and
    # waits for it to exit.
    errs = {id(p): p.communicate()[1] for p in reversed(procs)}
    for argv, p in zip(stages, procs):
        if p.returncode != 0:
            err = errs[id(p)].decode(errors="replace").strip()
            raise RuntimeError(f"{argv[0]} failed (exit {p.returncode}): {err}")
