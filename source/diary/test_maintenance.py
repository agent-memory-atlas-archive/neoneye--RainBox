"""The nightly diary refresh (diary.maintenance): sync every enabled source,
then embed its new passages under the embedding spec it already records.
Sandbox DB, synthetic fixture tree, fake embedder — no Ollama."""

import sqlalchemy as sa

import db
from diary import embeddings as emb
from diary.ingest import SyncReport
from diary.maintenance import refresh_enabled_sources
from diary.test_embeddings import FakeEmbedder, spec
from db.test_diary import app_ctx, manifest_for, source  # noqa: F401  (fixtures)


def _vectors(src):
    return db.session.execute(sa.text(
        "SELECT count(*) FROM diary_embedding WHERE source_uuid = :s"), {"s": src.uuid}).scalar()


def _enabled_with_vectors(src):
    from diary.ingest import sync_source
    sync_source(src.uuid)
    emb.embed_source(src.uuid, FakeEmbedder(), spec())
    emb.set_vector_mode(src.uuid, "exact")
    db.diary_set_enabled(src.uuid, True)
    db.session.expire_all()
    return db.session.get(db.DiarySource, src.uuid)


def test_refresh_syncs_and_embeds_new_text(source):
    src, root, _room = source
    src = _enabled_with_vectors(src)
    before = _vectors(src)
    (root / "current" / "1998.txt").write_text("19980105\n09h00\na brand new harbour note\n")
    [out] = refresh_enabled_sources([src], embedder_for=lambda _spec: FakeEmbedder())
    assert out.status == "ok" and out.problems == []
    assert out.sync.published == 1
    assert out.embed is not None and out.embed.embedded >= 1
    assert _vectors(src) > before
    assert "1 published" in out.summary() and "embedded" in out.summary()


def test_refresh_skips_disabled_sources(source):
    src, _root, _room = source
    assert src.enabled is False
    assert refresh_enabled_sources([src], embedder_for=lambda _spec: FakeEmbedder()) == []


def test_a_changed_embedder_model_is_reported_not_re_embedded(source):
    """A new digest would switch vector search off and re-embed every passage;
    that is the operator's call, not a nightly side effect."""
    src, root, _room = source
    src = _enabled_with_vectors(src)
    before = _vectors(src)
    (root / "current" / "1998.txt").write_text("19980105\n09h00\nanother note\n")
    [out] = refresh_enabled_sources([src], embedder_for=lambda _s: FakeEmbedder(digest="newdigest0000000"))
    assert out.status == "problem" and "embedder model changed" in out.problems[0]
    assert out.sync.published == 1 and out.embed is None
    db.session.expire_all()
    assert db.session.get(db.DiarySource, src.uuid).vector_mode == "exact"
    assert _vectors(src) == before


def test_a_source_without_vectors_is_synced_only(source):
    src, _root, _room = source
    db.diary_set_enabled(src.uuid, True)
    db.session.expire_all()
    src = db.session.get(db.DiarySource, src.uuid)
    called = []
    [out] = refresh_enabled_sources([src], embedder_for=lambda s: called.append(s) or FakeEmbedder())
    assert out.status == "ok" and out.embed is None and called == []
    assert out.sync.published > 0


def test_a_busy_source_is_skipped_until_next_time(source, monkeypatch):
    src, _root, _room = source
    db.diary_set_enabled(src.uuid, True)
    db.session.expire_all()
    src = db.session.get(db.DiarySource, src.uuid)
    import diary.maintenance as m
    monkeypatch.setattr(m, "sync_source", lambda uuid: SyncReport(uuid, busy=True))
    [out] = refresh_enabled_sources([src], embedder_for=lambda _s: FakeEmbedder())
    assert out.status == "busy" and out.embed is None and out.problems == []


def test_sync_failures_are_problems(source, monkeypatch):
    src, _root, _room = source
    db.diary_set_enabled(src.uuid, True)
    db.session.expire_all()
    src = db.session.get(db.DiarySource, src.uuid)
    import diary.maintenance as m
    monkeypatch.setattr(m, "sync_source", lambda uuid: SyncReport(
        uuid, quarantined=1, failures=[{"path": "current/1999.txt", "code": "read_failed"}]))
    [out] = refresh_enabled_sources([src], embedder_for=lambda _s: FakeEmbedder())
    assert out.status == "problem"
    assert any("quarantined" in p for p in out.problems)
    assert any("failed" in p for p in out.problems)
