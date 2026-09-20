"""Tests for webapp/git_views.py + static/git.js.

The /git page is frontend-only: the route renders the HTML shell (+ inline CSS)
and all interactivity lives in static/git.js. `_body()` returns the page
concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/git").get_data(as_text=True)
    js = client.get("/static/git.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_git_page_renders_with_nav():
    body = app.test_client().get("/git").get_data(as_text=True)
    assert 'class="git-split"' in body   # the git page layout
    assert "pp-nav" in body              # shared nav included
    assert "/static/git.js?v=" in body   # JS pulled in with a cache-buster


def test_nav_has_git_link():
    body = app.test_client().get("/git").get_data(as_text=True)
    assert ">Git<" in body
    assert "pp-active" in body


def test_js_has_core_markers():
    b = _body()
    for marker in ["gitLoadTree", "gitRenderTree", "gitRepoNode",
                   "gitAddRepoConfirm", "/git/api/repos",
                   "gitPickFolder", "/git/api/pick-folder",
                   "gitBrowseLoad", "/git/api/browse", "gitBrowseUse",
                   "gitFlushPendingSave", "gitLoadRepoDetail", "gitSavePush"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_tree_rows_are_real_links():
    # Folder and repo rows (and the static "All repositories" node) are
    # anchors with a real href so CMD/Ctrl click (and middle click) opens the
    # selection in a new tab via its ?id= deep link. Plain clicks are still
    # intercepted for in-page selection; modified clicks pass through.
    body = _body()
    assert "node.href = '/git?id=' + encodeURIComponent(f.id)" in body
    assert "n.href = '/git?id=' + encodeURIComponent(r.uuid)" in body
    assert '<a class="git-node" id="git-all" href="/git">' in body
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in body
    assert "text-decoration:none" in body


def test_tree_save_declares_no_deletes():
    """Per notes/ui-tree-persistence.md the tree PUT cannot delete, so the
    client must not carry a deletes counter — deletion goes to DELETE."""
    b = _body()
    assert "deletes" not in b
    assert "method: 'DELETE'" in b


def test_add_repo_modal_has_the_folder_picker_for_a_local_browser_only():
    """The native dialog opens on the server's display, so the button is
    rendered only for a browser on loopback (the test client is); the path
    field stays for everyone. No Name field: the node is named after the
    folder and renamed in place."""
    b = _body()
    assert 'id="git-browse-btn"' in b and 'onclick="gitPickFolder()"' in b
    assert 'id="git-browse-wait"' in b
    assert 'id="git-repo-path"' in b and 'git-repo-name' not in b
    # Cancel before Add, and Escape cancels this modal unconditionally.
    actions = b[b.index('id="git-repo-modal"'):]
    assert actions.index("gitCloseRepoModal()") < actions.index('id="git-repo-create"')
    js = _body()
    assert "if (!document.getElementById('git-repo-modal').hidden){ gitCloseRepoModal(); return; }" in js
    with app.test_client() as c:
        remote = c.get("/git", environ_base={"REMOTE_ADDR": "192.168.1.20"}).get_data(as_text=True)
    assert 'id="git-repo-path"' in remote
    assert 'git-browse-btn' not in remote
    for el in ("git-browse", "git-browse-path", "git-browse-list",
               "git-browse-up", "git-browse-use"):
        assert f'id="{el}"' in b, f"missing picker element {el}"
