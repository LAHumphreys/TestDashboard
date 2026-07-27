# Vendored third-party code

**Do not edit anything in this directory.** To update a package, replace
its directory wholesale with a new release and run the full suite. Local
edits are lost at the next update and there is no record that they
existed.

## Why vendor at all

testboard deploys onto a RHEL 8 box running the platform Python (3.6.8),
with no pip install step, no build step, and no virtual environment to
set up. That constraint is what makes the thing deployable by someone who
is not its author, and it is worth keeping.

The stdlib has no MySQL/MariaDB driver, so a MariaDB backend needs one.
Vendoring a **pure-Python** package keeps the property that matters:
the code is *present* rather than *installed*. Copy the checkout, run it.
Nothing to resolve, nothing to compile, no network access needed on the
server, no version to drift.

This is a narrow exemption, not an open door. It applies where the stdlib
has no equivalent and the package is pure Python with no dependencies of
its own.

## What is here

### pymysql — 1.0.2

| | |
|---|---|
| **Package** | PyMySQL |
| **Version** | 1.0.2 |
| **Source** | https://pypi.org/project/PyMySQL/1.0.2/ |
| **Retrieved** | 2026-07-27 |
| **Licence** | MIT (`pymysql/LICENSE`) — compatible with this repository's MIT licence |
| **Dependencies** | None. Every import is stdlib. |

**Why this version.** 1.0.2 is the last release that supports Python 3.6;
1.1.0 raised its floor to 3.7. Verified: every file parses under
`ast.parse(..., feature_version=(3, 6))`, which
`tests/test_python36_compat.py` re-checks on every run so an update
cannot quietly raise the floor.

**One deployment note that will otherwise cost someone an evening.**
`pymysql/_auth.py` imports the `cryptography` package inside a
`try`/`except`, and needs it for the `sha256_password` and
`caching_sha2_password` authentication plugins. `cryptography` is a
compiled package, so it is deliberately **not** vendored — vendoring it
would give up the whole "nothing to build" property.

MariaDB's default is `mysql_native_password`, which needs none of it. But
if the database account is created with a sha256-based plugin, connecting
fails with:

> `'cryptography' package is required for sha256_password or
> caching_sha2_password auth methods`

The fix is to create the account with `mysql_native_password`, not to
install anything. See `docs/MARIADB_MIGRATION.md` §A.2.

## How this is tested

- `tests/test_python36_compat.py` — `VendoredCodeTest` asserts every
  vendored file parses as 3.6 and uses nothing that fails at import on
  3.6. It deliberately does **not** apply this project's style rules to
  code we did not write: holding upstream to them would mean either
  editing it (making it un-updatable) or accumulating excuses (making the
  gate meaningless).
- `tests/test_vendored_driver.py` — asserts the package imports, exposes
  the DB-API surface the storage layer will use, ships its licence, and
  opens no socket and starts no thread merely by being imported.
