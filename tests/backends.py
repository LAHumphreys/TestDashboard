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

The named database is DROPPED and recreated at process start. The
config file is the guard against pointing this at anything precious:
whatever database it names is sacrificial by definition.

Python 3.6 compatible; standard library plus the vendored driver.
"""

import os
from typing import Any, List, Optional

from testboard import dbconfig
from testboard.storage import MIGRATIONS, Storage

MARIADB_CNF = os.environ.get("TESTBOARD_TEST_DB_CNF", "")
MARIADB_AVAILABLE = bool(MARIADB_CNF)

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
    ddl = exporter.ddl(exporter.Sizes(64, 255, 255))
    for statement in split_statements(ddl):
        _run(statement)
    for statement in split_statements(exporter.INDEXES):
        _run(statement)
    _TABLES = tuple(exporter.TABLE_ORDER)
    _schema_ready = True


def reset_database() -> None:
    """Empty every table and restore the schema_version row.

    TRUNCATE rather than DELETE: it also resets AUTO_INCREMENT, so the
    id-stability tests see the same fresh-database numbering a new
    SQLite file gives them.
    """
    ensure_schema()
    for table in _TABLES:
        _run("TRUNCATE TABLE `{0}`".format(table))
    _run("INSERT INTO schema_version (version) VALUES ({0})".format(
        MIGRATIONS[-1][0]))


def mariadb_storage(max_connections: Optional[int] = None) -> Storage:
    """A Storage over a freshly reset MariaDB test database."""
    reset_database()
    if max_connections is None:
        return Storage.mariadb(settings())
    return Storage.mariadb(settings(), max_connections=max_connections)
