"""One-off import of the old bot's export (``legacy/``) into Postgres.

``coach import legacy`` reads the export directory (``COACH_LEGACY_DIR`` or ``--dir``) and
writes through :mod:`thresy.store` only. See :mod:`thresy.importer.legacy` for what is read and
``docs/import.md`` for how to run and verify it.
"""
