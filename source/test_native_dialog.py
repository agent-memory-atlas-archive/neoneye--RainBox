"""native_dialog: the per-platform folder-dialog backends, chosen by host
and driven through a subprocess seam. Pure — no dialog is ever opened."""
from types import SimpleNamespace

import native_dialog as nd


def _runner(returncode=0, stdout="", stderr=""):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return run, calls


def _which(*names):
    """A fake shutil.which: the named tools exist under /bin; the running
    interpreter (tkinter's executable) is always found as itself."""
    def which(exe):
        if exe == nd.sys.executable:
            return exe
        return f"/bin/{exe}" if exe in names else None
    return which


def test_the_platform_native_backend_comes_first_and_tk_is_the_generic_fallback():
    order = [b.name for b, _ in nd.available_backends(
        platform="darwin", which=_which("osascript"), tk_available=lambda p: True)]
    assert order == ["osascript", "tkinter"]
    order = [b.name for b, _ in nd.available_backends(
        platform="linux", which=_which("kdialog"), tk_available=lambda p: True)]
    assert order == ["kdialog", "tkinter"]
    order = [b.name for b, _ in nd.available_backends(
        platform="win32", which=_which("powershell"), tk_available=lambda p: False)]
    assert order == ["powershell"]
    assert nd.available_backends(platform="linux", which=_which(),
                                 tk_available=lambda p: False) == []


def test_each_backend_gets_the_start_folder_as_an_argument_never_in_its_script(tmp_path):
    for platform, tool in (("darwin", "osascript"), ("win32", "powershell"),
                           ("linux", "zenity"), ("linux", "kdialog")):
        run, calls = _runner(0, stdout=str(tmp_path) + "\n")
        res = nd.pick_folder(str(tmp_path), runner=run, platform=platform,
                             which=_which(tool), tk_available=lambda p: False)
        assert res["ok"] and res["backend"] == tool and res["path"] == str(tmp_path.resolve())
        argv, kw = calls[0]
        assert argv[0] == f"/bin/{tool}"
        assert any(str(tmp_path.resolve()) in a for a in argv)
        assert str(tmp_path) not in (kw.get("input") or "")
    run, calls = _runner(0, stdout=str(tmp_path) + "\n")
    res = nd.pick_folder(str(tmp_path), runner=run, platform="linux",
                         which=_which(), tk_available=lambda p: True)
    assert res["ok"] and res["backend"] == "tkinter"
    assert calls[0][0][:2] == [nd.sys.executable, "-c"]


def test_cancel_is_not_an_error_and_a_missing_start_opens_at_home(tmp_path):
    run, _ = _runner(1, stderr="execution error: User canceled. (-128)")
    assert nd.pick_folder(None, runner=run, platform="darwin", which=_which("osascript"),
                          tk_available=lambda p: False) == {
        "ok": False, "cancelled": True, "backend": "osascript"}
    run, _ = _runner(1, stdout="", stderr="")          # zenity's cancel: exit 1, silent
    assert nd.pick_folder(None, runner=run, platform="linux", which=_which("zenity"),
                          tk_available=lambda p: False)["cancelled"] is True
    run, _ = _runner(0, stdout="\n")                   # tkinter's cancel: empty print
    assert nd.pick_folder(None, runner=run, platform="linux", which=_which(),
                          tk_available=lambda p: True)["cancelled"] is True
    run, calls = _runner(0, stdout="/tmp\n")
    nd.pick_folder(str(tmp_path / "missing"), runner=run, platform="darwin",
                   which=_which("osascript"), tk_available=lambda p: False)
    assert calls[0][0][2] == nd.os.path.expanduser("~")
    assert nd.pick_folder(None, runner=run, platform="linux", which=_which(),
                          tk_available=lambda p: False)["unsupported"] is True
