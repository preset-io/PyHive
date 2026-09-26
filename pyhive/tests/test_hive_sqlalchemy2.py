# encoding: utf-8
"""Offline tests for Hive DB-API parameters, errors and SQLAlchemy 2 reflection.

These need no server: the dialect is driven with a fake connection that answers
SHOW/DESCRIBE statements, and the DB-API with a fake Thrift client.
"""
from __future__ import absolute_import
from __future__ import unicode_literals

import collections
import datetime
import socket
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable
from thrift.transport.TTransport import TTransportException

from pyhive import hive
from pyhive.sqlalchemy_hive import HiveComplexType, HiveDialect, HiveNumeric
from TCLIService import ttypes

OK = ttypes.TStatus(statusCode=ttypes.TStatusCode.SUCCESS_STATUS)
ESC = hive.HiveParamEscaper()


# Parameters.

@pytest.mark.parametrize('value,expected', [
    (Decimal('12345678901234567890.123456789012345678'),
     '12345678901234567890.123456789012345678BD'),
    (Decimal('-1E-18'), '-0.000000000000000001BD'),
    (Decimal('1E+3'), '1000BD'),
    (0.1, '0.1D'),
    (1e300, '1e+300D'),
    (-2.25, '-2.25D'),
    (float('nan'), "CAST('NaN' AS DOUBLE)"),
    (float('inf'), "CAST('Infinity' AS DOUBLE)"),
    (float('-inf'), "CAST('-Infinity' AS DOUBLE)"),
    (-9223372036854775808, '(-9223372036854775807 - 1)'),
    (9223372036854775807, 9223372036854775807),
    (True, 'true'),
    (False, 'false'),
    (b'\x00\x01\xfe\xff', "unhex('0001feff')"),
    (datetime.datetime(2024, 2, 29, 23, 59, 58, 123456), "'2024-02-29 23:59:58.123456'"),
    (datetime.date(2000, 2, 29), "'2000-02-29'"),
    ("it's \\ ü", "'it\\'s \\\\ ü'"),
    (None, 'NULL'),
])
def test_escape_item(value, expected):
    assert ESC.escape_item(value) == expected


@pytest.mark.parametrize('value', [Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')])
def test_non_finite_decimals_are_rejected(value):
    with pytest.raises(hive.ProgrammingError):
        ESC.escape_item(value)


def test_aware_datetimes_are_rejected():
    aware = datetime.datetime(2024, 1, 1, 12, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
    with pytest.raises(hive.ProgrammingError):
        ESC.escape_item(aware)


def test_pep249_constructors():
    assert hive.Binary(b'\x00') == b'\x00'
    assert hive.Date(2000, 1, 2) == datetime.date(2000, 1, 2)
    assert hive.Timestamp(2000, 1, 2, 3) == datetime.datetime(2000, 1, 2, 3)


# Timestamps.

@pytest.mark.parametrize('raw,expected', [
    ('2024-01-01 10:00:00', datetime.datetime(2024, 1, 1, 10)),
    ('2024-01-01 10:00:00.5', datetime.datetime(2024, 1, 1, 10, 0, 0, 500000)),
    ('2024-01-01 10:00:00.123456', datetime.datetime(2024, 1, 1, 10, 0, 0, 123456)),
    ('2024-01-01 10:00:00.123456000', datetime.datetime(2024, 1, 1, 10, 0, 0, 123456)),
])
def test_timestamps_parse_exactly(raw, expected):
    assert hive._parse_timestamp(raw) == expected


def test_nanosecond_timestamps_are_not_truncated():
    with pytest.raises(hive.DataError):
        hive._parse_timestamp('2024-01-01 10:00:00.123456789')


# Transport failures.

class Response(object):
    def __init__(self, **kwargs):
        self.status = OK
        self.__dict__.update(kwargs)


class DroppedClient(object):
    """A Thrift client whose connection is gone."""

    def __init__(self, error):
        self.error = error

    def ExecuteStatement(self, req):
        raise self.error

    def CloseSession(self, req):
        raise self.error


class FakeConnection(object):
    sessionHandle = None

    def __init__(self, client):
        self.client = hive._ClientWrapper(client)


@pytest.mark.parametrize('error', [
    TTransportException(TTransportException.END_OF_FILE, 'TSocket read 0 bytes'),
    socket.error(104, 'Connection reset by peer'),
    EOFError(),
])
def test_transport_errors_are_operational_errors(error):
    cur = hive.Cursor(FakeConnection(DroppedClient(error)))
    with pytest.raises(hive.OperationalError) as info:
        cur.execute('SELECT 1')
    assert info.value.__cause__ is error
    assert hive.is_connection_lost(info.value)
    assert HiveDialect().is_disconnect(info.value, None, None)


def test_statement_errors_are_not_disconnects():
    err = hive.OperationalError(Response())
    assert not hive.is_connection_lost(err)
    assert not HiveDialect().is_disconnect(err, None, None)


class FakeTransport(object):
    closed = False

    def close(self):
        self.closed = True


def test_close_releases_the_transport_when_the_server_is_gone():
    conn = hive.Connection.__new__(hive.Connection)
    conn._transport = FakeTransport()
    conn._sessionHandle = None
    conn._client = hive._ClientWrapper(DroppedClient(TTransportException(message='gone')))
    with pytest.raises(hive.OperationalError):
        conn.close()
    assert conn._transport.closed


def test_refused_connection_is_an_operational_error():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens on this port now
    with pytest.raises(hive.OperationalError) as info:
        hive.connect('127.0.0.1', port)
    assert hive.is_connection_lost(info.value)


# SQLAlchemy reflection with a fake connection.

class Result(object):
    def __init__(self, keys, rows):
        self._keys = keys
        Row = collections.namedtuple('Row', keys)
        self._rows = [Row(*r) for r in rows]

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return list(self._rows)


DESCRIBE = [
    ('id', 'int', 'row id'),
    ('ti', 'tinyint', ''),
    ('d', 'double', ''),
    ('dec38', 'decimal(38,18)', ''),
    ('dec', 'decimal', ''),
    ('vc', 'varchar(20)', ''),
    ('ch', 'char(5)', ''),
    ('bin', 'binary', ''),
    ('arr', 'array<int>', ''),
    ('st', 'struct<a:int,b:decimal(10,2)>', ''),
    ('when', 'interval_day_time', ''),
    ('', None, None),
    ('# Partition Information', None, None),
    ('# col_name', 'data_type', 'comment'),
    ('ds', 'string', ''),
]

FORMATTED = DESCRIBE[:-4] + [
    ('# Detailed Table Information', None, None),
    ('Table Type:         ', 'VIRTUAL_VIEW        ', None),
    ('Table Parameters:', None, None),
    ('', 'bucketing_version   ', '2                   '),
    ('', 'comment             ', 'the comment         '),
    ('# Storage Information', None, None),
    ('SerDe Library:      ', 'null                ', None),
    ('# View Information', None, None),
    ('Original Query:     ', 'SELECT id FROM t', None),
]


class FakeSAConnection(object):
    def __init__(self, show_views=True):
        self.show_views = show_views
        self.statements = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith('SHOW TABLES'):
            return Result(['tab_name'], [('t',), ('v',)])
        if sql.startswith('SHOW VIEWS'):
            if not self.show_views:
                raise sa.exc.OperationalError(sql, {}, Exception('ParseException'))
            return Result(['tab_name'], [('v',)])
        if sql.startswith('DESCRIBE FORMATTED'):
            return Result(['col_name', 'data_type', 'comment'], FORMATTED)
        if sql.startswith('DESCRIBE'):
            return Result(['col_name', 'data_type', 'comment'], DESCRIBE)
        raise AssertionError(sql)


def test_table_names_exclude_views():
    conn = FakeSAConnection()
    assert HiveDialect().get_table_names(conn, schema='s') == ['t']
    assert HiveDialect().get_view_names(conn, schema='s') == ['v']
    assert conn.statements == ['SHOW TABLES IN `s`', 'SHOW VIEWS IN `s`', 'SHOW VIEWS IN `s`']


def test_servers_without_show_views_keep_the_old_listing():
    conn = FakeSAConnection(show_views=False)
    assert HiveDialect().get_table_names(conn) == ['t', 'v']
    assert HiveDialect().get_view_names(conn) == ['t', 'v']


def test_spark_style_show_tables_uses_the_name_column():
    class SparkConnection(object):
        def execute(self, statement):
            return Result(['namespace', 'tableName', 'isTemporary'], [('db', 't', False)])

    assert HiveDialect()._show_names(SparkConnection(), 'TABLES', None) == ['t']


def test_get_columns_types_and_comments():
    with pytest.warns(sa.exc.SAWarning, match='interval_day_time'):
        cols = HiveDialect().get_columns(FakeSAConnection(), 't')
    by_name = {c['name']: c for c in cols}
    assert [c['name'] for c in cols] == [
        'id', 'ti', 'd', 'dec38', 'dec', 'vc', 'ch', 'bin', 'arr', 'st', 'when']
    assert by_name['id']['comment'] == 'row id' and by_name['ti']['comment'] is None
    t = {k: c['type'] for k, c in by_name.items()}
    assert isinstance(t['d'], sa.DOUBLE)
    assert isinstance(t['dec38'], HiveNumeric)
    assert (t['dec38'].precision, t['dec38'].scale) == (38, 18)
    assert (t['dec'].precision, t['dec'].scale) == (10, 0)
    assert isinstance(t['vc'], sa.VARCHAR) and t['vc'].length == 20
    assert isinstance(t['ch'], sa.CHAR) and t['ch'].length == 5
    assert isinstance(t['bin'], sa.BINARY)
    assert isinstance(t['arr'], HiveComplexType) and str(t['arr']) == 'array<int>'
    assert str(t['st']) == 'struct<a:int,b:decimal(10,2)>'
    assert t['when'] is sa.types.NullType


def test_reflected_types_render_in_hive_ddl():
    cols = HiveDialect().get_columns(FakeSAConnection(), 't')[:-1]
    table = sa.Table('copy', sa.MetaData(), *[sa.Column(c['name'], c['type']) for c in cols])
    ddl = str(CreateTable(table).compile(dialect=HiveDialect()))
    for fragment in ('`ti` TINYINT', '`d` DOUBLE', '`dec38` DECIMAL(38, 18)',
                     '`bin` BINARY', '`arr` array<int>', '`st` struct<a:int,b:decimal(10,2)>'):
        assert fragment in ddl, ddl


@pytest.mark.parametrize('coltype,expected', [
    (sa.Numeric(20, 6), 'DECIMAL(20, 6)'),
    (sa.DECIMAL(38, 18), 'DECIMAL(38, 18)'),
    (sa.Numeric(12), 'DECIMAL(12)'),
    (sa.Numeric(), 'DECIMAL'),
])
def test_numeric_ddl_keeps_precision_and_scale(coltype, expected):
    assert coltype.compile(dialect=HiveDialect()) == expected


def test_decimal_results_are_exact():
    processor = HiveNumeric(38, 18).result_processor(HiveDialect(), None)
    assert processor('1.100000000000000001') == Decimal('1.100000000000000001')
    assert processor(Decimal('2.5')) == Decimal('2.5')
    assert processor(None) is None
    as_float = HiveNumeric(10, 2, asdecimal=False).result_processor(HiveDialect(), None)
    assert as_float(Decimal('2.5')) == 2.5


def test_bound_values_on_reflected_columns_compile():
    cols = HiveDialect().get_columns(FakeSAConnection(), 't')
    table = sa.Table('t', sa.MetaData(), *[sa.Column(c['name'], c['type']) for c in cols[:-1]])
    from pyhive.sqlalchemy_hive import HiveDate, HiveTimestamp
    t2 = sa.Table('t2', sa.MetaData(), sa.Column('d', HiveDate), sa.Column('ts', HiveTimestamp))
    for column, value in ((t2.c.d, datetime.date(2000, 1, 1)),
                          (t2.c.ts, datetime.datetime(2000, 1, 1, 1)),
                          (table.c.dec38, Decimal('1.5'))):
        compiled = (column == value).compile(dialect=HiveDialect())
        params = compiled.construct_params()
        processors = compiled._bind_processors
        key = list(params)[0]
        got = processors[key](params[key]) if key in processors else params[key]
        assert got == value


def test_table_comment_view_definition_and_constraints():
    dialect = HiveDialect()
    conn = FakeSAConnection()
    assert dialect.get_table_comment(conn, 'v') == {'text': 'the comment'}
    assert dialect.get_view_definition(conn, 'v') == 'SELECT id FROM t'
    assert dialect.get_unique_constraints(conn, 't') == []
    assert dialect.get_check_constraints(conn, 't') == []


def test_tinyint_compiles():
    from sqlalchemy.dialects.mysql import TINYINT
    assert TINYINT().compile(dialect=HiveDialect()) == 'TINYINT'
