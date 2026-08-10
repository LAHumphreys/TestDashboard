"""The MariaDB schema exactly as it existed at testboard schema v7.

**Derived, not hand-written.** This is a trimmed, byte-for-byte copy of
``tools/export_for_mariadb.py`` as it stood at commit ``8179536``
(WP-18, the last commit before ``6342d41`` (WP-20) added migration 8's
``environment_products`` table) — retrieved with::

    git show 6342d41^:tools/export_for_mariadb.py

and cut down to the three things ``tests/test_upgrade_mariadb_schema.py``
needs (``TABLE_ORDER``, ``Sizes``, ``ddl()``, ``INDEXES``): the
``LOAD DATA``/hex-encoding machinery is dropped because the fixture
seeds its handful of rows with plain parameterized ``INSERT``s instead
(``docs/MARIADB_MIGRATION.md``'s bulk-load path needs
``local_infile=ON`` on the server plus ``local_infile=True`` on the
driver, neither of which the test harness has any reason to depend on
for a dozen rows).

This is what "the existing export/load path" produced for a real
database at v7 — not a guess at what v7 "should" have looked like. If
``tools/upgrade_mariadb_schema.py`` is ever extended past migration 10,
this file stays frozen: it describes the *starting* point the tool
upgrades FROM, which does not change.

Python 3.6 compatible; standard library only.
"""


#: Tables that exist at schema v7 — no ``streams``, no
#: ``environment_products``, both added by migrations 9 and 8
#: respectively.
TABLE_ORDER = (
    "users",
    "runs",
    "run_outputs",
    "latest_runs",
    "comments",
    "assignments",
    "current_assignments",
    "test_retirements",
    "environment_expectations",
    "activity_hours",
    "script_hours",
    "schema_version",
)


class Sizes(object):
    """Chosen VARCHAR lengths for the identity columns."""

    def __init__(self, environment: int, script: int, test_name: int) -> None:
        self.environment = environment
        self.script = script
        self.test_name = test_name


def ddl(sizes: Sizes) -> str:
    """The MariaDB schema at v7, sized by *sizes*."""
    env = "VARCHAR({0})".format(sizes.environment)
    script = "VARCHAR({0})".format(sizes.script)
    name = "VARCHAR({0})".format(sizes.test_name)
    stamp = "VARCHAR(26) CHARACTER SET ascii COLLATE ascii_bin"
    result = "VARCHAR(20) CHARACTER SET ascii COLLATE ascii_bin"
    user = "VARCHAR(100)"
    return """CREATE TABLE users (
  username        {user} NOT NULL,
  created_at      {stamp} NOT NULL,
  deactivated_at  {stamp} NULL,
  deactivated_by  {user} NULL,
  PRIMARY KEY (username)
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
  PRIMARY KEY (id),
  UNIQUE KEY uq_runs_identity (environment, script, test_name, start_time)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE run_outputs (
  run_id BIGINT NOT NULL,
  output LONGBLOB NOT NULL,
  PRIMARY KEY (run_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE latest_runs (
  environment      {env} NOT NULL,
  script           {script} NOT NULL,
  test_name        {name} NOT NULL,
  run_id           BIGINT NOT NULL,
  start_time       {stamp} NOT NULL,
  result           {result} NOT NULL,
  prev_result      {result} NULL,
  duration_seconds DOUBLE NOT NULL DEFAULT 0,
  PRIMARY KEY (environment, script, test_name)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE comments (
  id          BIGINT NOT NULL AUTO_INCREMENT,
  environment {env} NOT NULL,
  script      {script} NOT NULL,
  test_name   {name} NOT NULL,
  author      {user} NOT NULL,
  created_at  {stamp} NOT NULL,
  text        TEXT NOT NULL,
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


#: Secondary indexes, at v7 — the four ``latest_runs`` indexes
#: migration 9 rebuilds with ``stream_id`` leading, none of the fifth
#: (``idx_latest_runs_triple``, added BY migration 9).
INDEXES = """CREATE INDEX idx_runs_start_time_result ON runs (start_time, result);
CREATE INDEX idx_latest_runs_result
  ON latest_runs (result, environment, script, test_name);
CREATE INDEX idx_latest_runs_start_time ON latest_runs (start_time);
CREATE INDEX idx_latest_runs_start_sort
  ON latest_runs (start_time, environment, script, test_name);
CREATE INDEX idx_latest_runs_duration_sort
  ON latest_runs (duration_seconds, environment, script, test_name);
CREATE INDEX idx_comments_triple
  ON comments (environment, script, test_name, id);
CREATE INDEX idx_assignments_triple
  ON assignments (environment, script, test_name, id);
CREATE INDEX idx_current_assignments_assignee
  ON current_assignments (assignee);
"""
