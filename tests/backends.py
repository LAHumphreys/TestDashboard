"""Backend selection for the dual-run test suites.

When ``TESTBOARD_TEST_DB_CNF`` names a mysql option file (the same
section-A.9/A.10 format everything else reads), the storage and API
suites can be run a second time against a real MariaDB server:
``tests/test_mariadb_backend.py`` generates a subclass per test class,
each overriding the ``_make_storage`` hook. When the variable is
unset — every ordinary local run — that module defines NOTHING, so the
test count does not move and no skip noise appears. SQLite is not the
fallback here; it is the other first-class backend, and its suite runs
unchanged either way.

The schema is created ONCE per process from
``tools/export_for_mariadb.ddl()`` — deliberately the exact DDL the
data migration loads, so the suites prove the app runs against the
schema the migration actually creates, not a lookalike. Per-test
isolation is TRUNCATE-all (which also resets AUTO_INCREMENT, matching
the fresh-file semantics the id-stability tests assume) plus
re-seeding the ``schema_version`` row. The app itself never runs DDL
on MariaDB; this harness is not the app.

**``TESTBOARD_TEST_DB_VIA_UPGRADE`` (opt-in, WP-27).** Set alongside
``TESTBOARD_TEST_DB_CNF`` and the ONE-TIME schema build above is
replaced: a v7 schema (``tests/mariadb_v7_fixture.py``) is loaded, then
``tools/upgrade_mariadb_schema.py``'s own ``plan()`` — the exact
statements a live operator run executes, not a re-derivation of them —
is run against it to reach v10. Everything downstream (TRUNCATE-based
per-test reset, ``schema_version``/mainline-stream reseeding) is
UNCHANGED, because it does not care how the schema it is truncating came
to exist. The point: with this set, the ENTIRE dual-backend suite (every
existing test in ``tests/test_mariadb_backend.py``, ~2,900 cases) runs
against a database that reached v10 by upgrading a v7 database, not by a
fresh v10 load — the one thing ``upgrade_mariadb_schema.py``'s own
schema-diff proves is STRUCTURALLY identical but cannot prove behaves
identically under real queries. Off by default, including in CI: the
same suite runs about twice as long with it on, and turning it on is a
deliberate choice about what a session is verifying, not a standing
cost every push should pay.

The named database is DROPPED and recreated at process start. The
config file is the guard against pointing this at anything precious:
whatever database it names is sacrificial by definition.

Python 3.6 compatible; standard library plus the vendored driver.
"""

import os
from typing import Any, List, Optional

from testboard import dbconfig, model
from testboard.storage import MAINLINE_STREAM_ID, MIGRATIONS, Storage

MARIADB_CNF = os.environ.get("TESTBOARD_TEST_DB_CNF", "")
MARIADB_AVAILABLE = bool(MARIADB_CNF)

#: See the module docstring. Any non-empty value opts in.
VIA_UPGRADE = bool(os.environ.get("TESTBOARD_TEST_DB_VIA_UPGRADE", ""))

_settings = None  # type: Optional[dbconfig.Settings]
_admin = None  # type: Any
_schema_ready = False
_TABLES = ()  # type: tuple


def settings() -> dbconfig.Settings:
    """The test-server settings, read once."""
    global _settings
    if _settings is None:
        _settings = dbconfig.read_option_file(MARIADB_CNF)
    return _settings


def _admin_conn() -> Any:
    """One process-wide connection for schema bootstrap and resets."""
    global _admin
    if _admin is None:
        from third_party import pymysql
        cfg = settings()
        kwargs = {
            "user": cfg.user,
            "password": cfg.password,
            "charset": "utf8mb4",
            "autocommit": True,
        }  # type: dict
        if cfg.unix_socket:
            kwargs["unix_socket"] = cfg.unix_socket
        else:
            kwargs["host"] = cfg.host
            kwargs["port"] = cfg.port
        _admin = pymysql.connect(**kwargs)
    return _admin


def _run(sql: str) -> None:
    cursor = _admin_conn().cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def ensure_schema() -> None:
    """Drop and recreate the test database with the migration's DDL.

    Once per process. The collation is the runbook's §A.3 choice —
    case-sensitive, NO PAD — because that IS the schema under test.
    """
    global _schema_ready, _TABLES
    if _schema_ready:
        return
    from tools import export_for_mariadb as exporter
    from tools.migrate_to_mariadb import split_statements
    name = settings().database
    _run("DROP DATABASE IF EXISTS `{0}`".format(name))
    _run("CREATE DATABASE `{0}` CHARACTER SET utf8mb4 "
         "COLLATE utf8mb4_nopad_bin".format(name))
    _run("USE `{0}`".format(name))
    if VIA_UPGRADE:
        _build_schema_via_upgrade()
    else:
        ddl = exporter.ddl(exporter.Sizes(64, 255, 255))
        for statement in split_statements(ddl):
            _run(statement)
        for statement in split_statements(exporter.INDEXES):
            _run(statement)
    _TABLES = tuple(exporter.TABLE_ORDER)
    _schema_ready = True


def _build_schema_via_upgrade() -> None:
    """The VIA_UPGRADE path: v7, then ``upgrade_mariadb_schema.plan()``.

    Loads the frozen v7 schema (``tests/mariadb_v7_fixture.py`` — the
    real exporter DDL as it stood before migration 8, see that module's
    own docstring for provenance), then runs the EXACT statement plan
    ``tools/upgrade_mariadb_schema.py``'s live ``upgrade`` command would
    run — not a second, hand-written translation of it — against this
    connection directly. No data: this is a schema-only build, so there
    is nothing for the ``ADD COLUMN ... DEFAULT`` backfills to do; what
    is under test is that the STATEMENTS this tool emits are the ones
    ``tools/export_for_mariadb.py``'s DDL generator would also produce,
    which ``upgrade_mariadb_schema.schema_diff`` already checks once per
    real upgrade run and this harness does not need to re-check per
    test.
    """
    from tests import mariadb_v7_fixture as v7_fixture
    from tools import export_for_mariadb as exporter
    from tools import upgrade_mariadb_schema as upgrade
    from tools.migrate_to_mariadb import split_statements
    sizes = v7_fixture.Sizes(64, 255, 255)
    for statement in split_statements(v7_fixture.ddl(sizes)):
        _run(statement)
    for statement in split_statements(v7_fixture.INDEXES):
        _run(statement)
    _run("INSERT INTO schema_version (version) VALUES (7)")
    now = model.format_iso(model.utcnow())
    oracle_sizes = exporter.Sizes(
        sizes.environment, sizes.script, sizes.test_name)
    for _from_version, statements in upgrade.plan(oracle_sizes, now):
        for statement in statements:
            _run(statement)


def reset_database() -> None:
    """Empty every table and restore the schema_version and mainline-
    stream rows.

    TRUNCATE rather than DELETE: it also resets AUTO_INCREMENT, so the
    id-stability tests see the same fresh-database numbering a new
    SQLite file gives them.

    ``streams`` row 1 (WP-21, migration 9): on SQLite this is seeded by
    a migration Python step, but this backend never runs migrations —
    its schema comes straight from ``exporter.ddl()`` (no data) — so
    the harness seeds it here, the same way it already seeds
    ``schema_version``. In production the row exists because the
    SQLite source database was migrated (and therefore seeded) BEFORE
    export; this harness has no source database to export from.
    """
    ensure_schema()
    for table in _TABLES:
        _run("TRUNCATE TABLE `{0}`".format(table))
    _run("INSERT INTO schema_version (version) VALUES ({0})".format(
        MIGRATIONS[-1][0]))
    now = model.format_iso(model.utcnow())
    _run(
        "INSERT INTO streams (id, product, kind, name, first_seen, "
        "last_seen) VALUES ({0}, '', 'mainline', '', '{1}', "
        "'{1}')".format(MAINLINE_STREAM_ID, now)
    )


def mariadb_storage(max_connections: Optional[int] = None) -> Storage:
    """A Storage over a freshly reset MariaDB test database."""
    reset_database()
    if max_connections is None:
        return Storage.mariadb(settings())
    return Storage.mariadb(settings(), max_connections=max_connections)
