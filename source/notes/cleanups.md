# Cleanups

Known-but-deferred work: things that are wrong, stale, or untidy, and were
left that way deliberately rather than missed. Each entry says where it is and
why it was deferred, so picking one up does not start with an investigation.

This is a backlog, not a design doc. Delete an entry when it is done.

## Open

### `db.db.engine` and friends keep the doubled spelling

Seven occurrences: `db.db.engine` (3), `db.db.select` (3), `db.db.Model` (1).
`db.session` exists as a facade alias and `db.db.session` is gone, but
`engine` cannot have the same one — it is a property that requires an
application context, so binding it at import raises, whereas `session` is a
`scoped_session` registry proxy that resolves per context on each access.

Fixing `engine` means a function (`db.engine()`) or a module `__getattr__`,
either of which changes the call shape rather than just the name. Seven
call sites do not justify that yet. `select` and `Model` are one-liners
whenever someone wants them.

### A first turn that names a language is labelled `named_language`

`agents/response_language_gate.py` — the name check runs before the
no-previous check, so a room's very first message naming a language records
`trigger: "named_language"` where `no_previous` is equally true. Both ask, so
behaviour is right and only the trace label is arguable. The ordering is
deliberate — the name check is cheapest and independent of everything else —
so this is a labelling question, not a logic one.

### A failed backup upload leaves the cron run `ok`

`db/cron.py`, the `backup` action — an upload failure posts
`✖ upload failed: …` to the cron room but the run still records `ok`, so the
`/cron` health column stays green while nothing reaches the remote. GitHub
rejected every push from 2026-10-16 to 2026-10-25 (files over 100 MiB) with
the health column green throughout; the operator caught it from the push
errors, not from `/cron`. A failing `pg_dump` (2026-10-21 to 2026-10-25, see
the next entry) did turn the run `error`, but showed up nowhere else. The upload is deliberately not a failure of the fire (the local backup
did succeed), so the fix is a third state — e.g. `degraded`, or `ok` plus a
recorded upload error the health column shows — not flipping it to `error`.
Deferred because it touches the `cron_run` status vocabulary every page reads.

### Nothing notices when Postgres loses the `vector` library

Homebrew's `pgvector` bottle ships only for the two newest Postgres majors.
A `brew upgrade` of the Postgres keg we run deletes a source-built
`vector.dylib`, and from then on every *new* backend fails on any vector
column — while old pooled connections keep working, so it looks intermittent.
On 2026-10-21 that silently broke the nightly backup and Diary sync's
embedding step for four days (Memory embedding sync stopped recording a
completion on the same nights, most likely the same cause). A cheap guard: a health probe
that runs `LOAD 'vector'` on a fresh connection (or the startup check doing
so) and says loudly what is wrong. Deferred: one occurrence, and the cluster
now runs Postgres 17 with the bottled pgvector (see the next entry for when
this returns).

### The next Postgres major upgrade will hit the same traps

The pgvector bottle will drop Postgres 17 once Postgres 19 ships, so plan
the 17 → 19 move before letting `brew upgrade` touch either. The 16 → 17
migration (plan:
`docs/superpowers/plans/2026-10-25-postgres-17-migration.md`, ledger in
`~/pg-migration-2026-10-25/progress.md`) found four traps the plan did not
foresee:

- `pg_upgrade --check` passes while the real run fails: dumping the schema of
  an hnsw index (`rainbox_claude` has `ix_diary_embedding_hnsw`) needs
  pgvector loadable in the **old** server. Build pgvector for the old major
  first if the bottle no longer covers it.
- Homebrew's post-install `initdb` uses `--locale=en_US.UTF-8`; these clusters
  are `C`, so `pg_upgrade` needs a cluster re-made with
  `--locale=C --encoding=UTF8 --locale-provider=libc`.
- A server started by hand (`pg_ctl`, and the servers `pg_upgrade` starts)
  needs `LC_ALL=en_US.UTF-8` in the environment on macOS, or it exits with
  "postmaster became multithreaded during startup". The launchd plist sets it.
- `pg_dump` comes from the launcher's inherited `PATH` (`~/.zshrc`, a symlink
  into the dotfiles repo). A launcher started from a shell opened before the
  PATH change keeps the old `pg_dump`, which refuses a newer server; restart
  the launcher from a fresh shell after the upgrade.

### `test_cron_tick_global_pause` fails on the night clocks go back

`db/test_cron_firing.py` compares `job.next_run_at` (read back in
`Europe/Copenhagen`) with the UTC value it stored. In the repeated hour of the
autumn DST change the read-back value has `fold=1`, and Python never treats an
ambiguous fold-1 time as equal to a time in another zone, though it is the same
instant. Observed 2026-10-25 between 02:00 and 03:00 local; the test is fine
the rest of the year. Fix: compare `.timestamp()` or normalize both to UTC.

### Deduplicated backups store whole tables, not new rows

`backup/store.py` deduplicates per pg_dump file, i.e. per table. A table that
gains one row is stored again in full that night. In practice that is
`chat_message` (~0.8 MB compressed) on every night, because the cron room
posts each backup's result lines, plus `assistant_step` and `llm_call` on
nights the assistant was used — a quiet night is about 1 MB, an active one a
few MB, and a night after a diary update adds ~13 MB once. Splitting
append-only tables into fixed id ranges would make those nights tiny too, but
needs a dump format other than one pg_dump file per table. Deferred: ~1–4 MB a
night is fine for now.

### The service venvs have no pytest, so their documented test runs fail

`notes/testing.md` ("Service suites") runs each standalone service's tests
with `cd <service> && venv/bin/python -m pytest -q`, but none of the six
service venvs (`reranker`, `voice_stt_whisper`, `voice_tts_kokoro`,
`voice_tts_dotstts`, `discord_service`, `telegram_service`) has pytest
installed, so every one of those commands fails with "No module named
pytest". Fix: add pytest to each service's dev requirements (or have the
doc install it). Found 2026-10-25; the services themselves start and answer
real requests.

### There is no backup prune tool

Parts are shared between backups (a manifest names parts written on many
earlier nights), so deleting a backup's part directory by hand can break
every later manifest. The rule is in `notes/backup.md` ("File layout"):
delete manifests first, then only the parts no remaining manifest names. A
tool would decrypt the remaining manifests (that needs the offline identity),
collect the parts they name, and delete the rest — and pruning git history
on top of that means a rewrite plus force-push of the backup repo. Deferred
until the backup repo's size matters.

## Not cleanups: decisions waiting on the operator

**Postgres 16 leftovers.** After the 2026-10-25 migration to Postgres 17
these are still on disk, kept as the rollback: the old cluster
(`/opt/homebrew/var/postgresql@16`, ~840 MB), its cold copy
(`~/pg-migration-2026-10-25/postgresql@16-cold`), the `postgresql@16` keg
with a source-built pgvector 0.8.0 installed into it, and the migration's
working directory. The plan's Task 6 removes them (`brew uninstall
postgresql@16`, then delete the two data directories and
`~/pg-migration-2026-10-25`) once a week of nightly backups has reached GitHub.

**Local-only full backups from 2026-10-16 to 2026-10-25.** The six backups
GitHub rejected (137–155 MB each, embeddings included) are in
`~/rainbox-backup-local-only/rainbox_database/2026-10-xx/`, outside any repo,
on this machine only. They are the only backups of those days; GitHub's
nearest are 2026-10-15 and the deduplicated ones from 2026-10-25 on. Keep,
copy somewhere off-machine, or delete.

**A retention policy for the backup repo.** Deduplication slowed the growth
but git still keeps every part forever. Something like daily for two weeks,
then weekly, then monthly would bound it; it needs the prune tool above.

The response-language gate ships behind `assistant.response_language_gate`,
default off. Two things should happen before it is judged, both described at
the end of
`docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md`:
bind `assistant.response_language_classifier` to a small model on
`/agentmodel` (it resolves to `assistant.default`, which is why an 81-token
answer costs 11.4s), then turn the switch on and read some runs.
