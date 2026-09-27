0.7.0.4
=======

**Behaviour changes** (Hive DB-API parameters):

- ``bytes`` parameters are bound as BINARY (``unhex('<hex>')``), no longer
  decoded as UTF-8 text. Comparing them with a STRING now fails
  (``unsupported conversion from type: binary``); pass ``str`` for text.
- Timezone-aware ``datetime`` parameters raise ``ProgrammingError`` instead of
  silently dropping their offset; convert them to naive datetimes first.

Changes:

- Hive DB-API: bind ``Decimal`` exactly (``<digits>BD``; values with more
  than 38 digits, e.g. ``1E+50`` or ``1E-40``, raise ``ProgrammingError``),
  keep ``float`` parameters DOUBLE (``CAST(<repr> AS DOUBLE)``, valid on every
  Hive version and Spark), bind the minimum BIGINT; add the PEP 249 type
  constructors (``Binary``, ``Date``, ``Timestamp``, ...).
- Hive DB-API: TIMESTAMP values with nanoseconds are still truncated to
  microseconds by default, now with a warning logged once;
  ``strict_timestamps=True`` (connection or cursor) raises ``DataError``
  instead.
- Hive DB-API: a lost or reset connection raises ``OperationalError`` (the
  Thrift transport error is its ``__cause__``) instead of a raw
  ``TTransportException``; ``Connection.close`` releases the socket even when
  the server is gone. A failure while connecting (refused connection, rejected
  SASL handshake) raises ``OperationalError("Could not connect to
  HiveServer2: ...")`` and is not reported as a lost connection.
- Hive DB-API: ``auth='KERBEROS'`` works without the ``kerberos`` (pykerberos)
  module: when pure-sasl has no Kerberos backend, SASL GSSAPI runs on
  python-gssapi (``auth`` quality of protection).
- Hive SQLAlchemy dialect: ``is_disconnect`` recognises lost connections, so
  ``pool_pre_ping`` and pool invalidation work after a server restart;
  ``get_table_names`` no longer lists views and ``get_view_names`` lists only
  views (servers whose ``SHOW VIEWS`` fails to parse keep the old listing;
  other errors are raised); columns reflect ``decimal(p,s)``, ``varchar(n)``, ``char(n)``,
  ``double`` (``DOUBLE`` on SQLAlchemy 2.0, ``Float`` on 1.x), ``binary`` and ``array``/``map``/``struct``/``uniontype`` (with the
  full Hive type) and carry their comments (Java escapes undone,
  ``from deserializer`` read as no comment); ``get_table_comment``,
  ``get_view_definition`` (every line of multi-line views),
  ``get_unique_constraints`` and
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
