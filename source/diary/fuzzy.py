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
# A misspelling in the diary is a near one-off: a variant of a word the diary
# contains counts only if it is within typo distance (edit_distance) and occurs
# in at most VARIANT_FLOOR passages, or VARIANT_SHARE of the word's passages
# when the word is common.
VARIANT_SHARE = 0.1
VARIANT_FLOOR = 2
VARIANT_PREFILTER = 0.3   # pg_trgm similarity to fetch candidates; distance decides
VARIANT_MIN_WORD = 5      # "have" -> "haven" is noise; "rubyforge" -> "rubyfroge" is not
VARIANT_MAX_WORDS = 3     # keyword searches; a long question is the vector route's job
MIN_WORD = 4
CANDIDATES_PER_WORD = 3
MAX_SUGGESTIONS = 5
_WORD = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)


def length_slack(word: str) -> int:
    """A typo changes a word's length by a character or two, not more:
    "rubyforg"/"rubyforge", "paralel"/"parallel" pass; "evening"/"even" and
    "relax"/"related" do not."""
    return 1 if len(word) < 8 else 2


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


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: insertions, deletions,
    substitutions and adjacent transpositions each cost one ("rubyforge" /
    "rubyfroge" is 1)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def typo_distance(word: str) -> int:
    return 1 if len(word) < 8 else 2


def rare_variants(word: str, source_ids: list[UUID]) -> list[str]:
    """The diary's own misspellings of a word it also spells right: within
    typo distance (one edit, two for words of eight or more letters) and a
    near one-off ("rubyforge" in five passages, "rubyfroge" in one). A
    neighbor that is a word of its own ("remove" / "remote", "operation" /
    "operator") is too far or too frequent and is left out. Empty without
    pg_trgm."""
    if len(word) < MIN_WORD or not trgm_available():
        return []
    rows = db.session.execute(sa.text(
        "WITH base AS (SELECT coalesce(sum(passages), 0) AS n FROM diary_word "
        "  WHERE source_uuid = ANY(:s) AND word = :w) "
        "SELECT v.word, similarity(v.word, :w) AS sim FROM diary_word v, base "
        "WHERE v.source_uuid = ANY(:s) AND v.word <> :w AND length(v.word) >= :m "
        "AND abs(length(v.word) - length(:w)) <= :d AND similarity(v.word, :w) >= :t "
        "GROUP BY v.word, base.n HAVING sum(v.passages) <= greatest(:floor, :share * base.n) "
        "ORDER BY sim DESC, v.word LIMIT 20"),
        {"s": source_ids, "w": word, "m": MIN_WORD, "d": typo_distance(word),
         "t": VARIANT_PREFILTER, "floor": VARIANT_FLOOR, "share": VARIANT_SHARE}).all()
    limit = typo_distance(word)
    return [r[0] for r in rows if edit_distance(word, r[0]) <= limit][:CANDIDATES_PER_WORD]


def _similar(w: str, source_ids: list[UUID]) -> list[str]:
    rows = db.session.execute(sa.text(
        "SELECT word FROM diary_word WHERE source_uuid = ANY(:s) AND word % :w "
        "AND length(word) >= :m AND abs(length(word) - length(:w)) <= :d "
        "AND similarity(word, :w) >= :t GROUP BY word "
        "ORDER BY max(similarity(word, :w)) DESC, word LIMIT :n"),
        {"s": source_ids, "w": w, "m": MIN_WORD, "d": length_slack(w),
         "t": SIMILARITY, "n": CANDIDATES_PER_WORD}).all()
    return [r[0] for r in rows]


def close_words(query: str, source_ids: list[UUID]) -> list[str]:
    """Vocabulary words for query words the diary does not contain (typos in
    the query; see variant_words for typos in the diary): joined
    variants that exist; then close spellings (pg_trgm similarity >=
    SIMILARITY, similar length) of joined variants, so a typo in a split
    word still finds it ("rby forge" -> "rubyforge"); then close spellings of
    single words. Parts of a joined word that matched, exactly or closely,
    are not fuzzed on their own. Empty of spellings when pg_trgm is not
    installed."""
    all_words = query_words(query)
    words = [w for w in all_words if len(w) >= MIN_WORD]
    pairs = [(a, b, a + b) for a, b in zip(all_words, all_words[1:])]
    joined = joined_lexemes(query)
    known = _known(all_words + joined, source_ids)
    out = [j for j in joined if j in known]
    covered = {w for a, b, ab in pairs if ab in known for w in (a, b)}
    if not trgm_available():
        return out
    for a, b, ab in pairs:
        # Only a pair with a part the diary lacks can be a split-word typo
        # ("rby forge"); two known words ("did i", "do in") are just words.
        if ab in known or len(ab) < MIN_WORD or (a in known and b in known):
            continue
        found = [w for w in _similar(ab, source_ids) if w not in out]
        if found:
            out += found
            covered |= {a, b}
    for w in words:
        if w in known or w in covered:
            continue
        out += [x for x in _similar(w, source_ids) if x not in out]
    return out


def variant_words(query: str, source_ids: list[UUID]) -> list[str]:
    """The diary's own misspellings (and rare inflections) of query words it
    also spells right, including joined forms it contains. Only for keyword
    searches (at most VARIANT_MAX_WORDS words of VARIANT_MIN_WORD+ letters):
    in a long question every word would drag in neighbors and bury the
    answer. Uncertain by nature — "wasting" is one edit from "waiting" and
    occurs once — so the search gives them half weight: they surface after
    exact hits, not among them."""
    words = [w for w in query_words(query) if len(w) >= VARIANT_MIN_WORD]
    if not words or len(words) > VARIANT_MAX_WORDS:
        return []
    joined = joined_lexemes(query)
    known = _known(words + joined, source_ids)
    out: list[str] = []
    for w in [w for w in words + joined if w in known]:
        out += [x for x in rare_variants(w, source_ids) if x not in out and x not in known]
    return out


def _as_written(candidates: list[str], eligible_sql: str, params: dict[str, Any],
                exclude: str | None = None) -> list[str]:
    """Each candidate in the diary's own spelling, if an eligible passage
    contains it (case-insensitively)."""
    out: list[str] = []
    for c in candidates:
        pattern = re.escape(c)
        row = db.session.execute(sa.text(
            "SELECT substring(p.text FROM :re) " + eligible_sql + " AND p.text ~* :bare LIMIT 1"),
            {**params, "re": "(?i)(" + pattern + ")", "bare": pattern}).first()
        if row is not None and row[0] and row[0] not in out and row[0] != exclude:
            out.append(row[0])
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def literal_variants(query: str, sources: list[Any], eligible_sql: str,
                     params: dict[str, Any]) -> list[str]:
    """For a literal query that did match: rarer spellings of it the diary
    also uses (its typos), as written, so the caller can look them up too.
    Only for a single-word query; a phrase has no one spelling to vary."""
    words = query_words(query)
    if len(words) != 1:
        return []
    variants = rare_variants(words[0], [s.uuid for s in sources])
    return _as_written(variants, eligible_sql, params, exclude=query)


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
    # The spelling as the diary writes it (literal mode is case-sensitive,
    # so "rubyforge" must come back as "RubyForge" if that is the text).
    return _as_written(candidates, eligible_sql, params)
