# Migrating testboard from SQLite to MariaDB

**Audience.** Two people, doing different jobs:

- **The DBA / whoever holds the MariaDB root password.** They run §A once, from
  a script you hand them. They need no knowledge of testboard.
- **You, the operator.** You run everything else with an unprivileged account.
  **This document assumes you have never administered MariaDB.** Every command
  says what it does and what a correct answer looks like.

Nothing here requires you to have, or to give anyone else, the root password.

---

## Before you start: what this buys, and what it doesn't

Worth being straight about, once, so the effort is spent knowingly.

**MariaDB gives you:** concurrent writers without file-level locking; backup,
replication and monitoring owned by the database rather than by a file on a
share; a database that lives on a server instead of on a network mount; and
alignment with however the rest of the estate is run. Those are good reasons and
they are why organisations make this move.

**MariaDB does not, by itself, make queries faster at this size.** 900 MB and
~4.4M rows is small. The several-second page loads you saw were diagnosed as a
slow network mount *plus* a page cache that never warmed, because the server
started a thread — and therefore a fresh SQLite connection, and therefore an
empty cache — for every single request. That second cause is fixed
(`tests/test_server_pool.py`), and **the fix has not been re-measured in
production.** MariaDB's `innodb_buffer_pool_size` is the same idea as the fix
you already have: keep hot pages in RAM on a machine that has plenty.

> **Do this first — it takes one command and it informs everything below:**
>
> ```
> python tools/diagnose_db.py --db /path/to/testboard.db --compare-local
> ```
>
> Run it *on the production web server*, now that connections persist. If the
> verdict is "the storage", MariaDB on a dedicated host addresses it directly.
> If the verdict is "nothing here is slow", you are migrating for the
> operational reasons above, not for speed — which is a fine reason, but you
> should know which one you are buying.

Everything below assumes the decision is made either way.

---

## 0. Prerequisites and versions

| Requirement | Why |
|---|---|
| **MariaDB 10.6 or newer** (LTS) | Needs ≥ 10.2 for `NO PAD` collations, `DYNAMIC` row format by default and 3072-byte index keys. 10.6+ is the supported LTS line. |
| Network route from the web server to the DB host, port 3306 | The app connects over the network now. |
| `mysql` command-line client on the web server | Used for the load and for every verification step. No Python driver needed for the migration itself. |
| Disk space on the web server ≈ 2.5 × the SQLite file | The export is text, and blobs are hex-encoded, which doubles them. ~2.5 GB for a 900 MB database. |
| A copy of the production database for a dry run | Non-negotiable. See §E.1. |

Check the version first — if it is below 10.2, stop and read §B.3, because the
collation guidance changes and the migration becomes materially riskier:

```sql
SELECT VERSION();
```

---

## A. For whoever holds the root password

Hand them this section. It is self-contained and it is the only thing they need
to do. Everything is scoped to one database; nothing here grants global
privileges.

### A.1 Create the database

```sql
CREATE DATABASE testboard
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_nopad_bin;
```

**The collation is not a detail — it is a correctness requirement.** testboard
identifies a test by the exact triple `(environment, script, test_name)` and a
user by their exact username. SQLite compares text byte-for-byte, so `Login` and
`login` are two different tests, and `Luke` and `luke` are two different users.
MariaDB's *default* collations are case-insensitive and ignore trailing spaces,
which would silently merge them — turning two tests into one and breaking
primary keys on load. `utf8mb4_nopad_bin` restores the SQLite behaviour exactly.
§C.2 proves it empirically rather than trusting this paragraph.

### A.2 Create two accounts, not one

Replace `WEBHOST` with the web server's hostname or IP as MariaDB sees it, and
choose two distinct strong passwords.

**Create both accounts with `mysql_native_password`** — that is MariaDB's
default, so on a stock server the plain `IDENTIFIED BY` below already does it.
If this server has been configured to default to a sha256-based plugin, say so
explicitly (`IDENTIFIED VIA mysql_native_password USING PASSWORD('...')`).

The reason is not preference. testboard's MySQL driver is vendored into the repo
so that nothing has to be installed on the web server, and PyMySQL needs the
compiled `cryptography` package for the `sha256_password` and
`caching_sha2_password` plugins. `cryptography` is deliberately **not** vendored
— vendoring a package that needs a compiler would give up the whole "nothing to
build on the server" property. So an account created with a sha256 plugin
produces, at connect time:

> `'cryptography' package is required for sha256_password or
> caching_sha2_password auth methods`

The fix is the auth plugin, not an install.

```sql
-- The application. Data only: it can never alter the schema.
CREATE USER 'testboard_app'@'WEBHOST' IDENTIFIED BY 'APP_PASSWORD_HERE';
GRANT SELECT, INSERT, UPDATE, DELETE
  ON testboard.* TO 'testboard_app'@'WEBHOST';

-- Migrations and the initial load. Used by a human, on purpose, never by the
-- running service.
CREATE USER 'testboard_migrate'@'WEBHOST' IDENTIFIED BY 'MIGRATE_PASSWORD_HERE';
GRANT SELECT, INSERT, UPDATE, DELETE,
      CREATE, ALTER, INDEX, DROP, REFERENCES,
      CREATE TEMPORARY TABLES, LOCK TABLES
  ON testboard.* TO 'testboard_migrate'@'WEBHOST';

FLUSH PRIVILEGES;
```

Why two: the running dashboard has no business being able to `DROP TABLE`. It
also means a schema change is a deliberate act by a person with a different
credential, which is what you want once the schema is locked.

### A.3 Server settings to confirm or change

```sql
SELECT @@innodb_buffer_pool_size, @@max_allowed_packet,
       @@sql_mode, @@local_infile, @@innodb_default_row_format;
```

| Setting | Required value | Why it matters |
|---|---|---|
| `innodb_buffer_pool_size` | As large as the host allows — at least 2 GB; ideally more than the whole database | This is the cache. The entire performance case for the move rests on it. A 900 MB database that fits in the pool is served from RAM. |
| `max_allowed_packet` | ≥ 64 MB | Captured test output is stored as a compressed blob. A single large row must fit in one packet or the load fails partway through with a confusing error. |
| `sql_mode` | must include `STRICT_TRANS_TABLES` or `STRICT_ALL_TABLES` | **Without strict mode, a test name longer than the column silently gets truncated** — and two different tests become one, permanently, with no error. This is the most damaging thing that can go wrong in this migration. |
| `local_infile` | `ON` (may be turned off again after the load) | Only needed if using the fast bulk load path (§D.3). |
| `innodb_default_row_format` | `dynamic` | Needed for 3072-byte index keys. Default on 10.2+. |

In `/etc/my.cnf.d/testboard.cnf` (or the site equivalent):

```ini
[mysqld]
innodb_buffer_pool_size = 4G
max_allowed_packet      = 128M
sql_mode                = STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION
local_infile            = ON
character_set_server    = utf8mb4
collation_server        = utf8mb4_nopad_bin
```

Restart required for `innodb_buffer_pool_size`.

### A.4 Tell the operator

They need: hostname, port, the two usernames and passwords, and confirmation
that §A.3 was applied. Nothing else.

---

## B. The schema, translated

This section is reference. §D generates the actual DDL — do not hand-type it.

### B.1 The index key length problem *(read this one)*

InnoDB caps an index key at **3072 bytes**. testboard's identity columns are
unbounded `TEXT` in SQLite, and `TEXT` cannot be indexed in full in MariaDB at
all. Every identity column must become a bounded `VARCHAR(n)`, and `n` is a
data question, not a taste question.

`utf8mb4` costs up to **4 bytes per character** for index-length purposes, so
the budget arithmetic is:

| Column | Type | Charset | Index bytes |
|---|---|---|---|
| `environment` | `VARCHAR(64)` | utf8mb4 | 256 |
| `script` | `VARCHAR(255)` | utf8mb4 | 1020 |
| `test_name` | `VARCHAR(255)` | utf8mb4 | 1020 |
| `start_time` | `VARCHAR(26)` | ascii | 26 |
| | | **total** | **2322** ≤ 3072 ✅ |

That is the widest index in the schema (`runs`'s UNIQUE constraint). Every other
index is smaller. There is ~750 bytes of headroom, so `script` and `test_name`
could go to 320 each if the audit says they need to — but **run the audit in
§C.1 before choosing.** Measured on the development database, the longest values
are `environment` 13, `script` 28, `test_name` 26 characters, so these limits are
roughly ten times the observed need. Production may differ; that is what the
audit is for.

`start_time` stays a **string**, not a `DATETIME`. The project's timestamps are
ISO-8601 UTC strings compared lexically, everywhere, by design — storage,
transport and comparison. `VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin`
preserves that exactly, keeps the index tiny, and changes nothing above the
storage layer. Converting to `DATETIME(6)` is a legitimate future improvement
and is *not* part of this migration; doing both at once means that if ordering
breaks you will not know which change did it.

### B.2 Type translation

| SQLite | MariaDB | Note |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT AUTO_INCREMENT PRIMARY KEY` | `BIGINT`, not `INT`: ids are consumed by re-imports (§B.5), so they run ahead of the row count. |
| `TEXT` (identity) | `VARCHAR(n)` per §B.1 | Bounded, or it cannot be indexed. |
| `TEXT` (`result`) | `VARCHAR(20) CHARACTER SET ascii COLLATE ascii_bin` | Values are `PASS`, `FAIL`, `FAILED_AS_EXPECTED`, `UNEXPECTED_PASS`. Do **not** use a MariaDB `ENUM` — the Python `Result` enum is the authority and an ENUM would give you two definitions that can drift. |
| `TEXT` (`source_link`) | `VARCHAR(1024)` | Not indexed. |
| `TEXT` (`known_failure_reason`) | `TEXT` | Nullable, not indexed. |
| `TEXT` (comment body) | `TEXT` | API caps it at 10,000 characters; `VARCHAR(10000)` in utf8mb4 would eat the 65,535-byte row limit. |
| `TEXT` (`username`) | `VARCHAR(100)` | Matches the API's `_MAX_USERNAME_LEN`. |
| `BLOB` (`run_outputs.output`) | `LONGBLOB` | `MEDIUMBLOB` caps at 16 MB. Output is zlib-compressed but is not bounded. |
| `REAL` | `DOUBLE` | |

### B.3 Collation, per column

Set at the database level in §A.1, but state it explicitly on identity columns
anyway — so that a future `ALTER` on a server with different defaults cannot
quietly change comparison semantics.

If your server is **older than 10.2** and `utf8mb4_nopad_bin` does not exist:
`utf8mb4_bin` gives you case sensitivity but *not* no-pad, so `"login"` and
`"login "` compare equal. That is a real difference from SQLite. Either upgrade,
or accept it knowingly after confirming with §C.1 that no identity value has a
trailing space.

### B.4 `PRAGMA` has no equivalent, and does not need one

The eight `PRAGMA` statements in `storage.py` are SQLite tuning: WAL mode, busy
timeout, page cache, mmap. All of them are replaced by server configuration
(§A.3) and none of them port. In particular:

- WAL mode → InnoDB's redo log. Nothing to do.
- `busy_timeout` → `innodb_lock_wait_timeout`.
- `cache_size` (the `--cache-mb` flag) → `innodb_buffer_pool_size`. **The
  per-connection cache budget stops being meaningful**: InnoDB has one shared
  buffer pool for the server, not one cache per connection. The worker pool
  still matters — for connection reuse and bounded concurrency — but the
  arithmetic in `Storage.cache_bytes_per_connection()` becomes SQLite-only.
  Do not delete it; make it conditional on the backend.

### B.5 `INSERT OR REPLACE` is a behaviour change, not a syntax change ⚠️

The most important line in this document.

SQLite's `INSERT OR REPLACE`, on a unique-key conflict, **deletes the existing
row and inserts a new one**. The new row gets a **new `id`**.

MariaDB's `INSERT ... ON DUPLICATE KEY UPDATE` **updates the existing row in
place**. The `id` is **unchanged**.

**Checked, and the news is better than that paragraph suggests.**
`tests/test_sql_portability.py` now pins what the code actually does:

- **`runs` never uses `INSERT OR REPLACE`.** The import path deliberately does
  SELECT-then-UPDATE-or-INSERT, so **`runs.id` is stable across re-import** —
  asserted by a test, along with `run_outputs` and `latest_runs` following the
  repair rather than being orphaned. That is the contract, it is what the feeder
  relies on every night and what `--force` relies on deliberately, and the
  MariaDB port has to reproduce it. `ON DUPLICATE KEY UPDATE` does.
- The only two `INSERT OR REPLACE` sites are `run_outputs` (keyed by `run_id`)
  and `test_retirements` (keyed by the test triple). **Neither has a generated
  id to churn and nothing holds a foreign key to either**, so delete-and-reinsert
  and update-in-place are indistinguishable from outside. Either MariaDB form
  works.

So this is not the blocker it looked like — but it was worth checking rather
than assuming, and the test now fails if a third `INSERT OR REPLACE` appears on
a table where it *would* matter.

`REPLACE INTO` reproduces SQLite's delete-and-reinsert exactly if you ever need
it. It is slower and it fires `ON DELETE` behaviour, so prefer
`ON DUPLICATE KEY UPDATE` unless a test says otherwise.

### B.6 Foreign keys become real

SQLite does not enforce `REFERENCES` unless `PRAGMA foreign_keys=ON`, which
testboard does not set. InnoDB **always** enforces them. So constraints that are
decorative today become load-order requirements: `users` before `comments`,
`runs` before `run_outputs` and `latest_runs`.

More consequentially, **any orphan rows in the current database will refuse to
load.** The audit in §C.1 finds them before you discover them at 2am.

---

## C. Pre-flight audit — run this on production

Read-only. Run it against the live SQLite file before anything else.

### C.1 Sizing and integrity

These run **on SQLite only** — they use SQLite syntax (`||`) deliberately,
because their job is to describe the source. The cross-engine constraint in
§E.4 does not apply here.

```sql
-- Longest identity values. These choose your VARCHAR(n) in §B.1.
SELECT MAX(LENGTH(environment)), MAX(LENGTH(script)),
       MAX(LENGTH(test_name)),   MAX(LENGTH(source_link))
FROM runs;

-- Timestamps must ALL be exactly 26 characters, or lexical ordering is
-- already broken and the migration is not the cause.
SELECT MIN(LENGTH(start_time)), MAX(LENGTH(start_time)),
       MIN(LENGTH(end_time)),   MAX(LENGTH(end_time)) FROM runs;

-- Trailing or leading whitespace in identity values (see §B.3).
SELECT COUNT(*) FROM runs
WHERE environment <> TRIM(environment)
   OR script      <> TRIM(script)
   OR test_name   <> TRIM(test_name);

-- Case-collision check: values that are distinct today but would COLLIDE
-- under a case-insensitive collation. Must be 0 — if it is not, §A.1's
-- collation is not optional, it is the only thing preventing data loss.
SELECT COUNT(*) - COUNT(DISTINCT LOWER(environment) || '/' || LOWER(script)
                        || '/' || LOWER(test_name) || '/' || start_time)
FROM runs;
SELECT COUNT(*) - COUNT(DISTINCT LOWER(username)) FROM users;

-- Orphans, which InnoDB will reject (§B.6). All must be 0.
SELECT COUNT(*) FROM run_outputs o
  LEFT JOIN runs r ON r.id = o.run_id WHERE r.id IS NULL;
SELECT COUNT(*) FROM latest_runs l
  LEFT JOIN runs r ON r.id = l.run_id WHERE r.id IS NULL;
SELECT COUNT(*) FROM comments c
  LEFT JOIN users u ON u.username = c.author WHERE u.username IS NULL;
SELECT COUNT(*) FROM assignments a
  LEFT JOIN users u ON u.username = a.assigned_by WHERE u.username IS NULL;
SELECT COUNT(*) FROM current_assignments ca
  LEFT JOIN users u ON u.username = ca.assignee
  WHERE ca.assignee IS NOT NULL AND u.username IS NULL;

-- Largest single blob, against max_allowed_packet (§A.3).
SELECT MAX(LENGTH(output)), SUM(LENGTH(output)) FROM run_outputs;

-- Volumes, for the load estimate.
SELECT (SELECT COUNT(*) FROM runs), (SELECT COUNT(*) FROM run_outputs),
       (SELECT COUNT(*) FROM latest_runs), (SELECT COUNT(*) FROM users),
       (SELECT COUNT(*) FROM comments), (SELECT COUNT(*) FROM assignments);

-- The schema version being migrated. Record it.
SELECT version FROM schema_version;
```

Write every answer down. §E.4 compares against them.

### C.2 Prove the collation, don't trust it

Run as `testboard_migrate`, on the empty MariaDB database, **before** loading
anything. This is the single most valuable five seconds in the whole procedure.

```sql
CREATE TABLE _collation_probe (k VARCHAR(64) NOT NULL PRIMARY KEY);
INSERT INTO _collation_probe (k) VALUES ('a'), ('A'), ('a ');
SELECT COUNT(*) AS must_be_three FROM _collation_probe;
DROP TABLE _collation_probe;
```

- **3** → case-sensitive and no-pad. Correct. Proceed.
- **Duplicate key error, or fewer than 3** → the collation is wrong. **Stop.**
  Loading now would merge distinct tests into one and the damage is not
  reversible without a reload. Return to §A.1.

### C.3 Prove your grants are sufficient

Cheaper to find out now than four hours into a load:

```sql
SHOW GRANTS FOR CURRENT_USER();
SELECT @@sql_mode, @@max_allowed_packet, @@local_infile,
       @@character_set_database, @@collation_database, VERSION();
CREATE TABLE _grant_probe (id BIGINT AUTO_INCREMENT PRIMARY KEY, v VARCHAR(10));
INSERT INTO _grant_probe (v) VALUES ('ok');
DROP TABLE _grant_probe;
```

### C.4 Prove strict mode is on

Truncation is silent without it, and silent truncation merges tests (§A.3):

```sql
CREATE TABLE _strict_probe (v VARCHAR(4));
INSERT INTO _strict_probe VALUES ('toolong');   -- MUST fail with an error
DROP TABLE _strict_probe;
```

If that `INSERT` **succeeds**, strict mode is off. Stop, and go back to §A.3.

---

## D. Export and load

### D.1 The tool you need

`tools/export_for_mariadb.py` — **does not exist yet**; specified here, listed
as WP-10 in [`UPGRADE_PLAN.md`](UPGRADE_PLAN.md). It is stdlib-only, Python 3.6,
opens SQLite read-only, and needs no MariaDB driver — it writes files, and the
`mysql` client loads them. That is deliberate: it means the export half is
fully testable tonight on a machine with no MariaDB anywhere near it.

It writes, into an output directory:

| File | Contents |
|---|---|
| `schema.sql` | `CREATE TABLE` DDL per §B, with the `VARCHAR` sizes passed in as arguments so §C.1's audit drives them |
| `<table>.tsv` | One file per table, tab-separated, `\N` for NULL, `\t`/`\n`/`\\` escaped |
| `run_outputs.tsv` | `run_id` and the blob **hex-encoded** — this is why the export is ~2× the database size |
| `load.sql` | Ordered `LOAD DATA` statements honouring foreign-key order (§B.6), with `UNHEX()` on the blob column |
| `verify_source.txt` | Every check in §E.4, computed from SQLite |
| `verify.sql` | The same checks, for MariaDB — **byte-identical SQL**, not a translation |

`verify.sql` and the queries behind `verify_source.txt` must be generated from
**one** list of query strings that both engines accept (§E.4). Two hand-written
variants will drift, and a verification step that drifts is worse than none: it
reports agreement between two different questions.

Required behaviours: refuse to overwrite a non-empty output directory; stream
rather than materialise (it must not hold 900 MB in memory); print row counts
and elapsed time per table; exit non-zero on any error.

### D.2 Export

```bash
python tools/export_for_mariadb.py \
    --db /path/to/testboard.db \
    --out /var/tmp/testboard-export \
    --env-len 64 --script-len 255 --test-len 255
```

Take the lengths from §C.1 and round up generously — you have headroom (§B.1),
and re-running the whole migration because a name was 8 characters too long is
a bad afternoon.

### D.3 Load

Credentials on a command line are visible to every user on the box via `ps`.
Use an option file instead:

```ini
# ~/.testboard-migrate.cnf   — chmod 600
[client]
host     = dbhost.example
user     = testboard_migrate
password = MIGRATE_PASSWORD_HERE
database = testboard
local-infile = 1
```

```bash
chmod 600 ~/.testboard-migrate.cnf
mysql --defaults-file=~/.testboard-migrate.cnf < /var/tmp/testboard-export/schema.sql
mysql --defaults-file=~/.testboard-migrate.cnf < /var/tmp/testboard-export/load.sql
```

If `LOAD DATA LOCAL INFILE` is refused — some builds disable it on the client
side regardless of the server setting — the fallback is batched multi-row
`INSERT` statements. Have `export_for_mariadb.py` emit those with
`--format=inserts`. It is several times slower and it is a supported path, not
a failure.

**Indexes:** create the tables with their primary keys, load, then add the
secondary indexes. Building an index once over a loaded table is much faster
than maintaining it row by row during the load. `load.sql` must do this in that
order.

---

## E. Cutover

### E.1 Dry run first — the dry run is also your estimate

Copy the production SQLite file to the web server's local disk and run §D
against it, into a scratch database (`testboard_dryrun`, same grants). Time
every step. **Do not estimate the downtime window; measure it here.** Then run
§E.4's verification against the dry-run database. Only when that passes do you
schedule the real cutover.

Keep the dry-run database until the real one is verified — it is a free second
opinion if a count disagrees.

### E.2 Freeze

1. **Stop the feeder** (disable the cron entry). It is the only automated
   writer.
2. Tell the team the dashboard is read-only for the window. Comments and
   assignments made during it will be lost — this is the one irreversible part,
   so keep the window short and do it outside working hours.
3. Note the high-water mark: `python run_feeder.py --config <cfg> --status`.
   You will need it in §E.5.

### E.3 Load

Run §D against the live file. Note the finish time.

### E.4 Verify — before letting anyone in

Compare `verify_source.txt` (from SQLite) against the output of `verify.sql`
(from MariaDB). Every line must match.

| Check | Why this one |
|---|---|
| `COUNT(*)` per table | Catches a truncated load. |
| `COUNT(*)` grouped by `environment, result` over `latest_runs` | A few dozen rows; catches misaligned columns, which a bare total will not. |
| `COUNT(*)` grouped by `SUBSTR(start_time, 1, 10), result` over `runs` | A few thousand rows; catches partial loads and timestamp mangling. |
| `MIN(start_time)`, `MAX(start_time)` | Catches truncated or reformatted timestamps. |
| `SUM(LENGTH(output))`, `COUNT(*)` on `run_outputs` | Catches blob corruption in the hex round-trip. **The most likely thing to go wrong** in the whole load. |
| Distinct identity-triple count (query below) | If this is *lower* in MariaDB, the collation merged tests. Stop and reload. |
| `schema_version` | Must equal the value recorded in §C.1. |

**Every check must be written so the identical SQL runs on both engines** —
otherwise you are comparing two different questions and calling the agreement
meaningful. Two traps in particular:

```sql
-- Distinct triples. COUNT(DISTINCT a, b, c) is MariaDB-only; SQLite's
-- COUNT(DISTINCT ...) takes exactly ONE argument. This form works on both:
SELECT COUNT(*) FROM (
  SELECT DISTINCT environment, script, test_name FROM runs
) AS t;

-- Per-day grouping. DATE(start_time) parses the ISO string on SQLite but is
-- given a VARCHAR containing a 'T' on MariaDB, where it may return NULL with
-- a warning rather than failing — two differently-broken groupings that both
-- produce numbers. SUBSTR does no date parsing at all:
SELECT SUBSTR(start_time, 1, 10) AS day, result, COUNT(*)
FROM runs GROUP BY day, result ORDER BY day, result;
```

`LENGTH()` on a blob is bytes on both engines, so the `run_outputs` check is
safe as written. `LENGTH()` on *text* is characters in SQLite and **bytes** in
MariaDB — do not use it to compare text columns across the two.

These are agreement checks, not cryptographic proof — but a mismatch in any one
of them means stop, and matching all of them means the shapes agree at every
level you can cheaply examine.

Then, by hand: open the dashboard, load a test detail page, read a run's output
(the one endpoint that reads a blob), post a comment, make an assignment.

### E.5 Restart the feeder

Point it at the new dashboard and run a catch-up. The high-water mark is in the
feeder's own state file on the feeder host, not in the database, so it survives
this migration untouched — the catch-up covers the freeze window automatically.

### E.6 Rollback

**Rollback is clean only until the first human write to MariaDB.** The SQLite
file is untouched by this procedure (opened read-only throughout), so reverting
is a configuration change plus a feeder catch-up. But comments, assignments and
retirements made after cutover exist *only* in MariaDB, and rolling back
discards them.

So: verify (§E.4) *before* announcing that the dashboard is back. That ordering
is the whole rollback plan.

Keep the SQLite file, and a copy of the export directory, for at least a month.

---

## F. The application code

The migration above moves the *data*. Making the *dashboard* talk to MariaDB is
separate work, and it has a blocker that is the user's decision.

### F.1 The driver problem, stated plainly

**There is no MariaDB/MySQL driver in the Python standard library.** The project
constraint is "standard library only, no pip installs". Those cannot both hold.
The options:

1. **Vendor PyMySQL into the repository** *(recommended)*. Pure Python, no
   compilation, works on Python 3.6, MIT-licensed — compatible with this repo's
   own MIT `LICENSE`. Dropped into `third_party/pymysql/` it is *present*, not
   *installed*: nothing runs `pip` on the server and the "no envs to set up"
   property is preserved. The constraint's actual purpose — never depend on the
   deployment host having anything — is honoured. Update the constraint's
   wording in `CLAUDE.md` to say so, rather than quietly breaking it.
2. **`mysqlclient` / `mariadb` via pip or RPM.** Faster (C extension), and it
   means a compiler or a system package on the server, plus a version to track.
   If the estate already ships `python3-PyMySQL` or similar as an RPM, this is
   more attractive than it sounds — check before dismissing it.
3. **Shell out to the `mysql` client.** Avoids the dependency and is
   unacceptable for a serving path: no parameter binding, so every query becomes
   a quoting problem, which is to say an injection problem. Listed only to be
   ruled out explicitly.

**Recommendation: option 1**, unless the estate already packages a driver, in
which case option 2.

### F.2 Sequencing

This is the part that cannot be done unattended tonight, because there is no
MariaDB in the dev environment and none in CI — an agent cannot verify a port
it cannot run.

**Phase 0 — tonight, SQLite-only, fully verifiable** (plan WP-5, WP-9):
remove the `julianday()` call by storing `duration_seconds`; funnel the three
`INSERT OR REPLACE` sites through one method; pin the re-import id behaviour
with a test (§B.5); add the portability inventory test so this document's
translation tables cannot silently go stale.

**Phase 1 — after the driver decision:** a `Dialect` seam in `storage.py`
(placeholder style, upsert form, blob type, connect/pragma behaviour) with
SQLite as one implementation and MariaDB as the other. `--db` grows a URL form.
The 59 execute sites do not each need porting; the ~27 dialect-specific
constructs do.

**Phase 2 — CI:** add a MariaDB service container to `.github/workflows/ci.yml`
and run the storage and API suites against **both** backends. GitHub Actions
supports this directly (`services: mariadb:10.6`). Until this exists, "it works
on MariaDB" is an assertion, not a fact — and this project's habit is to make
that kind of claim testable rather than to make it confidently.

---

## Appendix: errors you are likely to meet

| Symptom | Cause | Fix |
|---|---|---|
| `Specified key was too long; max key length is 3072 bytes` | `VARCHAR(n)` too large for an indexed column | §B.1 — recompute the budget |
| `Duplicate entry '...' for key 'PRIMARY'` during load | Case-insensitive or PAD-SPACE collation merging distinct rows | §A.1 and §C.2. Drop the database and reload; do not "fix" the duplicates |
| `Data too long for column 'test_name'` | `VARCHAR(n)` too small | Good news — strict mode caught it. Re-export with larger `--test-len` |
| Load succeeds but tests are missing afterwards | Strict mode **off**, values silently truncated and merged | §C.4. Reload from scratch |
| `MySQL server has gone away` mid-load | A row exceeded `max_allowed_packet` | §A.3, raise it; the culprit is a large `run_outputs` blob |
| `The used command is not allowed with this MariaDB version` | `LOAD DATA LOCAL INFILE` disabled | Enable `local_infile` both sides, or use `--format=inserts` (§D.3) |
| `Cannot add or update a child row: a foreign key constraint fails` | Orphan rows, or wrong load order | §B.6 and §C.1 |
| Blob comparison fails in §E.4 | Hex round-trip; usually a missing `UNHEX()` or a client charset mangling the hex text | Re-check `load.sql`; blobs must never pass through a character-set conversion |
| Dashboard works, is slow, buffer pool is large | Pool not warm yet, or not actually applied | `SHOW ENGINE INNODB STATUS`; confirm `@@innodb_buffer_pool_size` is what §A.3 set |
| `'cryptography' package is required for sha256_password…` at connect | The account uses a sha256 auth plugin; the vendored driver cannot do those without a compiled package | §A.2 — recreate the account with `mysql_native_password`. Do **not** install `cryptography` on the server; "nothing to build on the server" is the property this whole design protects |
