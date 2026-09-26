0.7.0.3
=======

- Presto SQLAlchemy dialect: keep full precision for DECIMAL results and
  parameters, return ``date``/``datetime``/``time`` objects for typed temporal
  columns, reflect parameterized types (``decimal(p,s)``, ``varchar(n)``,
  ``char(n)``, ``time``, ``... with time zone``), implement
  ``get_view_names``, return a constraint dict from ``get_pk_constraint``, and
  bind the minimum BIGINT.
- Release: derive the published sdist name from one normalized helper, used by
  both the existing-version check and the upload.

0.7.0a
======

- Add support for JWT authentication.
