"""Process names: every RainBox process runs under an executable named for
its role, so Activity Monitor, `ps` and `top` show "RainBox Launcher",
"RainBox Core", "RainBox Discord Main Bot" or "RainBox Agent direct_chat"
instead of a column of "Python".

Activity Monitor names a process after the file it executed, and nothing a
running process does later can change that (rewriting argv only changes what
`ps` prints). So the interpreter is hard-linked under the wanted name inside
the venv — `<venv>/procnames/RainBox <Title>` — and the process is exec'd
from that link. Python still finds the venv (`pyvenv.cfg` one directory up),
its packages, and the shared framework; `sys.executable` becomes the link, so
a child spawned from it inherits the name unless it asks for one of its own.

macOS framework builds need one extra step: `venv/bin/python` resolves to
`bin/python3.X`, a stub that re-execs `Python.app/Contents/MacOS/Python`,
and a link to the stub would still end up as "Python". The binary that
actually runs is therefore asked for once per interpreter (`_real_binary`:
`proc_pidpath` on macOS, `/proc/self/exe` on Linux) and that is what gets
linked. A link whose inode no longer matches (the interpreter was upgraded)
is remade on the next spawn.

Everything here is best effort: when a step fails — no venv around the
interpreter, a probe that does not answer, a volume that refuses hard links
and a copy that fails too — the plain interpreter is returned and one warning
logged, and the process simply keeps the old name. `RAINBOX_PROCNAME=0`
turns the whole mechanism off.

Stdlib only, on purpose: the launcher imports this and must stay small.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "RainBox "
DIRNAME = "procnames"
DISABLE_ENV = "RAINBOX_PROCNAME"

#: Finds the file the current process is executing (not `sys.executable`,
#: which a venv or the macOS stub may have rewritten). Run in-process for our
#: own interpreter and as `python -c` for another venv's; `result` holds it.
_PROBE_BODY = """
import os, sys
result = None
if sys.platform == "darwin":
    import ctypes
    buf = ctypes.create_string_buffer(4096)
    if ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidpath(os.getpid(), buf, 4096) > 0:
        result = buf.value.decode()
elif os.path.exists("/proc/self/exe"):
    result = os.readlink("/proc/self/exe")
result = result or os.path.realpath(sys.executable)
"""

_real_cache: dict[Path, Path | None] = {}
_warned: set[str] = set()


def enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    return (env if env is not None else os.environ).get(DISABLE_ENV, "1") != "0"


def clean_title(title: str) -> str:
    """One line of printable text without path separators, whitespace
    collapsed — the part after the prefix."""
    return re.sub(r"[/\\\x00-\x1f\x7f]", "-", " ".join(title.split()))


def process_name(title: str) -> str:
    return PREFIX + clean_title(title)


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _self_binary() -> Path:
    ns: dict[str, object] = {}
    exec(_PROBE_BODY, ns)
    return Path(str(ns["result"]))


def _venv_root(python: Path) -> Path | None:
    """The venv `python` belongs to: `pyvenv.cfg` beside it or one up (which
    is also where a link under `<venv>/procnames/` finds it)."""
    for directory in (python.parent, python.parent.parent):
        if (directory / "pyvenv.cfg").is_file():
            return directory
    return None


def _real_binary(python: Path) -> Path | None:
    """The file that runs when `python` is executed, after every stub and
    symlink. Cached per interpreter path for the life of this process."""
    if python in _real_cache:
        return _real_cache[python]
    real: Path | None = None
    try:
        if python == Path(sys.executable) or python.resolve() == Path(sys.executable).resolve():
            real = _self_binary()
        else:
            out = subprocess.run(
                [str(python), "-I", "-c", _PROBE_BODY + "print(result)"],
                capture_output=True, text=True, timeout=60, check=True,
            )
            real = Path(out.stdout.strip())
        if not real.is_file():
            real = None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn_once(f"probe:{python}", "process names: could not identify the binary behind %s (%s)", python, exc)
        real = None
    _real_cache[python] = real
    return real


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    if a.st_dev == b.st_dev and a.st_ino == b.st_ino:
        return True
    # A copy (hard links refused): same bytes, same mtime (copy2 keeps it).
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def named_interpreter(python: str | Path, title: str) -> str:
    """The path to exec so the process shows as `RainBox <title>`: a hard
    link (or copy) of the binary behind `python`, under the venv's
    `procnames/` directory. Returns `python` unchanged — never raises — when
    the mechanism is off or cannot be arranged, so callers spawn either way.
    Creating the link happens once; later calls are a couple of stats."""
    python_path = Path(python)
    if not enabled() or not python_path.exists():
        return str(python)
    try:
        venv = _venv_root(python_path)
        if venv is None:
            return str(python)
        real = _real_binary(python_path)
        if real is None:
            return str(python)
        target = venv / DIRNAME / process_name(title)
        real_stat = os.stat(real)
        try:
            if _same_file(os.lstat(target), real_stat):
                return str(target)
            target.unlink()
        except FileNotFoundError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(real, target)
        except OSError:
            shutil.copy2(real, target)
        return str(target)
    except (OSError, ValueError) as exc:
        _warn_once(f"link:{python}:{title}", "process names: cannot name %s as %r (%s); keeping the plain interpreter",
                   python, process_name(title), exc)
        return str(python)


def reexec_as(title: str) -> None:
    """Restart the current script under `RainBox <title>` unless it already
    runs as that. For `python main.py`-style entrypoints only (the script
    path and its arguments are replayed from `sys.argv`; `-c`/`-m` starts
    are left alone). Returns without doing anything when the name cannot be
    arranged; never returns when it succeeds."""
    if Path(sys.executable).name == process_name(title):
        return
    if not sys.argv or sys.argv[0] in ("", "-c", "-m"):
        return
    exe = named_interpreter(sys.executable, title)
    if exe == sys.executable:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(exe, [exe, *sys.argv])
    except OSError as exc:
        _warn_once(f"reexec:{title}", "process names: re-exec as %r failed (%s); continuing unnamed",
                   process_name(title), exc)
