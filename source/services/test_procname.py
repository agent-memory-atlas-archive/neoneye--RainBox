"""services/procname.py: a named link runs the same venv under the wanted
process name; every failure path returns the plain interpreter."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services import procname as pn

SELF = Path(sys.executable)
VENV: Path = pn._venv_root(SELF) or Path("/nonexistent")
pytestmark = pytest.mark.skipif(not (VENV / "pyvenv.cfg").exists(), reason="tests must run from a venv interpreter")


@pytest.fixture
def probe_link():
    """A fresh name for this test, removed afterwards."""
    title = f"Probe {os.getpid()}"
    yield title
    target = VENV / pn.DIRNAME / pn.process_name(title)
    if target.exists():
        target.unlink()


def test_named_link_runs_the_same_venv_under_the_new_name(probe_link):
    exe = pn.named_interpreter(sys.executable, probe_link)
    assert exe != sys.executable
    assert Path(exe).name == pn.process_name(probe_link)
    assert Path(exe).parent == VENV / pn.DIRNAME
    code = ("import os, subprocess, sys; print(sys.prefix); print(sys.executable); "
            "print(subprocess.run(['ps', '-c', '-o', 'comm=', '-p', str(os.getpid())], "
            "capture_output=True, text=True).stdout.strip() if sys.platform == 'darwin' "
            "else open('/proc/self/comm').read().strip())")
    out = subprocess.run([exe, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    prefix, executable, comm = out.stdout.splitlines()
    assert prefix == sys.prefix                       # same venv, same packages
    assert executable == exe                          # children inherit the name
    assert comm == (pn.process_name(probe_link) if sys.platform == "darwin"
                    else pn.process_name(probe_link)[:15])   # /proc comm is 15 chars


def test_second_call_reuses_the_link_without_probing_again(probe_link, monkeypatch):
    first = pn.named_interpreter(sys.executable, probe_link)
    ino = os.stat(first).st_ino
    monkeypatch.setattr(pn, "_self_binary", lambda: pytest.fail("probed again"))
    monkeypatch.setattr(pn.subprocess, "run", lambda *a, **k: pytest.fail("probed again"))
    assert pn.named_interpreter(sys.executable, probe_link) == first
    assert os.stat(first).st_ino == ino


def test_a_stale_file_under_the_name_is_replaced(probe_link):
    target = VENV / pn.DIRNAME / pn.process_name(probe_link)
    target.parent.mkdir(exist_ok=True)
    target.write_text("not an interpreter")
    exe = pn.named_interpreter(sys.executable, probe_link)
    assert Path(exe) == target
    assert os.stat(target).st_ino == os.stat(pn._self_binary()).st_ino


def test_no_venv_or_missing_interpreter_or_disabled_keeps_the_plain_path(tmp_path, monkeypatch):
    loose = tmp_path / "bin" / "python"
    loose.parent.mkdir()
    loose.symlink_to(sys.executable)
    assert pn.named_interpreter(loose, "Loose") == str(loose)        # no pyvenv.cfg around it
    assert not (tmp_path / pn.DIRNAME).exists()
    missing = tmp_path / "gone" / "venv" / "bin" / "python"
    assert pn.named_interpreter(missing, "Gone") == str(missing)     # nothing to probe
    monkeypatch.setenv(pn.DISABLE_ENV, "0")
    assert pn.named_interpreter(sys.executable, "Off") == sys.executable


def test_titles_are_cleaned_into_one_safe_filename():
    assert pn.process_name("Discord  Main\nBot") == "RainBox Discord Main Bot"
    assert pn.process_name("a/b\\c") == "RainBox a-b-c"
    assert pn.clean_title("  Core  ") == "Core"


def test_reexec_as_restarts_a_script_under_the_name(tmp_path, probe_link):
    script = tmp_path / "entry.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from services.procname import reexec_as\n"
        f"reexec_as({probe_link!r})\n"
        "print(sys.executable); print(sys.argv[2])\n"
    )
    out = subprocess.run([sys.executable, str(script), str(Path(__file__).resolve().parent.parent), "kept-arg"],
                         capture_output=True, text=True, check=True, timeout=60)
    exe_line, arg_line = out.stdout.splitlines()
    assert Path(exe_line).name == pn.process_name(probe_link)
    assert arg_line == "kept-arg"                                    # argv replayed intact
