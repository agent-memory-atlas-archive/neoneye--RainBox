# Representing a Long-Running Diary for AI Memory

**Status:** Proposal. The diary-specific components below are not built.
**Date:** 2026-09-21 (revision 7)
**Relates to:** [memory architecture](../memory-architecture.md),
[Q&A system](../qa-system.md),
[recall and retrieval granularity](2026-08-17-recall-filter-and-retrieval-granularity.md),
[memory design patterns](2026-06-30-memory-design-patterns.md),
[eval loop](../eval-loop.md),
[assistant design](../assistant-design.md) §Run summarizer.

**Recommendation:** Start with versioned source passages, exact and lexical
search, and passage embeddings. Return cited source text. Add event extraction,
reviewable belief proposals, and thread navigation only when they improve
measured recall. Reuse RainBox's governed belief store; do not turn a diary
import into automatic belief confirmation.

**Revision 7 — distinguish syntax, interpretation and authority.** Keep the
three historical diary dialects and the assistant diary introduced in revisions
4–6. Correct their overconfident parsing and attribution rules: a plausible time,
shell prompt or slash token is not conclusive evidence of an entry boundary or
addressee. Separate file revisions from parser generations, make restrictive
policy changes durable, and treat assistant digests as generated navigation to
run evidence. The original source-first recommendation remains. Earlier
revision history is in git.

## The problem and the answer contract

The input is plain text the operator has appended to for decades and
occasionally corrects. The format has changed over the years, and the
files keep whichever shape they were written in, so the design assumes a
small set of **dialects**, each with an explicit grammar and an ambiguity
policy. The current one: a line holding one date, `YYYYMMDD`, then entries each opened
by a time line — a start time `HHhMM` or a range `HHhMM - HHhMM` — followed
by free text until the next time line or date line. A synthetic day in
that shape:

```text
20270312
Woke at 07h10. Slept badly; the dog next door barked until two.

09h30 - 10h00
Physiotherapy exercises.

10h15
reread the threat model
https://example.org/notes/threat-model

11h02
find the two papers on sparse memory tables and write a note about them.
no need to download the models, there is no disk for it.

11h40
IDEA: let the assistant keep its own diary in this format, so we can
query each other's.

12h05
A user of the export tool asked for JSONL output (issue #41). Follow up
next week.

12h30
/goal process the queue

13h15
Mein Rücken fühlt sich wieder normal an.
```

An older dialect is a ChangeLog: a header line of a day-month-year date
with the month spelled out, followed by an author token, then one `*`
bullet per entry with continuation lines indented, no times, and the
newest day first in the file. Synthetic:

```text
27-juli-2027 kreese
*	refactoring: true/false return values from commands have been
	replaced by an exception system; a thrown exception means no
	modification occurred. This has improved robustness a lot.
*	version 0.8 is released.
*	bugfix: Edit::VSpace#move_left did not report its change,
	trashing undo/redo completely.

26-juli-2027 kreese
*	re-enabled Buffer#test_exception_xxx.
```

A third dialect keeps one file per day, `YYYY_MM_DD.txt`, with entries
opened by a bare four-digit time, blank lines inside an entry, and pasted
terminal sessions in the middle of the prose. Synthetic:

```text
0830
call with reese

exclude the admin tool
exclude images

we agree on what to build first


0910
reese says look at
http://trac.example.org/skynet/ticket/6

so I start with "product_info.php"

first the ignore list needs fixing.

www:/srv/skynet/www# svn st
?      admin
M      index.php
?      images/products/t800.gif
D      index.php.bak
www:/srv/skynet/www#


argh. commit fails, the repository is locked.
```

Entries mix languages, sometimes inside one entry; carry exact technical
strings; and differ in **whom they are for**: a note to self, an instruction
to an agent (an imperative, a `/word` command), a personal idea (`IDEA:`),
or a report of what another person said or asked. Health and body notes
sit between project notes. Ingestion never edits these files. All examples
in this document are synthetic; the operator's diary appears nowhere in
the repository.

These questions require different evidence:

| Question | Required evidence | What must not be inferred |
|---|---|---|
| “Find the exact error message I recorded.” | A literal match and its surrounding passage | That token extraction recognizes every possible error |
| “Why did I change this design three months later?” | The earlier decision, later change, and any stated reason | That chronology or a shared entity proves causation |
| “What design am I using now?” | An active, applicable confirmed claim, with supporting history if useful | That the latest diary mention is current truth |
| “What requests have no recorded outcome?” | Requests and explicitly linked outcomes within stated search coverage | That no retrieved outcome means a task is still open |
| “What did I ask you to do on the 12th?” | Entries addressed to an agent, with their recorded times | That the instruction applies to the current turn |

If only historical evidence exists, answer historically: “The September entry
says …; I have no confirmed current state.” If no reason is recorded, return the
sequence and say so. The representation must support that distinction rather
than forcing every question into a fact lookup.

## Principles

1. **Source text establishes what was recorded.** Operator authorship does not
   make third-party statements, speculation or pasted logs verified facts.
   Authorized passages can be read without confirming each quotation; extracted
   beliefs still require review.
2. **Historical instructions are data.** A diary request is not authorization
   to act now. This applies even when the addressee is unknown or incorrectly
   classified. Current user instructions, not recalled commands, govern action.
3. **Citations identify immutable snapshots.** A path and byte range alone are
   insufficient when a file can change. A citation resolves the exact imported
   bytes or explicitly reports their unavailability.
4. **Generated structure is an index.** Events, aliases and thread membership
   locate passages; their summaries are not factual answer context. Assistant
   diaries are also generated material: quoting a digest cannot turn it into
   independent evidence of an action.
5. **Indexes are rebuildable; decisions are durable.** Confirmations,
   rejections, exclusions, policy restrictions and promotion records survive
   parser/model upgrades and are not reconstructed from source prose.
6. **Access and freshness precede ranking.** Enforce the same eligibility on
   search, model inputs, citations, neighbor/thread expansion and injection.
7. **Current belief state is separate from history.** Neither import order nor
   an inferred date changes a confirmed belief. Unknown attribution and dates
   remain unknown rather than acquiring convenient defaults.

## What exists, and what must change

The implementation references below describe the repository at this revision.
They take precedence over older design notes when those notes differ.

| Existing component | Reuse | Required diary work |
|---|---|---|
| [`MemoryClaim` and `MemoryEvidence`](../../db/models.py) | Claim lifecycle, provenance and supersession links | Link evidence to immutable passages; no claim schema change in the first milestone |
| [`record_belief`](../../db/memory.py) | `model_inferred` writes, conflict review and rejected-value tombstones | An idempotent promotion adapter; rerunning inference must not add support again |
| [`retrieve_memories_hybrid`](../../memory/retrieval.py) | Filter-before-rank pattern and embedding client | A separate passage retriever; this function currently retrieves claims |
| [`MemoryEmbedding`](../../db/models.py), [`memory/embeddings.py`](../../memory/embeddings.py) | Embedding infrastructure; currently 768-dimensional `embeddinggemma:300m` | Passage-owned embedding rows; the existing table has a foreign key to claims |
| [`_filter_recalled_candidates`](../../agents/assistant.py) | Shared relevance stage, configurable LLM or reranker backend | Typed passage candidates, passage rendering, diagnostics and bounded fallback |
| [`eval_case`](../../db/models.py), [`evals/runner.py`](../../evals/runner.py) | Cases, run records and comparisons | A `diary_recall` case type, schema constraint migration and scorer; the current memory scorer uses lexical claim retrieval |
| [`AssistantRun`](../../db/models.py), [`assistant_run_summarizer.py`](../../agents/assistant_run_summarizer.py) | Run identity, timestamps, trace and optional structured digest | An idempotent room-scoped export; the digest contains trigger/obstacles/outcome, not a verified narrative of completed actions |

Two limits matter. `memory_claim` is a governed belief store, not a complete
valid-time database: `supersedes_uuid` records lineage and `expires_at` controls
retrieval eligibility. Neither says when a fact became true. Also,
`diary_passage` is not an accepted evidence source type today: both the database
constraint and `validate_evidence` restrict it. Initially use `source_type=file`
with a versioned diary locator in `source_id`, plus an exact excerpt, rather
than adding an unnecessary source-type migration.

## Layer 0 — versioned source, entries and passages

This is a logical schema, not migration-ready SQL. Its important distinction is
between source bytes, the parse of those bytes, and bounded retrieval units.

```text
diary_source
  id, root_key, author_kind, author_id, encoding, timezone_policy,
  dialect_override, parser_options, enabled,
  scope, room_uuid, agent_uuid, sensitivity, policy_version

diary_file
  id, source_id, relative_path, current_generation_id, availability

diary_revision
  id, file_id, content_sha256, raw_bytes, ingested_at

diary_parse_generation
  id, revision_id, parser_fingerprint, parser_config,
  dialect, parse_status, diagnostics

diary_entry
  id, generation_id, byte_start, byte_end, source_order,
  date_local, clock_start, clock_end, time_precision, date_basis,
  timezone_name, time_status, author_token, context_ranges

diary_passage
  id, entry_id, byte_start, byte_end, text_hash, part_index, language_hint

diary_pasted_span
  entry_id, byte_start, byte_end, kind, detection_basis, certainty

diary_embedding
  passage_id, model_digest, input_hash, dimension, embedding
```

An entry is a time-stamped record, a ChangeLog bullet or a fallback text block.
A passage is a bounded contiguous slice of an entry; a short entry needs only
one. Entry identity retains context and supports reassembly without confusing
three chunks with three independent observations. All offsets refer to the
original revision, including context and pasted-span offsets. Date/author
headers and separators remain addressable even if not independently ranked.

`root_key` resolves a configured root, not a model-supplied path. Resolve files
and symlinks inside it before reading. Source authorship is configured as
`operator`, `assistant` or `unknown`; it does not identify every speaker quoted
inside a file. A ChangeLog author token needs a configured identity mapping,
otherwise its author remains unknown.

Source scope and sensitivity are the baseline for every derived object. Start
with explicitly authorized room/agent contexts and private sensitivity; private
alone is not room isolation. Project scope waits until retrieval can enforce it.
All proposed policy rows and lineage records below need foreign keys and
uniqueness constraints in the implementation design.

### Snapshot and citation contract

Preserve raw bytes without newline normalization. UTF-8 is the default; legacy
encodings require an explicit source/file override and a tested decoded-text to
original-byte offset map. Undecodable input is quarantined with diagnostics,
not silently repaired or guessed. Ranges are half-open `[start, end)` and end
on decoded character boundaries. Search text must match the decoded slice.
Identical paragraphs on two dates remain distinct occurrences.

A locator such as `diary:<revision_uuid>:<start>-<end>` resolves through the
source's current policy and exclusions. Display path, date precision and
revision identity. If the live file differs, show “source has changed” and open
the saved snapshot; never apply old offsets to new bytes. Old revisions are
available for explicit provenance inspection, not normal search. A parser
upgrade can change entry IDs without invalidating these byte-based citations.
Citation metadata retains the generation and its decoding configuration, so a
later encoding correction cannot silently change the displayed interpretation
of an earlier citation. Original bytes remain available alongside the decode.

### Parsing the three dialects

Prefer a configured file/directory dialect override. Otherwise recognizers must
validate dates, times and the surrounding record structure; selecting whichever
regex matches the most lines is insufficient. Record competing matches and
ambiguities. A mixed or inconclusive file falls back to `plain` blocks until
an override resolves it. Preserve all content, flag metadata as incomplete, and
retain an unambiguous filename date even in fallback mode.

| Dialect | Entry boundaries | Context retained |
|---|---|---|
| `timed` | A valid `YYYYMMDD` date header; `HHhMM` or `HHhMM - HHhMM` time lines outside recognized pasted regions | Text before the first time is date-only; text before any date is undated |
| `daily` | Date from `YYYY_MM_DD.txt`; a valid bare `HHMM` at file start or after a blank line is a **candidate** time boundary | Blank lines inside entries do not end them; preamble retains the filename date |
| `changelog` | A validated `<day>-<month>-<year> <author-token>` header; top-level `*` bullets with their continuation text | Date, author token, source order and unassigned inter-entry text |
| `plain` | Bounded blank-line blocks, without invented entry times | Filename date if unambiguous; otherwise unknown |

Validate calendar days and `00–23` hours / `00–59` minutes; a digit pattern
alone accepts impossible dates and times. ChangeLog blank lines are not by
themselves a reason to discard subsequent text: a new bullet/header closes an
entry, and unmatched text remains a visible fallback block. Indentation is
preserved, never folded out of the stored quote.

Some ambiguities cannot be solved by regex. A blank line followed by `2026`
could be 20:26 or a number in command output. A prompt-shaped line in prose does
not establish where a pasted session ends. Explicit fenced regions have known
boundaries; shell-prompt patterns produce **likely pasted spans**, not verified
speaker labels. Do not end a terminal session merely at its next prompt, or
assume everything after the final prompt is output. Ambiguous boundaries retain
a larger unsplit block and a diagnostic; do not manufacture a precise time or
speaker. File-specific overrides can resolve consistent historical conventions.

Keep the logical entry intact when its text is large. Split retrieval passages
at line boundaries under a versioned cap, with continuation order and entry
context retained. A single oversized line needs character-boundary-safe slices
marked as partial. Chunk boundaries never imply a new time, speaker or event;
extraction may use bounded neighboring context, included in its input hash.

The shared date parser uses anchored header/filename grammars and versioned,
explicit month-name tables (including configured languages). Do not scan prose
for dates to invent boundaries. A header/filename mismatch is retained as a
conflict with both candidates; it needs an explicit policy or remains uncertain.
Recognizer, month-table, encoding and configuration changes all enter the parser
fingerprint. A registry change must reconsider previously `plain` or ambiguous
files, not only files already labeled with the changed dialect.

### Time and order

Keep the written local date and clock values, their precision and basis.
A day-only entry is not an event at midnight. A written time range may describe
an activity, not when the author typed it; it is a diary time label, not proof
of occurrence or file creation time. Unknown timezone remains unknown. Use a
source/era timezone policy where available; changing today's profile timezone
must not reinterpret decades of entries. Ambiguous/nonexistent daylight-saving
times and an end before a start are flagged. Do not infer next-day rollover
without an explicit convention or supporting date.

Maintain two orders: source order for reading/neighbors and parsed local date/
time for timelines. Newest-first ChangeLogs are sorted by date for a timeline;
same-time or date-only ties retain source order. Nonmonotonic times remain
visible as written. Across sources with unknown zones, do not claim a precise
absolute ordering. Query-time relative dates use the current query timezone;
source dates retain their original basis.

### Publishing and incremental work

Build from a stable read and verify its hash. An actual content change creates
a new immutable revision; a parser/configuration change creates a new generation
over existing bytes. Publish entry/passage rows and deterministic indexes by an
atomic switch of `current_generation_id`. Successful parsing must account for
all source bytes as entry text, context or retained separators/fallback text.
A changed file whose replacement cannot be parsed is unavailable for normal
recall until repaired; old content is not silently served as current.

An unchanged `(content hash, parser fingerprint)` is a no-op. Publish lexical
search without waiting for embeddings; absence or incompatibility of vectors
disables that signal only. Reuse extraction/embeddings by the complete input
hash, including context, not passage text alone. One-to-one entry lineage can
preserve promotion/exclusion decisions through an append. Duplicate or ambiguous
matches never collapse or become new independent corroboration automatically.
Old generations become ineligible at the switch, even before vector pruning.

Recheck generation and policy when a background worker publishes, before text
is sent to a scorer, and at final injection. A stricter policy or exclusion
invalidates cached selections immediately. Previously sent prompts cannot be
retracted; log their generation/policy identity. A rename preserves identity
only when unambiguous; otherwise reconcile as removal plus addition without
losing applicable exclusions. Missing files are marked unavailable only after a
successful scan; a failed root scan does not delete the corpus. Content-addressed
snapshot storage can deduplicate unchanged bytes; measure retained snapshot and
index size as well as model cost.

### Removal and forgetting

Separate three operations in the operator UI:

- **Reject a belief:** use existing claim tombstones. This does not erase the
  historical text from which the belief was proposed.
- **Exclude diary material from recall:** persist an exclusion outside the
  rebuildable indexes and apply it to all retrieval routes. Carry exclusions
  across revisions; if occurrence matching is ambiguous, suppress the affected
  file pending review rather than re-expose the text.
- **Purge an imported source:** remove stored snapshots, excerpts, vectors and
  other copied content, including diary evidence excerpts. Keep only permitted
  non-content audit metadata. Explain that previously recorded chat/trace
  copies and backups have their own retention; this is not retroactive erasure
  from every past answer.

Evidence-bearing confirmed claims need separate handling when their source is
removed or restricted. Do not silently delete a human confirmation. Record the
source change and require review of affected claims; until resolved, exclude
claims whose only content-bearing evidence is the unavailable diary source.
A broader claim scope requires explicit human review and independently
retained evidence. A diary import must not turn private source material into a
global claim.

This requires a queryable claim-to-diary evidence dependency, beyond a locator
string, and a shared eligibility check on every claim read path, including
direct UUID reads and profile injection. The current claim filters do not
implement diary-policy invalidation. That integration is a prerequisite for
Layer 3; a UI label alone cannot enforce it.

## Layer 1 — literal, lexical and semantic retrieval

```text
diary_identifier
  passage_id, kind, raw_value, lookup_value, byte_start, byte_end
```

Index recognizable URLs, hashes, paths, symbols, issue references, model names
and literal markers. A `/word` token is not intrinsically an agent command:
`/tmp` may be a path, and `/goal` can appear in quoted documentation. A configured
command vocabulary at an unquoted entry position supplies a command **hint**;
`IDEA:` supplies an idea hint, not a recipient. Store hints with their source
range, rule version and basis. They are useful before Layer 2 exists and do not
require a model extraction row. Unknown forms remain searchable text.

Keep original spelling. Normalize conservatively by kind: paths, URL paths and
symbols must not be blindly lowercased. Hash prefixes can match several values;
return the ambiguity instead of claiming a
unique hit. Identifier recognition is a retrieval hint, not a validator.

Provide literal substring search over eligible source text as well. Regular
expressions cannot enumerate every error message, extensionless path or unusual
symbol. In particular, a punctuation-heavy error must remain findable even if
FTS tokenization loses the punctuation. Bound literal scans and report incomplete
coverage when a configured search limit is reached.

FTS starts with a language-neutral configuration for mixed prose and code;
language-specific indexes are an evaluated improvement. Passage embeddings use
the existing embedding client but a new passage table. Store model identity,
dimension and the hash of the actual embedding input, and never compare vectors
from incompatible models. Cross-language performance must be measured.

Literal matching must span passage boundaries within an eligible entry; map a
cross-boundary match back to its original byte range and adjoining passages.
A splitter change must not make an unchanged error string unfindable.

Exact matches get a reserved place in the candidate budget and retain their
match span. For an explicit literal lookup, at least one eligible matching
excerpt survives relevance scoring; ambiguous matches remain labeled. For a
broader question containing an identifier, the match is a ranking signal, not
proof the passage answers the question. Lexical and semantic routes still run.

## Layer 2 — optional extracted events

One structured local-model pass per bounded passage can emit **zero or more**
events. Empty output is a successful result. The initial vocabulary is:

```text
observation  decision  task  idea  question  instruction
request      feedback  bug   experiment  result
```

This vocabulary is a starting hypothesis, not a claim that a small model
classifies it reliably. An uncertain event can remain unclassified. Layer 1's
marker hints are input evidence, not mandatory event/addressee assignments.
One entry can contain a past command, an assertion and a quoted request;
classify each supported span instead of assigning one meaning to the entry.

```text
diary_extraction_run
  id, passage_id, input_hash, extractor_version, model_digest,
  prompt_schema_hash, sampling_config, status, attempts,
  input_tokens, output_tokens, error

diary_event
  id, extraction_run_id, ordinal, kind,
  evidence_start, evidence_end, summary,
  speaker_kind, speaker_id, attribution_basis, quotation_status,
  addressee_kind, addressee_id, addressee_basis,
  sensitivity_hint,
  occurred_start, occurred_end, time_basis

diary_event_entity
  event_id, surface_form, normalized_key, role
```

Evidence ranges must resolve inside the input passage or its explicitly supplied
context. Have the model identify an exact supporting quote and locate it in
code using the decoding offset map; do not rely on it to count source bytes.
Reject a quote that is absent or cannot be disambiguated. This
checks grounding of the span, not truth of the interpretation. Keep summaries
in the passage's language and in the index/inspector only. Extract no belief
from an event summary without re-reading its source span.

Authorship, speaker and addressee are separate. Source configuration establishes
who keeps the diary; `speaker_kind` (`operator`, `person`, `agent`, `unknown`)
identifies whose statement an event represents. Pasted content can have an
unknown speaker. `quotation_status` (`direct`, `quoted`, `uncertain`) is a
separate dimension, not a speaker value. A likely terminal span signals
uncertainty; it does not prove who said a surrounding sentence. Cross-boundary
claims need enough context to support attribution or remain unclassified.

`addressee_kind` is `operator`, `agent`, `person` or `unknown`, with an optional
identity and an evidence basis (`source_metadata`, `configured_marker`,
`explicit_text`, `inferred`, `unknown`). An imperative such as “find the two
papers” could be a reminder to self or a request to an agent. A slash command
cannot identify *which* agent unless source context provides that identity.
“What did I ask you?” therefore cannot match every agent interchangeably.

Represent “a user asked for JSONL output; follow up next week” as two events:
a reported request and a possible task. Neither proves an accepted commitment.
Render uncertain attribution as uncertain. No inference, marker, fence or short
summary guarantees that a model will ignore historical instructions; that
behavior must be evaluated even with Layer 2 disabled.

### Sensitivity without a dependency on extraction

The entire unclassified source must be suitable for every configured consumer
of that source, including extraction, embedding and ranking services. Milestone
2 cannot rely on a model pass that has not run to hide health notes. Where that
baseline is too broad, split source access or explicitly restrict entries before
indexing them for those consumers.

A `sensitivity_hint` is a review signal. A configured deterministic policy may
turn it into a **durable restriction** applying to the whole entry, including
all chunks, neighbor expansion, cached results and derived claims. Effective
sensitivity is the stricter of source, explicit entry and retained restrictions;
allowed scope is their intersection. In RainBox, `private` alone does not
exclude particular rooms; room restrictions must be expressed as scope/access
rules. A policy may map a hint to `secret`, which normal retrieval excludes.
Model absence, failure, re-extraction or disabling Layer 2 must never remove an
existing restriction. Only an explicit operator policy change can relax it.

Distinguish three clocks:

- **Diary time:** local date/time label parsed under Layer 0, with precision
  and basis retained; unknown stays null, never file modification time.
- **Occurrence time:** when the passage says the event happened, possibly a
  range or unknown. Using the diary date as a fallback is labeled as such and
  is not an explicit occurrence date.
- **Ingestion time:** when RainBox observed this revision.

“You mentioned it in April” filters diary time. “It happened in March” uses
occurrence time, including its uncertainty. Relative dates with an unambiguous
anchor can be resolved in code; ambiguity remains visible. Entities preserve
surface forms. Case normalization does not solve aliases across languages or
distinguish two people with the same name; uncertain aliases stay separate.

### Extraction jobs and cost

Use the existing structured-call machinery with an explicitly configured local
model and no tool calls. Do not silently route private diary content to a remote
fallback. Bound input, event count and output, yield to interactive work, and
support cancellation, progress and bounded retries. Record success-empty,
success-with-events and failure separately. Persist a completed generation
atomically and activate it only for the matching source/input/version; an old
worker cannot publish after its source changes.

A model, prompt, schema or relevant context change invalidates that pass's
cache. Reuse successful work rather than selecting “passages with no event
row,” which retries empty results forever. Store the actual model digest and
sampling configuration with the run, not only a mutable model tag.

The earlier 75-minute estimate assumed prefix caching without pricing a cold
run. The examples include short entries and long pasted sessions; their
frequency across the corpus is unknown. Measure both, without assuming an
average from the examples. For **illustration only**, 3,000 passages, 300
passage tokens, a 600-token prefix per call, and 50 output tokens per
passage give:

```text
uncached input = 3,000 × (600 + 300) = 2.7 million tokens
output        = 3,000 × 50          = 150,000 tokens
elapsed ≈ uncached_input / prefill_rate + output / decode_rate + overhead
```

At hypothetical rates of 700 input and 50 output tokens/s, this is about
64 + 50 = **114 minutes**, before overhead, retries and embeddings. Perfect
prefix reuse would reduce prefill toward 21 minutes, not guarantee it. Several
events with evidence quotes may need far more than 50 output tokens. Benchmark
a representative sample first and record cold/warm throughput, output lengths,
validation failures, total wall time and interference with interactive turns.

## Layer 3 — proposals into the existing belief store

A supported assertion, decision or result may be useful as a durable belief.
Instructions, requests, questions and hypothetical goals are not eligible as
asserted world facts, regardless of addressee. “Process the queue” must not
become standing policy. Conversely, an assertion does not become an instruction
merely because it was addressed to an agent. Keep mixed content in separate
events and review the assertion's own supporting span. A historical claim that
a request was made is distinct from accepting or executing it.

Start with operator-selected events and a capped proposal queue. Assistant
digests are not eligible as independent evidence; follow their run/step lineage
to underlying observations before proposing a claim.

The promotion adapter re-reads the source and calls `record_belief` with actor
`model_inferred`, provenance `inferred_by_model`, `source_type=file`, the
versioned locator and an exact excerpt. Preserve scope/sensitivity and existing
conflict/tombstone rules. Outcomes can be a candidate, a conflict candidate,
corroboration of an existing claim, or refusal by a tombstone; the adapter must
handle each explicitly. It never calls low-level create or activate helpers to
bypass those outcomes.

**Promotion must be idempotent.** Today, repeating `record_belief` for equivalent
text increments support and adds evidence. Persist a promotion ledger and the
belief write in one transaction, using a key tied to the source observation
and proposed belief, not the extraction-run ID. One-to-one occurrence lineage
across unchanged revisions identifies the same observation. An ambiguous match
or a differently phrased re-extraction of already promoted evidence goes to
review; it must not automatically count as independent corroboration. A retry,
model upgrade or index rebuild adds neither duplicate proposals nor support.
Keep that ledger with human decisions, outside disposable extraction tables.

**Do not add `valid_from` and call temporal reasoning solved.** Two dated,
different values still conflict under today's `record_belief`; dates do not
prove that the later value replaces the earlier one. Human review continues to
decide supersession. Historical chronology comes from passages and events.

A later temporal-belief extension would need a separate design for effective
start/end ranges, uncertainty, overlapping values, future-effective decisions,
backdated corrections, same-value recurrence, scope and review ordering. It
would also need to distinguish valid time from when RainBox learned a claim,
and define migration behavior for undated claims. `expires_at` must retain its
existing retrieval meaning. None of this blocks useful diary recall.

## Layer 4 — optional threads and relationships

Threads are saved navigation groups over events/passages. They are not a second
belief store and do not have an authoritative model-written `state` sentence.
Their answers are assembled from source passages within the same access rules.

Start with topic membership from explicit headings, project identifiers and
unambiguous entity aliases. Retrieve across the whole history for a topic; a
short proximity window would miss the design reversal six months later. Within
a topic, proximity can form bounded episodes such as a debugging session. An
event may belong to multiple topics. Generic entities such as “Python” must not
connect every entry into one enormous thread.

Store thread membership with its grouping version and reason. Rebuild membership
when inputs change. Cross-passage `responds_to`, `resolved_by` or `caused_by`
relations require supporting source spans and an extraction/review status.
Shared vocabulary and temporal order are candidate links only. A request with
no linked result is “no outcome found in this coverage,” not an open commitment.
Actual task status belongs in an explicitly maintained task system or a reviewed
claim.

Model-generated titles or summaries may help an inspector navigate, but remain
labeled derived text and stay out of answer context. One short sentence can
hallucinate a state just as easily as a long summary.

## The assistant's own diary

Keep this as an independently gated extension after passage retrieval. The
assistant writes only to its own configured root. Each source is scoped to the
originating room/agent; a single daily file must not combine rooms and then
inherit one broad source policy.

The current run summarizer stores an optional `{trigger, obstacles[], outcome}`
object on `assistant_run.summary`. It can fail or be rerun, and its model-rated
`resolved` outcome is not proof that all requested side effects occurred. Export
what actually exists using a deterministic renderer: run UUID, timestamps,
terminal status, digest origin and available fields. No extra call is needed to
invent a first-person narrative. Synthetic shape:

```text
20270312
12h31 - 12h34
run: 9f3c28b1-4d2e-4f60-a17b-79ad3e0c6542
status: finished
summary_origin: model_inferred
trigger: Queue processing
outcome: partial
obstacles: Missing token
```

An export ledger keyed by `(run_uuid, export_input_hash, renderer_version)` makes
retries idempotent. Serialize writes per file and use atomic replacement of the
managed daily view; reconcile ledger and file after a crash. The hash covers
all rendered inputs, including status, timestamps and any digest. Re-summarization
replaces that run's rendered entry, producing a new snapshot, rather than a
second apparent work session. Preserve prior revisions for provenance under the
same retention/access rules. Missing digests render `summary unavailable` with
deterministic status metadata. A run spanning midnight retains full start/end
timestamps in metadata. Export and summarizer work must not recursively create
new diary entries about their own exports.

**The file is a readable projection of run records, not a new source of truth.**
Store run/step links and generated origin outside the prose so copied text cannot
choose its own authority. Digests can select runs and appear as labeled text in
the source viewer. To answer “what did you do?”, inject eligible typed run status
and supporting step observations, not digest prose. If traces are unavailable,
report the metadata or absence of evidence,
not a reconstructed success story. Merely counting `finished` runs or quoting
`outcome: resolved` does not prove task completion.

Retrieval must preserve provenance through later copies and summaries. Reading
an assistant diary, quoting it in another run and exporting that run again does
not create independent corroboration. Direct links to a diary request and its
run may establish “responds to”; a textual resemblance does not. The shared
retrieval interface therefore needs typed run-evidence candidates as well as
operator passages before this extension ships.

## Retrieval and context assembly

Integrate first with the assistant's explicit `memory_query` action. Always-on
chat/profile injection is a separate scope decision; importing a diary should
not put its contents into every conversation.

```text
query + authorized context + retrieval mode + captured clock
  → source/entry access, exclusions and active-generation eligibility
  → reliable date/source constraints before each candidate route is capped
  → bounded literal/identifier, FTS and vector routes
  → merge by entry/passage; preserve match spans and retrieval reasons
  → soft inferred speaker/addressee/time signals; shared relevance stage
  → bounded context expansion, with eligibility rechecked for every addition
  → budgeted cited excerpts or typed run evidence, alongside claims/seeds
```

Use per-route caps and rank-based fusion rather than adding unrelated similarity
scales. Deduplicate overlapping windows and diversify by entry so one large log
cannot occupy every result slot. Track retrieved, filtered, expanded and injected
IDs, generations, omissions and timings. Namespace claim/seed/diary/run IDs.
Embedding or relevance-service failure falls back to bounded eligible literal/
lexical results; it never bypasses access checks.

Queries need more than one mode. **Lookup** ranks relevance; **timeline** scans
eligible entries in a date interval in stable date/source order with pagination;
**comparison** seeks both sides of a change and any explicit reason. A diary
day can have no shared words with “what happened that day?”, so timeline mode
must not require a keyword or vector match. Return coverage (`complete`,
`partial`, `unknown`), searched date/source range and continuation metadata.
Completeness is relative to the declared eligible corpus and known date coverage,
not a guarantee that every real-world event was recorded. Pagination pins source
generations and ordering; edits invalidate its cursor rather than mixing views.
“No outcome found” cannot become “no outcome exists” when extraction, dates or
pagination are incomplete.

Apply reliable date/source constraints before route limits; filtering a global
top-K afterward can miss every relevant entry from an older year. Inferred
speaker, addressee and occurrence dates are soft signals by default. Strict
filtering is an explicit query option and reports unknown/unclassified coverage;
an explicit natural-language question alone does not make inferred metadata
reliable. Resolve “you” against the current agent identity where available.
Unknown identity can be shown as “an agent; recipient not established.”

Compute unambiguous relative calendar ranges in code using the captured query
timezone. “Before the rewrite” first needs a supported event anchor; ambiguity
produces alternatives or a soft hint. Past commands are labeled as historical
data independently of their classification. No label grants present authority.

For historical comparison, gather both sides of a change and any stated reason,
then present them chronologically. For current-state queries, diary passages
remain supporting history alongside the active claim projection. Never silently
replace that projection with a fresher-looking quotation.

### Source fidelity within a finite budget

The existing assistant renderer shortens long facts and uses a character-based
admission threshold; it is not already a whole-passage, hard token budget.
Diary rendering therefore needs its own contract:

- Prefer whole bounded passages. If a relevant block is too large, select a
  contiguous source window around the literal match or a smaller pre-indexed
  passage. Mark it as an excerpt with its exact range and parent locator.
  Do not paraphrase it or join disconnected fragments into a fabricated quote.
- Include enough adjacent source to preserve attribution and negation. Charge
  neighbor expansion to the same budget. Report omissions and continuation IDs;
  do not pretend a partial traceback or code block is complete.
- Reserve a diary share of the common recall allowance for diary-focused
  requests; otherwise use the allowance remaining after claim/seed context.
  Count citations, labels and fence overhead with the answering model's
  tokenizer. If none fits, return a bounded locator/omission notice rather than
  exceeding the total context budget. An unavailable tokenizer needs a tested
  conservative bound, not an assumed characters-to-tokens equivalence.
- Provide typed direct passage lookup with bounded continuation. Every fetch
  rechecks source policy, exclusions and revision availability. Do not assume
  the existing claim/seed UUID-fetch branch implements this contract.

“Verbatim” describes the stored slice and source viewer. Prompt rendering still
uses the existing recalled-data fence and structural escaping, which may replace
characters such as angle brackets. Retain the original bytes for exact copying
and state when displayed text is escaped. Fences reduce structural injection;
they do not make old instructions authoritative or guarantee model behavior.
No event summary, thread state or scorer explanation accompanies the quotes as
a fact.

## Evaluation before rollout

Build a small, synthetic multilingual diary fixture under `data/` before the
first retriever ships. Include code, prose, duplicate paragraphs, long entries,
file revisions and parser generations. Keep operator text out of repository
fixtures and CI output.
Each case names the query, mode, access context, fixed clock/timezone, source
revision manifest, required evidence ranges and forbidden passages. The
fixture covers all three dialects (date and time lines with ranges;
ChangeLog headers with bullets; file-per-day with `HHMM` lines and pasted
terminal output), two languages including a spelled-out month name,
Terminator-universe content and author tokens, with both an operator diary
and an assistant diary.

| Family | Cases that distinguish success from a plausible-looking answer |
|---|---|
| Literal | Full/prefix hashes, ambiguous prefixes, extensionless paths, punctuation-heavy errors, absent strings |
| Semantic/language | Paraphrases, Danish/English queries, code mixed with prose, unresolved aliases |
| Time | Recorded vs occurred dates, uncertain ranges, future plans, backdated entries, ambiguous “before the rewrite” |
| History/current state | Earlier decision + reversal + stated reason; no reason; confirmed state vs newer unconfirmed mention |
| Attribution/outcomes | Quoted person vs operator, pasted agent commands, unsupported causal links, incomplete outcome coverage |
| Addressee | `/tmp` vs configured `/goal`; quoted commands; an imperative with unknown recipient; two agents; mixed request/assertion/task spans; no action authorized solely by recall |
| Format | All dialects, mixed/ambiguous fallback, overrides, invalid dates/times, filename/header conflict, unknown author token, blank-line `2026` in terminal output, text after the final prompt, multiline bullets, retained unmatched text, configured legacy encoding |
| Parse invariants | Every byte retained/addressable; literal matches across chunks; new recognizer reconsiders `plain`; parser upgrade preserves citations; interrupted publication exposes no mixed generation |
| Time precision | Date-only and unknown-zone entries; DST gaps/overlaps; midnight ranges; profile timezone change; chronological vs source order; date-only query retrieves entries without token overlap |
| Assistant diary | Missing/rerun digest, retry/crash/concurrent export, midnight run, room isolation, `finished` with failed actions, generated claim contradicted by trace, unavailable trace, no recursive self-corroboration |
| Lifecycle | Append, duplicate text, edits, rename/removal, stale workers/vectors, empty extraction, retries, re-extraction without added support |
| Context/access | Long-block evidence, budget exhaustion, hidden neighbor, excluded citation, adversarial fence text, restriction survives disabled/rebuilt extraction, policy changes during ranking, private source before classification |

Measure candidate Recall@K separately from **injected evidence coverage**: finding
a passage does not help if the scorer or budget drops its answer-bearing span.
For multi-passage questions, require all named evidence spans. Also record
citation/range correctness, forbidden-source exposure, attribution and
current-state correctness, duplicate proposal/support counts, context tokens,
latency, retained storage and extraction cost. Score entry-boundary accuracy,
metadata coverage and false attribution separately from retrieval: a parser can
retain text while assigning it to the wrong date or person. Report how often
unknown/fallback is used so apparent precision cannot hide poor coverage.
“Injected” is not proof the answer used it; score answer behavior separately.

Compare the same case inventory and source snapshot:

- **A:** literal/identifier + FTS baseline.
- **B:** A + passage vectors.
- **C:** B + event metadata.
- **D:** C + thread/neighbor navigation.

Test relevance-stage and budget policies as explicit variants, including a
literal lookup that a scorer incorrectly rates irrelevant. Pin splitter,
embedder, extractor, prompt/schema, retrieval budgets and model versions in each
run. Deterministic tests use fixed embeddings/extraction outputs to verify
contracts. Separately run live models on the same cases to measure extraction,
cross-language recall and answer quality; do not call live inference
deterministic because its scorer is deterministic.

Release gates require zero forbidden-source exposure, invalid citations,
duplicate support from retries, unconfirmed belief activation, and
**instruction carryover** — an action or present commitment authorized solely
by recalled text. Test the action trace as well as the final answer, including
with extraction disabled. Quoting a past request in answer to a history question
is permitted; new explicit user authorization is evaluated separately. An added
layer must improve its target family on held-out cases, preserve baseline-passing
regression cases, and stay within latency/token limits fixed before the run.
Publish per-family counts, failures and repeated live-run variation. Keep a
layer disabled when the evidence is too small or mixed to justify its cost.

## Order of work and stopping points

1. **Fixture and source contract:** define eligible sources, revision/range
   semantics, exclusions, the dialect registry and its fallback, splitter
   caps, encoding/time policy, generation publication and evaluation cases.
   This makes provenance and access testable before retrieval integration.
   A local dry-run reports dialect/encoding failures, date coverage, ambiguous
   boundaries and entry-size distributions before publishing indexes; corpus
   text stays out of repository fixtures and logs.
2. **Smallest useful retrieval:** Layer 0 + Layer 1, explicit `memory_query`
   integration, source viewer, bounded rendering and diagnostics. Ship literal
   and FTS first; add passage embeddings against baseline A. No generative
   extraction is needed. Include timeline pagination and marker hints. Prove
   append/edit/removal, parser-upgrade and service-failure behavior.
3. **Events, if recall needs them:** benchmark a local model, implement the
   versioned job ledger and evidence validation, then evaluate attribution,
   addressee and temporal metadata. Keep ordinary passage search independent
   of this pass. Durable restrictions must also survive its removal.
4. **Assistant diary, independently optional:** after milestone 2, add the
   managed export, idempotent reconciliation and typed run-evidence retrieval.
   It does not depend on event extraction, and generated digests must not bypass
   the source-first answer contract.
5. **Belief proposals, if useful:** add selected-event promotion, durable
   idempotency and source-change review. Preserve existing governance; make no
   automatic temporal supersession change.
6. **Threads, if longitudinal questions still fail:** add bounded topic/episode
   navigation and supported relations. Evaluate full-history comparisons and
   context cost before adding model-derived navigation aids.

Stop after any milestone if the next layer does not earn its complexity.
Disabling an optional layer returns to passage retrieval without losing source
citations, retained restrictions or operator decisions. A full rollback disables
the diary source; it does not require deleting or modifying the operator's files.

## Deliberately deferred

No Datalog engine, graph database, summary-of-summary hierarchy, canonical
language, or runtime clustering is needed for the first design. PostgreSQL
relations and joins are sufficient for the proposed navigation. Offline corpus
visualization may help evaluate grouping, but does not establish truth. A
bitemporal belief model, exhaustive task tracking and always-on diary injection
are separate proposals with their own evidence and review costs.
