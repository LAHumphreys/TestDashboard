# Migrating testboard from SQLite to MariaDB

**Assumptions, simplified 2026-08-07.** The general two-host, two-account
version of this runbook lives in git history if it is ever needed again.
As of this revision: **the dashboard and MariaDB run on the same host**,
everything connects over the **unix socket**, and there is **one database
account** — used by the migration and by the running app alike. The
dedicated app/migrate account pair was judged overkill for a
single-operator, single-box install; §A.4 records what that trades away
and how to restore it later.

**Audience.** Two hats, quite possibly one head:

- **The administrator** — root on the box and the MariaDB root password.
  Runs §A once, top to bottom.
- **The operator** — runs §C to §E, and almost all of it is one command:
  `tools/migrate_to_mariadb.py`.

---

## Before you start: what this buys, and what it doesn't

Worth being straight about, once, so the effort is spent knowingly.

**MariaDB gives you:** concurrent writers without file-level locking; backup,
replication and monitoring owned by the database rather than by a file on a
share; a database that lives on a server instead of on a network mount; and
alignment with however the rest of the estate is run. Those are good reasons and
they are why organisations make this move.

**MariaDB does not, by itself, make queries faster at this size.** 950 MB and
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
> python3 tools/diagnose_db.py --db /path/to/testboard.db --compare-local
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
| **MariaDB 10.2 or newer**; 10.6+ preferred | 10.2 is the correctness floor: below it there is no `NO PAD` collation and §B.3 applies. Whether you can get 10.6 on RHEL 8 depends on the module streams that host offers — §A.2 decides it with a command rather than from memory. |
| Nothing network | Same box: the tools and the app connect over the unix socket. No port to open, no firewall, no route. |
| **Nothing installed on the web server** | §C–§E connect through the driver vendored in `third_party/pymysql`, which ships with the checkout. No client package, no pip, no build step — that property is the whole point, and the migration is not allowed to be the thing that breaks it. |
| *(optional)* `mysql` command-line client | Only for the by-hand fallback in §D.3, or for poking at the server yourself. `dnf install mariadb` — §A.7. |
| Python 3.6 on the web server | The platform Python on RHEL 8. The migration tool is stdlib-only, like everything else here. |
| Disk space on the web server ≈ 2.5 × the SQLite file | The export is text, and blobs are hex-encoded, which doubles them. ~2.4 GB for a 950 MB database. The tool checks this before it starts. |
| A copy of the production database for a dry run | Non-negotiable. See §E.1. |

---

## A. For the administrator — the whole privileged half

Hand them this section. It is self-contained. Nothing outside it needs `root`
or the MariaDB root password, and nothing in it needs any knowledge of
testboard.

One machine, one pass, top to bottom:

- **A.2 – A.5**: install MariaDB, create the database and the account, set
  the server options.
- **A.7 – A.10**: the service account and group, the directories, and the
  one credentials file.
- **A.11** is the hand-over checklist. (A.1 states the one-box assumption
  and its one trap; A.6 is retired — numbering is kept stable so every
  cross-reference in the tools and tests stays true.)

Commands are `#`-prefixed where they need root. Everything is scoped to one
database and one service account; nothing here grants a global privilege.

> **§A was renumbered** when the RHEL 8 install steps were added. Anything
> elsewhere in the repository still pointing at the old numbers maps like this:
> **§A.1 (create the database) → §A.3**, **§A.2 (the two accounts) → §A.4**,
> **§A.3 (server settings) → §A.5**. The header comment in a generated
> `schema.sql` is one such pointer.

### A.1 One box — and the localhost trap

Everything runs on this host; the account is granted `@'localhost'` and every
connection uses the **unix socket**. The trap worth knowing anyway, because
it is the single most common source of "access denied": MariaDB matches an
account against the host it sees the connection *coming from*, and
`'user'@'localhost'` (which means the Unix socket) is a completely different
account from `'user'@'127.0.0.1'` (which means TCP to the same machine).
That is why the credentials file (§A.9) carries an explicit `socket =` line
— the vendored driver, unlike the `mysql` client, does not treat
`host = localhost` as "use the socket".

(If the database ever moves to its own host: git history holds this
runbook's two-host revision — grants per source host, firewalld, bind
address, the lot.)

### A.2 Install MariaDB

RHEL 8 ships MariaDB as an AppStream *module*, and which versions are offered
depends on the minor release. **Ask the box rather than trusting a version
number from a document:**

```bash
# dnf module list mariadb
```

You get a list of streams with one marked `[d]` (default) and possibly one
`[e]` (enabled). Choose like this:

- **Pick the newest stream offered.** Anything 10.3 or newer satisfies this
  project's correctness floor (§0), so any stream RHEL 8 offers will work.
- A stream whose *upstream* version is past end-of-life is not automatically
  unsupported: Red Hat maintains AppStream module streams on the RHEL
  lifecycle, not upstream's. Check your own support position if it matters.
- **If you need a newer MariaDB than any stream offers** — say your standard is
  the 10.6 or 10.11 LTS line — add MariaDB's own repository for RHEL 8 instead
  of the module. That is a supported path, and it is a decision about which
  vendor supports your database, not a testboard requirement.

Then, enabling whichever stream you chose (`10.11` here is an example, **not** a
recommendation — use what `dnf module list` actually showed):

```bash
# dnf module enable mariadb:10.11 -y
# dnf install mariadb-server -y
# systemctl enable --now mariadb
# systemctl status mariadb          # must be: active (running)
```

Set the root password and remove the defaults:

```bash
# mysql_secure_installation
```

Answer: set a root password (**yes**), remove anonymous users (**yes**),
disallow remote root login (**yes**), remove the test database (**yes**),
reload privileges (**yes**).

Confirm the version you actually got — this is the number §0 cares about:

```bash
# mysql -u root -p -e "SELECT VERSION();"
```

If that prints anything below **10.2**, stop and read §B.3 before going on: the
collation guidance changes and the migration becomes materially riskier.

### A.3 Create the database

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
The operator proves this empirically in §C.2 rather than trusting this
paragraph.

If `utf8mb4_nopad_bin` is rejected as unknown, the server is older than 10.2.
See §B.3 — it has a workable but lossier fallback, and it must be an informed
choice, not a substitution made at the prompt.

### A.4 Create the account — one, deliberately

One account, one strong password. It runs the migration AND the app:

```sql
CREATE USER 'testboard'@'localhost' IDENTIFIED BY 'PASSWORD_HERE';
GRANT SELECT, INSERT, UPDATE, DELETE,
      CREATE, ALTER, INDEX, DROP, REFERENCES,
      CREATE TEMPORARY TABLES, LOCK TABLES
  ON testboard.* TO 'testboard'@'localhost';

FLUSH PRIVILEGES;
```

**What this trades away, on the record** (decided 2026-08-07): the earlier
two-account design meant the *serving* app could never `DROP TABLE` — schema
changes needed a different credential. With one account the app connection
holds DDL rights it never uses: the dashboard runs no DDL on MariaDB by
design (the backend refuses to, and `DriverImportAllowlistTest` plus the
schema-version check stand behind that), every query is parameterized, and
this is a single-operator install on one box. Judged acceptable. To restore
the split later, create a data-only app account and swap the credentials
file — two GRANT statements and a file edit, no code:

```sql
CREATE USER 'testboard_app'@'localhost' IDENTIFIED BY '...';
GRANT SELECT, INSERT, UPDATE, DELETE ON testboard.* TO 'testboard_app'@'localhost';
```

**The account must use `mysql_native_password`.** That is MariaDB's default,
so on a stock server the plain `IDENTIFIED BY` above already does it. If this
server has been configured to default to a sha256-based plugin, say so
explicitly:

```sql
CREATE USER 'testboard'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('PASSWORD_HERE');
```

(The `USING PASSWORD('...')` form needs MariaDB 10.4+. On a 10.3 stream,
compute the hash first — `SELECT PASSWORD('PASSWORD_HERE');` — and pass
it as the `USING` value: `... USING '*94BDCE...'`.)

Confirm what you actually created:

```sql
SELECT user, host, plugin FROM mysql.user WHERE user LIKE 'testboard%';
```

The reason is not preference. The MySQL driver is vendored into the
repository so that nothing has to be installed on the server, and PyMySQL
needs the compiled `cryptography` package for the `sha256_password` and
`caching_sha2_password` plugins. `cryptography` is deliberately **not**
vendored — vendoring a package that needs a compiler would give up the whole
"nothing to build on the server" property. An account created with a sha256
plugin produces, at preflight and at every service start:

> `'cryptography' package is required for sha256_password or
> caching_sha2_password auth methods`

The fix is the auth plugin, not an install. The operator's preflight (§C.2)
connects with that same vendored driver, so a sha256 account fails there with
a message naming this section — but check the `plugin` column here anyway:
this is the only point in the procedure where somebody has the privileges to
look.

### A.5 Server settings

```sql
SELECT @@innodb_buffer_pool_size, @@max_allowed_packet,
       @@sql_mode, @@local_infile, @@innodb_default_row_format;
```

| Setting | Required value | Why it matters |
|---|---|---|
| `innodb_buffer_pool_size` | As large as the host allows — at least 2 GB; ideally more than the whole database | This is the cache. The entire performance case for the move rests on it. A 950 MB database that fits in the pool is served from RAM. |
| `max_allowed_packet` | ≥ 64 MB (128 MB recommended) | Captured test output is stored as a compressed blob. A single large row must fit in one packet or the load fails partway through with a confusing error. |
| `sql_mode` | must include `STRICT_TRANS_TABLES` or `STRICT_ALL_TABLES` | **Without strict mode, a test name longer than the column silently gets truncated** — and two different tests become one, permanently, with no error. This is the most damaging thing that can go wrong in this migration. |
| `local_infile` | `ON` (may be turned off again after the load) | Needed for the bulk load path (§D.3). |
| `innodb_default_row_format` | `dynamic` | Needed for 3072-byte index keys. Default on 10.2+. |

Write `/etc/my.cnf.d/testboard.cnf`:

```ini
[mysqld]
innodb_buffer_pool_size = 4G
max_allowed_packet      = 128M
sql_mode                = STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION
local_infile            = ON
character_set_server    = utf8mb4
collation_server        = utf8mb4_nopad_bin
```

```bash
# systemctl restart mariadb
```

`innodb_buffer_pool_size` needs the restart; do not skip it and assume the
value took.

### A.6 Network access — retired

Nothing to do: same box, socket connections. Leave MariaDB listening on
localhost only (RHEL 8's default) — a database that cannot be reached from
the network cannot be attacked from it. If the database ever moves to its
own host, recover this section's firewalld and bind-address guidance from
git history. (The number is kept so §A's cross-references stay stable.)

### A.7 The service account and group

This is the account the dashboard runs as. It is a system account: no login, no
password, no home directory to speak of.

```bash
# groupadd --system testboard
# useradd --system --gid testboard \
          --home-dir /var/lib/testboard --create-home \
          --shell /sbin/nologin \
          --comment "testboard dashboard service" testboard
```

Now give the human operators access **through the group**, rather than by
sharing the account:

```bash
# usermod -aG testboard <operator-username>        # repeat per operator
```

A group membership added with `usermod -aG` applies to *new* logins only. The
operator must log out and back in, or start a new session with `newgrp
testboard`, before they can read anything group-owned. Confirm with `id
<operator-username>` — `testboard` must appear in the group list.

**The migration needs nothing installed here.** It connects through the driver
vendored in the repository (`third_party/pymysql`), which is why testboard can be
deployed by copying a checkout. Do not install a database client on the operator's
behalf and do not let anything in this procedure come to depend on one.

Optionally, for a human to inspect the server by hand (**client only** — this box
is not a database server):

```bash
# dnf install mariadb -y        # optional; §D.3's by-hand fallback uses it
```

### A.8 Directories

```bash
# mkdir -p /etc/testboard /var/lib/testboard /var/log/testboard
# chown root:testboard  /etc/testboard      && chmod 0750 /etc/testboard
# chown testboard:testboard /var/lib/testboard /var/log/testboard
# chmod 0770 /var/lib/testboard /var/log/testboard
```

`/etc/testboard` is `root`-owned and group-readable: the service reads its
credentials from there but must never be able to rewrite them.

The migration needs a scratch directory with ~2.5 × the database in free space
(§0). `/var/tmp` is usually right; check first, and put it somewhere else if
not:

```bash
# df -h /var/tmp
```

**SELinux.** If the dashboard is launched as a plain systemd service it runs
unconfined and needs nothing here. If it is served under a confined domain —
behind `httpd`, most commonly — that domain needs permission to open a network
connection to a database:

```bash
# getenforce                                          # Enforcing?
# setsebool -P httpd_can_network_connect_db on        # only if under httpd
```

Do not set booleans speculatively. If §E's verification and a page load both
work, there was nothing to fix.

### A.9 The credentials file — one file, for everything

One account means one file. `/etc/testboard/db.cnf` is read by the migration
tool (`--config`), by the running dashboard (`--db-config`, §F), and by the
`mysql` client (`--defaults-file`, the §D.3 by-hand fallback):

```bash
# cat > /etc/testboard/db.cnf <<'EOF'
[client]
socket   = /var/lib/mysql/mysql.sock
user     = testboard
password = PASSWORD_HERE
database = testboard
local-infile = 1
EOF
# chown root:testboard /etc/testboard/db.cnf
# chmod 0640 /etc/testboard/db.cnf
# ls -l /etc/testboard/db.cnf          # must be -rw-r----- root testboard
```

The `socket =` line is not decoration: the vendored driver — unlike the
`mysql` client — does **not** treat `host = localhost` as "use the socket";
it would open TCP, which MariaDB may match against a different account
(§A.1). With the socket line, the tool, the app and the client all
authenticate identically, as `'testboard'@'localhost'`.

It is a file rather than a command line for one reason: **anything on a
command line is visible to every user on the box through `ps`.** The tools
refuse to accept a password any other way.

`root:testboard` at `0640` is the design, not an oversight: the service
account and the operators (who are in the `testboard` group, §A.7) can read
it; nobody else can; only `root` can change it. Group-readability here is
deliberate — the file parser warns only about *world*-readable credentials.

### A.10 The service unit

The credentials file is §A.9's — the app reads the same one. What the
service needs, for the cutover day:

> **`run_server.py --db-config /etc/testboard/db.cnf` is the switch** (§F).
> Creating the file does not move anything: the server keeps serving SQLite
> until it is *started* with `--db-config`, and SQLite remains a first-class
> backend afterwards (a second instance can always start on `--db PATH` with
> nothing else set up).

`/etc/systemd/system/testboard.service`, both forms:

```ini
[Unit]
Description=testboard dashboard
After=network-online.target mariadb.service

[Service]
User=testboard
Group=testboard
WorkingDirectory=/opt/testboard
# Before cutover (SQLite, as today):
ExecStart=/usr/bin/python3 /opt/testboard/run_server.py --host 0.0.0.0 --port 8000 --db /var/lib/testboard/testboard.db
# At cutover, replace ExecStart with:
#ExecStart=/usr/bin/python3 /opt/testboard/run_server.py --host 0.0.0.0 --port 8000 --db-config /etc/testboard/db.cnf --site-notes /var/lib/testboard/site_notes.json
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`--site-notes` is required with `--db-config` (there is no database file to
keep it beside); `/var/lib/testboard/` is the right home — the service
account owns it (§A.8). If the notes file lived beside the old `--db`, move
it there at cutover so nobody's site notes vanish.

### A.11 Hand over

Tell the operator (or, wearing the other hat, confirm to yourself):

- [ ] The `testboard` account's password, by whatever channel your site uses
      for secrets — not email
- [ ] That `/etc/testboard/db.cnf` is in place, `root:testboard`, 0640, with
      the `socket =` line (§A.9)
- [ ] The output of `SELECT VERSION();`
- [ ] Confirmation that §A.5's settings were applied **and the server was
      restarted**
- [ ] That the operator is in the `testboard` group and has logged in since
      (§A.7)
- [ ] Which scratch filesystem has room for the export (§A.8)

Nothing else. The operator does not need root beyond this section.

---

## B. The schema, translated

This section is reference. `tools/export_for_mariadb.py` generates the actual
DDL — do not hand-type it.

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
index is smaller. The three identity columns share a budget of 761 characters
between them and the defaults spend 574, so there is real but not unlimited
headroom. **You do not choose these by hand:** §C.1's audit measures the longest
value in each column and the tool sizes the columns from that, doubling for
headroom where the budget allows and tightening — or refusing outright, with the
query to find the culprit — where it does not. Measured on the development
database the longest values are `environment` 13, `script` 28, `test_name` 26
characters, roughly ten times inside the defaults. Production may differ; that
is what the audit is for.

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
| `TEXT` (`source_link`) | `VARCHAR(1024)` | Not indexed. The audit checks nothing exceeds it. |
| `TEXT` (`known_failure_reason`) | `TEXT` | Nullable, not indexed. |
| `TEXT` (comment body) | `TEXT` | API caps it at 10,000 characters; `VARCHAR(10000)` in utf8mb4 would eat the 65,535-byte row limit. |
| `TEXT` (`username`) | `VARCHAR(100)` | Matches the API's `_MAX_USERNAME_LEN`. |
| `BLOB` (`run_outputs.output`) | `LONGBLOB` | `MEDIUMBLOB` caps at 16 MB. Output is zlib-compressed but is not bounded. |
| `REAL` | `DOUBLE` | |

### B.3 Collation, per column

Set at the database level in §A.3, and the identity columns (environment,
script, test_name, username) deliberately **inherit** it — the generated DDL
does not repeat a collation on them, so the choice lives in exactly one
place. The machine-generated columns (timestamps, `result`, `hour`,
`output_fingerprint`) are pinned to `ascii_bin` explicitly: exact,
case-sensitive, and a quarter the index cost of utf8mb4. Two gates stand
behind the inherited default: the preflight refuses a database whose default
collation is not binary (§C.2 `database_collation`), and the collation probe
proves the behaviour empirically before anything loads. A later
`ALTER TABLE ... ADD COLUMN` inherits the *table's* collation, fixed at
CREATE time from that gated default, so future columns cannot quietly
diverge either.

If your server is **older than 10.2** and `utf8mb4_nopad_bin` does not exist:
`utf8mb4_bin` gives you case sensitivity but *not* no-pad, so `"login"` and
`"login "` compare equal. That is a real difference from SQLite. Either upgrade,
or accept it knowingly after confirming with §C.1 that no identity value has a
trailing space — the audit reports exactly that count.

### B.4 `PRAGMA` has no equivalent, and does not need one

The five `PRAGMA` statements in `storage.py` are SQLite tuning: WAL mode, busy
timeout, foreign keys, page cache, mmap. All of them are replaced by server
configuration (§A.5) or by the backend's connect settings, and none of them
port as text. In particular:

- WAL mode → InnoDB's redo log. Nothing to do.
- `busy_timeout` → `innodb_lock_wait_timeout` (the MariaDB backend sets it
  per session at connect).
- `cache_size` (the `--cache-mb` flag) → `innodb_buffer_pool_size`. **The
  per-connection cache budget stops being meaningful**: InnoDB has one shared
  buffer pool for the server, not one cache per connection. The worker pool
  still matters — for connection reuse and bounded concurrency — but the
  arithmetic in `Storage.cache_bytes_per_connection()` is SQLite-only and
  answers None on MariaDB (`run_server.py` rejects `--cache-mb` with
  `--db-config` outright).
- `foreign_keys=ON` → see §B.6; the generated schema declares no constraints,
  so there is nothing for InnoDB to enforce.

### B.5 `INSERT OR REPLACE` is a behaviour change, not a syntax change ⚠️

The most important line in this document.

SQLite's `INSERT OR REPLACE`, on a unique-key conflict, **deletes the existing
row and inserts a new one**. The new row gets a **new `id`**.

MariaDB's `INSERT ... ON DUPLICATE KEY UPDATE` **updates the existing row in
place**. The `id` is **unchanged**.

**Checked, and the news is better than that paragraph suggests.**
`tests/test_sql_portability.py` pins what the code actually does:

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

### B.6 Foreign keys: deliberately NOT carried over

A correction, recorded rather than papered over: an earlier revision of this
section claimed testboard leaves `PRAGMA foreign_keys` off. It does not —
`storage.py` sets `foreign_keys=ON` on every connection, so SQLite HAS been
enforcing the `REFERENCES` clauses at runtime. What is true either way: the
serving code never *relies* on enforcement — every delete path removes child
rows itself, in order (`prune` deletes `run_outputs` before `runs`;
environment deletion sweeps its tables explicitly) — which is exactly why it
runs green with enforcement on.

The generated MariaDB DDL still **declares no constraints**, and that stays
the right call: the load is faster and simpler without them (`load.sql`'s
FK-order and the audit's orphan gate do the same job), a bulk load under
`FOREIGN_KEY_CHECKS=0` is never re-validated by InnoDB afterwards anyway, and
enforcement differences between engines are one more variable the port does
not need. The code's own delete-order discipline is what actually protects
integrity, on both engines. Adopting declared constraints in MariaDB is
separate, deliberate future work.

Two disciplines the tooling keeps, because they cost nothing and are what
makes that future work possible:

- `load.sql` still loads parents before children: `users` before `comments`,
  `runs` before `run_outputs` and `latest_runs`.
- **The audit still blocks on orphan rows** (§C.1). They would load without
  complaint into the constraint-free schema — which is precisely the problem:
  dangling references are latent corruption, and the migration is the one
  moment they are cheap to find. Delete them at the source rather than
  carrying them over.

---

## C. Pre-flight — run this on production

Read-only against the SQLite file, read-only-ish against the empty MariaDB
database (it creates and drops three tiny probe tables). Nothing here changes
production.

Two environment facts before the first command:

- **Run under a UTF-8 locale** (`echo $LANG` — any `.UTF-8` value is fine).
  Python 3.6 does not coerce the C locale the way 3.7+ does, so under
  `LANG=C` (a stripped cron or sudo environment) the tool cannot print its
  own output and stops at its first line with `UnicodeEncodeError`. It fails
  before doing anything, so the fix is `export LANG=en_US.UTF-8` and re-run.
- **The tool must match the database.** The exporter knows the schema of the
  code it ships with. Run it from the same checkout that is *deployed* — a
  newer tool against an older database is stopped by the `source_tables`
  gate, but an **older tool against a newer database silently skips the
  tables it has never heard of**, and the verification, generated by the same
  tool, agrees with the omission.

### C.1 The source audit — one command

```bash
python3 tools/migrate_to_mariadb.py audit \
    --db /path/to/testboard.db \
    --json /var/tmp/testboard-audit.json
```

It prints one line per check, and it **exits 3 if anything blocking failed** —
so it can be run from a wrapper without a person reading the output. What it
checks, and why each one is where it is:

| Check | Blocking? | What it means |
|---|---|---|
| `source_tables` | yes | Every table the exporter knows about exists. A database older than the code fails the export halfway through. |
| `identity_lengths` | no | Measures the longest `environment`, `script` and `test_name`, and picks the `VARCHAR` sizes from them (§B.1). |
| `source_link_length` | yes | Nothing exceeds `VARCHAR(1024)`. |
| `timestamp_widths` | yes | Every timestamp is exactly 26 characters. If not, lexical ordering is *already* broken and the migration is not the cause. |
| `identity_whitespace` | no | Counts leading/trailing spaces in identity values. Harmless under `utf8mb4_nopad_bin`; the measurement of the damage if §A.3 was not followed. |
| `case_collisions` | no | How many rows differ only by case — i.e. exactly what a case-insensitive collation would merge. Non-zero makes §C.2 the only thing standing between you and data loss. |
| `orphan_rows` | yes | Rows whose parent is missing — latent corruption, blocked on principle (§B.6). |

It also records the volumes, the largest captured output (which §C.3 checks
against `max_allowed_packet`), and the `schema_version` — §E.4 compares that
last one after the load.

<details>
<summary>The same checks as raw SQL, if you would rather see them yourself</summary>

These use SQLite syntax (`||`) deliberately, because their job is to describe
the source. The cross-engine constraint in §E.4 does not apply here.

```sql
-- Longest identity values. These choose your VARCHAR(n) in §B.1.
SELECT MAX(LENGTH(environment)), MAX(LENGTH(script)),
       MAX(LENGTH(test_name)),   MAX(LENGTH(source_link))
FROM runs;

-- Timestamps must ALL be exactly 26 characters.
SELECT MIN(LENGTH(start_time)), MAX(LENGTH(start_time)),
       MIN(LENGTH(end_time)),   MAX(LENGTH(end_time)) FROM runs;

-- Trailing or leading whitespace in identity values (see §B.3).
SELECT COUNT(*) FROM runs
WHERE environment <> TRIM(environment)
   OR script      <> TRIM(script)
   OR test_name   <> TRIM(test_name);

-- Values that are distinct today but would COLLIDE under a
-- case-insensitive collation.
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

-- Largest single blob, against max_allowed_packet (§A.5).
SELECT MAX(LENGTH(output)), SUM(LENGTH(output)) FROM run_outputs;

-- The schema version being migrated. Record it.
SELECT version FROM schema_version;
```

</details>

### C.2 / C.3 / C.4 The server preflight — one command

```bash
python3 tools/migrate_to_mariadb.py preflight \
    --config /etc/testboard/db.cnf \
    --max-blob-bytes <from the audit>
```

Every one of these is a **gate**: it exits 3 and the load does not run.

| Check | What it proves |
|---|---|
| `server_version` | ≥ 10.2, so `NO PAD` collations exist (§B.3). |
| `sql_mode_strict` | Truncation is an error, not a silent merge. |
| `database_collation` | The database is binary-collated (§A.3). |
| `max_allowed_packet` | The largest captured output fits in one packet, with margin for the hex form on the wire. |
| `local_infile` | The bulk load path is available. |
| `grants` | The account can create, insert and drop — i.e. §A.4's GRANT was actually applied to the account the option file names. |
| `collation_probe` | **The important one.** Stores `'a'`, `'A'` and `'a '` in a primary key and counts them. Three means case-sensitive and no-pad. Fewer — or a duplicate-key error — means loading now would merge distinct tests, permanently. |
| `strict_probe` | Inserts a 7-character value into `VARCHAR(4)`. **It must fail.** A probe that passed by succeeding could not tell strict mode from a statement that never ran. |
| `target_is_empty` | You are not loading on top of an earlier attempt. `--force` overrides, for scratch databases. |

The probe tables are created and dropped inside the check. If one is left behind
by an interrupted run, the next `target_is_empty` check ignores it (probe names
start with `_`) but you should drop it.

This step is not run by the same tool the operator uses for everything else by
accident: it is worth more than the rest of the procedure put together, and it
takes about a second.

---

## D. Export and load

### D.1 The two tools

Both exist and both are covered by tests.

| Tool | What it does | Needs a database server? |
|---|---|---|
| `tools/export_for_mariadb.py` | Reads SQLite read-only, writes the load files | No |
| `tools/migrate_to_mariadb.py` | Audits, preflights, calls the exporter, creates the schema, loads, verifies | Yes, via the vendored driver — nothing installed |

The exporter writes, into an output directory:

| File | Contents |
|---|---|
| `schema.sql` | `CREATE TABLE` DDL per §B, with the `VARCHAR` sizes from the audit |
| `<table>.tsv` | One file per table, tab-separated, `\N` for NULL, `\t`/`\n`/`\\` escaped |
| `run_outputs.tsv` | `run_id` and the blob **hex-encoded** — this is why the export is ~2× the database size |
| `load.sql` | Ordered `LOAD DATA` statements honouring foreign-key order (§B.6), with `UNHEX()` on the blob column, and the secondary indexes created *after* the load |
| `verify_source.txt` | Every check in §E.4, computed from SQLite |
| `verify.sql` | The same checks, for MariaDB — **byte-identical SQL**, not a translation |

`verify.sql` and `verify_source.txt` are generated from **one** list of query
strings that both engines accept. Two hand-written variants would drift, and a
verification step that drifts is worse than none: it reports agreement between
two different questions.

**Why the migration tool uses the vendored driver and never the `mysql`
client.** Because the deployment property is the point. testboard is deployable
by copying a checkout onto a RHEL 8 box — no pip, no build step, no virtualenv —
and shelling out would have quietly added "a MariaDB client package must be
installed on the web server" to that list. An RPM is a dependency in exactly the
same way a pip install is, and it fails in the same place: on someone else's
machine, during the cutover. `third_party/pymysql` is *present*, not installed.

Two things follow. The preflight connects with the same driver the dashboard
will use (§F), so an auth plugin it cannot do is found in §C.2 rather than at
first service start. And `tests/test_vendored_driver.py` now allowlists this one
tool rather than forbidding every import — `testboard/` and `feeder/` are still
held to "not yet", so the driver's revert story is unchanged.

### D.2 The whole thing, one command

```bash
python3 tools/migrate_to_mariadb.py all \
    --db     /path/to/testboard.db \
    --out    /var/tmp/testboard-export \
    --config /etc/testboard/db.cnf
```

That is: audit → preflight → export → schema → load → verify, **stopping at the
first failed gate** with exit code 3. The server preflight deliberately comes
*before* the export: the collation probe costs a second and the export costs
twenty minutes and 2.4 GB, and on cutover night those twenty minutes are inside
the freeze window. It prints the elapsed time of every phase
at the end, which is the number §E.1 exists to obtain.

Exit codes: `0` everything passed · `3` a check said no · anything else
(`1`/`2`) is a usage or environment error — bad flags, a missing file, no
connection. A wrapper should treat only `0` as go.

The steps can also be run one at a time — `audit`, `preflight`, `export`,
`load`, `verify` — and that is what you want when something has failed and you
are re-running one part. `--help` on any of them.

### D.3 What it does, and how to do it by hand

If you need to intervene by hand, this is the same procedure through the `mysql`
client. It is a **fallback for a human**, not a path the tooling takes — the
client has to be installed for it, which is exactly what §D.1 explains the
tooling avoids. The credentials file is §A.9's.

```bash
# 1. export (no database needed)
python3 tools/export_for_mariadb.py \
    --db /path/to/testboard.db \
    --out /var/tmp/testboard-export \
    --env-len 64 --script-len 255 --test-len 255

# 2. schema, then data — from inside the export directory, because
#    load.sql names its .tsv files relatively
cd /var/tmp/testboard-export
mysql --defaults-file=/etc/testboard/db.cnf < schema.sql
mysql --defaults-file=/etc/testboard/db.cnf --local-infile=1 < load.sql

# 3. verify: compare this output against verify_source.txt
mysql --defaults-file=/etc/testboard/db.cnf --batch --raw \
      --skip-column-names < verify.sql
```

Take the `--env-len` etc. from §C.1 and round up generously — re-running the
whole migration because a name was 8 characters too long is a bad afternoon. The
`all` command does this for you from the measured maxima.

If `LOAD DATA LOCAL INFILE` is refused — some builds disable it on the client
side regardless of the server setting — the fallback is batched multi-row
`INSERT` statements. It is several times slower and it is a supported path, not
a failure; it is not implemented in the exporter yet, so raise it as work if you
hit it.

**Indexes:** the generated `load.sql` creates the tables with their primary
keys, loads, and only then adds the secondary indexes. Building an index once
over a loaded table is much faster than maintaining it row by row during the
load. Do not reorder that.

---

## E. Cutover

### E.1 Dry run first — the dry run is also your estimate

Copy the production SQLite file to local disk and run §D against it, into a
scratch database (`testboard_dryrun`). Root creates it exactly as §A.3
created the real one — **same collation** — and repeats §A.4's GRANT
`ON testboard_dryrun.*` for the same account. The dry run needs its own copy
of the credentials file: `install -m 600 /etc/testboard/db.cnf
~/dryrun.cnf`, then edit `database = testboard_dryrun` and pass
`--config ~/dryrun.cnf`. A scratch database created casually, without the
collation, fails the preflight — that is the probe doing its job, not a
fault in the rehearsal. **Do not
estimate the downtime window; measure it here** — the `all` command prints
per-phase timings for exactly this reason. Then check §E.4's verification
passed. Only then schedule the real cutover.

A ~950 MB database is not a thing you can rehearse on a laptop: the export is
~2.4 GB of text and the load is tens of minutes at best. Run the dry run on the
real hardware, over the real network path.

Keep the dry-run database until the real one is verified — it is a free second
opinion if a count disagrees.

### E.2 Freeze

Yours to do, not the script's: these are decisions about when users lose writes.

1. **Stop the feeder** (disable the cron entry). It is the only automated
   writer.
2. Tell the team the dashboard is read-only for the window. Comments and
   assignments made during it will be lost — this is the one irreversible part,
   so keep the window short and do it outside working hours.
3. Note the high-water mark: `python3 run_feeder.py --config <cfg> --status`.
   You will need it in §E.5.

### E.3 Load

Run §D.2 against the live file. Note the finish time.

### E.4 Verify — before letting anyone in

`verify` is part of `all`, but run it again on its own if you have done anything
by hand:

```bash
python3 tools/migrate_to_mariadb.py verify \
    --config /etc/testboard/db.cnf \
    --out /var/tmp/testboard-export
```

It runs each check against MariaDB and compares it to the answer recorded from
SQLite at export time, reporting the first row that differs rather than a wall
of output. Exit 3 on any disagreement.

| Check | Why this one |
|---|---|
| `COUNT(*)` per table | Catches a truncated load. |
| `COUNT(*)` grouped by `environment, result` over `latest_runs` | A few dozen rows; catches misaligned columns, which a bare total will not. |
| `COUNT(*)` grouped by `SUBSTR(start_time, 1, 10), result` over `runs` | A few thousand rows; catches partial loads and timestamp mangling. |
| `MIN(start_time)`, `MAX(start_time)` | Catches truncated or reformatted timestamps. |
| `SUM(LENGTH(output))`, `COUNT(*)` on `run_outputs` | Catches blob corruption in the hex round-trip. **The most likely thing to go wrong** in the whole load. |
| Distinct identity-triple count | If this is *lower* in MariaDB, the collation merged tests. Stop and reload. |
| `schema_version` | Must equal the value recorded in §C.1. |

**Every check is written so the identical SQL runs on both engines** — otherwise
you are comparing two different questions and calling the agreement meaningful.
Two traps in particular:

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

Point it at the dashboard and run a catch-up. The high-water mark is in the
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

Keep the SQLite file, and a copy of the export directory, for at least a
month. The credentials file stays — it is the app's (§A.9); delete only the
dry run's `~/dryrun.cnf` copy (§E.1) if you made one.

---

## F. The application code

The migration above moves the *data*. Making the *dashboard* talk to MariaDB
was separate work, and **it is done** (WP-19, drop 2026-08-07): the server
started with `--db-config /etc/testboard/db.cnf --site-notes PATH` serves
from MariaDB through the vendored driver. **SQLite did not become legacy** —
`--db PATH` is unchanged, permanent, and the zero-setup way to stand up a
second instance; both backends run the storage and API suites in CI on every
push. What remains operator work is exactly §E: the dry run, the freeze, the
load, the verification, and the decision to flip the flag.

### F.1 The driver — decided and shipped

**There is no MariaDB/MySQL driver in the Python standard library**, and the
project constraint is "standard library only, no pip installs". That was
resolved by **vendoring PyMySQL 1.0.2 into `third_party/pymysql/`**: pure
Python, no compilation, works on Python 3.6, MIT-licensed. Dropped into the tree
it is *present*, not *installed* — nothing runs `pip` on the server and the "no
envs to set up" property is preserved. `CLAUDE.md` records the exemption and its
limits; `tests/test_vendored_driver.py` pins the version, proves the import
costs nothing, and holds the driver's serving-path blast radius to exactly
one module (`testboard/mariadb.py` — `DriverImportAllowlistTest`), so that
reverting the decision is still one commit: the vendored tree, the backend
module, the migration tool, and no SQL. `feeder/` remains forbidden from
touching it (it talks HTTP). `tools/migrate_to_mariadb.py` uses it too — see
§D.1 for why that is the point rather than an exception.

The alternatives, recorded so the choice can be re-examined rather than
re-litigated: a C-extension driver (`mysqlclient`, `mariadb`) is faster but
means a compiler or an RPM on the server — worth reconsidering only if your
estate already ships `python3-PyMySQL`; and shelling out to the `mysql` client
is unacceptable **everywhere**, not just in the serving path. In a serving path
the absence of parameter binding makes every query a quoting problem, which is
to say an injection problem. Anywhere else it silently adds a package that must
be installed on the deployment host, which is the one thing this project's
design exists to avoid.

### F.2 How it is built — as shipped (WP-19)

**Phase 0 — SQLite-only groundwork** (plan WP-5, WP-9, WP-10, WP-11):
`julianday()` removed by storing `duration_seconds`; the re-import id
behaviour pinned by a test (§B.5); the portability inventory test that stops
this document's translation tables silently going stale; the export tool; the
vendored driver.

**Phase 1 — the backend seam, shipped.** One `Storage` class, two backend
objects: `_SqliteBackend` in `storage.py` (byte-identical to the pre-seam
behaviour) and `MariaDBBackend` in `testboard/mariadb.py`, reached only via
`Storage.mariadb()` so SQLite deployments never import the driver. The SQL in
`storage.py` stays qmark-canonical **permanently**; the MariaDB connection
wrapper translates at execute time. The whole dialect surface, each rewrite
pinned by a unit test:

- `BEGIN IMMEDIATE` → `START TRANSACTION`; `INSERT OR REPLACE` → `REPLACE`
  (the two §B.5-safe sites); `?` → `%s` with `%` doubled first.
- Two composed-SQL fragments chosen per backend: the search clause
  (case-insensitive over the `_bin` columns via an explicit COLLATE, with the
  `ESCAPE` spelling each engine's literal parsing needs) and the
  LIMIT-with-only-an-offset idiom.
- Connect: `CLIENT.FOUND_ROWS` (rowcount = rows *matched*, as SQLite
  reports), `binary_prefix` (blobs are zlib bytes), autocommit plus explicit
  transactions, `innodb_lock_wait_timeout` per session, and a strict-mode
  assertion — the server that refuses to load non-strict also refuses to
  *serve* non-strict.
- Reconnect: ping-on-borrow after 60 s idle, never inside a transaction,
  never retrying a statement.
- **No DDL, ever.** On MariaDB the startup check verifies `schema_version`
  equals the build's and refuses both directions; the schema only ever comes
  from this runbook's §D.

The 135 execute sites did not each need porting — the seam plus the rewrites
above and the two fragments were the whole job, exactly because Phase 0 had
already removed the date functions and kept the upsert SELECT-first (which
the `output_fingerprint` re-import skip depends on; the port kept it).
`activity_hours` and `script_hours` needed no dialect work at all — SUBSTR
bucketing and plain COUNT/MIN/MAX recomputation are portable as written.

**Phase 2 — CI, shipped.** `.github/workflows/ci.yml` runs a
`python36-mariadb` job: the same ubi8/python-36 container as the
authoritative 3.6 gate plus a **`mariadb:10.3`** service — 10.3 to match the
production stream, not the newest. `TESTBOARD_TEST_DB_CNF` activates
generated dual-backend suites (`tests/backends.py`): every storage and API
test class runs a second time against a schema created from the exporter's
own DDL, so "it works on MariaDB" is a CI fact, on the deployed version,
against the schema the migration actually creates. A handful of tests whose
*instrument* is SQLite (PRAGMA introspection, trace-callback query counting,
a perf pin) are skipped there, each with its reason recorded in
`tests/test_mariadb_backend.py`.

---

## Appendix: errors you are likely to meet

| Symptom | Cause | Fix |
|---|---|---|
| `Access denied for user 'testboard'@'...'` | The grant names a different host from the one MariaDB sees you coming from | §A.1/§A.9 — `localhost` (socket) and the machine's own IP (TCP) are different accounts; the cnf's `socket =` line is what keeps everyone on the socket |
| `the vendored MySQL driver is missing` | `third_party/pymysql` is absent — an incomplete checkout, not a missing install | Restore the directory from the repository. Nothing needs installing; see `third_party/README.md` |
| `Specified key was too long; max key length is 3072 bytes` | `VARCHAR(n)` too large for an indexed column | §B.1 — the tool sizes these from the audit; if you passed them by hand, don't |
| `Duplicate entry '...' for key 'PRIMARY'` during load | Case-insensitive or PAD-SPACE collation merging distinct rows | §A.3 and §C.2. Drop the database and reload; do not "fix" the duplicates |
| `Data too long for column 'test_name'` | `VARCHAR(n)` too small | Good news — strict mode caught it. Re-run the audit and re-export |
| Load succeeds but tests are missing afterwards | Strict mode **off**, values silently truncated and merged | §C.4's probe exists to make this impossible. If you skipped it: reload from scratch |
| `MySQL server has gone away` mid-load | A row exceeded `max_allowed_packet` | §A.5, raise it; the culprit is a large `run_outputs` blob |
| `The used command is not allowed with this MariaDB version` | `LOAD DATA LOCAL INFILE` disabled | Enable `local_infile` on both sides (§A.5, and `local-infile = 1` in the option file) |
| `Cannot add or update a child row: a foreign key constraint fails` | Foreign key constraints were added by hand — the generated schema declares none (§B.6) | Load into the generated schema as-is; adopt constraints, if wanted, as separate later work |
| A load failed partway and the next run says the target is not empty | The wreckage of the first attempt | Drop the tables (the account holds `DROP` on this database, so it can — dropping the *database* itself needs root), then run the `load` step again. Do not load on top |
| `File 'runs.tsv' not found` | The client was run from outside the export directory | §D.3 — `cd` into it first; the tool does this for you |
| Blob comparison fails in §E.4 | Hex round-trip; usually a missing `UNHEX()` or a client charset mangling the hex text | Re-check `load.sql`; blobs must never pass through a character-set conversion |
| Dashboard works, is slow, buffer pool is large | Pool not warm yet, or not actually applied | `SHOW ENGINE INNODB STATUS`; confirm `@@innodb_buffer_pool_size` is what §A.5 set — and that MariaDB was **restarted** after the config change |
| `'cryptography' package is required for sha256_password…` at connect | The account uses a sha256 auth plugin; the vendored driver cannot do those without a compiled package | §A.4 — recreate the account with `mysql_native_password`. Do **not** install `cryptography` on the server; "nothing to build on the server" is the property this whole design protects |
