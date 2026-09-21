# Representing a Long-Running Diary for AI Memory

**Status:** Proposal. Nothing here is built.
**Date:** 2026-09-21 (revision 2, same day)
**Relates to:** `memory-architecture.md`, `qa-system.md`,
`2026-08-17-recall-filter-and-retrieval-granularity.md`,
`2026-06-30-memory-design-patterns.md` §diary ingestion, `eval-loop.md`.

**Revision 2 — what changed.** Revision 1 surveyed three architectures as if
RainBox had no memory system. It does: `memory_claim` already is the
temporal fact table revision 1 proposed (with `supersedes_uuid`,
`conflicts_with_uuid`, `expires_at`, `epistemic_confidence`,
`support_count`), `memory_evidence` is its provenance, the rejected-value
tombstones stop laundering, the actor/trust model decides what a model may
assert, and `retrieve_memories_hybrid` plus the recall filter is the
hybrid retrieval it recommended. This revision maps every idea onto that,
keeps only what is genuinely new, decides the open forks instead of
listing them, and adds the two constraints revision 1 ignored: extraction
runs on small local models with a token budget, and model-phrased text is
a candidate, never a belief. It also takes one position revision 1 did
not: for a diary, retrieval should quote the operator's own passage, and
the extracted structure is only the index that finds it.

---

## The diary

A folder of dated Markdown files the operator appends to, in more than one
language, mixing personal observations, project notes, debugging sessions,
commands given to agents, feedback received from other people, ideas,
tasks, and exact technical strings (URLs, commit hashes, symbols, error
messages). It grows for years. It is the operator's own words.

Two questions it must answer, which are different retrieval problems and
must not share one representation:

- "Find the exact error message I recorded."
- "Why did I change this design three months later?"

All examples in this document are synthetic.

## Principles

1. **The diary is immutable source.** Ingestion never edits it. Every
   derived row points back at a byte range of a file.
2. **Derived layers are rebuildable.** Extraction is versioned; a better
   local model later means re-running a pass, not migrating data.
3. **Quote, don't paraphrase.** What reaches a prompt from the diary is the
   operator's passage, fenced as recalled data. Extracted events, entities
   and summaries decide *which* passage; they are not what the model reads
   as fact. This is the diary's answer to the trust model: the passage is
   operator-authored and needs no confirmation, while the model's reading of
   it stays a candidate.
4. **Current state is a projection**, answered from `memory_claim`, never
   inferred from which diary chunk ranked highest.
5. **Exact strings get exact search.** An embedding must never be the only
   route to a commit hash.

---

# What already exists, and what the diary adds

| Revision 1 proposed | RainBox already has | Diary work needed |
|---|---|---|
| `memory_fact` with validity, supersession, epistemic status | `memory_claim` (`supersedes_uuid`, `conflicts_with_uuid`, `expires_at`, `epistemic_confidence`, `support_count`, `subj_pred_key`) | a `valid_from` column: claims know when they were superseded, not when they became true; a diary is the one source that dates facts |
| provenance to source passages | `memory_evidence` (`source_type`, `source_id`, `excerpt`) | a `source_type` of `diary_passage` whose `source_id` is `file:byte_start-byte_end`, so evidence can open the passage |
| actor kinds; keep "I decided" apart from "a user asked" | the actor/trust model (override-authorized vs candidate-by-default) | nothing new for trust; a `speaker` on the *event* for who said it inside the diary |
| tombstones against re-entry of rejected values | `memory_rejected_value` | nothing |
| hybrid retrieval: exact, lexical, vector, filters, rerank | `retrieve_memories_hybrid` (FTS + pgvector + entity boost), the recall-filter call | an exact-identifier route; time and speaker filters; the diary passage as a retrievable unit |
| embeddings | `memory_embedding`, `embeddinggemma:300m`, 768-d, multilingual | embed passages, not only claims |
| eval benchmark | `eval_case` / `eval_run` / `eval_result`, deterministic runner | a `diary_recall` case kind and the categories below |

Everything below the double rule is new. It is one proposal, not three.

---

# The design

## Layer 0 — source

```sql
diary_file (
    id            uuid primary key,
    path          text unique,        -- relative to the diary root
    sha256        text,               -- of the last ingested content
    ingested_at   timestamptz
);

diary_passage (
    id            uuid primary key,
    file_id       uuid references diary_file(id),
    byte_start    integer,
    byte_end      integer,
    recorded_on   date,               -- from the file name or a heading
    language      text,               -- detected tag, e.g. "da", "en"
    text          text,               -- verbatim copy, for FTS and display
    text_hash     text
);
```

A passage is a deterministic split: a dated heading, a blank-line-separated
block, or a fenced code block, whichever is smaller than the passage cap.
No model is involved. Re-ingesting a changed file re-splits it and keeps
every passage whose `text_hash` is unchanged, so derived rows survive an
append.

## Layer 1 — exact index (deterministic, no model)

```sql
diary_identifier (
    passage_id    uuid references diary_passage(id),
    kind          text,   -- url | commit | path | symbol | error | issue | model
    value         text,
    primary key (passage_id, kind, value)
);
```

Regular expressions, not a model: URLs, 7-40 hex-digit hashes, paths with
a slash and an extension, `CamelCase` and `snake_case()` tokens inside code
spans, lines that look like exceptions, `#123`-style references, model
names in `name:tag` form. Exact and prefix lookup on `value` is the first
route for any query that itself contains such a token. This layer alone
answers the first of the two questions above, and it costs nothing to
rebuild.

## Layer 2 — events (one local-model pass per passage)

```sql
diary_event (
    id                 uuid primary key,
    passage_id         uuid references diary_passage(id),
    occurred_on        date,           -- defaults to the passage date
    kind               text,           -- see the vocabulary below
    speaker            text,           -- self | named person | agent | quoted
    summary            text,           -- one sentence, in the passage's language
    extraction_version integer,
    model              text
);

diary_event_entity (
    event_id     uuid references diary_event(id),
    entity       text,                 -- normalized name
    role         text,                 -- subject | object | mentioned
    primary key (event_id, entity, role)
);
```

Kind vocabulary, kept small on purpose:

```text
observation  decision  task  idea  question
request      feedback  bug   experiment  result
```

Revision 1 listed seventeen kinds; a small model sorts reliably into ten.
Anything else is `observation` with entities.

`speaker` is not the trust actor. Every row here is `model_inferred` in the
trust model's terms, because a model wrote the summary; `speaker` records
who the diary says said it, so "a user asked for JSONL export" and "I
decided to add it" never merge.

**Cost.** This is the pass that revision 1 never priced. A diary of 3 000
passages at roughly 300 tokens each, with a 600-token instruction prefix
shared across the run, is about 1 M input tokens and 150 k output tokens
once. On the operator's local `gemma4:e4b` at the throughput measured on
a live run today (about 700 tok/s uncached prefill, about 50 tok/s decode)
that is roughly 25 minutes of prefill and 50 minutes of decode — an hour
and a quarter, once per `extraction_version`, never per turn. The pass is incremental (only passages
without a row at the current version), resumable, and runs as a background
job on the assistant's model slot with the same structured-output contract
as every other call — no tool calling, one JSON object per passage, no
example values in the instruction.

## Layer 3 — beliefs (candidates into the existing store)

From events of kind `decision`, `result` and any event with a
`subject`-`predicate` shape, the pass proposes `memory_claim` candidates
through the governed write path, actor `model_inferred`, with a
`memory_evidence` row of `source_type = diary_passage`. They obey every
existing rule: candidate status, tombstones, conflict detection on
`subj_pred_key`. What is new is `valid_from = occurred_on`, so two
decisions six months apart about the same predicate become an ordered
history rather than a conflict.

The operator confirms candidates on `/memory` as today. The diary does not
get a shortcut past confirmation: its *passages* are trusted as quotes, its
*claims* are not trusted as beliefs until confirmed. That split is the
whole reason principle 3 exists.

## Layer 4 — threads (deterministic first, model second)

```sql
diary_thread (
    id          uuid primary key,
    title       text,
    kind        text,        -- topic | debugging | project | person
    opened_on   date,
    closed_on   date null,
    state       text null    -- one sentence, model-written, dated
);

diary_thread_event (
    thread_id   uuid references diary_thread(id),
    event_id    uuid references diary_event(id),
    primary key (thread_id, event_id)
);
```

A thread is what revision 1 called an episode or a memory object. The
first assignment is deterministic: events sharing an entity within a
window of days join the same thread. A model pass then writes only the
`state` sentence, dated, from the thread's passages — a sentence, not a
dossier, so there is nothing long enough to hallucinate a history into.
The thread's content at retrieval time is its passages, quoted.

This replaces revision 1's consolidation ladder (daily → thread → monthly →
object). Summaries of summaries are exactly the chain principle 3 forbids;
a thread pointing at its passages gives the compact context revision 1
wanted without a derived text that can go stale.

## Not in the design

- **Datalog.** Every query revision 1 wrote in Datalog (`open_commitment`,
  `follow_up_needed`) is a join over `diary_event` and `memory_claim`. Write
  them as SQL views first. Bring in a Datalog engine only when a query
  needs recursion Postgres cannot express well — none of the listed ones
  does. This removes a dependency and a second copy of the data.
- **A graph database.** The relationships that matter (`supersedes`,
  `conflicts_with`, `resolved_by`, `caused_by`) are columns or a small
  `diary_relation(event_id, relation, target_event_id)` table; two hops is
  a self-join.
- **UMAP/HDBSCAN at runtime.** Useful as a one-off script under `tools/`
  to look at the corpus and check whether threads cluster as expected;
  never a source of truth and never in the request path.
- **A canonical language.** Passages stay in their language; summaries are
  written in the passage's language; entities are normalized strings the
  extraction pass is told to keep language-independent (a project name is
  the same in every language). The embedder is multilingual, so
  cross-language recall is measured, not assumed.

---

# Retrieval

The diary joins the existing hybrid path as one more candidate source, with
two additions and one rule.

```text
query
  ├─ contains an identifier?  →  diary_identifier exact/prefix  (first, cheap)
  ├─ FTS over diary_passage.text
  ├─ pgvector over passage embeddings
  ├─ time filter (a date, "last spring", "before the rewrite")
  └─ speaker filter ("what did people ask for")
        ↓
  candidate passages (+ the thread each belongs to)
        ↓
  recall-filter call (existing; scores claims, Q&A entries and passages alike)
        ↓
  context: kept claims as today, then passages quoted in a recalled fence
           with their date and file, at most N passages, thread state
           sentence when the passages share a thread
```

The rule: **a passage is injected verbatim, never summarized.** The budget
follows the existing recalled-fence budget. If the best passages do not
fit, fewer passages, never shorter ones — a truncated error message is
worse than none.

Time expressions resolve in code against the profile's timezone and the
current local time, before the query reaches any index; a small model is
not asked to do date arithmetic.

---

# Evaluation

Use the existing eval tables with a `diary_recall` case kind and a small
synthetic diary fixture under `data/` (Terminator-universe content, several
languages, no operator text). Case categories:

```text
exact lookup        the query holds a hash / symbol / error string
semantic recall     paraphrased topic, no shared tokens
temporal recall     "what was I doing in the week of …"
cross-language      query in one language, passage in another
state change        two dated decisions; the answer is the later one
open follow-ups     a request with no later result event
who-said-it         a request by a person vs. the operator's own decision
```

Metrics: Recall@5 on passage ids, current-state correctness, and
provenance correctness (the injected passage is the one the case names).
Run the deterministic runner against: (A) FTS only, (B) FTS + vector,
(C) B + identifier route, (D) C + threads. Ship a layer only when it moves
its category without hurting the others — the same discipline the profile
gate applies.

---

# Order of work

1. **Layer 0 + 1 + FTS + passage embeddings + the retrieval route.** No
   model pass. This alone answers exact and semantic recall and is the
   smallest thing that changes a real conversation.
2. **Evaluation fixture and cases**, so 3 and 4 are measured.
3. **Layer 2 events** as a background pass, and time/speaker filters.
4. **Layer 3 claims** through the governed write path, with `valid_from`.
5. **Layer 4 threads**, deterministic grouping first, the state sentence
   second.

Stop after any step if the eval says the next layer does not pay.

---

# What this proposal decides

- One design, four layers, all rebuildable from the files.
- The passage is the unit of retrieval and is quoted; structure is index.
- Beliefs go through the existing store and trust model; the only schema
  change to `memory_claim` is `valid_from`.
- No Datalog, no graph store, no consolidation ladder, no canonical
  language, no runtime clustering.
- Extraction is priced, versioned, incremental, and off the request path.
