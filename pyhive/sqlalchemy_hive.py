"""Integration between SQLAlchemy and Hive.

Some code based on
https://github.com/zzzeek/sqlalchemy/blob/rel_0_5/lib/sqlalchemy/databases/sqlite.py
which is released under the MIT license.
"""

from __future__ import absolute_import
from __future__ import unicode_literals

import datetime
import decimal

import re
from sqlalchemy import exc
from sqlalchemy.sql import text
try:
    from sqlalchemy import processors
except ImportError:
    # Required for SQLAlchemy>=2.0
    from sqlalchemy.engine import processors
from sqlalchemy import types
from sqlalchemy import util
# TODO shouldn't use mysql type
try:
    from sqlalchemy.databases import mysql
    mysql_tinyinteger = mysql.MSTinyInteger
except ImportError:
    # Required for SQLAlchemy>2.0
    from sqlalchemy.dialects import mysql
    mysql_tinyinteger = mysql.base.MSTinyInteger
from sqlalchemy.engine import default
from sqlalchemy.sql import compiler
from sqlalchemy.sql.compiler import SQLCompiler

from pyhive import hive
from pyhive.common import UniversalSet

from dateutil.parser import parse
from decimal import Decimal


class HiveStringTypeBase(types.TypeDecorator):
    """Translates strings returned by Thrift into something else"""
    impl = types.String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # The DB-API escaper renders date, datetime and Decimal values itself.
        return value


class HiveDate(HiveStringTypeBase):
    """Translates date strings to date objects"""
    impl = types.DATE

    def process_result_value(self, value, dialect):
        return processors.str_to_date(value)

    def result_processor(self, dialect, coltype):
        def process(value):
            if isinstance(value, datetime.datetime):
                return value.date()
            elif isinstance(value, datetime.date):
                return value
            elif value is not None:
                return parse(value).date()
            else:
                return None

        return process

    def adapt(self, impltype, **kwargs):
        return self.impl


class HiveDateResult(types.Date):
    """Plain Date columns: Hive returns DATE values as 'YYYY-MM-DD' strings."""

    def result_processor(self, dialect, coltype):
        return HiveDate.result_processor(self, dialect, coltype)


class HiveTimestamp(HiveStringTypeBase):
    """Translates timestamp strings to datetime objects"""
    impl = types.TIMESTAMP

    def process_result_value(self, value, dialect):
        return processors.str_to_datetime(value)

    def result_processor(self, dialect, coltype):
        def process(value):
            if isinstance(value, datetime.datetime):
                return value
            elif value is not None:
                return parse(value)
            else:
                return None

        return process

    def adapt(self, impltype, **kwargs):
        return self.impl


class HiveDecimal(HiveStringTypeBase):
    """Translates strings to decimals"""
    impl = types.DECIMAL

    def process_result_value(self, value, dialect):
        if value is not None:
            return decimal.Decimal(value)
        else:
            return None

    def result_processor(self, dialect, coltype):
        def process(value):
            if isinstance(value, Decimal):
                return value
            elif value is not None:
                return Decimal(value)
            else:
                return None

        return process

    def adapt(self, impltype, **kwargs):
        return self.impl


class HiveNumeric(types.DECIMAL):
    """DECIMAL(p, s) as reflected from Hive.

    The DB-API already returns ``Decimal`` for DECIMAL columns; strings are converted too so
    values are exact whichever way the column is read.
    """

    def result_processor(self, dialect, coltype):
        as_decimal = self.asdecimal

        def process(value):
            if value is None:
                return None
            if not isinstance(value, Decimal):
                value = Decimal(value)
            return value if as_decimal else float(value)

        return process


class HiveComplexType(types.UserDefinedType):
    """ARRAY, MAP, STRUCT and UNIONTYPE columns.

    HiveServer2 returns their values as JSON text, so values are strings; the type keeps the
    full Hive type (e.g. ``array<struct<x:int>>``) for DDL and display.
    """

    cache_ok = True

    def __init__(self, type_text):
        self.type_text = type_text

    def get_col_spec(self, **kw):
        return self.type_text

    @property
    def python_type(self):
        return str

    def __repr__(self):
        return 'HiveComplexType({!r})'.format(self.type_text)


def _parse_type(col_type):
    """Map a type from ``DESCRIBE`` (e.g. ``decimal(10,2)``, ``map<string,int>``) to SQLAlchemy.

    Returns None for unknown types.
    """
    col_type = col_type.strip()
    name = re.search(r'^\w+', col_type).group(0).lower()
    args = re.match(r'^\w+\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)$', col_type)
    if name in ('array', 'map', 'struct', 'uniontype'):
        return HiveComplexType(col_type)
    if name == 'decimal':
        if args:
            precision = int(args.group(1))
            scale = int(args.group(2)) if args.group(2) is not None else 0
        else:
            precision, scale = 10, 0  # Hive's default for a bare DECIMAL
        return HiveNumeric(precision=precision, scale=scale)
    if name in ('varchar', 'char') and args:
        return (types.VARCHAR if name == 'varchar' else types.CHAR)(int(args.group(1)))
    coltype = _type_map.get(name)
    if coltype is None:
        return None
    return coltype()


class HiveIdentifierPreparer(compiler.IdentifierPreparer):
    # Just quote everything to make things simpler / easier to upgrade
    reserved_words = UniversalSet()

    def __init__(self, dialect):
        super(HiveIdentifierPreparer, self).__init__(
            dialect,
            initial_quote='`',
        )


_type_map = {
    'boolean': types.Boolean,
    'tinyint': mysql_tinyinteger,
    'smallint': types.SmallInteger,
    'int': types.Integer,
    'bigint': types.BigInteger,
    'float': types.Float,
    'double': types.DOUBLE,
    'string': types.String,
    'varchar': types.String,
    'char': types.String,
    'date': HiveDate,
    'timestamp': HiveTimestamp,
    'binary': types.BINARY,
    'array': types.String,
    'map': types.String,
    'struct': types.String,
    'uniontype': types.String,
    'decimal': HiveDecimal,
}


class HiveCompiler(SQLCompiler):
    def visit_concat_op_binary(self, binary, operator, **kw):
        return "concat(%s, %s)" % (self.process(binary.left), self.process(binary.right))

    def visit_insert(self, *args, **kwargs):
        result = super(HiveCompiler, self).visit_insert(*args, **kwargs)
        # Massage the result into Hive's format
        #   INSERT INTO `pyhive_test_database`.`test_table` (`a`) SELECT ...
        #   =>
        #   INSERT INTO TABLE `pyhive_test_database`.`test_table` SELECT ...
        regex = r'^(INSERT INTO) ([^\s]+) \([^\)]*\)'
        assert re.search(regex, result), "Unexpected visit_insert result: {}".format(result)
        return re.sub(regex, r'\1 TABLE \2', result)

    def visit_column(self, *args, **kwargs):
        result = super(HiveCompiler, self).visit_column(*args, **kwargs)
        dot_count = result.count('.')
        assert dot_count in (0, 1, 2), "Unexpected visit_column result {}".format(result)
        if dot_count == 2:
            # we have something of the form schema.table.column
            # hive doesn't like the schema in front, so chop it out
            result = result[result.index('.') + 1:]
        return result

    def visit_char_length_func(self, fn, **kw):
        return 'length{}'.format(self.function_argspec(fn, **kw))


class HiveTypeCompiler(compiler.GenericTypeCompiler):
    def visit_INTEGER(self, type_):
        return 'INT'

    def visit_TINYINT(self, type_):
        # Reflected TINYINT columns use the MySQL type class.
        return 'TINYINT'

    def visit_NUMERIC(self, type_):
        # A bare DECIMAL is DECIMAL(10,0) in Hive, which would turn larger values into NULL
        # and drop the fraction.
        if type_.precision is None:
            return 'DECIMAL'
        if type_.scale is None:
            return 'DECIMAL({})'.format(type_.precision)
        return 'DECIMAL({}, {})'.format(type_.precision, type_.scale)

    def visit_DECIMAL(self, type_):
        return self.visit_NUMERIC(type_)

    def visit_CHAR(self, type_):
        return 'STRING'

    def visit_VARCHAR(self, type_):
        return 'STRING'

    def visit_NCHAR(self, type_):
        return 'STRING'

    def visit_TEXT(self, type_):
        return 'STRING'

    def visit_CLOB(self, type_):
        return 'STRING'

    def visit_BLOB(self, type_):
        return 'BINARY'

    def visit_TIME(self, type_):
        return 'TIMESTAMP'

    def visit_DATE(self, type_):
        # Hive has had a DATE type since 0.12.
        return 'DATE'

    def visit_DATETIME(self, type_):
        return 'TIMESTAMP'


class HiveExecutionContext(default.DefaultExecutionContext):
    """This is pretty much the same as SQLiteExecutionContext to work around the same issue.

    http://docs.sqlalchemy.org/en/latest/dialects/sqlite.html#dotted-column-names

    engine = create_engine('hive://...', execution_options={'hive_raw_colnames': True})
    """

    @util.memoized_property
    def _preserve_raw_colnames(self):
        # Ideally, this would also gate on hive.resultset.use.unique.column.names
        return self.execution_options.get('hive_raw_colnames', False)

    def _translate_colname(self, colname):
        # Adjust for dotted column names.
        # When hive.resultset.use.unique.column.names is true (the default), Hive returns column
        # names as "tablename.colname" in cursor.description.
        if not self._preserve_raw_colnames and '.' in colname:
            return colname.split('.')[-1], colname
        else:
            return colname, None


class HiveDialect(default.DefaultDialect):
    name = 'hive'
    driver = 'thrift'
    execution_ctx_cls = HiveExecutionContext
    preparer = HiveIdentifierPreparer
    statement_compiler = HiveCompiler
    supports_views = True
    supports_alter = True
    supports_pk_autoincrement = False
    supports_default_values = False
    supports_empty_insert = False
    supports_native_decimal = True
    supports_native_boolean = True
    supports_unicode_statements = True
    supports_unicode_binds = True
    returns_unicode_strings = True
    description_encoding = None
    supports_multivalues_insert = True
    type_compiler = HiveTypeCompiler
    colspecs = {types.Date: HiveDateResult}
    supports_sane_rowcount = False
    supports_statement_cache = False

    @classmethod
    def dbapi(cls):
        return hive
    
    @classmethod
    def import_dbapi(cls):
        return hive

    def create_connect_args(self, url):
        kwargs = {
            'host': url.host,
            'port': url.port or 10000,
            'username': url.username,
            'password': url.password,
            'database': url.database or 'default',
        }
        kwargs.update(url.query)
        return [], kwargs

    def get_schema_names(self, connection, **kw):
        # Equivalent to SHOW DATABASES
        return [row[0] for row in connection.execute(text('SHOW SCHEMAS'))]

    def _show_names(self, connection, what, schema):
        query = 'SHOW {}'.format(what)
        if schema:
            query += ' IN ' + self.identifier_preparer.quote_identifier(schema)
        result = connection.execute(text(query))
        keys = list(result.keys())
        # HiveServer2 returns one ``tab_name`` column; Spark Thrift Server returns
        # (namespace, tableName|viewName, isTemporary).
        index = 0
        for i, key in enumerate(keys):
            if key.split('.')[-1].lower() in ('tablename', 'viewname'):
                index = i
        return [row[index] for row in result]

    def _view_names(self, connection, schema):
        """View names, or None when the server has no SHOW VIEWS (Hive < 2.2)."""
        try:
            return self._show_names(connection, 'VIEWS', schema)
        except exc.OperationalError:
            return None

    def get_view_names(self, connection, schema=None, **kw):
        views = self._view_names(connection, schema)
        if views is None:
            # No SHOW VIEWS: keep the old behaviour of listing every table.
            return self._show_names(connection, 'TABLES', schema)
        return views

    def _get_table_columns(self, connection, table_name, schema):
        full_table = table_name
        if schema:
            full_table = schema + '.' + table_name
        # TODO using TGetColumnsReq hangs after sending TFetchResultsReq.
        # Using DESCRIBE works but is uglier.
        try:
            # This needs the table name to be unescaped (no backticks).
            rows = connection.execute(text('DESCRIBE {}'.format(full_table))).fetchall()
        except exc.OperationalError as e:
            # Does the table exist?
            # Hive 4 qualifies the name with the database even when the query did not.
            regex_fmt = (r'TExecuteStatementResp.*SemanticException'
                         r'.*Table not found (?:[^\s.]+\.)?{}(?!\w)')
            regex = regex_fmt.format(re.escape(full_table))
            if re.search(regex, e.args[0]):
                raise exc.NoSuchTableError(full_table)
            else:
                raise
        else:
            # Hive is stupid: this is what I get from DESCRIBE some_schema.does_not_exist
            regex = r'Table .* does not exist'
            if len(rows) == 1 and re.match(regex, rows[0].col_name):
                raise exc.NoSuchTableError(full_table)
            return rows

    def has_table(self, connection, table_name, schema=None, **kw):
        try:
            self._get_table_columns(connection, table_name, schema)
            return True
        except exc.NoSuchTableError:
            return False

    def get_columns(self, connection, table_name, schema=None, **kw):
        rows = self._get_table_columns(connection, table_name, schema)
        # Strip whitespace
        rows = [[col.strip() if col else None for col in row] for row in rows]
        # Filter out empty rows and comment
        rows = [row for row in rows if row[0] and row[0] != '# col_name']
        result = []
        for (col_name, col_type, _comment) in rows:
            if col_name == '# Partition Information':
                break
            # Take out the more detailed type information
            # e.g. 'map<int,int>' -> 'map'
            #      'decimal(10,1)' -> decimal
            coltype = _parse_type(col_type)
            if coltype is None:
                util.warn("Did not recognize type '%s' of column '%s'" % (col_type, col_name))
                coltype = types.NullType

            result.append({
                'name': col_name,
                'type': coltype,
                'nullable': True,
                'default': None,
                'comment': _comment or None,
            })
        return result

    def _describe_formatted(self, connection, table_name, schema):
        full_table = table_name
        if schema:
            full_table = schema + '.' + table_name
        self._get_table_columns(connection, table_name, schema)  # NoSuchTableError
        rows = connection.execute(text('DESCRIBE FORMATTED {}'.format(full_table))).fetchall()
        return [tuple(col.strip() if col else col for col in row) for row in rows]

    def get_table_comment(self, connection, table_name, schema=None, **kw):
        in_params = False
        for key, name, value in self._describe_formatted(connection, table_name, schema):
            if key == 'Table Parameters:':
                in_params = True
            elif in_params and key:
                break
            elif in_params and name == 'comment':
                return {'text': value}
        return {'text': None}

    def get_view_definition(self, connection, view_name, schema=None, **kw):
        for key, value, _ in self._describe_formatted(connection, view_name, schema):
            # Hive 4 labels it "Original Query:", Hive 2/3 "View Original Text:".
            if key in ('Original Query:', 'View Original Text:'):
                return value
        raise exc.NoSuchTableError(view_name)

    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        return []

    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        return []

    def is_disconnect(self, e, connection, cursor):
        return hive.is_connection_lost(e)

    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        # Hive has no support for foreign keys.
        return []

    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        # Hive has no enforced primary keys; SQLAlchemy expects a constraint dict.
        return {'constrained_columns': [], 'name': None}

    def get_indexes(self, connection, table_name, schema=None, **kw):
        rows = self._get_table_columns(connection, table_name, schema)
        # Strip whitespace
        rows = [[col.strip() if col else None for col in row] for row in rows]
        # Filter out empty rows and comment
        rows = [row for row in rows if row[0] and row[0] != '# col_name']
        for i, (col_name, _col_type, _comment) in enumerate(rows):
            if col_name == '# Partition Information':
                break
        # Handle partition columns
        col_names = []
        for col_name, _col_type, _comment in rows[i + 1:]:
            col_names.append(col_name)
        if col_names:
            return [{'name': 'partition', 'column_names': col_names, 'unique': False}]
        else:
            return []

    def get_table_names(self, connection, schema=None, **kw):
        tables = self._show_names(connection, 'TABLES', schema)
        # SHOW TABLES lists views too; SQLAlchemy expects tables only.
        views = set(self._view_names(connection, schema) or ())
        return [name for name in tables if name not in views]

    def do_rollback(self, dbapi_connection):
        # No transactions for Hive
        pass

    def _check_unicode_returns(self, connection, additional_tests=None):
        # We decode everything as UTF-8
        return True

    def _check_unicode_description(self, connection):
        # We decode everything as UTF-8
        return True


class HiveHTTPDialect(HiveDialect):

    name = "hive"
    scheme = "http"
    driver = "rest"

    def create_connect_args(self, url):
        kwargs = {
            "host": url.host,
            "port": url.port or 10000,
            "scheme": self.scheme,
            "username": url.username or None,
            "password": url.password or None,
        }
        if url.query:
            kwargs.update(url.query)
            return [], kwargs
        return ([], kwargs)


class HiveHTTPSDialect(HiveHTTPDialect):

    name = "hive"
    scheme = "https"
