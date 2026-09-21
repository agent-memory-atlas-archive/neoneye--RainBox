# Representing a Long-Running Diary for AI Memory

**Status:** Proposal. The diary-specific components below are not built.
**Date:** 2026-09-21 (revision 5)
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

**Revision 5 — more than one dialect.** The diary spans decades and its
format changed along the way. Revision 4 described only the current one
(a `YYYYMMDD` line, then `HHhMM` entries). Older years use a ChangeLog
shape: a header of `DD-month-YYYY` plus an author token, `*` bullets with
indented continuation lines, no times, and the newest day first. The
splitter is therefore a registry of versioned dialect recognizers chosen
per file, entry order comes from parsed dates and never from file
position, and date parsing accepts numeric and month-name forms in more
than one language. The eval "Format" family gains dialect cases.

**Revision 4 — the real format, addressees, and the assistant's own diary.**
Revisions 1–3 assumed a folder of dated Markdown files. The diary is plain
text with a fixed grammar: a date line, then time-stamped entries. That makes
the split exact, dates every entry to the minute, and removes the heading
heuristics. Revision 3 also under-weighted the diary's hardest property: many
entries are instructions the operator gave to an agent (slash commands,
imperatives, goals for a session) sitting beside personal notes, ideas and
other people's requests; an earlier attempt by another model conflated the
operator's goals with instructions to the agent. Every event now carries an
`addressee`, with one rule: an entry addressed to an agent is a record that a
command was given, never a command and never a belief. The revision also
lets inferred sensitivity narrow but never widen source policy, adds the
assistant's own diary in the same format, and adds the eval families that
check all of this.

**Revision 3 — substantive corrections.** Revision 2 established the useful
split between source passages and inferred beliefs, but overstated what the
existing system supplies. This revision fixes mutable-file provenance,
distinguishes recording time from event time, removes automatic temporal
conflict resolution, keeps generated thread summaries out of answer context,
and defines retry, access, budget, and evaluation behavior. It also replaces an
unsupported runtime estimate with explicit assumptions. Earlier revisions remain
in git.

## The problem and the answer contract

The input is plain text the operator has appended to for decades and
occasionally corrects. The format has changed over the years, and the
files keep whichever shape they were written in, so the design assumes a
small set of **dialects**, each with a fixed and small grammar. The
current one: a line holding one date, `YYYYMMDD`, then entries each opened
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

1. **Source text is evidence of what was recorded.** Operator authorship does
   not make quoted third-party statements, speculation, old commands, or pasted
   logs verified facts or executable instructions. Authorized passages can be
   read without confirming each quotation; extracted beliefs still require
   review.
1. **An instruction in the diary is a record, not a command.** An entry
   addressed to an agent is retrieved as "on this day the operator asked for
   X", with its time. It never re-enters a prompt as something to do now, and
   it never becomes a belief about the world. The recalled-data fence and the
   system prompt already treat recalled text as data; the `addressee` field
   makes the distinction queryable and lets the answer say so.
2. **Citations identify immutable snapshots.** A path and byte range alone are
   not enough when a file can change. A citation must resolve the exact ingested
   revision and range, or explicitly report that it is unavailable.
3. **Generated structure is an index.** Events, aliases, and thread membership
   help find passages. Their summaries do not enter the answering prompt as
   facts. Source excerpts remain contiguous and attributable.
4. **Indexes are rebuildable; human decisions are durable.** Extraction can be
   replaced. Confirmations, rejections, access settings, and suppression records
   cannot be reconstructed from the diary and must survive rebuilds.
5. **Access and freshness are checked before ranking.** The same rules apply
   to search, direct citation lookup, neighboring passages, and thread expansion.
6. **Current belief state and historical evidence remain separate.** Neither
   ingestion order nor a model-assigned date changes a confirmed belief.

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

Two limits matter. `memory_claim` is a governed belief store, not a complete
valid-time database: `supersedes_uuid` records lineage and `expires_at` controls
retrieval eligibility. Neither says when a fact became true. Also,
`diary_passage` is not an accepted evidence source type today: both the database
constraint and `validate_evidence` restrict it. Initially use `source_type=file`
with a versioned diary locator in `source_id`, plus an exact excerpt, rather
than adding an unnecessary source-type migration.

## Layer 0 — versioned source and passages

This is a logical schema, not migration-ready SQL. Final migrations must add
foreign keys, uniqueness constraints and indexes for the contracts below.

```text
diary_source
  id, root_key, enabled, scope, room_uuid, agent_uuid, sensitivity

diary_file
  id, source_id, relative_path, current_revision_id, availability

diary_revision
  id, file_id, content_sha256, raw_bytes, ingested_at

diary_passage
  id, revision_id, byte_start, byte_end, text_hash,
  dialect, dialect_version,
  recorded_start, recorded_end, time_basis, author_token, language_hint

diary_embedding
  passage_id, model_digest, input_hash, dimension, embedding
```

A passage is one diary entry in whatever dialect the file is written in:
a time line and the text under it, or one `*` bullet with its
continuation lines.
`root_key` resolves an operator-configured diary root; it is not a path supplied
by the model. Scope and sensitivity are configured at the source and inherited
by all derived objects. Start with explicit room/agent access and private
sensitivity; private must not mean globally visible. Do not offer project scope
until the retrieval context can enforce it. Resolve files inside the configured
root, including symlinks, before reading.

### Snapshot and citation contract

Store the original UTF-8 bytes without newline normalization; quarantine files
that cannot be decoded rather than silently replacing bytes. Every range is
half-open, `[byte_start, byte_end)`, and ends on a UTF-8 boundary. The decoded
slice is the passage text; a materialized text column for search must match it.
The identity is a passage occurrence in a revision, not its content hash:
identical paragraphs on two dates remain two occurrences.

A locator such as `diary:<revision_uuid>:<start>-<end>` resolves through the
source's current access policy. Display the relative path, recorded date and
revision identity with the quote. If the live file differs, display “source has
changed” and open the saved snapshot; never silently apply old offsets to the
new file. Old revisions are available for explicit provenance inspection, not
normal current-source search.

### Splitting and incremental ingestion

The splitter is a registry of **dialect recognizers**, each a few anchored
patterns, each versioned on its own. A file's dialect is chosen once per
revision by which recognizer matches the most header lines; a tie or a
file with no recognized header falls back to the `plain` dialect
(blank-line paragraphs, `time_basis = unknown`, no dates) so nothing is
lost, only unlabeled. The dialect and its version are stored on every
passage, and a recognizer change re-splits only files of that dialect.

- **`timed`** (current): a line that is exactly eight digits sets the
  current date; a line matching `^\d{2}h\d{2}( - \d{2}h\d{2})?$` opens an
  entry that runs to the next such line or date line. Text between a date
  line and the first time line is an entry with `time_basis = date_only`.
  `recorded_start` is the date plus the entry time in the profile's
  timezone, `recorded_end` the range end when there is one; a time earlier
  than the previous entry's on the same date is kept as written and
  flagged, not reordered. A malformed time line is text, not a boundary.
- **`changelog`** (older years): a header line `<day>-<month>-<year>
  <token>` sets the current date and records the token as
  `author_token` (it is not parsed as a name anywhere else; speaker stays
  `self` unless Layer 2 finds otherwise); each following line starting
  with `*` opens an entry that runs until the next `*` line, header line
  or blank line, with indented continuation lines folded in.
  `time_basis = date_only` for every entry; entries within a day keep
  file order as their tie-break, and days are ordered by the parsed date,
  because this dialect lists the newest day first.

Dates are parsed by one shared routine that accepts the numeric forms
(`YYYYMMDD`, `DD-MM-YYYY`) and the day-month-year form with the month
spelled out, matched case-insensitively against a per-language table of
month names (full and three-letter, in the profile's declared languages
plus English); an unparseable date leaves the passage undated rather than
guessing. Nothing else in a body is parsed. A chunk cap is a bound, not a
reason to split an entry into sentences: an oversized entry is sliced at
line boundaries with all continuations labeled and neighbor links
retained; a pathological single long line needs bounded UTF-8-safe slices,
marked as partial.

Build a revision from one stable read, verify its hash, and publish its passages
and deterministic indexes by atomically changing `current_revision_id`. A file
changed during the read is retried. Identical content with the same splitter
version is a no-op. Publish lexical search without waiting for embeddings;
missing or incompatible vectors simply disable that retrieval signal.

An append creates a new revision and new occurrence IDs. Reuse expensive work
only when its complete input hash matches: source text plus any headings,
dates, language hints or other context used by that pass. A text hash alone
cannot validate an event extracted under a different heading. Matching old and
new occurrences is one-to-one; duplicate text must not collapse. Ambiguous
matches are recomputed rather than guessed. Switching a revision makes all
old occurrences ineligible for normal search immediately, even if their vector
rows have not yet been pruned.

A rename retains file identity only when reconciled unambiguously; otherwise
it is removal plus addition. A missing file is marked unavailable on a
successful scan; a failed scan must not classify the entire root as deleted.
Disabling a source immediately hides its passages and derived indexes.

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

## Layer 1 — literal, lexical and semantic retrieval

```text
diary_identifier
  passage_id, kind, raw_value, lookup_value, byte_start, byte_end
```

Index recognizable URLs, hashes, paths, symbols, issue references and model
names deterministically, plus two markers the format makes exact: a body
line starting with `/word` is a **command** to an agent, and a body starting
with `IDEA:` is an **idea**. Both are recorded here as identifiers of kind
`command` and `marker`, so Layer 2 can inherit them as decided rather than
inferred. Keep the original spelling. Normalize conservatively
by kind: paths, URL paths and symbols must not be blindly lowercased. Hash
prefixes can match several values; return the ambiguity instead of claiming a
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
classifies it reliably. An uncertain event can remain unclassified. Two
assignments are made before the model sees the entry, from Layer 1's
markers: a `/word` line is an `instruction` addressed to an agent, and an
`IDEA:` body is an `idea` addressed to self.

```text
diary_extraction_run
  id, passage_id, input_hash, extractor_version, model_digest,
  prompt_schema_hash, sampling_config, status, attempts,
  input_tokens, output_tokens, error

diary_event
  id, extraction_run_id, ordinal, kind,
  evidence_start, evidence_end, summary,
  speaker_kind, speaker_name, attribution_basis,
  addressee, addressee_basis,
  sensitivity_hint,
  occurred_start, occurred_end, time_basis

diary_event_entity
  event_id, surface_form, normalized_key, role
```

Evidence ranges must resolve inside the input passage. Have the model identify
an exact supporting quote and locate it in code; do not rely on it to count
UTF-8 bytes. Reject a quote that is absent or cannot be disambiguated. This
checks grounding of the span, not truth of the interpretation. Keep summaries
in the passage's language and in the index/inspector only. Extract no belief
from an event summary without re-reading its source span.

`speaker_kind` is `self`, `person`, `agent`, or `unknown`; `speaker_name` is
optional. Attribution needs its own support, including whether the statement
was quoted. Do not assume first person inside pasted text refers to the
operator. Speaker metadata is inferred and never grants authority.

`addressee` is `self`, `agent`, `other`, or `unknown`, with an
`addressee_basis` (`marker` when decided by a `/word` line, `inferred`
otherwise). It answers a different question from `speaker`: whose words
these are versus whom they were for. "A user asked for JSONL output" has
speaker `person`, addressee `self`; "find the two papers" has speaker
`self`, addressee `agent`; "follow up next week" is a `task` for `self`.
An `instruction` always has an addressee of `agent` or `other`. The
consequences are mechanical: an event with addressee `agent` is never
eligible for Layer 3, and retrieval presents its passage as a past request
with its recorded time (see Retrieval). Inferred addressees are soft: a
missed one costs a quote presented without the "you asked" framing, never
an instruction acted on, because the fence and system prompt already make
recalled text data.

`sensitivity_hint` is the model's reading of one entry (a health or body
note, a personal observation, a project note). Source policy sets the
sensitivity every passage inherits; the hint may only **narrow** it — an
entry the model flags as more sensitive than its source is excluded from
rooms the stricter level would exclude — and never widen it. A diary whose
source is private stays private whatever the hint says.

Distinguish three clocks:

- **Recorded time:** date/range from the filename or an explicit dated heading,
  with the basis retained; unknown stays null, never file modification time.
- **Occurrence time:** when the passage says the event happened, possibly a
  range or unknown. A recording-date fallback is labeled as such and is not an
  explicit occurrence date.
- **Ingestion time:** when RainBox observed this revision.

“You mentioned it in April” filters recorded time. “It happened in March” uses
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
run. Entries in this format are short — many are one line — so the passage
count is higher and the tokens per passage lower than the figures below;
measure on the fixture. For **illustration only**, 3,000 passages, 300
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

A decision, result or apparent subject–predicate statement is eligible for
review, not automatically useful as a durable belief. An event whose
addressee is an agent is never eligible: an instruction is not a statement
about the world, and promoting one would turn "process the queue" into a
belief the assistant later reads as standing policy. Start with operator-selected
events and a capped proposal queue; do not flood `/memory` with every observation.

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

The operator's idea from the sample, adopted as an extension after the
first retrieval milestone: the assistant keeps a diary in the same grammar,
one file per day under its own configured root, as a `diary_source` of its
own with `speaker_kind = agent` fixed for every passage. The run summarizer
already writes a digest per run; the diary entry is that digest under the
run's start time and range, in the room's language, with the run identifier
as an exact token:

```text
20270312
12h31 - 12h34
Processed the queue: three items, two done, one needs the operator
(missing token). run 9f3c…
```

It goes through the same layers and the same access rules, and both
diaries share one retrieval path — which is what makes "what did you do
while I was away on the 12th" and "when did I ask you to process the queue"
the same kind of question with two authors. The assistant's entries are
quotes of its own record; its claims about what it did are `model_inferred`
candidates like any other. `memory-architecture.md` §Directions already
wanted journal rows promoted into episodic memory; this is that, in a
format the operator can read and grep without the app.

## Retrieval and context assembly

Integrate first with the assistant's explicit `memory_query` action. Always-on
chat/profile injection is a separate scope decision; importing a diary should
not put its contents into every conversation.

```text
query + authorized source context + explicit retrieval mode
  → source access, sensitivity, exclusion and current-revision eligibility
  → bounded literal/identifier, FTS and vector candidate routes
  → time, addressee and speaker constraints (see below)
  → merge by passage occurrence; preserve match spans and retrieval reasons
  → shared relevance stage, extended to typed diary candidates
  → bounded neighbor/topic expansion; recheck eligibility for every addition
  → budgeted source excerpts with citations, alongside eligible claims/seeds
```

Use per-route candidate caps and rank-based fusion for diary routes rather than
adding unrelated similarity scales. Keep a trace of retrieved, filtered,
expanded and injected IDs, source revisions, omissions and timings. Mixed
claim/seed/diary candidate IDs must be namespaced. The initial fallback when
embedding or relevance services fail is bounded eligible literal/lexical
results, with degraded mode recorded. Failure must never bypass access checks.

Date, addressee and speaker constraints need an explicit interpretation.
Unambiguous calendar ranges — including times of day, which this format
records — are computed against the configured timezone and a captured
query time. "What did I ask you to do" filters addressee `agent`; "what
have users asked for" filters speaker `person`; both are soft unless the
query is explicit. A passage whose event has addressee `agent` is rendered
with its recorded time and the label of a past request, inside the same
fence as every other quote. An expression such as “before the rewrite” first needs evidence of
which rewrite and when; it cannot be resolved by date arithmetic alone. If an
anchor is ambiguous, present alternatives or leave it as a ranking hint.
Inferred speaker/event dates are soft filters by default so extraction mistakes
do not silently erase recall; explicit strict mode reports its limited coverage.

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
first retriever ships. Include code, prose, duplicate paragraphs, long entries
and file revisions. Keep operator text out of repository fixtures and CI output.
Each case names the query, mode, access context, fixed clock/timezone, source
revision manifest, required evidence ranges and forbidden passages. The
fixture covers both dialects (date and time lines with ranges; ChangeLog
headers with bullets), two languages including a spelled-out month name,
Terminator-universe content and author tokens, with both an operator diary
and an assistant diary.

| Family | Cases that distinguish success from a plausible-looking answer |
|---|---|
| Literal | Full/prefix hashes, ambiguous prefixes, extensionless paths, punctuation-heavy errors, absent strings |
| Semantic/language | Paraphrases, Danish/English queries, code mixed with prose, unresolved aliases |
| Time | Recorded vs occurred dates, uncertain ranges, future plans, backdated entries, ambiguous “before the rewrite” |
| History/current state | Earlier decision + reversal + stated reason; no reason; confirmed state vs newer unconfirmed mention |
| Attribution/outcomes | Quoted person vs operator, pasted agent commands, unsupported causal links, incomplete outcome coverage |
| Addressee | A `/word` line and an imperative to the agent come back as past requests, never as tasks the answer performs; a `task` for self is not an instruction; an operator goal is not an agent goal |
| Format | Both dialects in one fixture; a file whose dialect is ambiguous falls back to `plain`; ranges; an entry before the first time line; two dates in one file; a malformed time line; a time earlier than the previous entry; newest-first `changelog` days ordered by date; a bullet with three continuation lines; a month name in another language; a one-line entry; an entry that mixes two languages |
| Assistant diary | "What did you do on the 12th" answered from the assistant's file with the run identifier; the two diaries never merge speakers |
| Lifecycle | Append, duplicate text, edits, rename/removal, stale workers/vectors, empty extraction, retries, re-extraction without added support |
| Context/access | Answer near middle/end of long block, budget exhaustion, hidden neighbor/thread member, excluded citation, adversarial fence text |

Measure candidate Recall@K separately from **injected evidence coverage**: finding
a passage does not help if the scorer or budget drops its answer-bearing span.
For multi-passage questions, require all named evidence spans. Also record
citation/range correctness, forbidden-source exposure, attribution and
current-state correctness, duplicate proposal/support counts, context tokens,
latency and extraction cost. “Injected” is not proof the answer used it; score
answer behavior separately.

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
**addressee leaks** — a recalled instruction that the answer performs or
restates as a current task. An added
layer must improve its target family on held-out cases, preserve baseline-passing
regression cases, and stay within latency/token limits fixed before the run.
Publish per-family counts, failures and repeated live-run variation. Keep a
layer disabled when the evidence is too small or mixed to justify its cost.

## Order of work and stopping points

1. **Fixture and source contract:** define eligible sources, revision/range
   semantics, exclusions, the dialect registry and its fallback, splitter
   caps, lookup behavior and evaluation cases.
   This makes provenance and access testable before retrieval integration.
2. **Smallest useful retrieval:** Layer 0 + Layer 1, explicit `memory_query`
   integration, source viewer, bounded rendering and diagnostics. Ship literal
   and FTS first; add passage embeddings against baseline A. No generative
   extraction is needed. Prove append/edit/removal and service-failure behavior.
3. **Events, if recall needs them:** benchmark a local model, implement the
   versioned job ledger and evidence validation, then evaluate attribution,
   addressee and temporal metadata. Keep ordinary passage search independent
   of this pass. The two marker-decided assignments (`/word`, `IDEA:`) and
   the time filters need no model and belong to milestone 2.
4. **The assistant's diary:** write it from run digests and register it as a
   second source. Cheap once milestone 2 exists, and it gives the operator a
   readable record of the assistant's work immediately.
5. **Belief proposals, if useful:** add selected-event promotion, durable
   idempotency and source-change review. Preserve existing governance; make no
   automatic temporal supersession change.
6. **Threads, if longitudinal questions still fail:** add bounded topic/episode
   navigation and supported relations. Evaluate full-history comparisons and
   context cost before adding model-derived navigation aids.

Stop after any milestone if the next layer does not earn its complexity.
Disabling an optional layer returns to passage retrieval without losing source
citations or operator decisions. A full rollback disables the diary source;
it does not require deleting or modifying the operator's files.

## Deliberately deferred

No Datalog engine, graph database, summary-of-summary hierarchy, canonical
language, or runtime clustering is needed for the first design. PostgreSQL
relations and joins are sufficient for the proposed navigation. Offline corpus
visualization may help evaluate grouping, but does not establish truth. A
bitemporal belief model, exhaustive task tracking and always-on diary injection
are separate proposals with their own evidence and review costs.
