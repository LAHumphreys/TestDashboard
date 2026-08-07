"""The shared option-file parser, tested as the library it now is.

``tools/migrate_to_mariadb.py`` and ``run_server.py --db-config`` read
the same credentials file through :mod:`testboard.dbconfig`. The
migration tool's own tests keep pinning its SystemExit behaviour; these
pin the library contract underneath — most importantly that errors are
:class:`DbConfigError` and NOT ``SystemExit``, because the server has
to catch them and print a startup message rather than die with a
traceback-shaped exit.

Python 3.6 compatible; standard library only.
"""

import io
import os
import shutil
import tempfile
import unittest

from testboard.dbconfig import DbConfigError, Settings, read_option_file


class ParseTest(unittest.TestCase):
    """The mysql client's file format, faithfully."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_dbconfig_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text: str) -> str:
        path = os.path.join(self.tmp, "db.cnf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_it_reads_the_client_section(self) -> None:
        path = self.write(
            "[client]\n"
            "host = dbhost.example\n"
            "port = 3307\n"
            "user = testboard_app\n"
            "password = s3cret\n"
            "database = testboard\n")
        settings = read_option_file(path)
        self.assertEqual(settings.host, "dbhost.example")
        self.assertEqual(settings.port, 3307)
        self.assertEqual(settings.user, "testboard_app")
        self.assertEqual(settings.password, "s3cret")
        self.assertEqual(settings.database, "testboard")
        self.assertIsNone(settings.unix_socket)

    def test_host_and_port_have_defaults(self) -> None:
        settings = read_option_file(self.write(
            "[client]\nuser=u\npassword=p\ndatabase=d\n"))
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 3306)

    def test_a_socket_line_is_carried_through(self) -> None:
        """On a same-box install the socket IS the identity the grants
        were written for (runbook §A.9)."""
        settings = read_option_file(self.write(
            "[client]\nuser=u\npassword=p\ndatabase=d\n"
            "socket=/var/lib/mysql/mysql.sock\n"))
        self.assertEqual(settings.unix_socket, "/var/lib/mysql/mysql.sock")

    def test_bare_keys_and_include_directives_do_not_break_it(self) -> None:
        """my.cnf allows both; configparser rejects both — which is why
        this is hand-parsed."""
        settings = read_option_file(self.write(
            "!includedir /etc/my.cnf.d\n"
            "[client]\nlocal-infile\nuser=u\npassword=p\ndatabase=d\n"))
        self.assertEqual(settings.user, "u")

    def test_quotes_are_stripped_once(self) -> None:
        settings = read_option_file(self.write(
            "[client]\nuser=u\npassword=\"p a s s\"\ndatabase=d\n"))
        self.assertEqual(settings.password, "p a s s")

    def test_a_hash_mid_password_is_not_a_comment(self) -> None:
        settings = read_option_file(self.write(
            "[client]\nuser=u\npassword=aa#bb\ndatabase=d\n"))
        self.assertEqual(settings.password, "aa#bb")

    def test_server_sections_are_ignored(self) -> None:
        """[mysqld] is the server's own config; reading it would adopt
        the daemon's user= as our login."""
        settings = read_option_file(self.write(
            "[mysqld]\nuser=mysql\n[client]\nuser=u\npassword=p\n"
            "database=d\n"))
        self.assertEqual(settings.user, "u")


class ErrorTest(unittest.TestCase):
    """Errors are DbConfigError — catchable, never process-killing."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testboard_dbconfig_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text: str) -> str:
        path = os.path.join(self.tmp, "db.cnf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_errors_are_not_system_exit(self) -> None:
        """The server catches these at startup. SystemExit would skip
        an except Exception handler and kill the process instead."""
        self.assertTrue(issubclass(DbConfigError, Exception))
        self.assertFalse(issubclass(DbConfigError, SystemExit))

    def test_a_missing_file_names_the_runbook_section(self) -> None:
        with self.assertRaises(DbConfigError) as caught:
            read_option_file(os.path.join(self.tmp, "nope.cnf"))
        self.assertIn("A.9", str(caught.exception))

    def test_missing_credentials_name_what_is_missing(self) -> None:
        with self.assertRaises(DbConfigError) as caught:
            read_option_file(self.write("[client]\nuser=u\ndatabase=d\n"))
        self.assertIn("password", str(caught.exception))

    def test_a_non_numeric_port_is_a_config_error_not_a_traceback(
            self) -> None:
        """int('abc') raising bare ValueError at server startup would
        read as a crash in the parser, not as the typo it is."""
        with self.assertRaises(DbConfigError) as caught:
            read_option_file(self.write(
                "[client]\nuser=u\npassword=p\ndatabase=d\nport=abc\n"))
        self.assertIn("port", str(caught.exception))
        self.assertIn("abc", str(caught.exception))


class DescribeTest(unittest.TestCase):
    """describe() goes into logs somebody keeps."""

    def test_it_never_contains_the_password(self) -> None:
        settings = Settings(host="h", port=3306, user="u",
                            password="hunter2", database="d",
                            unix_socket=None)
        self.assertNotIn("hunter2", settings.describe())
        self.assertIn("u@", settings.describe())

    def test_a_socket_replaces_host_and_port_in_the_description(
            self) -> None:
        settings = Settings(host="h", port=3306, user="u", password="p",
                            database="d", unix_socket="/tmp/x.sock")
        self.assertIn("/tmp/x.sock", settings.describe())
        self.assertNotIn("3306", settings.describe())


if __name__ == "__main__":
    unittest.main()
