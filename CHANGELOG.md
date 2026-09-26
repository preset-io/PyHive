0.7.0.4
=======

- Hive DB-API: bind ``Decimal`` exactly (``<digits>BD``), keep ``float``
  parameters DOUBLE, bind the minimum BIGINT, bind ``bytes`` as BINARY and
  reject timezone-aware datetimes; add the PEP 249 type constructors
  (``Binary``, ``Date``, ``Timestamp``, ...). TIMESTAMP values with non-zero
  nanoseconds raise ``DataError`` instead of being truncated.
- Hive DB-API: a lost, reset or refused connection raises ``OperationalError``
  (the Thrift transport error is its ``__cause__``) instead of a raw
  ``TTransportException``; ``Connection.close`` releases the socket even when
  the server is gone.
- Hive SQLAlchemy dialect: ``is_disconnect`` recognises lost connections, so
  ``pool_pre_ping`` and pool invalidation work after a server restart;
  ``get_table_names`` no longer lists views and ``get_view_names`` lists only
  views; columns reflect ``decimal(p,s)``, ``varchar(n)``, ``char(n)``,
  ``double``, ``binary`` and ``array``/``map``/``struct``/``uniontype`` (with the
  full Hive type) and carry their comments; ``get_table_comment``,
  ``get_view_definition``, ``get_unique_constraints`` and
  ``get_check_constraints`` are implemented; ``Numeric(p, s)`` DDL keeps its
  precision and scale; ``TINYINT`` compiles; values can be bound against
  reflected DATE/TIMESTAMP/DECIMAL columns.

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
