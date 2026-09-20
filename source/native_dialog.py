"""The native "choose a folder" dialog, opened by the server process.

This is a local app: the server runs on the operator's own machine, so it
can open that machine's real folder dialog and hand the chosen absolute
path back to the page — which a browser's own picker withholds by design.

One backend per platform dialog tool, each a small `Backend` row: which
executable it needs, how to invoke it with a start folder, and how to read
the answer. The first available backend wins; `pick_folder` returns
`unsupported` when none is, which is the page's cue to fall back to its
in-page listing. Adding a platform or a desktop environment is one more
row, not another branch.

    macOS     osascript   AppleScript `choose folder` (the Finder panel)
    Windows   powershell  System.Windows.Forms.FolderBrowserDialog
    Linux     zenity      GNOME/GTK file chooser, `--directory`
              kdialog     KDE folder chooser
    any       python+tk   tkinter's askdirectory (native on macOS/Windows)

A start folder always travels as an argument or on stdin, never
interpolated into a script, so a path with quotes or backslashes cannot
alter what runs. Every backend is a subprocess with a long timeout: the
dialog stays up until the operator chooses or cancels, and the one request
thread waits with it while the server keeps answering others.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

PROMPT = "Select the folder that contains the repository"

# The dialog stays up until the operator acts; cap the wait anyway so a
# forgotten dialog cannot hold a request thread forever.
TIMEOUT_SECONDS = 600

_OSASCRIPT = """\
on run argv
    activate
    set startPath to item 1 of argv
    try
        set startFolder to POSIX file startPath as alias
    on error
        set startFolder to path to home folder
    end try
    set chosen to choose folder with prompt "%s" default location startFolder
    return POSIX path of chosen
end run
""" % PROMPT

_POWERSHELL = """\
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = '%s'
$d.SelectedPath = $args[0]
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }
""" % PROMPT

_TKINTER = """\
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
print(filedialog.askdirectory(initialdir=sys.argv[1], title=%r, mustexist=True))
""" % PROMPT


@dataclass(frozen=True)
class Backend:
    name: str
    platforms: tuple[str, ...]          # sys.platform prefixes; () = any
    executable: str                     # found with shutil.which
    argv: Callable[[str, str], list[str]]   # (exe, start) -> command line
    stdin: str = ""                     # script fed on stdin, if any
    cancel_markers: tuple[str, ...] = ()   # stderr text that means "cancelled"


def _tk_available(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", "import tkinter"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


BACKENDS: tuple[Backend, ...] = (
    Backend("osascript", ("darwin",), "osascript",
            lambda exe, start: [exe, "-", start], stdin=_OSASCRIPT,
            cancel_markers=("-128", "canceled")),
    Backend("powershell", ("win32",), "powershell",
            lambda exe, start: [exe, "-NoProfile", "-NonInteractive",
                                "-Command", _POWERSHELL, start]),
    Backend("zenity", ("linux",), "zenity",
            lambda exe, start: [exe, "--file-selection", "--directory",
                                f"--title={PROMPT}",
                                f"--filename={start.rstrip('/')}/"]),
    Backend("kdialog", ("linux",), "kdialog",
            lambda exe, start: [exe, "--getexistingdirectory", start,
                                "--title", PROMPT]),
    Backend("tkinter", (), sys.executable,
            lambda exe, start: [exe, "-c", _TKINTER, start]),
)


def available_backends(*, platform: str = sys.platform,
                       which: Callable[[str], str | None] = shutil.which,
                       tk_available: Callable[[str], bool] = _tk_available,
                       ) -> list[tuple[Backend, str]]:
    """The backends this host can run, in preference order, each with the
    executable path that satisfied it. The platform-native tool comes
    first; tkinter is the generic last resort and needs the Tk module to
    import, which a Python built without it does not."""
    found: list[tuple[Backend, str]] = []
    for backend in BACKENDS:
        if backend.platforms and not platform.startswith(backend.platforms):
            continue
        exe = which(backend.executable)
        if not exe:
            continue
        if backend.name == "tkinter" and not tk_available(exe):
            continue
        found.append((backend, exe))
    return found


def pick_folder(start: Any = None, *,
                runner: Callable[..., Any] = subprocess.run,
                platform: str = sys.platform,
                which: Callable[[str], str | None] = shutil.which,
                tk_available: Callable[[str], bool] = _tk_available,
                ) -> dict[str, Any]:
    """Open the native folder dialog and report the operator's choice.

    {ok, path, backend} on a choice; {ok: False, cancelled: True} when the
    dialog was dismissed; {ok: False, unsupported: True, error} when no
    backend can run here. A start folder that does not exist opens at home.
    The keyword seams are for tests; production calls with the defaults."""
    raw = start if isinstance(start, str) else ""
    start_dir = os.path.realpath(os.path.expanduser(raw.strip() or "~"))
    if not os.path.isdir(start_dir):
        start_dir = os.path.expanduser("~")
    candidates = available_backends(platform=platform, which=which,
                                    tk_available=tk_available)
    if not candidates:
        return {"ok": False, "unsupported": True,
                "error": "no native folder dialog is available on this host"}
    backend, exe = candidates[0]
    try:
        proc = runner(backend.argv(exe, start_dir),
                      input=backend.stdin or None,
                      capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True,
                "error": "the folder dialog timed out", "backend": backend.name}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"folder dialog failed: {exc}",
                "backend": backend.name}
    chosen = (proc.stdout or "").strip()
    if proc.returncode != 0 or not chosen:
        stderr = (proc.stderr or "").lower()
        if (not chosen and not stderr.strip()) or any(
                m.lower() in stderr for m in backend.cancel_markers):
            return {"ok": False, "cancelled": True, "backend": backend.name}
        return {"ok": False, "backend": backend.name,
                "error": (proc.stderr or "").strip() or "folder dialog failed"}
    return {"ok": True, "backend": backend.name,
            "path": os.path.realpath(chosen.rstrip("/") or "/")}
