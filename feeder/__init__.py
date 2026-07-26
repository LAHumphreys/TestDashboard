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
- :mod:`feeder.state` — the daily-mode high-water-mark state file.

Python 3.6 compatible; standard library only.
"""
