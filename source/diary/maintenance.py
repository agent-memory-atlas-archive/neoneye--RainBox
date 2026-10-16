"""The nightly diary refresh, run by the `diary_sync` cron action.

For every enabled source: sync (snapshots, parses, identifiers, FTS), then
embed the passages that have no vector yet, under the embedding spec the
source already records. Nothing here changes a source's configuration: a
manifest or parser-version change still goes through `register`, and an
embedder whose served model no longer matches the recorded digest is
reported rather than re-embedded — a new epoch switches vector search off
and re-embeds every passage, which is the operator's call, not a side effect
of the night.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import db
from diary.embeddings import EmbedReport, Embedder, OllamaEmbedder, embed_source
from diary.ingest import SyncReport, sync_source


@dataclass
class SourceRefresh:
    name: str
    status: str                       # ok | busy | problem
    sync: SyncReport
    embed: EmbedReport | None = None
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One line for the cron room: what changed, and anything wrong."""
        if self.status == "busy":
            return f"{self.name}: busy (a sync is already running), skipped"
        s = self.sync
        parts = [f"{s.published} published", f"{s.unchanged} unchanged"]
        if s.missing:
            parts.append(f"{s.missing} missing")
        line = f"{self.name}: " + ", ".join(parts)
        if self.embed is not None:
            line += f"; {self.embed.embedded} embedded"
        if self.problems:
            line += " — " + "; ".join(self.problems)
        return line


def _default_embedder(spec: dict[str, Any]) -> Embedder:
    return OllamaEmbedder(spec["base_url"], spec["model"])


def refresh_enabled_sources(
    sources: list[Any] | None = None, *,
    embedder_for: Callable[[dict[str, Any]], Embedder] = _default_embedder,
) -> list[SourceRefresh]:
    """Refresh each enabled source in `sources` (default: every source)."""
    if sources is None:
        sources = db.diary_list_sources()
    out: list[SourceRefresh] = []
    for source in sources:
        if not source.enabled:
            continue
        report = sync_source(source.uuid)
        if report.busy:
            out.append(SourceRefresh(source.name, "busy", report))
            continue
        problems: list[str] = []
        if report.aborted:
            problems.append(f"sync aborted ({report.aborted})")
        if report.scan_failed:
            problems.append("scan failed")
        if report.quarantined:
            problems.append(f"{report.quarantined} quarantined")
        if report.failures:
            problems.append(f"{len(report.failures)} files failed")
        embedded = None
        spec = source.embedding_spec
        if spec and not report.aborted:
            try:
                embedder = embedder_for(spec)
                if embedder.digest() != spec["digest"]:
                    problems.append("embedder model changed; run `embed` by hand to start "
                                    "a new epoch")
                else:
                    embedded = embed_source(source.uuid, embedder, spec)
                    if embedded.failed_batches or embedded.failures:
                        problems.append(f"{embedded.failed_batches} embedding batches failed")
            except Exception as exc:   # noqa: BLE001 — an embedder outage leaves the sync standing
                db.session.rollback()
                problems.append(f"embedding unavailable: {exc}")
        out.append(SourceRefresh(source.name, "problem" if problems else "ok", report,
                                 embedded, problems))
    return out
