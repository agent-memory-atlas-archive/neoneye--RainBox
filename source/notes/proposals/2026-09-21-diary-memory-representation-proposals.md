# Representing a Long-Running Diary for AI Memory

## Purpose

This document proposes three ways to represent a long-running, multilingual diary so that an AI assistant can retrieve relevant prior information without relying only on a short rolling chat window.

The diary is assumed to contain heterogeneous material such as:

- personal observations
- project notes
- technical debugging
- commands sent to AI agents
- external user feedback
- ideas and hypotheses
- tasks and follow-ups
- URLs, commit hashes, code symbols, and error messages
- English, Spanish, and potentially mixed-language passages

All examples below are synthetic. No real diary content is used.

The central design principle is:

> Preserve the original diary as immutable source material, and derive additional representations from it rather than replacing it with a single normalized form.

A useful memory system should support both of these questions:

- “Find the exact error message I recorded.”
- “Why did I change this design three months later?”

Those are different retrieval problems and should not be forced through the same representation.

---

# Proposal 1 — Temporal Event Store with Typed Records

## Summary

Treat the diary as an append-only event stream.

Each diary entry is segmented into atomic events, and every event receives structured metadata. PostgreSQL remains the canonical store.

This is the most conservative and practical architecture.

## Core model

Keep the source text unchanged:

```sql
diary_source (
    id                  uuid primary key,
    source_date         date,
    sequence_no         integer,
    raw_text            text,
    created_at          timestamptz
);
```

Extract zero or more events from each source block:

```sql
memory_event (
    id                  uuid primary key,
    source_id           uuid references diary_source(id),

    occurred_at         timestamptz,
    event_kind          text,
    language            text,

    author_actor_id     uuid null,
    target_actor_id     uuid null,

    project_id          uuid null,
    thread_id           uuid null,

    title               text null,
    normalized_summary  text null,

    confidence          real,
    extraction_version  text
);
```

Typical `event_kind` values:

```text
observation
task
idea
decision
requirement
agent_command
external_feedback
feature_request
bug_report
diagnostic
experiment
result
resource
state_change
commitment
follow_up
question
```

Actors are separate entities:

```sql
actor (
    id            uuid primary key,
    kind          text,
    display_name  text,
    aliases       jsonb
);
```

Example actor kinds:

```text
self
person
project_user
ai_agent
organization
unknown
```

This matters because:

```text
A user requested X
```

is different from:

```text
I decided X
```

and both are different from:

```text
An AI assistant suggested X
```

## Preserve exact technical evidence

Developer diaries often contain information that semantic embeddings should not normalize:

```text
FooBarConfig
_prepare_client()
7f0a1b4...
TypeError: unexpected keyword argument
/api/models
```

Store these separately:

```sql
memory_identifier (
    event_id       uuid,
    identifier     text,
    identifier_kind text
);
```

Possible kinds:

```text
code_symbol
commit_hash
url
route
filename
error_signature
model_name
issue_id
```

Exact search should be preferred for these fields.

## Model state changes explicitly

An event should be able to supersede prior state.

Example:

```text
2026-01: backend = Redis
2026-04: backend = PostgreSQL
```

Store that as temporal state:

```sql
memory_fact (
    id              uuid primary key,
    subject_id      uuid,
    predicate       text,
    object_value    jsonb,

    valid_from      timestamptz,
    valid_to        timestamptz null,

    epistemic_status text,
    source_event_id uuid,
    superseded_by   uuid null
);
```

This lets the assistant answer:

```text
What do I use now?
```

rather than returning both historical states as if they were simultaneously true.

## Epistemic status

Do not treat all extracted statements as facts.

Useful values:

```text
observed
reported_by_other
hypothesized
inferred
confirmed
disproved
superseded
```

For example:

```text
"Service X crashed because of Y"
```

may initially be a hypothesis and later be confirmed or disproved.

## Retrieval

Use several retrieval routes:

1. exact identifier lookup
2. PostgreSQL full-text search
3. vector similarity
4. temporal filtering
5. actor filtering
6. project/thread filtering
7. structured SQL queries
8. reranking over the merged candidate set

Conceptually:

```text
query
  |
  +--> exact symbols
  +--> lexical search
  +--> embeddings
  +--> structured filters
  +--> time constraints
  |
candidate union
  |
reranker
  |
context builder
```

## Strengths

- simple mental model
- works well with PostgreSQL
- provenance is straightforward
- excellent for timeline queries
- supports exact technical retrieval
- easy to inspect and debug
- derived representations can be regenerated later

## Weaknesses

- entity resolution can become messy
- high-level narratives are not represented naturally
- multi-step causal questions require additional logic
- many small events can create retrieval noise

## Best fit

Choose this if the first priority is:

> Build something reliable, inspectable, and difficult to corrupt.

For a first serious implementation, this is the strongest foundation.

---

# Proposal 2 — Temporal Knowledge Graph / Datalog Projection

## Summary

Represent extracted events and facts as a graph of actors, projects, concepts, events, artifacts, decisions, and relationships.

The raw diary remains immutable, but a graph becomes the primary reasoning layer.

This proposal is especially useful for questions involving relationships, history, causality, commitments, and multi-hop retrieval.

## Example graph

Synthetic diary fragment:

```text
09:10
A project user requested JSONL export.

10:20
I decided to investigate it next week.

Two weeks later
Implemented JSONL export in commit abc123.
```

Possible graph:

```text
User_A
  |
  | requested
  v
Feature_JSONL
  |
  | creates
  v
Commitment_17
  |
  | assigned_to
  v
Self

Commit_abc123
  |
  | implements
  v
Feature_JSONL

Commitment_17
  |
  | resolved_by
  v
Commit_abc123
```

Now the assistant can answer:

```text
Which user requests are still unresolved?
```

without semantic search.

## Datalog representation

A derived Datalog database could contain:

```prolog
requested(user_a, jsonl_export, event_17).
committed_to(self, investigate(jsonl_export), event_18).
implemented(commit_abc123, jsonl_export).
resolves(commit_abc123, event_18).
```

Rules can derive:

```prolog
open_commitment(C) :-
    commitment(C),
    not resolved(C).
```

Or:

```prolog
follow_up_needed(User, Request) :-
    requested(User, Request, _),
    committed_to(self, Request, _),
    not resolved_request(Request).
```

## Why Datalog is attractive

Compared with unrestricted Prolog, Datalog is a better fit for memory experiments because it is usually:

- finite
- declarative
- easier to reason about
- well suited to joins and rules
- less dependent on procedural evaluation order

The diary should not be stored only as Datalog.

Instead:

```text
raw diary
   |
structured events
   |
canonical PostgreSQL representation
   |
generated Datalog facts
```

The Datalog layer should be disposable and regeneratable.

## Temporal relationships

Use explicit relations:

```text
precedes
follows
supersedes
contradicts
caused_by
resolved_by
elaborates
reopens
depends_on
reported_by
requested_by
affects
```

This is particularly valuable for debugging episodes.

Example:

```text
Bug_Report
   |
   +--> caused_by --> ConfigOverride
   |
   +--> investigated_by --> Experiment_4
   |
   +--> resolved_by --> Commit_abc123
```

## Threads and episodes

A graph makes it natural to represent an episode:

```text
DebuggingEpisode_42
  |
  +-- symptom
  +-- hypothesis
  +-- failed experiment
  +-- new observation
  +-- decision
  +-- fix
  +-- regression
```

That gives the assistant something much closer to human episodic recall than isolated chunks.

## Retrieval

A query can use hybrid graph expansion:

```text
user query
   |
semantic seed retrieval
   |
matching entities/events
   |
1-2 graph hops
   |
candidate subgraph
   |
rerank
```

Example:

```text
"Why did I stop using feature X?"
```

could resolve:

```text
feature X
  -> related decision
  -> preceding failed experiments
  -> diagnostic evidence
  -> replacement design
```

## Strengths

- excellent multi-hop retrieval
- strong provenance
- good representation of commitments and follow-ups
- supports temporal reasoning
- natural fit for Datalog experiments
- useful for “why?” and “what led to?” questions
- can expose unresolved relationships directly

## Weaknesses

- entity resolution becomes critical
- extraction mistakes can create misleading graph edges
- schema design can expand without bound
- graph databases are not automatically better than PostgreSQL
- requires careful handling of uncertainty

## Best practice

Do not attempt to build an ontology for everything.

Prefer a small vocabulary of stable relationships and allow free-form tags for the long tail.

For example:

```text
requested_by
reported_by
supersedes
contradicts
resolved_by
caused_by
related_to
part_of
```

is better than inventing hundreds of predicates immediately.

## Best fit

Choose this if the research question is:

> Can symbolic structure make long-term recall more reliable than semantic similarity alone?

This is the most interesting proposal for Datalog/Prolog experimentation.

---

# Proposal 3 — Hierarchical Semantic Memory with Consolidated “Memory Objects”

## Summary

Instead of primarily storing individual facts, periodically consolidate diary events into durable higher-level memory objects.

Examples:

```text
project state
person dossier
research thread
debugging episode
decision history
open commitments
monthly summary
current beliefs
```

This is closest to how an assistant might maintain a usable long-term working memory.

## Hierarchy

The diary naturally supports multiple resolutions:

```text
raw entry
   |
atomic event
   |
daily summary
   |
thread summary
   |
monthly summary
   |
long-running memory object
```

Example synthetic research thread:

```text
Topic: alternative retrieval methods

Started:
2026-02

Current state:
Actively comparing symbolic, vector, and graph approaches.

Investigated:
- method A
- method B
- method C

Current hypothesis:
Hybrid retrieval appears more robust than a single index.

Open questions:
- cross-language retrieval
- temporal supersession
- duplicate memories

Important artifacts:
- note-a.md
- experiment-results.csv
```

That object is updated over time.

## Memory object types

Useful object types might include:

```text
PersonMemory
ProjectMemory
ResearchThread
DebuggingEpisode
DecisionRecord
OpenCommitment
KnowledgeState
PreferenceState
ArtifactHistory
```

Example:

```yaml
type: ResearchThread
id: retrieval-research

title: Retrieval architecture experiments

status: active

summary:
  Investigating several representations for long-term assistant memory.

questions:
  - How much does semantic retrieval miss?
  - Can symbolic retrieval improve precision?
  - How should superseded state be represented?

related_entities:
  - postgres
  - vector_search
  - datalog

sources:
  - event:123
  - event:175
  - event:220

last_updated: 2026-09-21
```

## Consolidation

A consolidation pass compares new memories with existing objects:

```text
new event
   |
find related memory objects
   |
   +-- duplicate -----> reinforce
   +-- elaboration ---> expand
   +-- contradiction -> preserve + mark conflict
   +-- state change --> supersede
   +-- continuation --> append
   +-- new topic -----> create object
```

The original source is never deleted.

## Retrieval

The assistant first retrieves high-level objects:

```text
query
   |
memory objects
   |
relevant episodes/threads
   |
supporting raw evidence
```

This prevents the system from filling context with twenty nearly identical diary chunks.

A good context builder might include:

```text
1 current-state object
2 relevant thread summaries
3-5 supporting raw events
```

instead of:

```text
top 20 embedding matches
```

## Multilingual handling

Keep original text in its original language.

Derived memory objects may use a configurable canonical language, but concepts and identifiers should be language-independent where possible.

For retrieval, evaluate:

```text
English query -> English source
Spanish query -> Spanish source
English query -> Spanish source
Spanish query -> English source
mixed query   -> mixed source
```

A multilingual embedding model can help, but structured entities and concepts should not depend on translation.

## Clustering and UMAP

UMAP is useful in this architecture as an exploratory layer:

```text
embeddings
   |
UMAP
   |
HDBSCAN
   |
candidate clusters
   |
LLM labels / summaries
```

Good uses:

- discover major themes
- inspect whether expected topics cluster
- find outliers
- identify candidate thread boundaries
- debug the embedding space

Less suitable uses:

- determining current truth
- deciding whether one state supersedes another
- resolving commitments
- representing causal history

UMAP should be treated primarily as an analysis and routing tool, not as the canonical memory representation.

## Strengths

- produces compact context
- resembles durable human memory
- reduces duplicate retrieval
- good conversational continuity
- excellent for long-running projects and research threads
- supports progressive summarization

## Weaknesses

- consolidation can hallucinate
- summaries can erase important nuance
- difficult to know when to update versus create
- requires rigorous provenance
- stale summaries can become dangerous

## Best fit

Choose this if the main goal is:

> Make conversations feel continuous over months or years.

This architecture is the most likely to reduce the subjective feeling of amnesia.

---

# Recommended Best Practice — Hybrid Architecture

None of the three proposals should be used alone.

The strongest design is:

```text
                    immutable diary
                          |
                          v
                 deterministic parser
                          |
              +-----------+-----------+
              |                       |
              v                       v
      exact technical data      LLM extraction
      URLs, hashes, symbols     events, actors,
      timestamps, commands      relationships
              |                       |
              +-----------+-----------+
                          |
                          v
                  temporal event store
                          |
              +-----------+------------+
              |            |            |
              v            v            v
           vectors      graph facts   summaries
           + FTS         / Datalog     / memory
                                       objects
              \            |            /
               \           |           /
                +----------+----------+
                           |
                    retrieval router
                           |
                 candidate aggregation
                           |
                       reranker
                           |
                    context builder
```

The canonical source of truth should be:

```text
1. immutable source text
2. structured events with provenance
```

Everything else should be rebuildable.

That includes:

- embeddings
- Datalog facts
- graph edges
- summaries
- UMAP coordinates
- clusters
- entity dossiers

If an extraction model improves later, the derived layers can be regenerated.

---

# Recommended PostgreSQL Layout

A practical starting point:

```text
diary_source
memory_event
actor
entity
event_entity
memory_fact
memory_relation
memory_identifier
memory_embedding
memory_thread
thread_event
memory_object
memory_object_source
```

Do not start with all of them.

A minimal first experiment could use:

```text
diary_source
memory_event
entity
event_entity
memory_embedding
memory_relation
```

Then add memory objects after observing actual retrieval failures.

---

# Retrieval Best Practices

## 1. Route before retrieving

Classify the query first.

Examples:

```text
exact technical lookup
temporal question
person/entity question
project question
diagnostic question
open commitment
semantic recall
```

Different query types should use different indexes.

## 2. Prefer exact retrieval for exact artifacts

If the query contains:

```text
commit hash
filename
function name
class name
error message
URL
issue number
```

search exact and lexical indexes before embeddings.

## 3. Use embeddings for semantic recall, not truth

Embeddings are good for:

```text
"that experiment about model memory"
```

They are poor at determining:

```text
what is currently true?
which value superseded another?
was this fixed?
who said this?
```

## 4. Preserve provenance

Every derived claim should be traceable to one or more source passages.

Never allow:

```text
summary -> summary -> summary -> fact
```

without retaining source references.

## 5. Represent uncertainty

Use confidence and epistemic state.

A memory system should know the difference between:

```text
observed
claimed
suspected
confirmed
disproved
superseded
```

## 6. Keep actors separate

Do not collapse:

```text
user statement
external user's statement
AI-generated suggestion
quoted web content
```

into a single pool of asserted facts.

## 7. Store current state separately from history

Keep both:

```text
historical events
current projection
```

The historical log answers:

```text
What happened?
```

The projection answers:

```text
What is true now?
```

---

# Experimental Plan

The goal should not be to choose an architecture by intuition.

Build a small benchmark from real questions you would naturally ask your assistant.

Do not include sensitive content in public test fixtures.

## Query categories

Create questions across:

```text
exact lookup
semantic recall
temporal recall
cross-language retrieval
state changes
debugging history
external feedback
open follow-ups
decision rationale
multi-hop relationships
```

## Compare systems

Run the same benchmark against:

```text
A. vector search only
B. vector + reranker
C. vector + FTS
D. event store + hybrid retrieval
E. event store + Datalog
F. event store + memory objects
G. full hybrid
```

## Metrics

Useful metrics:

```text
Recall@5
Recall@10
MRR
irrelevant-context rate
duplicate-context rate
answerability after retrieval
provenance correctness
current-state correctness
```

The last two matter more than standard retrieval metrics for a memory system.

A system that retrieves the historical value perfectly but answers with obsolete state is still failing.

---

# Recommended Order of Implementation

## Phase 1 — Establish the event model

Implement:

```text
immutable source
timestamps
event kinds
actors
entities
exact identifiers
multilingual embeddings
FTS
```

## Phase 2 — Add temporal state

Implement:

```text
supersedes
contradicts
resolved_by
current-state projections
```

## Phase 3 — Add Datalog

Generate a small Datalog projection and test relational queries.

Avoid trying to encode the entire diary ontology.

## Phase 4 — Add memory objects

Introduce:

```text
research threads
debugging episodes
project state
open commitments
```

Measure whether conversational continuity improves.

## Phase 5 — Explore UMAP/clustering

Use it to understand the corpus and discover themes.

Do not make it responsible for correctness.

---

# Overall Recommendation

Use **Proposal 1 as the foundation**, **Proposal 2 as the reasoning experiment**, and **Proposal 3 as the conversational memory layer**.

In shorthand:

```text
Event store = truth and provenance
Graph/Datalog = relationships and reasoning
Memory objects = compression and continuity
Vectors/FTS = discovery
UMAP = exploration
```

The most important best practice is not a particular database or algorithm.

It is to avoid having a single representation of memory.

A long-running diary contains exact strings, events, changing state, relationships, commitments, observations, external statements, technical evidence, and high-level narratives. Each of those benefits from a different representation.

The original diary should remain immutable.

Everything else should be a derived, versioned, rebuildable projection.
