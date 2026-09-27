# encoding: utf-8
"""Offline tests for Presto type handling under SQLAlchemy.

These drive the real DB-API cursor and SQLAlchemy dialect against a fake HTTP
session that answers like the Presto REST protocol, so they need no server.
"""
from __future__ import absolute_import
from __future__ import unicode_literals

import datetime
import warnings
from decimal import Decimal

import dateutil.tz
import pytest
import sqlalchemy as sa

from pyhive import common
from pyhive import exc
from pyhive import hive
from pyhive import presto
from pyhive.sqlalchemy_hive import HiveDialect
from pyhive.sqlalchemy_presto import PrestoDialect
from pyhive.sqlalchemy_trino import TrinoDialect

# Same name as the setup.py entry point, so this works without an installed package.
sa.dialects.registry.register('trino.pyhive', 'pyhive.sqlalchemy_trino', 'TrinoDialect')

BIG = Decimal('12345678901234567890.123456789012345678')
BIGINT_MIN = -9223372036854775808
SHOW_COLUMNS = [
    {'name': 'Column', 'type': 'varchar'},
    {'name': 'Type', 'type': 'varchar'},
    {'name': 'Extra', 'type': 'varchar'},
    {'name': 'Comment', 'type': 'varchar'},
]


class FakeResponse(object):
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession(object):
    """Records every statement and answers with canned columns/rows."""

    def __init__(self, columns=None, data=None):
        self.statements = []
        self.columns = columns or [{'name': 'x', 'type': 'integer'}]
        self.data = data if data is not None else [[1]]

    def post(self, url, data=None, headers=None, **kwargs):
        self.statements.append(data.decode('utf-8'))
        return FakeResponse({'id': 'q', 'columns': self.columns, 'data': self.data})


def engine(session, database='memory/analytics', scheme='presto'):
    return sa.create_engine(
        scheme + '://tester@example.invalid:8080/' + database,
        connect_args={'requests_session': session},
    )


def scalar(columns, data, expression, scheme='presto'):
    with engine(FakeSession(columns, data), scheme=scheme).connect() as conn:
        return conn.scalar(sa.select(expression))


# Typed DECIMAL results must keep every digit.

def test_typed_decimal_select_is_exact():
    got = scalar([{'name': 'amount', 'type': 'decimal(38,18)'}],
                 [[str(BIG)]], sa.literal_column('amount', sa.DECIMAL(38, 18)))
    assert got == BIG and type(got) is Decimal


def test_decimal_result_processor_does_not_go_through_float():
    assert sa.DECIMAL(38, 18).result_processor(PrestoDialect(), None) is None


@pytest.mark.parametrize('sa_type', [sa.Float, sa.Float(precision=53), sa.REAL, sa.FLOAT])
def test_float_columns_still_return_float(sa_type):
    got = scalar([{'name': 'r', 'type': 'double'}], [[1.25]],
                 sa.literal_column('r', sa_type))
    assert got == 1.25 and type(got) is float


def test_float_asdecimal_still_returns_decimal():
    got = scalar([{'name': 'r', 'type': 'double'}], [[1.25]],
                 sa.literal_column('r', sa.Float(asdecimal=True)))
    assert got == Decimal('1.2500000000') and type(got) is Decimal


# Numeric over DOUBLE/REAL/integer results still returns Decimal, as on master.

@pytest.mark.parametrize('presto_type,value,sa_type,expected', [
    ('double', 1.1, sa.Numeric(10, 2), Decimal('1.10')),
    ('real', 1.1, sa.Numeric(10, 2), Decimal('1.10')),
    ('double', 1.1, sa.Numeric, Decimal('1.1000000000')),
    ('double', 1.1, sa.Numeric(10, 2, decimal_return_scale=4), Decimal('1.1000')),
    ('double', 1.1, sa.NUMERIC(10, 2), Decimal('1.10')),
    ('bigint', 9223372036854775807, sa.Numeric(20, 2), Decimal('9223372036854775807.00')),
    ('integer', -5, sa.Numeric(10, 0), Decimal('-5')),
])
def test_numeric_over_non_decimal_results_returns_decimal(presto_type, value, sa_type, expected):
    got = scalar([{'name': 'v', 'type': presto_type}], [[value]],
                 sa.literal_column('v', sa_type))
    assert got == expected and type(got) is Decimal
    assert str(got) == str(expected)


@pytest.mark.parametrize('sa_type', [sa.Numeric(38, 18), sa.NUMERIC(38, 18), sa.Numeric])
def test_numeric_over_decimal_results_stays_exact(sa_type):
    got = scalar([{'name': 'amount', 'type': 'decimal(38,18)'}],
                 [[str(BIG)]], sa.literal_column('amount', sa_type))
    assert got == BIG and type(got) is Decimal


def test_numeric_asdecimal_false_returns_float():
    got = scalar([{'name': 'amount', 'type': 'decimal(10,2)'}], [['1.25']],
                 sa.literal_column('amount', sa.Numeric(10, 2, asdecimal=False)))
    assert got == 1.25 and type(got) is float


def test_numeric_null_stays_null():
    assert scalar([{'name': 'v', 'type': 'double'}], [[None]],
                  sa.literal_column('v', sa.Numeric(10, 2))) is None


# Decimal bound parameters.

def test_decimal_parameter_is_a_decimal_literal():
    session = FakeSession()
    with engine(session).connect() as conn:
        conn.execute(sa.text('SELECT :v'), {'v': BIG})
    assert session.statements[-1] == "SELECT DECIMAL '{}'".format(BIG)


def test_core_insert_into_decimal_column_keeps_digits():
    session = FakeSession()
    table = sa.Table('t', sa.MetaData(), sa.Column('amount', sa.DECIMAL(38, 18)))
    with engine(session).connect() as conn:
        conn.execute(table.insert(), [{'amount': BIG}])
    assert "DECIMAL '{}'".format(BIG) in session.statements[-1]


@pytest.mark.parametrize('value,literal', [
    (Decimal('1E+2'), "DECIMAL '100'"),
    (Decimal('-0.000000000000000001'), "DECIMAL '-0.000000000000000001'"),
    (Decimal('0.10'), "DECIMAL '0.10'"),
])
def test_decimal_literal_spelling(value, literal):
    assert presto.PrestoParamEscaper().escape_item(value) == literal


@pytest.mark.parametrize('value', [Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')])
def test_non_finite_decimal_is_rejected(value):
    with pytest.raises(exc.ProgrammingError):
        presto.PrestoParamEscaper().escape_item(value)


def test_decimal_inside_in_list():
    escaped = presto.PrestoParamEscaper().escape_item([Decimal('1.5'), 2])
    assert escaped == "(DECIMAL '1.5',2)"


# Typed DATE / TIMESTAMP / TIME results.

@pytest.mark.parametrize('presto_type,value,sa_type,expected', [
    ('date', '2000-02-29', sa.Date, datetime.date(2000, 2, 29)),
    ('date', '0001-01-01', sa.DATE, datetime.date(1, 1, 1)),
    ('timestamp', '2026-09-24 12:34:56.123', sa.TIMESTAMP,
     datetime.datetime(2026, 9, 24, 12, 34, 56, 123000)),
    ('timestamp', '1970-01-01 00:00:00.000', sa.DateTime, datetime.datetime(1970, 1, 1)),
    ('timestamp', '2026-09-24 12:34:56', sa.DateTime, datetime.datetime(2026, 9, 24, 12, 34, 56)),
    ('timestamp', '2026-09-24 12:34:56.123456000', sa.DateTime,
     datetime.datetime(2026, 9, 24, 12, 34, 56, 123456)),
    ('time', '01:02:03.456', sa.Time, datetime.time(1, 2, 3, 456000)),
])
def test_typed_temporal_select(presto_type, value, sa_type, expected):
    got = scalar([{'name': 'v', 'type': presto_type}], [[value]],
                 sa.literal_column('v', sa_type))
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize('value,offset', [
    ('2026-09-24 12:34:56.123 UTC', datetime.timedelta(0)),
    ('2026-09-24 12:34:56.123 +05:30', datetime.timedelta(hours=5, minutes=30)),
    ('2026-09-24 12:34:56.123 -08:00', datetime.timedelta(hours=-8)),
    ('2026-09-24 12:34:56.123 America/New_York', datetime.timedelta(hours=-4)),
    ('2026-01-24 12:34:56.123 America/New_York', datetime.timedelta(hours=-5)),
])
def test_timestamp_with_time_zone(value, offset):
    got = scalar([{'name': 'v', 'type': 'timestamp with time zone'}], [[value]],
                 sa.literal_column('v', sa.TIMESTAMP(timezone=True)))
    assert got.utcoffset() == offset
    assert got.replace(tzinfo=None).microsecond == 123000


@pytest.mark.parametrize('scheme', ['presto', 'trino+pyhive'])
@pytest.mark.parametrize('value,offset', [
    ('01:02:03.456 +01:00', datetime.timedelta(hours=1)),
    # Trino's SqlTimeWithTimeZone has no space before the offset.
    ('01:02:03.456+01:00', datetime.timedelta(hours=1)),
    ('12:34:56.123-05:30', -datetime.timedelta(hours=5, minutes=30)),
    ('00:14:42.457+00:00', datetime.timedelta(0)),
])
def test_time_with_time_zone(scheme, value, offset):
    got = scalar([{'name': 'v', 'type': 'time with time zone'}], [[value]],
                 sa.literal_column('v', sa.Time(timezone=True)), scheme=scheme)
    assert got.utcoffset() == offset


def test_trino_dialect_uses_the_presto_types():
    assert type(engine(FakeSession(), scheme='trino+pyhive').dialect) is TrinoDialect
    assert TrinoDialect.colspecs is PrestoDialect.colspecs


@pytest.mark.parametrize('zone,offset', [
    ('UTC', datetime.timedelta(0)),
    ('Z', datetime.timedelta(0)),
    ('Zulu', datetime.timedelta(0)),
    ('GMT', datetime.timedelta(0)),
    ('UCT', datetime.timedelta(0)),
    ('Etc/UTC', datetime.timedelta(0)),
    # POSIX sign inversion: Etc/GMT+5 is UTC-05:00.
    ('Etc/GMT+5', datetime.timedelta(hours=-5)),
    ('Etc/GMT-14', datetime.timedelta(hours=14)),
    ('Etc/GMT0', datetime.timedelta(0)),
])
def test_time_with_fixed_region_zone(zone, offset):
    got = scalar([{'name': 'v', 'type': 'time with time zone'}], [['00:13:38.240 ' + zone]],
                 sa.literal_column('v', sa.Time(timezone=True)))
    assert got.tzinfo is not None and got.utcoffset() == offset
    assert got == datetime.time(0, 13, 38, 240000, datetime.timezone(offset))


def test_time_with_region_zone_raises():
    # A TIME has no date, so a DST-observing zone has no single offset.
    with pytest.raises(exc.DataError, match='Region time zone'):
        scalar([{'name': 'v', 'type': 'time with time zone'}],
               [['00:13:38.240 America/New_York']],
               sa.literal_column('v', sa.Time(timezone=True)))


def test_timestamp_with_fixed_zone_uses_datetime_timezone():
    got = scalar([{'name': 'v', 'type': 'timestamp with time zone'}],
                 [['2026-09-24 12:34:56.123 Etc/GMT+5']],
                 sa.literal_column('v', sa.TIMESTAMP(timezone=True)))
    assert got.tzinfo == datetime.timezone(datetime.timedelta(hours=-5))


@pytest.mark.parametrize('value,message', [
    # 01:30 happens twice on 2026-11-01 in New York (EDT, then EST).
    ('2026-11-01 01:30:00.000 America/New_York', 'Ambiguous'),
    # 02:30 does not happen on 2026-03-08 in New York.
    ('2026-03-08 02:30:00.000 America/New_York', 'Nonexistent'),
])
def test_timestamp_at_dst_transition_raises(value, message):
    with pytest.raises(exc.DataError, match=message):
        scalar([{'name': 'v', 'type': 'timestamp with time zone'}], [[value]],
               sa.literal_column('v', sa.TIMESTAMP(timezone=True)))


@pytest.mark.parametrize('value,offset', [
    ('2026-11-01 00:59:59.999 America/New_York', datetime.timedelta(hours=-4)),
    ('2026-11-01 02:00:00.000 America/New_York', datetime.timedelta(hours=-5)),
    ('2026-03-08 03:00:00.000 America/New_York', datetime.timedelta(hours=-4)),
])
def test_timestamp_next_to_dst_transition(value, offset):
    got = scalar([{'name': 'v', 'type': 'timestamp with time zone'}], [[value]],
                 sa.literal_column('v', sa.TIMESTAMP(timezone=True)))
    assert got.utcoffset() == offset


def test_null_temporal_stays_null():
    assert scalar([{'name': 'v', 'type': 'date'}], [[None]],
                  sa.literal_column('v', sa.Date)) is None


@pytest.mark.parametrize('value', [
    '2026-09-24 12:34:56.123456789',  # nanoseconds that datetime cannot hold
    '2026-09-24 12:34:56.123 Not/AZone',
    'yesterday',
])
def test_unrepresentable_temporal_value_raises(value):
    with pytest.raises(exc.DataError):
        scalar([{'name': 'v', 'type': 'timestamp'}], [[value]],
               sa.literal_column('v', sa.DateTime))


@pytest.mark.parametrize('presto_type,value,sa_type', [
    # Presto returns these for DATE; datetime.date cannot hold them.
    ('date', '-0001-01-01', sa.Date),
    ('date', '+10000-01-01', sa.Date),
    ('date', '0000-01-01', sa.Date),
    ('date', '2001-02-29', sa.Date),
    # A Date-typed expression over a timestamp value.
    ('timestamp', '2026-09-24 12:34:56.123', sa.Date),
    ('timestamp', '-0001-01-01 00:00:00.000', sa.DateTime),
    ('time', '24:00:00.000', sa.Time),
])
def test_out_of_range_temporal_value_raises_data_error(presto_type, value, sa_type):
    with pytest.raises(exc.DataError) as caught:
        scalar([{'name': 'v', 'type': presto_type}], [[value]],
               sa.literal_column('v', sa_type))
    assert value in str(caught.value)


def test_untyped_temporal_values_are_unchanged():
    with engine(FakeSession([{'name': 'd', 'type': 'date'}], [['2000-02-29']])).connect() as c:
        assert c.exec_driver_sql('SELECT d').scalar() == '2000-02-29'


# Reflected column types.

def reflect(rows):
    session = FakeSession(SHOW_COLUMNS, [[name, type_, '', ''] for name, type_ in rows])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        columns = sa.inspect(engine(session)).get_columns('t')
    return {c['name']: c['type'] for c in columns}, [str(w.message) for w in caught]


def test_parameterized_types_reflect():
    types_, caught = reflect([
        ('amount', 'decimal(38,18)'), ('plain', 'decimal'), ('txt', 'varchar(200)'),
        ('unbounded', 'varchar'), ('code', 'char(3)'), ('t', 'time'),
        ('tz', 'timestamp with time zone'), ('ttz', 'time with time zone'),
        ('trino_ts', 'timestamp(6) with time zone'), ('n', 'integer'), ('d', 'date'),
        ('ts3', 'timestamp(3)'), ('t3', 'time(3)'), ('ttz3', 'time(3) with time zone'),
        ('ts', 'timestamp'),
    ])
    assert caught == []
    assert isinstance(types_['amount'], sa.DECIMAL)
    assert (types_['amount'].precision, types_['amount'].scale) == (38, 18)
    assert isinstance(types_['plain'], sa.DECIMAL) and types_['plain'].precision is None
    assert isinstance(types_['txt'], sa.VARCHAR) and types_['txt'].length == 200
    assert isinstance(types_['unbounded'], sa.String)
    assert isinstance(types_['code'], sa.CHAR) and types_['code'].length == 3
    assert isinstance(types_['t'], sa.TIME) and not types_['t'].timezone
    assert isinstance(types_['tz'], sa.TIMESTAMP) and types_['tz'].timezone
    assert isinstance(types_['ttz'], sa.TIME) and types_['ttz'].timezone
    assert isinstance(types_['trino_ts'], sa.TIMESTAMP) and types_['trino_ts'].timezone
    assert isinstance(types_['n'], sa.Integer)
    assert isinstance(types_['d'], sa.DATE)
    assert isinstance(types_['ts3'], sa.TIMESTAMP) and not types_['ts3'].timezone
    assert isinstance(types_['t3'], sa.TIME) and not types_['t3'].timezone
    assert isinstance(types_['ttz3'], sa.TIME) and types_['ttz3'].timezone
    assert isinstance(types_['ts'], sa.TIMESTAMP) and not types_['ts'].timezone


def test_trino_show_columns_reflect_with_precision():
    session = FakeSession(SHOW_COLUMNS, [['ts', 'timestamp(3)', '', ''],
                                         ['t', 'time(3)', '', ''],
                                         ['tz', 'timestamp(3) with time zone', '', '']])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        columns = sa.inspect(engine(session, scheme='trino+pyhive')).get_columns('t')
    assert [str(w.message) for w in caught] == []
    assert [type(c['type']) for c in columns] == [sa.TIMESTAMP, sa.TIME, sa.TIMESTAMP]
    assert [c['type'].timezone for c in columns] == [False, False, True]


@pytest.mark.parametrize('type_str', ['timestamp(3, 1)', 'time(3,6) with time zone',
                                      'date(3)', 'interval day to second'])
def test_unsupported_temporal_spellings_still_warn(type_str):
    types_, caught = reflect([('x', type_str)])
    assert isinstance(types_['x'], sa.types.NullType)
    assert len(caught) == 1


def test_unknown_types_still_warn_and_reflect_as_null_type():
    types_, caught = reflect([('j', 'json'), ('a', 'array(integer)'), ('r', 'row(x integer)')])
    assert all(isinstance(t, sa.types.NullType) for t in types_.values())
    assert len(caught) == 3 and all('Did not recognize type' in m for m in caught)


# View names.

def test_get_view_names_uses_connection_schema():
    session = FakeSession([{'name': 'table_name', 'type': 'varchar'}], [['v1'], ['v2']])
    assert sa.inspect(engine(session)).get_view_names() == ['v1', 'v2']
    assert session.statements[-1] == (
        "SELECT table_name FROM information_schema.views "
        "WHERE table_schema = 'analytics' ORDER BY table_name")


def test_get_view_names_explicit_schema_is_escaped():
    session = FakeSession([{'name': 'table_name', 'type': 'varchar'}], [])
    assert sa.inspect(engine(session)).get_view_names(schema="o'x") == []
    assert "table_schema = 'o''x'" in session.statements[-1]


def test_get_view_names_defaults_like_the_dbapi():
    session = FakeSession([{'name': 'table_name', 'type': 'varchar'}], [])
    sa.inspect(engine(session, database='memory')).get_view_names()
    assert "table_schema = 'default'" in session.statements[-1]


# Primary key reflection.

def test_get_pk_constraint_is_a_constraint_dict():
    pk = sa.inspect(engine(FakeSession())).get_pk_constraint('t')
    assert pk['constrained_columns'] == [] and pk['name'] is None


# Minimum BIGINT.

def test_bigint_min_parameter_is_parseable():
    session = FakeSession()
    with engine(session).connect() as conn:
        conn.execute(sa.text('SELECT :v'), {'v': BIGINT_MIN})
    assert session.statements[-1] == 'SELECT (-9223372036854775807 - 1)'


@pytest.mark.parametrize('value,escaped', [
    (9223372036854775807, 9223372036854775807),
    (-9223372036854775807, -9223372036854775807),
    (True, True),
    (False, False),
    (-9.223372036854775808e18, -9.223372036854775808e18),
])
def test_other_numbers_are_unchanged(value, escaped):
    got = presto.PrestoParamEscaper().escape_item(value)
    assert got == escaped and type(got) is type(escaped)


# The Hive driver shares common.ParamEscaper; its behaviour must not change.

@pytest.mark.parametrize('escaper', [common.ParamEscaper(), hive.HiveParamEscaper()])
def test_hive_and_common_escaping_is_unchanged(escaper):
    assert escaper.escape_item(BIGINT_MIN) == BIGINT_MIN
    assert escaper.escape_item(1.5) == 1.5
    assert escaper.escape_item(None) == 'NULL'
    assert escaper.escape_item(datetime.date(2020, 4, 17)) == "'2020-04-17'"
    with pytest.raises(exc.ProgrammingError):
        escaper.escape_item(Decimal('1.5'))


def test_hive_dialect_does_not_pick_up_presto_types():
    presto_types = set(PrestoDialect.colspecs.values())
    assert not issubclass(HiveDialect, PrestoDialect)
    assert not presto_types & set(HiveDialect.colspecs.values())


def test_dateutil_region_zones_are_available():
    assert dateutil.tz.gettz('America/New_York') is not None
