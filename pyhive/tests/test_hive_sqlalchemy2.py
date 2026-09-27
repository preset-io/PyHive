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

# SQLAlchemy 1.x has no DOUBLE type; the dialect reflects double as Float there.
DOUBLE = getattr(sa, 'DOUBLE', None)
OK = ttypes.TStatus(statusCode=ttypes.TStatusCode.SUCCESS_STATUS)
ESC = hive.HiveParamEscaper()


# Parameters.

@pytest.mark.parametrize('value,expected', [
    (Decimal('12345678901234567890.123456789012345678'),
     '12345678901234567890.123456789012345678BD'),
    (Decimal('-1E-18'), '-0.000000000000000001BD'),
    (Decimal('1E+3'), '1000BD'),
    (0.1, 'CAST(0.1 AS DOUBLE)'),
    (1e300, 'CAST(1e+300 AS DOUBLE)'),
    (-2.25, 'CAST(-2.25 AS DOUBLE)'),
    (5e-324, 'CAST(5e-324 AS DOUBLE)'),
    (Decimal('-1E-38'), '-0.00000000000000000000000000000000000001BD'),
    (Decimal('1E+37'), '1' + '0' * 37 + 'BD'),
    (Decimal('1.' + '0' * 40), '1BD'),
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


@pytest.mark.parametrize('value', [Decimal('1E+50'), Decimal('1E-40'), Decimal('1' * 39)])
def test_decimals_hive_cannot_hold_are_rejected(value):
    # Hive would return NULL (1E+50) or round to 0 (1E-40).
    with pytest.raises(hive.ProgrammingError, match='38 digits'):
        ESC.escape_item(value)


class ReprFloat(float):
    # numpy >= 2 float64: repr() is 'np.float64(0.1)'
    def __repr__(self):
        return 'np.float64({})'.format(float.__repr__(self))


def test_float_subclasses_render_as_plain_floats():
    assert ESC.escape_item(ReprFloat(0.1)) == 'CAST(0.1 AS DOUBLE)'
    np = pytest.importorskip('numpy')
    assert ESC.escape_item(np.float64(0.1)) == 'CAST(0.1 AS DOUBLE)'


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


NANOS = '2024-01-01 10:00:00.123456789'


def test_nanosecond_timestamps_are_truncated_by_default(monkeypatch, caplog):
    monkeypatch.setattr(hive, '_warned_truncated_timestamp', False)
    with caplog.at_level('WARNING', logger='pyhive.hive'):
        assert hive._parse_timestamp(NANOS) == datetime.datetime(2024, 1, 1, 10, 0, 0, 123456)
        hive._parse_timestamp('2024-01-01 10:00:00.000000001')
    # warned once, not per value
    assert [r.getMessage() for r in caplog.records if 'Truncating' in r.getMessage()] == [
        'Truncating TIMESTAMP "{}" to microseconds; pass strict_timestamps=True to raise '
        'DataError instead (this warning is logged once)'.format(NANOS)]


def test_strict_timestamps_refuse_to_truncate():
    with pytest.raises(hive.DataError):
        hive._parse_timestamp(NANOS, strict=True)
    assert hive._parse_timestamp('2024-01-01 10:00:00.123456000', strict=True) == \
        datetime.datetime(2024, 1, 1, 10, 0, 0, 123456)


class FetchClient(object):
    """Serves one TIMESTAMP column, then an empty batch."""

    def __init__(self, values):
        self.batches = [values, []]

    def ExecuteStatement(self, req):
        return Response(operationHandle=ttypes.TOperationHandle(hasResultSet=True))

    def GetResultSetMetadata(self, req):
        type_desc = ttypes.TTypeDesc(types=[ttypes.TTypeEntry(
            primitiveEntry=ttypes.TPrimitiveTypeEntry(type=ttypes.TTypeId.TIMESTAMP_TYPE))])
        return Response(schema=ttypes.TTableSchema(
            columns=[ttypes.TColumnDesc(columnName='ts', typeDesc=type_desc, position=1)]))

    def FetchResults(self, req):
        values = self.batches.pop(0)
        column = ttypes.TColumn(stringVal=ttypes.TStringColumn(values=values, nulls=b''))
        return Response(results=ttypes.TRowSet(startRowOffset=0, rows=[], columns=[column]))

    def CloseOperation(self, req):
        return Response()


def test_strict_timestamps_option_on_connection_and_cursor():
    conn = FakeConnection(FetchClient([NANOS]))
    cur = hive.Cursor(conn)
    assert cur.strict_timestamps is False
    cur.execute('SELECT ts')
    assert cur.fetchall() == [(datetime.datetime(2024, 1, 1, 10, 0, 0, 123456),)]

    conn = FakeConnection(FetchClient([NANOS]))
    conn.strict_timestamps = True
    cur = hive.Cursor(conn)
    cur.execute('SELECT ts')
    with pytest.raises(hive.DataError):
        cur.fetchall()
    # the cursor argument overrides the connection
    assert hive.Cursor(conn, strict_timestamps=False).strict_timestamps is False


@pytest.mark.parametrize('value,expected', [
    (True, True), ('true', True), ('1', True), (False, False), ('false', False), ('0', False)])
def test_strict_timestamps_accepts_url_query_strings(value, expected):
    # create_engine('hive://...?strict_timestamps=true') passes the flag as a string
    assert hive._as_bool(value) is expected


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
    with pytest.raises(hive.OperationalError, match='^Could not connect to HiveServer2') as info:
        hive.connect('127.0.0.1', port)
    # never connected, so nothing was lost
    assert not hive.is_connection_lost(info.value)


class FailingTransport(FakeTransport):
    """thrift_sasl raises this from open() when the server rejects the credentials."""

    def open(self):
        raise TTransportException(message='Bad status: 3 (Error validating the login)')


def test_failed_sasl_handshake_is_not_a_lost_connection():
    transport = FailingTransport()
    with pytest.raises(hive.OperationalError) as info:
        hive.Connection(thrift_transport=transport, strict_timestamps='true')
    assert str(info.value) == (
        'Could not connect to HiveServer2: Bad status: 3 (Error validating the login)')
    assert isinstance(info.value.__cause__, TTransportException)
    assert not hive.is_connection_lost(info.value)
    assert not HiveDialect().is_disconnect(info.value, None, None)
    assert transport.closed


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
    ('tabbed', 'string', 'col\\tc \\\\ \\u00fc'),
    ('serde', 'string', 'from deserializer'),
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
    ('', 'comment             ', 'the\\ncomment         '),
    ('# Storage Information', None, None),
    ('SerDe Library:      ', 'null                ', None),
    ('# View Information', None, None),
    ('Original Query:     ', 'SELECT a,           ', None),
    ('', '                    ', '  b                 '),
    ('', '                    ', 'FROM t_c            '),
    ('', '                    ', 'WHERE a > 1         '),
    ('Expanded Query:     ', 'SELECT `t_c`.`a`,   ', None),
    ('', '                    ', '  `t_c`.`b`         '),
]


class FakeSAConnection(object):
    def __init__(self, show_views=True, show_views_error='ParseException'):
        self.show_views = show_views
        self.show_views_error = show_views_error
        self.statements = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith('SHOW TABLES'):
            return Result(['tab_name'], [('t',), ('v',)])
        if sql.startswith('SHOW VIEWS'):
            if not self.show_views:
                raise sa.exc.OperationalError(sql, {}, Exception(self.show_views_error))
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


@pytest.mark.parametrize('error', [
    "FAILED: ParseException line 1:5 cannot recognize input near 'SHOW' 'VIEWS' '<EOF>'",
    "mismatched input 'VIEWS' expecting {'COLUMNS', 'CREATE', ...}",
])
def test_show_views_parse_errors_mean_unsupported(error):
    conn = FakeSAConnection(show_views=False, show_views_error=error)
    assert HiveDialect().get_view_names(conn) == ['t', 'v']


@pytest.mark.parametrize('error', [
    'HiveAccessControlException Permission denied: user [u] does not have [SELECT] privilege',
    'Lost connection to HiveServer2: TSocket read 0 bytes',
])
def test_other_show_views_errors_are_raised(error):
    conn = FakeSAConnection(show_views=False, show_views_error=error)
    with pytest.raises(sa.exc.OperationalError, match=error.split()[0]):
        HiveDialect().get_table_names(conn)
    with pytest.raises(sa.exc.OperationalError):
        HiveDialect().get_view_names(conn)


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
        'id', 'tabbed', 'serde', 'ti', 'd', 'dec38', 'dec', 'vc', 'ch', 'bin', 'arr', 'st', 'when']
    assert by_name['id']['comment'] == 'row id' and by_name['ti']['comment'] is None
    # Hive Java-escapes comments; 'from deserializer' is its placeholder for none
    assert by_name['tabbed']['comment'] == 'col\tc \\ \u00fc'
    assert by_name['serde']['comment'] is None
    t = {k: c['type'] for k, c in by_name.items()}
    assert isinstance(t['d'], DOUBLE or sa.Float)
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
    for fragment in ('`ti` TINYINT', '`d` DOUBLE' if DOUBLE else '`d` FLOAT',
                     '`dec38` DECIMAL(38, 18)',
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
    assert dialect.get_table_comment(conn, 'v') == {'text': 'the\ncomment'}
    # continuation rows joined, indentation kept, the expanded query not included
    assert dialect.get_view_definition(conn, 'v') == 'SELECT a,\n  b\nFROM t_c\nWHERE a > 1'
    assert dialect.get_unique_constraints(conn, 't') == []
    assert dialect.get_check_constraints(conn, 't') == []


def test_tinyint_compiles():
    from sqlalchemy.dialects.mysql import TINYINT
    assert TINYINT().compile(dialect=HiveDialect()) == 'TINYINT'


# KERBEROS through python-gssapi when pure-sasl has no kerberos backend.

class FakeGSSContext(object):
    """Scripted initiator: two context tokens, then the RFC 4752 layer exchange."""

    def __init__(self, offer=b'\x01\x00\x10\x00'):
        self.complete = False
        self.steps = []
        self.offer = offer

    def step(self, token=None):
        self.steps.append(token)
        if token is None:
            return b'initial'
        self.complete = True
        return None

    def unwrap(self, message):
        assert message == b'wrapped-offer'
        return collections.namedtuple('Unwrapped', 'message')(self.offer)

    def wrap(self, message, encrypt):
        assert encrypt is False
        return collections.namedtuple('Wrapped', 'message')(b'wrapped:' + message)


def gssapi_client(context):
    from pyhive.sasl_compat import GSSAPIClient
    client = GSSAPIClient.__new__(GSSAPIClient)
    client.error = None
    client._context = context
    return client


def test_gssapi_client_handshake():
    client = gssapi_client(FakeGSSContext())
    assert client.start('GSSAPI') == (True, 'GSSAPI', b'initial')
    assert client.step(b'server-token') == (True, b'')
    # "no security layer", maximum size 0, no authorization id
    assert client.step(b'wrapped-offer') == (True, b'wrapped:\x01\x00\x00\x00')
    assert client.encode(b'x') == (True, b'x') and client.decode(b'y') == (True, b'y')


def test_gssapi_client_rejects_servers_without_the_auth_layer():
    client = gssapi_client(FakeGSSContext(offer=b'\x04\x00\x10\x00'))
    client.start('GSSAPI')
    client.step(b'server-token')
    assert client.step(b'wrapped-offer') == (False, None)
    assert 'quality of protection' in client.getError()


def test_gssapi_is_used_only_without_pure_sasl_kerberos(monkeypatch):
    from pyhive import sasl_compat
    created = []
    monkeypatch.setattr(sasl_compat, 'GSSAPIClient',
                        lambda **kw: created.append(kw) or 'gssapi-client')
    monkeypatch.setattr(hive, '_has_gssapi', lambda: True)
    monkeypatch.setattr(hive, '_pure_sasl_has_kerberos', lambda: False)
    assert hive.get_pure_sasl_client('h', 'GSSAPI', service='hive') == 'gssapi-client'
    assert created == [{'host': 'h', 'service': 'hive'}]
    monkeypatch.setattr(hive, '_pure_sasl_has_kerberos', lambda: True)
    assert hive.get_pure_sasl_client('h', 'GSSAPI', service='hive') != 'gssapi-client'
    # PLAIN never uses it
    monkeypatch.setattr(hive, '_pure_sasl_has_kerberos', lambda: False)
    assert hive.get_pure_sasl_client('h', 'PLAIN', username='u', password='p') != 'gssapi-client'


# Result types: Numeric, dates, zoned timestamps, newer Thrift type ids.

def _result(type_, value):
    processor = type_.dialect_impl(HiveDialect()).result_processor(HiveDialect(), None)
    return processor(value) if processor else value


@pytest.mark.parametrize('type_,value,expected', [
    (sa.Numeric(10, 2), 1.1, Decimal('1.10')),        # DOUBLE result
    (sa.Numeric(), 1.1, Decimal('1.1000000000')),
    (sa.Numeric(10, 2, decimal_return_scale=4), 1.1, Decimal('1.1000')),
    (sa.Numeric(20, 2), 9223372036854775807, Decimal('9223372036854775807.00')),  # BIGINT
    (sa.Numeric(10, 0), -5, Decimal('-5')),
    (sa.Numeric(38, 18), Decimal('1.000000000000000001'), Decimal('1.000000000000000001')),
    (sa.Numeric(10, 2, asdecimal=False), Decimal('1.25'), 1.25),
    (sa.Float(), 1.1, 1.1),
])
def test_numeric_results(type_, value, expected):
    got = _result(type_, value)
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize('value', ['0000-01-01', '-0001-01-01', '+10000-01-01', '2024-02-30'])
def test_dates_date_cannot_hold_raise(value):
    with pytest.raises(hive.DataError):
        _result(sa.Date(), value)


def test_valid_dates_parse():
    assert _result(sa.Date(), '0001-01-01') == datetime.date(1, 1, 1)
    assert _result(sa.Date(), '9999-12-31') == datetime.date(9999, 12, 31)


@pytest.mark.parametrize('value', ['0000-01-01 00:00:00', '-0001-01-01 00:00:00'])
def test_timestamps_datetime_cannot_hold_raise(value):
    with pytest.raises(hive.DataError):
        hive._parse_timestamp(value)


def test_zoned_timestamps():
    got = _result(sa.DateTime(timezone=True), '2024-07-01 12:00:00.0 America/New_York')
    assert got == datetime.datetime(2024, 7, 1, 16, 0, tzinfo=datetime.timezone.utc)
    assert got.tzinfo is not None
    # plain TIMESTAMP values already arrive as datetime and pass through
    naive = datetime.datetime(2024, 1, 1)
    assert _result(sa.DateTime(), naive) is naive


@pytest.mark.parametrize('value,message', [
    ('2024-11-03 01:30:00.0 America/New_York', 'Ambiguous'),
    ('2024-03-10 02:30:00.0 America/New_York', 'Nonexistent'),
    ('2024-07-01 12:00:00.0 Not/AZone', 'Unknown time zone'),
])
def test_zoned_timestamps_that_cannot_be_resolved_raise(value, message):
    with pytest.raises(hive.DataError, match=message):
        _result(sa.DateTime(timezone=True), value)


def test_newer_thrift_type_ids_do_not_crash():
    assert hive._type_name(ttypes.TTypeId.TIMESTAMP_TYPE) == 'TIMESTAMP_TYPE'
    assert hive._type_name(22) == 'TIMESTAMPLOCALTZ_TYPE'
    assert hive._type_name(999) == 'STRING_TYPE'
