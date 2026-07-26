# -*- coding: utf-8 -*-
"""Demo and self-test data tooling for testboard.

This package holds the entry scripts that make a fresh clone of the
repository immediately browsable:

- ``tools.generate_demo_data`` — deterministic simulated test-run history
  for a fake ``linux-sim`` environment.
- ``tools.run_self_tests`` — runs this repository's own unittest suite
  in-process and converts each test into a run record (environment
  ``local-unittest``).
- ``tools.demo_bootstrap`` — one zero-argument command from clean clone to
  a browsable dashboard (seeds the database directly, then serves).

This ``__init__`` exists so the modules are importable as ``tools.*`` from
the test suite; all real logic lives in the individual modules.
"""
