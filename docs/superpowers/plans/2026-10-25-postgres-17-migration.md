# PostgreSQL 16 → 17 Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to carry out this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. Every step below that stops a service, deletes a directory or edits a dotfile is the operator's to approve. Run it only on their go-ahead in chat.

**Goal:** Move the local Homebrew cluster (all databases, `rainbox_production` included) from `postgresql@16` to `postgresql@17`. The Homebrew `pgvector` bottle then provides `vector.dylib`, and diary sync, memory embedding sync and the nightly backup start working again.

**Architecture:** `pg_upgrade --copy` from the stopped 16 cluster into a freshly initialized 17 cluster. Postgres copies the data files as they are and only dumps and restores the schema. The old cluster stays untouched as the rollback. Dump and restore can't be used: `pg_dump` against the 16 server fails on every vector column, because writing out vector values needs the missing `vector.dylib`.

**Tech Stack:** Homebrew `postgresql@16` 16.15_1 (server process still runs the 16.12 binary it started with on 2026-08-14), Homebrew `postgresql@17` 17.11, Homebrew `pgvector` 0.8.7 (bottled for 17 and 18 only), psycopg 3.2.10 binary (its own bundled libpq 17, so it doesn't depend on any Homebrew keg).

**Spec:** none. This plan is the spec. The Background section below records the root cause.

## Background (as found 2026-10-25)

- The databases use extension `vector` **0.8.0**. That version was built from source against Postgres 16, as `source/README.md` describes, so the build lived inside the `postgresql@16` 16.12 keg. On 2026-10-21 at 02:19–02:22, Homebrew upgraded `postgresql@16` to 16.15_1 and `pgvector` to 0.8.7. The new keg has no `vector.dylib`, and the bottle ships it only under `lib/postgresql@17` and `lib/postgresql@18`.
- Postgres processes started before the upgrade still have the library loaded, so some queries keep working. Every new process (new connections, parallel workers, `pg_dump`) fails with `could not access file "$libdir/vector"`. That's why it looks intermittent. **Restarting the 16 server would make every vector query fail.**
- `cron_run` in `rainbox_production` shows: `Database backup` ok up to 2026-10-20 and `error` (pg_dump exit 1) every night since 2026-10-21. `Diary sync` reports "embedding unavailable", and `Memory embedding sync` ends with "no completion recorded". **The newest good encrypted backup is from 2026-10-20.**
- Cluster: ~840 MB on disk, 304 GiB free. Databases: `arcinteractive` (another project, plpgsql only), `postgres`, `rainbox` (vector), `rainbox_claude` (vector, pg_trgm), `rainbox_production` (vector, pg_trgm). All are UTF8 with collate/ctype `C` and the libc provider. Data checksums off (version 0). Roles: the install user (superuser), `postgres`, `arcinteractiveuser`.
- Vector columns: `memory_embedding.embedding`, `data_seed_memory.embedding`, `diary_embedding.embedding`. `rainbox_claude` has one hnsw index, `ix_diary_embedding_hnsw`, which `pg_upgrade` copies as it is. Printing its definition during the schema dump needs pgvector loaded in the **old** server, and `pg_upgrade --check` doesn't test that. So when the old server lacks pgvector, build it for the old major first (`source/README.md`).
- Config: `postgresql.conf` holds only the values initdb generated, `postgresql.auto.conf` is empty, and `pg_hba.conf` is stock trust on local, 127.0.0.1 and ::1. A plain initdb reproduces all of it.
- Clients: the Rainbox launcher (`RainBox Launcher main.py` → `RainBox Core core.py`), started from an interactive zsh. `pg_dump` is found on PATH through `~/.zshrc` line 39 (`export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"`). No `PG_DUMP` override exists in `.env` or `app_setting`.

## Global Constraints

- Every `psql`/`pg_*` command names its binary by full path (`$PG16`/`$PG17` below) and its database by name. Never rely on PATH or a bare default during the migration ([[database-url-on-every-db-command]]).
- Set this in every shell used for the migration (the counts function takes a bin dir and a database, and prints `schema.table|rows` per table):

```bash
export PG16=/opt/homebrew/opt/postgresql@16/bin PG17=/opt/homebrew/opt/postgresql@17/bin WORK="$HOME/pg-migration-2026-10-25"
counts() { printf '%s\n' "SELECT format('SELECT %L, count(*) FROM %I.%I', schemaname||'.'||relname, schemaname, relname) FROM pg_stat_user_tables ORDER BY 1 \gexec" | "$1/psql" -d "$2" -At; }
```
- The old data dir `/opt/homebrew/var/postgresql@16` is never modified or deleted by this plan. Cleanup is a separate, later decision (Task 6).
- The new cluster must match the old one: `--locale=C --encoding=UTF8 --locale-provider=libc`, no `--data-checksums`, bootstrap superuser = `$(whoami)`.
- Only one server runs on port 5432 at a time.
- No retroactive data changes. Records missed while things were broken (e.g. nights without a backup) stay missed ([[no-retroactive-data-changes]]).

## Review Focus

1. **The 16 server won't stop because Rainbox still holds connections** (`brew services stop` uses smart shutdown, which waits for clients). Expected: Task 2 checks `pg_stat_activity` for zero client backends before stopping. Task 2 Step 3 verifies `Database cluster state: shut down`.
2. **The backup still calls `pg_dump` 16 against server 17** and fails with "server version mismatch", because the launcher kept the old PATH. Expected: Task 4 fixes `~/.zshrc`, restarts the launcher from a *new* shell, and fires `Database backup` by hand.
3. **Homebrew's post-install already created `/opt/homebrew/var/postgresql@17`** with settings that don't match the old cluster. Expected: Task 3 Step 2 inspects it and re-initializes only if it holds no user databases.
4. **Row loss or truncation during the upgrade.** Expected: exact per-table row counts taken before (Task 1) and after (Task 4) are identical for every database.
5. **The extension catalog still says vector 0.8.0 while the library is 0.8.7.** Expected: Task 4 runs `ALTER EXTENSION vector UPDATE` in each vector database and confirms `0.8.7`.

---

### Task 1: Snapshot facts and stop Rainbox

**Files:** none in the repo. The working dir is `$WORK`.

- [ ] **Step 1: Create the working dir and record exact row counts while 16 is still up**

```bash
mkdir -p "$WORK" && cd "$WORK"
for d in arcinteractive rainbox rainbox_claude rainbox_production; do counts $PG16 "$d" > "counts-before-$d.txt"; done
wc -l counts-before-*.txt
```

Expected: one file per database, each non-empty. `count(*)` never writes out vector values, so it works without the library.

- [ ] **Step 2: Quit the Rainbox launcher** (Ctrl-C in the terminal that runs `main.py`, or however the operator normally stops it)

Run: `pgrep -fl 'RainBox (Launcher|Core)'`
Expected: no output.

- [ ] **Step 3: Confirm no client is connected to 16**

Run: `$PG16/psql -d postgres -At -c "SELECT datname, application_name FROM pg_stat_activity WHERE backend_type='client backend' AND pid <> pg_backend_pid()"`
Expected: no rows. If the arcinteractive app or anything else shows up, stop it first.

### Task 2: Stop 16 cleanly and take a cold copy

- [ ] **Step 1: Stop the 16 service** (this also unloads its launchd agent, so it won't respawn)

Run: `brew services stop postgresql@16`

- [ ] **Step 2: Verify it's down**

Run: `$PG16/pg_ctl status -D /opt/homebrew/var/postgresql@16`
Expected: `pg_ctl: no server running`

- [ ] **Step 3: Verify the shutdown was clean**

Run: `$PG16/pg_controldata /opt/homebrew/var/postgresql@16 | grep 'cluster state'`
Expected: `Database cluster state:               shut down`

- [ ] **Step 4: Cold copy as an extra rollback** (APFS clone, instant, takes almost no extra space)

Run: `cp -cRp /opt/homebrew/var/postgresql@16 "$WORK/postgresql@16-cold"`

### Task 3: Install 17 and upgrade

- [ ] **Step 1: Install**

Run: `brew install postgresql@17 && ls /opt/homebrew/lib/postgresql@17/vector.dylib`
Expected: the dylib path is printed (pgvector's own keg links it there).

- [ ] **Step 2: Get a matching, empty 17 cluster**

If `/opt/homebrew/var/postgresql@17` exists (Homebrew's post-install may create one), check it's brand new: `$PG17/pg_controldata /opt/homebrew/var/postgresql@17 | grep -E 'checksum|state'` should show checksum version 0. Then start it briefly with `$PG17/pg_ctl -D /opt/homebrew/var/postgresql@17 -l "$WORK/pg17-probe.log" start` and run `$PG17/psql -d postgres -At -c "SELECT datname, datcollate, datctype, datlocprovider FROM pg_database"`. Expected: only `postgres`, `template0`, `template1`, all `C|C|c`. Stop it again with `$PG17/pg_ctl -D /opt/homebrew/var/postgresql@17 stop`.

If it shows anything else (other databases, non-C locale, checksums on), stop and show the operator before removing it. Once they approve, recreate it:

```bash
rm -rf /opt/homebrew/var/postgresql@17
$PG17/initdb -D /opt/homebrew/var/postgresql@17 -U "$(whoami)" --locale=C --encoding=UTF8 --locale-provider=libc
```

If the directory doesn't exist, run only the `initdb` line.

- [ ] **Step 3: Dry run**

```bash
cd "$WORK"
$PG17/pg_upgrade --old-bindir $PG16 --new-bindir $PG17 \
  --old-datadir /opt/homebrew/var/postgresql@16 --new-datadir /opt/homebrew/var/postgresql@17 \
  --username "$(whoami)" --copy --check
```

Expected: ends with `*Clusters are compatible*`. Of the failures that can happen here, only "Checking for presence of required libraries" points at pgvector. That would mean `$libdir/vector` doesn't load under 17, so confirm Step 1's dylib exists before doing anything else. On a locale or encoding mismatch, redo Step 2's `initdb`.

- [ ] **Step 4: Real run** (same command without `--check`)

Expected: ends with `Upgrade Complete`. Keep the log directory it names (inside the new data dir under `pg_upgrade_output.d`) until Task 6.

### Task 4: Bring 17 up and verify

**Files:**
- Modify: `~/.zshrc:39` (operator's dotfile, outside the repo)

- [ ] **Step 1: Start 17 as the service**

Run: `brew services start postgresql@17 && $PG17/psql -d postgres -At -c 'SHOW server_version'`
Expected: `17.11` (or whatever 17.x was installed)

- [ ] **Step 2: The library loads in a fresh process**

Run: `$PG17/psql -d rainbox_production -At -c "LOAD 'vector'" && echo ok`
Expected: `ok`

- [ ] **Step 3: Update the extension catalogs**

```bash
for d in rainbox rainbox_claude rainbox_production; do
  $PG17/psql -d "$d" -At -c 'ALTER EXTENSION vector UPDATE' -c "SELECT extversion FROM pg_extension WHERE extname='vector'"
done
```

Expected: `0.8.7` three times. If pg_upgrade wrote `update_extensions.sql` into `$WORK`, run it with `$PG17/psql -d postgres -f update_extensions.sql` as well.

- [ ] **Step 4: Rebuild planner statistics** (pg_upgrade 17 doesn't carry them over)

Run: `$PG17/vacuumdb --all --analyze-in-stages`

- [ ] **Step 5: Row counts match**

```bash
cd "$WORK"
for d in arcinteractive rainbox rainbox_claude rainbox_production; do
  counts $PG17 "$d" > "counts-after-$d.txt"
  diff -q "counts-before-$d.txt" "counts-after-$d.txt" && echo "$d same"
done
```

Expected: `<db> same` four times.

- [ ] **Step 6: Vector values can be read and written out again**

Run: `$PG17/pg_dump -d rainbox_production --data-only -t memory_embedding -t diary_embedding -f /dev/null && echo ok`
Expected: `ok` (this is the exact command that failed on 16)

- [ ] **Step 7: Point the shell at 17**

Change `~/.zshrc` line 39 to `export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"`. In a **new** terminal: `which pg_dump` → `/opt/homebrew/opt/postgresql@17/bin/pg_dump`.

- [ ] **Step 8: Start Rainbox from that new terminal and fire the three broken jobs by hand**

Start the launcher the usual way, then on `/cron` fire `Database backup`, `Diary sync` and `Memory embedding sync`.
Expected: all three runs end `ok` in `cron_run`. A `Database backup` that ends `ok` is the first good encrypted backup since 2026-10-20. Also open `/chat` and run one diary-backed question to check search end to end.

### Task 5: Update docs and memory

**Files:**
- Modify: `source/notes/backup.md:194` (the `PG_DUMP` example path → `postgresql@17`)
- Create: memory `postgres-17-cluster.md` + its `MEMORY.md` line (outside the repo)

- [ ] **Step 1:** Change the example in `source/notes/backup.md:194` to `export PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump`. `source/README.md`'s "build from source for older majors" section stays: it describes the general rule, not this machine.
- [ ] **Step 2:** Write the memory: production is on Homebrew `postgresql@17` with the bottled pgvector. The bottle covers only the two newest majors, so when Postgres 19 ships the bottle will drop 17 and this same breakage comes back. Plan the next major upgrade before running `brew upgrade` at that point.
- [ ] **Step 3: Commit** (sweep for PII per the commit workflow first)

```bash
git add source/notes/backup.md
git commit -m "docs(backup): PG_DUMP example points at postgresql@17"
```

### Task 6: Cleanup (separate decision, not before a week of good nightly backups)

Only on the operator's explicit go-ahead in chat, each item on its own:

- [ ] `brew uninstall postgresql@16`
- [ ] `rm -rf /opt/homebrew/var/postgresql@16 "$WORK/postgresql@16-cold"` (pg_upgrade's `delete_old_cluster.sh` covers the first path)
- [ ] `rm -rf "$WORK"`

## Rollback (any point before Task 6)

`brew services stop postgresql@17`, `brew services start postgresql@16`, revert `~/.zshrc:39`, then restart the launcher from a new shell. This brings back the state before the migration, which still has the missing-pgvector breakage. To get a *working* 16 you'd also need to build pgvector 0.8.0 from source against `$PG16/pg_config`, following `source/README.md`. If the 16 data dir is ever in doubt, `$WORK/postgresql@16-cold` is the clean copy from right after shutdown.
