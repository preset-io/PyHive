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


def engine(session, database='memory/analytics'):
    return sa.create_engine(
        'presto://tester@example.invalid:8080/' + database,
        connect_args={'requests_session': session},
    )


def scalar(columns, data, expression):
    with engine(FakeSession(columns, data)).connect() as conn:
        return conn.scalar(sa.select(expression))


# Typed DECIMAL results must keep every digit.

def test_typed_decimal_select_is_exact():
    got = scalar([{'name': 'amount', 'type': 'decimal(38,18)'}],
                 [[str(BIG)]], sa.literal_column('amount', sa.DECIMAL(38, 18)))
    assert got == BIG and type(got) is Decimal


def test_decimal_result_processor_does_not_go_through_float():
    assert sa.DECIMAL(38, 18).result_processor(PrestoDialect(), None) is None


def test_float_columns_still_return_float():
    got = scalar([{'name': 'r', 'type': 'double'}], [[1.25]],
                 sa.literal_column('r', sa.Float))
    assert got == 1.25 and type(got) is float


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


def test_time_with_time_zone():
    got = scalar([{'name': 'v', 'type': 'time with time zone'}], [['01:02:03.456 +01:00']],
                 sa.literal_column('v', sa.Time(timezone=True)))
    assert got.utcoffset() == datetime.timedelta(hours=1)


def test_null_temporal_stays_null():
    assert scalar([{'name': 'v', 'type': 'date'}], [[None]],
                  sa.literal_column('v', sa.Date)) is None


@pytest.mark.parametrize('value', [
    '2026-09-24 12:34:56.123456789',  # nanoseconds that datetime cannot hold
    '2026-09-24 12:34:56.123 Not/AZone',
    'yesterday',
])
def test_unrepresentable_temporal_value_raises(value):
    with pytest.raises(ValueError):
        scalar([{'name': 'v', 'type': 'timestamp'}], [[value]],
               sa.literal_column('v', sa.DateTime))


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


# The Presto change must not leak into the shared common.ParamEscaper. (Hive's own
# escaper renders these values for Hive; see test_hive_sqlalchemy2.py.)

@pytest.mark.parametrize('escaper', [common.ParamEscaper()])
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
