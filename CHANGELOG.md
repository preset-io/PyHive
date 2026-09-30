0.7.0.5
=======

- Revert the Presto/Trino view exclusion introduced by 35a862c2.
  ``get_table_names`` returns SHOW TABLES directly again (including views),
  without querying ``information_schema.views``. Superset already subtracts
  ``get_view_names``; that method and its schema handling are unchanged.

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
- Presto DB-API: a refused, reset or timed-out request raises ``OperationalError``
  (the requests exception is its ``__cause__``) instead of a raw
  ``requests.exceptions.ConnectionError``; ``PrestoDialect.is_disconnect``
  recognises it.
- Hive SQLAlchemy dialect: ``Numeric`` over DOUBLE/FLOAT/integer results returns
  ``Decimal`` (``Float`` stays ``float``); typed DATE/TIMESTAMP values ``datetime`` cannot
  hold (year 0, negative years) raise ``DataError`` instead of being misread or raising
  ``ValueError``; typed ``DateTime`` reads of TIMESTAMP WITH LOCAL TIME ZONE return aware
  datetimes, and ambiguous (DST fall-back), nonexistent or unknown-zone values raise
  ``DataError``.
- Hive DB-API: result columns of a type id newer than the bundled Thrift definitions
  (TIMESTAMP WITH LOCAL TIME ZONE, id 22) no longer fail with ``KeyError``.
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
- Presto SQLAlchemy dialect: keep full precision for DECIMAL results and
  parameters, return ``date``/``datetime``/``time`` objects for typed temporal
  columns (including Trino's ``12:34:56.123+05:30`` TIME WITH TIME ZONE
  format), reflect parameterized types (``decimal(p,s)``, ``varchar(n)``,
  ``char(n)``, ``time``, ``timestamp(p)``, ``time(p)``, ``... with time
  zone``), implement ``get_view_names`` (``get_table_names`` no longer lists
  views), return a constraint dict from
  ``get_pk_constraint``, and bind the minimum BIGINT. ``Numeric`` over a
  DOUBLE/REAL/integer result still returns ``Decimal``; ``Float`` returns
  ``float``. The Trino dialect shares all of this.
- Hive: ``executemany`` of INSERT and other statements without a result set
  no longer fails; ``has_table`` returns False for Hive 4's database-qualified
  "Table not found" error; ``get_pk_constraint`` returns a constraint dict;
  ``Date`` columns are created as ``DATE`` and typed reads return ``date``.
- Release: derive the published sdist name from one normalized helper, used by
  both the existing-version check and the upload. PR builds are versioned
  ``<version>+pr.<n>.g<revision>`` and skip the existing-version check.

**Behaviour changes** (typed temporal reads, Hive ``Date`` columns):

Typed Presto/Trino ``Date``, ``DateTime`` and ``Time`` results used to come back
as the raw strings. They are now parsed, and a value that cannot be represented
exactly raises ``pyhive.exc.DataError`` for the whole fetch instead of
returning a wrong value or a string. That covers:

- dates outside ``datetime.date``'s range, which Presto does return (for
  example ``-0001-01-01`` and ``+10000-01-01``), and a ``Date``-typed
  expression over a timestamp value;
- non-zero digits below microsecond precision;
- unknown time zones;
- a TIME WITH TIME ZONE in a region zone such as ``America/New_York`` (a time
  has no date to resolve the offset against). Fixed zones (``UTC``, ``Z``,
  ``GMT``, ``Etc/UTC``, ``Etc/GMT+5``, ...) become ``datetime.timezone``;
- a TIMESTAMP WITH TIME ZONE whose local time is ambiguous (the repeated hour
  when DST ends) or nonexistent (the skipped hour when DST starts) in its
  region zone.

Untyped reads (``exec_driver_sql``, ``text()`` without column types) are
unchanged and still return strings.

Hive: ``Date`` columns are now created as ``DATE`` rather than ``TIMESTAMP``,
and typed ``Date`` reads return ``datetime.date`` instead of a string.
Existing tables are not altered.

0.7.0a
======

- Add support for JWT authentication.
