"""Integration between SQLAlchemy and Presto.

Some code based on
https://github.com/zzzeek/sqlalchemy/blob/rel_0_5/lib/sqlalchemy/databases/sqlite.py
which is released under the MIT license.
"""

from __future__ import absolute_import
from __future__ import unicode_literals

import datetime
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
    if with_zone:
        if name == 'timestamp':
            return types.TIMESTAMP(timezone=True)
        if name == 'time':
            return types.TIME(timezone=True)
        return None
    if name == 'decimal' and len(params) in (0, 2):
        return types.DECIMAL(*params)
    if name == 'varchar' and len(params) == 1:
        return types.VARCHAR(params[0])
    if name == 'char' and len(params) <= 1:
        return types.CHAR(*params)
    if name == 'time' and not params:
        return types.TIME()
    if name in _type_map and not params:
        return _type_map[name]
    return None


# Presto's REST protocol returns date and time values as strings, e.g.
# '2000-02-29', '2026-09-24 12:34:56.123', '12:34:56.123 +05:30' or
# '2026-09-24 12:34:56.123 America/New_York'.
_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_TIME_RE = re.compile(r'^(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?: (.+))?$')
_OFFSET_RE = re.compile(r'^([+-])(\d{2}):(\d{2})$')


def _microseconds(fraction, value):
    if not fraction:
        return 0
    if len(fraction) > 6 and fraction[6:].strip('0'):
        # datetime cannot hold it; refusing is better than silently truncating.
        raise ValueError('Sub-microsecond precision in {!r} cannot be represented'.format(value))
    return int(fraction[:6].ljust(6, '0'))


def _tzinfo(zone, value):
    if zone is None:
        return None
    offset = _OFFSET_RE.match(zone)
    if offset:
        sign, hours, minutes = offset.groups()
        delta = datetime.timedelta(hours=int(hours), minutes=int(minutes))
        return datetime.timezone(-delta if sign == '-' else delta)
    tzinfo = dateutil.tz.gettz(zone)
    if tzinfo is None:
        raise ValueError('Unknown time zone in {!r}'.format(value))
    return tzinfo


def _parse_date(value):
    match = _DATE_RE.match(value)
    if not match:
        raise ValueError('Invalid Presto date {!r}'.format(value))
    return datetime.date(*map(int, match.groups()))


def _parse_time(value):
    match = _TIME_RE.match(value)
    if not match:
        raise ValueError('Invalid Presto time {!r}'.format(value))
    hour, minute, second, fraction, zone = match.groups()
    return datetime.time(int(hour), int(minute), int(second),
                         _microseconds(fraction, value), _tzinfo(zone, value))


def _parse_datetime(value):
    day, _, clock = value.partition(' ')
    # combine() keeps the time's tzinfo; region zones resolve per instant.
    return datetime.datetime.combine(_parse_date(day), _parse_time(clock))


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
        return [row.Table for row in connection.execute(text(query))]

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
