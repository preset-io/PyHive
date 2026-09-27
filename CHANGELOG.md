0.7.0.3
=======

- Presto SQLAlchemy dialect: keep full precision for DECIMAL results and
  parameters, return ``date``/``datetime``/``time`` objects for typed temporal
  columns (including Trino's ``12:34:56.123+05:30`` TIME WITH TIME ZONE
  format), reflect parameterized types (``decimal(p,s)``, ``varchar(n)``,
  ``char(n)``, ``time``, ``timestamp(p)``, ``time(p)``, ``... with time
  zone``), implement ``get_view_names``, return a constraint dict from
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

Behaviour changes
-----------------

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
