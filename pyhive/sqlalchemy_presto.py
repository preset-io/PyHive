"""Integration between SQLAlchemy and Presto.

Some code based on
https://github.com/zzzeek/sqlalchemy/blob/rel_0_5/lib/sqlalchemy/databases/sqlite.py
which is released under the MIT license.
"""

from __future__ import absolute_import
from __future__ import unicode_literals

import datetime
import decimal
import re

import dateutil.tz
import sqlalchemy
from sqlalchemy import exc
from sqlalchemy import types
from sqlalchemy import util
# TODO shouldn't use mysql type
from sqlalchemy.sql import text
try:
    from sqlalchemy.databases import mysql
    mysql_tinyinteger = mysql.MSTinyInteger
except ImportError:
    # Required for SQLAlchemy>=2.0
    from sqlalchemy.dialects import mysql
    mysql_tinyinteger = mysql.base.MSTinyInteger
from sqlalchemy.engine import default
from sqlalchemy.sql import compiler
from sqlalchemy.sql.compiler import SQLCompiler

from pyhive import exc as pyhive_exc
from pyhive import presto
from pyhive.common import UniversalSet

sqlalchemy_version = float(re.search(r"^([\d]+\.[\d]+)\..+", sqlalchemy.__version__).group(1))

class PrestoIdentifierPreparer(compiler.IdentifierPreparer):
    # Just quote everything to make things simpler / easier to upgrade
    reserved_words = UniversalSet()


_type_map = {
    'boolean': types.Boolean,
    'tinyint': mysql_tinyinteger,
    'smallint': types.SmallInteger,
    'integer': types.Integer,
    'bigint': types.BigInteger,
    'real': types.Float,
    'double': types.Float,
    'varchar': types.String,
    'timestamp': types.TIMESTAMP,
    'date': types.DATE,
    'varbinary': types.VARBINARY,
}

# Types whose SHOW COLUMNS spelling carries parameters or a zone qualifier.
_TYPE_RE = re.compile(r'^([a-z ]+?)\s*(?:\(([^)]*)\))?\s*(with time zone)?$')


def _parse_type(type_str):
    """Return the SQLAlchemy type for a Presto type string, or None."""
    match = _TYPE_RE.match(type_str.strip().lower())
    if not match:
        return None
    name, args, with_zone = match.groups()
    params = [p.strip() for p in args.split(',')] if args else []
    if not all(p.isdigit() for p in params):
        return None  # array(...), map(...), row(...) and friends
    params = [int(p) for p in params]
    if name in ('timestamp', 'time') and len(params) <= 1:
        # Trino reports precision, e.g. timestamp(3) or time(6) with time zone.
        # datetime holds microseconds; finer values raise when read.
        temporal = types.TIMESTAMP if name == 'timestamp' else types.TIME
        return temporal(timezone=bool(with_zone))
    if with_zone:
        return None
    if name == 'decimal' and len(params) in (0, 2):
        return types.DECIMAL(*params)
    if name == 'varchar' and len(params) == 1:
        return types.VARCHAR(params[0])
    if name == 'char' and len(params) <= 1:
        return types.CHAR(*params)
    if name in _type_map and not params:
        return _type_map[name]
    return None


# Presto's REST protocol returns date and time values as strings, e.g.
# '2000-02-29', '2026-09-24 12:34:56.123', '12:34:56.123 +05:30' or
# '2026-09-24 12:34:56.123 America/New_York'. Trino renders TIME WITH TIME ZONE
# without a space before the offset: '12:34:56.123+05:30'.
#
# Policy: a typed value that cannot be represented exactly raises
# pyhive.exc.DataError rather than returning a wrong value or the raw string.
# That covers dates outside datetime's range ('-0001-01-01', '+10000-01-01'),
# non-zero sub-microsecond digits, unknown zones, region zones on a TIME (there
# is no date to resolve the offset against), and local times that are
# ambiguous or nonexistent in their region zone because of a DST transition.
_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_TIME_RE = re.compile(
    r'^(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?: (.+)|([+-]\d{2}:\d{2}))?$')
_OFFSET_RE = re.compile(r'^([+-])(\d{2}):(\d{2})$')
_ETC_GMT_RE = re.compile(r'^Etc/GMT([+-])(\d{1,2})$')
_UTC_ZONES = frozenset([
    'UTC', 'Z', 'GMT', 'UCT', 'Zulu', 'Universal', 'Greenwich', 'GMT0',
    'Etc/UTC', 'Etc/GMT', 'Etc/UCT', 'Etc/Zulu', 'Etc/Universal', 'Etc/Greenwich',
    'Etc/GMT0', 'Etc/GMT+0', 'Etc/GMT-0',
])


def _fixed_zone(zone):
    """Return a datetime.timezone for a zone id with a constant offset, or None."""
    if zone in _UTC_ZONES:
        return datetime.timezone.utc
    offset = _OFFSET_RE.match(zone)
    if offset:
        sign, hours, minutes = offset.groups()
        delta = datetime.timedelta(hours=int(hours), minutes=int(minutes))
        return datetime.timezone(-delta if sign == '-' else delta)
    etc = _ETC_GMT_RE.match(zone)
    if etc and int(etc.group(2)) <= 14:
        # POSIX sign convention: Etc/GMT+5 is five hours *behind* UTC.
        sign, hours = etc.groups()
        delta = datetime.timedelta(hours=int(hours))
        return datetime.timezone(delta if sign == '-' else -delta)
    return None


def _tzinfo(zone, value, region_allowed):
    if zone is None:
        return None
    fixed = _fixed_zone(zone)
    if fixed is not None:
        return fixed
    tzinfo = dateutil.tz.gettz(zone)
    if tzinfo is None:
        raise pyhive_exc.DataError('Unknown time zone in {!r}'.format(value))
    if not region_allowed:
        raise pyhive_exc.DataError(
            'Region time zone in {!r} has no fixed offset for a time without a date'
            .format(value))
    return tzinfo


def _microseconds(fraction, value):
    if not fraction:
        return 0
    if len(fraction) > 6 and fraction[6:].strip('0'):
        # datetime cannot hold it; refusing is better than silently truncating.
        raise pyhive_exc.DataError(
            'Sub-microsecond precision in {!r} cannot be represented'.format(value))
    return int(fraction[:6].ljust(6, '0'))


def _parse_date(value, source=None):
    source = value if source is None else source
    match = _DATE_RE.match(value)
    if not match:
        raise pyhive_exc.DataError('Invalid or out-of-range Presto date {!r}'.format(source))
    try:
        return datetime.date(*map(int, match.groups()))
    except ValueError as e:
        raise pyhive_exc.DataError('Invalid Presto date {!r}: {}'.format(source, e))


def _time_parts(value, region_allowed, source=None):
    source = value if source is None else source
    match = _TIME_RE.match(value)
    if not match:
        raise pyhive_exc.DataError('Invalid Presto time {!r}'.format(source))
    hour, minute, second, fraction, zone, attached_offset = match.groups()
    try:
        return datetime.time(int(hour), int(minute), int(second),
                             _microseconds(fraction, source),
                             _tzinfo(zone or attached_offset, source, region_allowed))
    except ValueError as e:
        raise pyhive_exc.DataError('Invalid Presto time {!r}: {}'.format(source, e))


def _parse_time(value):
    return _time_parts(value, region_allowed=False)


def _parse_datetime(value):
    day, _, clock = value.partition(' ')
    result = datetime.datetime.combine(_parse_date(day, value), _time_parts(clock, True, value))
    tzinfo = result.tzinfo
    if tzinfo is not None and not isinstance(tzinfo, datetime.timezone):
        # A region zone: the wall-clock time must name exactly one instant.
        # combine() always picks fold=0, which is an hour off for the second
        # occurrence of a repeated hour.
        if not dateutil.tz.datetime_exists(result):
            raise pyhive_exc.DataError(
                'Nonexistent local time (DST gap) in {!r}'.format(value))
        if dateutil.tz.datetime_ambiguous(result):
            raise pyhive_exc.DataError(
                'Ambiguous local time (DST fall-back) in {!r}'.format(value))
    return result


def _string_result(parse):
    def result_processor(self, dialect, coltype):
        def process(value):
            return parse(value) if isinstance(value, str) else value
        return process
    return result_processor


class PrestoDate(types.Date):
    result_processor = _string_result(_parse_date)


class PrestoDateTime(types.DateTime):
    result_processor = _string_result(_parse_datetime)


class PrestoTime(types.Time):
    result_processor = _string_result(_parse_time)


class PrestoNumeric(types.Numeric):
    """Numeric that returns Decimal whatever the column's Presto type.

    The DB-API returns Decimal for DECIMAL columns (kept exact) but float for
    DOUBLE/REAL and int for integer types. With supports_native_decimal,
    SQLAlchemy's Numeric would pass floats through, so convert them the way
    SQLAlchemy's to_decimal processor does when asdecimal is set.
    """

    def result_processor(self, dialect, coltype):
        if not self.asdecimal:
            return super(PrestoNumeric, self).result_processor(dialect, coltype)
        scale = self._effective_decimal_return_scale
        fmt = '%.{:d}f'.format(scale)

        def process(value):
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                return value  # None, and Decimal from DECIMAL columns (exact)
            if isinstance(value, float):
                return decimal.Decimal(fmt % value)
            # Integers are exact; formatting through float would lose digits.
            if scale:
                return decimal.Decimal('{}.{}'.format(value, '0' * scale))
            return decimal.Decimal(value)
        return process


class PrestoCompiler(SQLCompiler):
    def visit_char_length_func(self, fn, **kw):
        return 'length{}'.format(self.function_argspec(fn, **kw))


class PrestoTypeCompiler(compiler.GenericTypeCompiler):
    def visit_CLOB(self, type_, **kw):
        raise ValueError("Presto does not support the CLOB column type.")

    def visit_NCLOB(self, type_, **kw):
        raise ValueError("Presto does not support the NCLOB column type.")

    def visit_DATETIME(self, type_, **kw):
        raise ValueError("Presto does not support the DATETIME column type.")

    def visit_FLOAT(self, type_, **kw):
        return 'DOUBLE'

    def visit_TEXT(self, type_, **kw):
        if type_.length:
            return 'VARCHAR({:d})'.format(type_.length)
        else:
            return 'VARCHAR'


class PrestoDialect(default.DefaultDialect):
    name = 'presto'
    driver = 'rest'
    paramstyle = 'pyformat'
    preparer = PrestoIdentifierPreparer
    statement_compiler = PrestoCompiler
    supports_alter = False
    supports_pk_autoincrement = False
    supports_default_values = False
    supports_empty_insert = False
    supports_multivalues_insert = True
    supports_unicode_statements = True
    supports_unicode_binds = True
    supports_statement_cache = False

    def is_disconnect(self, e, connection, cursor):
        return presto.is_connection_lost(e)
    returns_unicode_strings = True
    description_encoding = None
    supports_native_boolean = True
    # The DBAPI returns exact Decimal values for decimal columns and binds
    # Decimal as a DECIMAL literal, so SQLAlchemy must not route either
    # direction through float.
    supports_native_decimal = True
    type_compiler = PrestoTypeCompiler
    colspecs = {
        types.Date: PrestoDate,
        types.DateTime: PrestoDateTime,
        types.Time: PrestoTime,
        types.Numeric: PrestoNumeric,
        # Float subclasses Numeric; keep SQLAlchemy's float handling for it.
        types.Float: types.Float,
    }

    @classmethod
    def dbapi(cls):
        return presto
    
    @classmethod
    def import_dbapi(cls):
        return presto

    def create_connect_args(self, url):
        db_parts = (url.database or 'hive').split('/')
        kwargs = {
            'host': url.host,
            'port': url.port or 8080,
            'username': url.username,
            'password': url.password
        }
        kwargs.update(url.query)
        if len(db_parts) == 1:
            kwargs['catalog'] = db_parts[0]
        elif len(db_parts) == 2:
            kwargs['catalog'] = db_parts[0]
            kwargs['schema'] = db_parts[1]
        else:
            raise ValueError("Unexpected database format {}".format(url.database))
        return [], kwargs

    def get_schema_names(self, connection, **kw):
        return [row.Schema for row in connection.execute(text('SHOW SCHEMAS'))]

    def _get_table_columns(self, connection, table_name, schema):
        full_table = self.identifier_preparer.quote_identifier(table_name)
        if schema:
            full_table = self.identifier_preparer.quote_identifier(schema) + '.' + full_table
        try:
            return connection.execute(text('SHOW COLUMNS FROM {}'.format(full_table)))
        except (presto.DatabaseError, exc.DatabaseError) as e:
            # Normally SQLAlchemy should wrap this exception in sqlalchemy.exc.DatabaseError, which
            # it successfully does in the Hive version. The difference with Presto is that this
            # error is raised when fetching the cursor's description rather than the initial execute
            # call. SQLAlchemy doesn't handle this. Thus, we catch the unwrapped
            # presto.DatabaseError here.
            # Does the table exist?
            msg = (
                e.args[0].get('message') if e.args and isinstance(e.args[0], dict)
                else e.args[0] if e.args and isinstance(e.args[0], str)
                else None
            )
            regex = r"Table\ \'.*{}\'\ does\ not\ exist".format(re.escape(table_name))
            if msg and re.search(regex, msg):
                raise exc.NoSuchTableError(table_name)
            else:
                raise

    def has_table(self, connection, table_name, schema=None, **kw):
        try:
            self._get_table_columns(connection, table_name, schema)
            return True
        except exc.NoSuchTableError:
            return False

    def get_columns(self, connection, table_name, schema=None, **kw):
        rows = self._get_table_columns(connection, table_name, schema)
        result = []
        for row in rows:
            coltype = _parse_type(row.Type)
            if coltype is None:
                util.warn("Did not recognize type '%s' of column '%s'" % (row.Type, row.Column))
                coltype = types.NullType
            result.append({
                'name': row.Column,
                'type': coltype,
                # newer Presto no longer includes this column
                'nullable': getattr(row, 'Null', True),
                'default': None,
            })
        return result

    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        # Hive has no support for foreign keys.
        return []

    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        # Presto has no primary keys; SQLAlchemy expects a constraint dict.
        return {'constrained_columns': [], 'name': None}

    def get_indexes(self, connection, table_name, schema=None, **kw):
        rows = self._get_table_columns(connection, table_name, schema)
        col_names = []
        for row in rows:
            part_key = 'Partition Key'
            # Presto puts this information in one of 3 places depending on version
            # - a boolean column named "Partition Key"
            # - a string in the "Comment" column
            # - a string in the "Extra" column
            if sqlalchemy_version >= 1.4:
                row = row._mapping
            is_partition_key = (
                (part_key in row and row[part_key])
                or row['Comment'].startswith(part_key)
                or ('Extra' in row and 'partition key' in row['Extra'])
            )
            if is_partition_key:
                col_names.append(row['Column'])
        if col_names:
            return [{'name': 'partition', 'column_names': col_names, 'unique': False}]
        else:
            return []

    def get_table_names(self, connection, schema=None, **kw):
        query = 'SHOW TABLES'
        if schema:
            query += ' FROM ' + self.identifier_preparer.quote_identifier(schema)
        tables = [row.Table for row in connection.execute(text(query))]
        # SHOW TABLES lists views too; get_view_names reports those separately.
        views = set(self.get_view_names(connection, schema))
        return [table for table in tables if table not in views]

    def get_view_names(self, connection, schema=None, **kw):
        if schema is None:
            schema = self._connection_schema(connection)
        query = text(
            'SELECT table_name FROM information_schema.views '
            'WHERE table_schema = :schema ORDER BY table_name'
        )
        return [row[0] for row in connection.execute(query, {'schema': schema})]

    def _connection_schema(self, connection):
        """The schema the connection's queries run in (the DBAPI default is 'default')."""
        pooled = connection.connection
        dbapi_connection = getattr(pooled, 'dbapi_connection', None) or pooled.connection
        return dbapi_connection._kwargs.get('schema', 'default')

    def do_rollback(self, dbapi_connection):
        # No transactions for Presto
        pass

    def _check_unicode_returns(self, connection, additional_tests=None):
        # requests gives back Unicode strings
        return True

    def _check_unicode_description(self, connection):
        # requests gives back Unicode strings
        return True
