0.7.0.3
=======

- Presto SQLAlchemy dialect: keep full precision for DECIMAL results and
  parameters, return ``date``/``datetime``/``time`` objects for typed temporal
  columns, reflect parameterized types (``decimal(p,s)``, ``varchar(n)``,
  ``char(n)``, ``time``, ``... with time zone``), implement
  ``get_view_names``, return a constraint dict from ``get_pk_constraint``, and
  bind the minimum BIGINT.
- Hive: ``executemany`` of INSERT and other statements without a result set
  no longer fails; ``has_table`` returns False for Hive 4's database-qualified
  "Table not found" error; ``get_pk_constraint`` returns a constraint dict;
  ``Date`` columns are created as ``DATE`` and typed reads return ``date``.
- Release: derive the published sdist name from one normalized helper, used by
  both the existing-version check and the upload.

0.7.0a
======

- Add support for JWT authentication.
