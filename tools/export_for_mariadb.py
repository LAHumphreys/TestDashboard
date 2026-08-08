#!/usr/bin/env python3
"""Export a testboard SQLite database into files MariaDB can load.

Deliberately needs **no MySQL driver**: it reads SQLite read-only and
writes text, and the `mysql` command-line client does the loading. That
is what lets the whole data migration run under the project's "nothing
installed on the server" constraint, and it is why this half can be
written and tested on a machine with no MariaDB anywhere near it.

What it writes (see ``docs/MARIADB_MIGRATION.md`` §D.1):

  schema.sql        CREATE TABLE DDL, with VARCHAR sizes passed in
  <table>.tsv       one file per table, tab-separated
  run_outputs.tsv   run_id + the blob HEX-ENCODED (hence ~2x the size)
  load.sql          LOAD DATA statements in foreign-key order
  verify_source.txt the agreement checks, computed from SQLite
  verify.sql        the SAME checks, for MariaDB

The verification queries are generated from ONE list so the two sides
cannot drift into asking different questions and reporting the agreement
as meaningful.

**The load half cannot be verified here.** There is no MariaDB in this
environment or in CI, so the tests cover the export, the escaping and
the ordering. Run §E.1's dry run before trusting the rest.

Python 3.6 compatible; standard library only.
"""

import argparse
import binascii
import io
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

#: NULL sentinel understood by ``LOAD DATA`` in its default escaping.
NULL = "\\N"

#: Tables in an order that satisfies the foreign keys InnoDB will
#: actually enforce (SQLite does not, so the current file may contain
#: orphans — see the runbook §C.1).
TABLE_ORDER = (
    "users",
    "streams",
    "runs",
    "run_outputs",
    "latest_runs",
    "comments",
    "assignments",
    "current_assignments",
    "test_retirements",
    "environment_expectations",
    "environment_products",
    "activity_hours",
    "script_hours",
    "schema_version",
)

#: The blob column, exported as hex and re-assembled with UNHEX().
BLOB_COLUMNS = {"run_outputs": "output"}

#: Agreement checks, as SQL that runs BYTE-IDENTICALLY on SQLite and
#: MariaDB. Anything engine-specific here would compare two different
#: questions — see the runbook §E.4. In particular: COUNT(DISTINCT a,b,c)
#: is MariaDB-only, and DATE() parses an ISO string on one engine and
#: not the other.
VERIFY_QUERIES = (
    ("runs_total", "SELECT COUNT(*) FROM runs"),
    ("outputs_total", "SELECT COUNT(*) FROM run_outputs"),
    ("latest_total", "SELECT COUNT(*) FROM latest_runs"),
    ("users_total", "SELECT COUNT(*) FROM users"),
    ("comments_total", "SELECT COUNT(*) FROM comments"),
    ("assignments_total", "SELECT COUNT(*) FROM assignments"),
    ("retirements_total", "SELECT COUNT(*) FROM test_retirements"),
    ("expectations_total",
     "SELECT COUNT(*) FROM environment_expectations"),
    ("products_total",
     "SELECT COUNT(*) FROM environment_products"),
    ("streams_total", "SELECT COUNT(*) FROM streams"),
    ("activity_total",
     "SELECT COUNT(*), SUM(count) FROM activity_hours"),
    ("script_activity_total",
     "SELECT COUNT(*), SUM(count) FROM script_hours"),
    ("schema_version", "SELECT version FROM schema_version"),
    ("run_span", "SELECT MIN(start_time), MAX(start_time) FROM runs"),
    ("output_bytes",
     "SELECT COUNT(*), SUM(LENGTH(output)) FROM run_outputs"),
    ("distinct_tests",
     "SELECT COUNT(*) FROM (SELECT DISTINCT environment, script, "
     "test_name FROM runs) AS t"),
    ("by_env_result",
     "SELECT environment, result, COUNT(*) FROM latest_runs "
     "GROUP BY environment, result ORDER BY environment, result"),
    ("by_day_result",
     "SELECT SUBSTR(start_time, 1, 10) AS day, result, COUNT(*) "
     "FROM runs GROUP BY day, result ORDER BY day, result"),
)


class Sizes(object):
    """Chosen VARCHAR lengths for the identity columns."""

    def __init__(self, environment: int, script: int, test_name: int) -> None:
        self.environment = environment
        self.script = script
        self.test_name = test_name

    def index_bytes(self) -> int:
        """Bytes the widest index would need, for the 3072 limit.

        utf8mb4 costs up to 4 bytes per character; ``start_time`` is
        ASCII and 26 characters. See the runbook §B.1.
        """
        return (self.environment + self.script + self.test_name) * 4 + 26


def ddl(sizes: Sizes) -> str:
    """The MariaDB schema, sized by *sizes*."""
    env = "VARCHAR({0})".format(sizes.environment)
    script = "VARCHAR({0})".format(sizes.script)
    name = "VARCHAR({0})".format(sizes.test_name)
    # ASCII + binary collation for the machine-generated columns: exact,
    # case-sensitive, and far smaller in an index than utf8mb4.
    stamp = "VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin"
    result = "VARCHAR(20) CHARACTER SET ascii COLLATE ascii_bin"
    user = "VARCHAR(100)"
    return """-- testboard schema for MariaDB. Generated by
-- tools/export_for_mariadb.py; see docs/MARIADB_MIGRATION.md.
--
-- The database must already exist with a case-sensitive, NO PAD
-- collation (runbook A.1) and strict mode must be on (A.3). Prove both
-- with the probes in C.2 and C.4 BEFORE loading anything: a
-- case-insensitive collation silently merges distinct tests, and a
-- non-strict server silently truncates an over-long test name into a
-- collision.

CREATE TABLE users (
  username        {user} NOT NULL,
  created_at      {stamp} NOT NULL,
  deactivated_at  {stamp} NULL,
  deactivated_by  {user} NULL,
  PRIMARY KEY (username)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE streams (
  id         BIGINT NOT NULL AUTO_INCREMENT,
  product    VARCHAR(255) NOT NULL,
  kind       VARCHAR(20) NOT NULL,
  name       VARCHAR(255) NOT NULL,
  first_seen {stamp} NOT NULL,
  last_seen  {stamp} NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_streams_identity (product, kind, name)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE runs (
  id                   BIGINT NOT NULL AUTO_INCREMENT,
  environment          {env} NOT NULL,
  script               {script} NOT NULL,
  test_name            {name} NOT NULL,
  result               {result} NOT NULL,
  start_time           {stamp} NOT NULL,
  end_time             {stamp} NOT NULL,
  source_link          VARCHAR(1024) NOT NULL,
  known_failure_reason TEXT NULL,
  output_fingerprint   VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NULL,
  stream_id            BIGINT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  UNIQUE KEY uq_runs_identity (environment, script, test_name, start_time)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE run_outputs (
  run_id BIGINT NOT NULL,
  output LONGBLOB NOT NULL,
  PRIMARY KEY (run_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE latest_runs (
  stream_id        BIGINT NOT NULL DEFAULT 1,
  environment      {env} NOT NULL,
  script           {script} NOT NULL,
  test_name        {name} NOT NULL,
  run_id           BIGINT NOT NULL,
  start_time       {stamp} NOT NULL,
  result           {result} NOT NULL,
  prev_result      {result} NULL,
  duration_seconds DOUBLE NOT NULL DEFAULT 0,
  PRIMARY KEY (stream_id, environment, script, test_name)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE comments (
  id          BIGINT NOT NULL AUTO_INCREMENT,
  environment {env} NOT NULL,
  script      {script} NOT NULL,
  test_name   {name} NOT NULL,
  author      {user} NOT NULL,
  created_at  {stamp} NOT NULL,
  text        TEXT NOT NULL,
  stream_id   BIGINT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE assignments (
  id          BIGINT NOT NULL AUTO_INCREMENT,
  environment {env} NOT NULL,
  script      {script} NOT NULL,
  test_name   {name} NOT NULL,
  assignee    {user} NULL,
  assigned_by {user} NOT NULL,
  assigned_at {stamp} NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE current_assignments (
  environment {env} NOT NULL,
  script      {script} NOT NULL,
  test_name   {name} NOT NULL,
  assignee    {user} NULL,
  PRIMARY KEY (environment, script, test_name)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE test_retirements (
  environment {env} NOT NULL,
  script      {script} NOT NULL,
  test_name   {name} NOT NULL,
  retired_at  {stamp} NOT NULL,
  retired_by  {user} NOT NULL,
  PRIMARY KEY (environment, script, test_name)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE environment_expectations (
  environment    {env} NOT NULL,
  expected_tests INT NOT NULL,
  updated_at     {stamp} NOT NULL,
  updated_by     {user} NOT NULL,
  PRIMARY KEY (environment)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE environment_products (
  environment {env} NOT NULL,
  product     VARCHAR(255) NOT NULL,
  updated_at  {stamp} NOT NULL,
  updated_by  {user} NOT NULL,
  PRIMARY KEY (environment)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE activity_hours (
  environment {env} NOT NULL,
  hour        VARCHAR(13) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  result      {result} NOT NULL,
  count       INT NOT NULL,
  PRIMARY KEY (environment, hour, result)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE script_hours (
  environment {env} NOT NULL,
  hour        VARCHAR(13) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  script      {script} NOT NULL,
  result      {result} NOT NULL,
  count       INT NOT NULL,
  first_start {stamp} NOT NULL,
  last_end    {stamp} NOT NULL,
  PRIMARY KEY (environment, hour, script, result)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE schema_version (
  version INT NOT NULL
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
""".format(env=env, script=script, name=name, stamp=stamp,
           result=result, user=user)


#: Secondary indexes, created AFTER the load. Building an index once
#: over a full table is far cheaper than maintaining it row by row.
INDEXES = """-- Created after loading: building an index once beats
-- maintaining it per row.
CREATE INDEX idx_runs_start_time_result ON runs (start_time, result);
CREATE INDEX idx_latest_runs_result
  ON latest_runs (stream_id, result, environment, script, test_name);
CREATE INDEX idx_latest_runs_start_time
  ON latest_runs (stream_id, start_time);
CREATE INDEX idx_latest_runs_start_sort
  ON latest_runs (stream_id, start_time, environment, script, test_name);
CREATE INDEX idx_latest_runs_duration_sort
  ON latest_runs (stream_id, duration_seconds, environment, script,
                   test_name);
CREATE INDEX idx_latest_runs_triple
  ON latest_runs (environment, script, test_name);
CREATE INDEX idx_comments_triple
  ON comments (environment, script, test_name, id);
CREATE INDEX idx_assignments_triple
  ON assignments (environment, script, test_name, id);
CREATE INDEX idx_current_assignments_assignee
  ON current_assignments (assignee);
"""


def escape(value: Any) -> str:
    """Render one value for a tab-separated ``LOAD DATA`` field.

    ``LOAD DATA`` reads backslash escapes by default, so the backslash
    itself must be escaped FIRST — doing it after would double the
    backslashes introduced for tabs and newlines.
    """
    if value is None:
        return NULL
    if isinstance(value, bytes):
        return binascii.hexlify(value).decode("ascii")
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace("\t", "\\t")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")
    return text


def unescape(field: str) -> Optional[str]:
    """Inverse of :func:`escape`, for the round-trip test."""
    if field == NULL:
        return None
    out = []  # type: List[str]
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 1 < len(field):
            nxt = field[index + 1]
            out.append({"t": "\t", "n": "\n", "r": "\r",
                        "\\": "\\"}.get(nxt, nxt))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def columns_of(conn: sqlite3.Connection, table: str) -> List[str]:
    """Column names of *table*, in declaration order."""
    return [row[1] for row in conn.execute(
        "PRAGMA table_info({0})".format(table))]


def export_table(
    conn: sqlite3.Connection, table: str, path: str
) -> Tuple[int, List[str]]:
    """Stream one table to a TSV file. Returns (rows, columns).

    Streams rather than materialising: the production database is
    ~900 MB and most of it is one table.
    """
    cols = columns_of(conn, table)
    blob = BLOB_COLUMNS.get(table)
    written = 0
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        cursor = conn.execute(
            "SELECT {0} FROM {1}".format(", ".join(cols), table))
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for row in batch:
                handle.write("\t".join(escape(v) for v in row))
                handle.write("\n")
                written += 1
    return written, cols


def load_sql(tables: Dict[str, List[str]]) -> str:
    """LOAD DATA statements, in foreign-key order, blobs UNHEXed."""
    parts = [
        "-- Generated by tools/export_for_mariadb.py.",
        "-- Run AFTER schema.sql, with --local-infile=1 (runbook D.3).",
        "SET FOREIGN_KEY_CHECKS = 0;",
        "SET UNIQUE_CHECKS = 0;",
        "",
    ]
    for table in TABLE_ORDER:
        if table not in tables:
            continue
        cols = tables[table]
        blob = BLOB_COLUMNS.get(table)
        if blob:
            # The blob arrives as hex text in a user variable and is
            # turned back into bytes by UNHEX. It must never pass
            # through a character-set conversion.
            targets = ["@hex_" + c if c == blob else c for c in cols]
            setter = "\nSET {0} = UNHEX(@hex_{0})".format(blob)
        else:
            targets = list(cols)
            setter = ""
        parts.append(
            "LOAD DATA LOCAL INFILE '{table}.tsv'\n"
            "INTO TABLE {table}\n"
            "CHARACTER SET utf8mb4\n"
            "FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\'\n"
            "LINES TERMINATED BY '\\n'\n"
            "({cols}){setter};\n".format(
                table=table, cols=", ".join(targets), setter=setter))
    parts.append("SET UNIQUE_CHECKS = 1;")
    parts.append("SET FOREIGN_KEY_CHECKS = 1;")
    parts.append("")
    parts.append(INDEXES)
    return "\n".join(parts)


def verify_source(conn: sqlite3.Connection) -> str:
    """Run the agreement checks against SQLite and format the answers."""
    lines = [
        "# Agreement checks computed from the SQLite source.",
        "# Compare against the output of verify.sql run on MariaDB.",
        "# Every query is byte-identical on both engines (runbook E.4).",
        "",
    ]
    for name, sql in VERIFY_QUERIES:
        lines.append("== {0}".format(name))
        for row in conn.execute(sql):
            lines.append("\t".join(
                NULL if v is None else str(v) for v in row))
        lines.append("")
    return "\n".join(lines)


def verify_sql() -> str:
    """The same checks, for the mysql client."""
    lines = [
        "-- The SAME queries as verify_source.txt, from one list in",
        "-- tools/export_for_mariadb.py. Two hand-written variants would",
        "-- drift, and a verification step that drifts reports agreement",
        "-- between two different questions.",
        "",
    ]
    for name, sql in VERIFY_QUERIES:
        lines.append("SELECT '== {0}' AS check_name;".format(name))
        lines.append(sql + ";")
        lines.append("")
    return "\n".join(lines)


def write(path: str, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def export(
    db_path: str, out_dir: str, sizes: Sizes,
    log: Any = None,
) -> Dict[str, int]:
    """Do the export. Returns row counts per table."""
    say = log or (lambda message: None)
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise SystemExit(
            "output directory {0} is not empty. Refusing to overwrite an "
            "export — point --out at a new directory.".format(out_dir))
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    if sizes.index_bytes() > 3072:
        raise SystemExit(
            "those column sizes need {0} bytes of index key and InnoDB "
            "allows 3072. Reduce --script-len / --test-len; see "
            "docs/MARIADB_MIGRATION.md B.1.".format(sizes.index_bytes()))

    conn = sqlite3.connect("file:{0}?mode=ro".format(db_path), uri=True)
    try:
        counts = {}  # type: Dict[str, int]
        tables = {}  # type: Dict[str, List[str]]
        for table in TABLE_ORDER:
            started = time.time()
            rows, cols = export_table(
                conn, table, os.path.join(out_dir, table + ".tsv"))
            counts[table] = rows
            tables[table] = cols
            say("  {0:<22} {1:>10,} rows  {2:.1f}s".format(
                table, rows, time.time() - started))
        write(os.path.join(out_dir, "schema.sql"), ddl(sizes))
        write(os.path.join(out_dir, "load.sql"), load_sql(tables))
        write(os.path.join(out_dir, "verify_source.txt"),
              verify_source(conn))
        write(os.path.join(out_dir, "verify.sql"), verify_sql())
        return counts
    finally:
        conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run the length audit in docs/MARIADB_MIGRATION.md C.1 "
               "first and round the sizes up generously — re-running the "
               "whole migration because a name was eight characters too "
               "long is a bad afternoon.",
    )
    parser.add_argument("--db", required=True, help="SQLite file (read-only)")
    parser.add_argument("--out", required=True, help="empty output directory")
    parser.add_argument("--env-len", type=int, default=64)
    parser.add_argument("--script-len", type=int, default=255)
    parser.add_argument("--test-len", type=int, default=255)
    args = parser.parse_args(argv)

    sizes = Sizes(args.env_len, args.script_len, args.test_len)
    print("exporting {0} -> {1}".format(args.db, args.out))
    print("index key budget: {0} of 3072 bytes".format(sizes.index_bytes()))
    counts = export(args.db, args.out, sizes, log=print)
    print("done. {0:,} runs, {1:,} outputs.".format(
        counts.get("runs", 0), counts.get("run_outputs", 0)))
    print("next: load schema.sql then load.sql (runbook D.3), then "
          "compare verify_source.txt with verify.sql's output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
