"""Generic feeder framework for pushing test-run records into testboard.

The feeder is the half of the import pipeline that ships with the open-source
project; the *reader* half (how records are extracted from a site's own log
files / databases / CI artifacts) is written on site and plugged in via
``feeder.reader.load_reader``.

Modules:

- :mod:`feeder.reader` — the :class:`~feeder.reader.Reader` abstract base
  class, the built-in JSON-lines reader, and the reader-spec loader.
- :mod:`feeder.submitter` — validation, batching, HTTP submission with
  retry/backoff, and failed-batch replay files.
- :mod:`feeder.state` — the catchup-mode high-water-mark state file.
- :mod:`feeder.check` — offline validation of a reader, no server needed.
- :mod:`feeder.config` — the optional JSON config file.
- :mod:`feeder.preflight` — checks run before an import does any work.
- :mod:`feeder.init` — the interactive ``--init`` setup wizard.

Python 3.6 compatible; standard library only.
"""

#: Reported by ``run_feeder.py --version``. The feeder and the dashboard
#: ship together, so this identifies the checkout rather than a separately
#: released package.
__version__ = "1.0.0"
