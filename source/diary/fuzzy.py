"""Spelling tolerance for diary search: joined words and close spellings.

Two gaps a literal or lexeme match cannot close:

- **Joined words.** "ruby forge" is two lexemes; the diary wrote one,
  "rubyforge". Adjacent query words are also tried glued together
  (`joined_lexemes`), in the ordinary FTS route. Needs nothing extra.
- **Typos.** "rubyfroge" shares no lexeme with "rubyforge". Each source
  keeps a vocabulary of its current words (`diary_word`, rebuilt by sync);
  a query word that is not in it is matched to vocabulary words by pg_trgm
  similarity, and those words feed a ranked "fuzzy" route.

Literal mode stays exact. When it finds nothing, it offers close spellings
that do occur — each verified against the caller's eligible passages, so a
suggestion never reveals a word that only an excluded file contains.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import sqlalchemy as sa

import db

SIMILARITY = 0.4          # rubyfroge->rubyforge 0.43; ruby->rubyforge 0.36 stays out
MIN_WORD = 4
CANDIDATES_PER_WORD = 3
MAX_SUGGESTIONS = 5
_WORD = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)


def trgm_available() -> bool:
    try:
        row = db.session.execute(sa.text(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).first()
        return row is not None
    except Exception:   # noqa: BLE001
        db.session.rollback()
        return False


def query_words(query: str) -> list[str]:
    """The query's words, casefolded, in order (the `simple` parser's view
    of plain words; punctuation-only tokens dropped)."""
    return [w.casefold() for w in _WORD.findall(query)]


def joined_lexemes(query: str) -> list[str]:
    """Adjacent words glued together: "ruby forge" -> ["rubyforge"];
    "docker compose yaml" -> ["dockercompose", "composeyaml"]. Hyphens and
    underscores inside a word are glued too ("ruby-forge" -> "rubyforge")."""
    words = query_words(query)
    out: list[str] = []
    for a, b in zip(words, words[1:]):
        out.append(a + b)
    for w in words:
        glued = re.sub(r"[-_]", "", w)
        if glued != w:
            out.append(glued)
    seen: list[str] = []
    for w in out:
        if w not in seen:
            seen.append(w)
    return seen


def rebuild_vocabulary(source_uuid: UUID) -> int:
    """Replace the source's vocabulary with the lexemes of its current,
    ready, non-excluded passages. Called at the end of every sync."""
    db.session.execute(sa.text("DELETE FROM diary_word WHERE source_uuid = :s"), {"s": source_uuid})
    inner = (
        "SELECT p.search_vector FROM diary_passage p "
        "JOIN diary_entry e ON e.uuid = p.entry_uuid "
        "JOIN diary_file f ON f.current_generation_uuid = e.generation_uuid "
        f"WHERE f.source_uuid = '{UUID(str(source_uuid))}' AND f.availability = 'ready' "
        "AND NOT EXISTS (SELECT 1 FROM diary_exclusion x WHERE x.source_uuid = f.source_uuid "
        "AND x.relative_path = f.relative_path)")
    n = db.session.execute(sa.text(
        "INSERT INTO diary_word (source_uuid, word, passages) "
        "SELECT :s, word, ndoc FROM ts_stat(:inner)"),
        {"s": source_uuid, "inner": inner}).rowcount
    db.session.commit()
    return n or 0


def _known(words: list[str], source_ids: list[UUID]) -> set[str]:
    if not words:
        return set()
    rows = db.session.execute(sa.text(
        "SELECT DISTINCT word FROM diary_word WHERE source_uuid = ANY(:s) AND word = ANY(:w)"),
        {"s": source_ids, "w": words}).all()
    return {r[0] for r in rows}


def close_words(query: str, source_ids: list[UUID]) -> list[str]:
    """Vocabulary words for query words the diary does not contain: joined
    variants that exist, then spellings with pg_trgm similarity >=
    SIMILARITY. Empty when pg_trgm is not installed."""
    all_words = query_words(query)
    words = [w for w in all_words if len(w) >= MIN_WORD]
    joined = joined_lexemes(query)
    known = _known(words + joined, source_ids)
    out = [j for j in joined if j in known]
    # The parts of a joined word the diary contains are not typos of anything.
    covered = {w for a, b in zip(all_words, all_words[1:]) if a + b in known for w in (a, b)}
    unknown = [w for w in words if w not in known and w not in covered]
    if unknown and trgm_available():
        for w in unknown:
            rows = db.session.execute(sa.text(
                "SELECT word FROM diary_word WHERE source_uuid = ANY(:s) AND word % :w "
                "AND length(word) >= :m AND similarity(word, :w) >= :t GROUP BY word "
                "ORDER BY max(similarity(word, :w)) DESC, word LIMIT :n"),
                {"s": source_ids, "w": w, "m": MIN_WORD, "t": SIMILARITY,
                 "n": CANDIDATES_PER_WORD}).all()
            out += [r[0] for r in rows if r[0] not in out]
    return out


def literal_suggestions(query: str, sources: list[Any], eligible_sql: str,
                        params: dict[str, Any]) -> list[str]:
    """Close spellings for a literal query that found nothing: the query
    with spaces/hyphens removed, and similar vocabulary words. Each is kept
    only if an eligible passage actually contains it."""
    source_ids = [s.uuid for s in sources]
    candidates: list[str] = []
    compact = re.sub(r"[\s\-_]+", "", query)
    if compact != query and len(compact) >= MIN_WORD:
        candidates.append(compact)
    for w in close_words(query, source_ids):
        if w not in candidates and w != query.casefold():
            candidates.append(w)
    out: list[str] = []
    for c in candidates:
        # The spelling as the diary writes it (literal mode is case-sensitive,
        # so "rubyforge" must come back as "RubyForge" if that is the text).
        pattern = re.escape(c)
        row = db.session.execute(sa.text(
            "SELECT substring(p.text FROM :re) " + eligible_sql + " AND p.text ~* :bare LIMIT 1"),
            {**params, "re": "(?i)(" + pattern + ")", "bare": pattern}).first()
        if row is not None and row[0] and row[0] not in out:
            out.append(row[0])
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out
