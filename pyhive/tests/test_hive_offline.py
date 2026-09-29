# encoding: utf-8
"""Offline tests for the Hive DB-API cursor and SQLAlchemy dialect.

These use a fake Thrift client or connection, so they need no server.
"""
from __future__ import absolute_import
from __future__ import unicode_literals

import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

from pyhive import hive
from pyhive.sqlalchemy_hive import HiveDialect
from TCLIService import ttypes

OK = ttypes.TStatus(statusCode=ttypes.TStatusCode.SUCCESS_STATUS)


class Response(object):
    def __init__(self, **kwargs):
        self.status = OK
        self.__dict__.update(kwargs)


class FakeClient(object):
    """Answers like HiveServer2 for statements with or without a result set."""

    def __init__(self, has_result_set):
        self.has_result_set = has_result_set
        self.statements = []
        self.fetches = 0

    def ExecuteStatement(self, req):
        self.statements.append(req.statement)
        handle = ttypes.TOperationHandle(hasResultSet=self.has_result_set)
        return Response(operationHandle=handle)

    def CloseOperation(self, req):
        return Response()

    def GetResultSetMetadata(self, req):
        return Response(schema=ttypes.TTableSchema(columns=[]))

    def FetchResults(self, req):
        self.fetches += 1
        return Response(results=ttypes.TRowSet(rows=[], columns=[]))


class FakeConnection(object):
    sessionHandle = None

    def __init__(self, client):
        self.client = client


def cursor(has_result_set):
    client = FakeClient(has_result_set)
    return hive.Cursor(FakeConnection(client)), client


# executemany of statements without a result set.

def test_executemany_runs_every_insert():
    cur, client = cursor(has_result_set=False)
    cur.executemany('INSERT INTO t VALUES (%(x)s)', [{'x': 1}, {'x': 2}, {'x': 3}])
    assert client.statements == [
        'INSERT INTO t VALUES (1)', 'INSERT INTO t VALUES (2)', 'INSERT INTO t VALUES (3)']
    assert client.fetches == 0


def test_executemany_still_drains_result_sets():
    cur, client = cursor(has_result_set=True)
    cur.executemany('SELECT %(x)s', [{'x': 1}, {'x': 2}])
    assert client.statements == ['SELECT 1', 'SELECT 2']
    assert client.fetches == 1  # the first result set is consumed before the second execute


def test_fetch_without_result_set_still_raises():
    cur, _ = cursor(has_result_set=False)
    cur.execute('INSERT INTO t VALUES (1)')
    with pytest.raises(hive.ProgrammingError):
        cur.fetchone()


def test_core_executemany_insert_through_the_dialect():
    client = FakeClient(has_result_set=False)
    engine = sa.create_engine('hive://', creator=lambda: FakeDBAPIConnection(client))
    table = sa.Table('t', sa.MetaData(), sa.Column('x', sa.Integer))
    with engine.connect() as conn:
        conn.execute(table.insert(), [{'x': 1}, {'x': 2}])
    assert client.statements[-2:] == [
        'INSERT INTO TABLE `t` VALUES (1)', 'INSERT INTO TABLE `t` VALUES (2)']


class FakeDBAPIConnection(object):
    def __init__(self, client):
        self._connection = FakeConnection(client)

    def cursor(self):
        return hive.Cursor(self._connection)

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


# has_table on Hive 4 error messages.

class RaisingConnection(object):
    def __init__(self, message):
        self.message = message

    def execute(self, statement):
        raise sa.exc.OperationalError(str(statement), {}, Exception(self.message))


def not_found(name):
    return ("TExecuteStatementResp(status=TStatus(statusCode=3, errorMessage='Error while "
            "compiling statement: FAILED: SemanticException [Error 10001]: "
            "Table not found {}'), operationHandle=None)".format(name))


@pytest.mark.parametrize('message,table,schema', [
    (not_found('missing'), 'missing', None),               # Hive 2/3
    (not_found('default.missing'), 'missing', None),       # Hive 4
    (not_found('other_db.missing'), 'missing', 'other_db'),
])
def test_has_table_is_false_for_missing_tables(message, table, schema):
    assert HiveDialect().has_table(RaisingConnection(message), table, schema) is False


@pytest.mark.parametrize('message', [
    not_found('default.missing_other'),  # a different table
    'TExecuteStatementResp(status=TStatus(statusCode=3, errorMessage=\'Permission denied\'))',
])
def test_other_errors_still_raise(message):
    with pytest.raises(sa.exc.OperationalError):
        HiveDialect().has_table(RaisingConnection(message), 'missing')


# Primary key reflection.

def test_get_pk_constraint_is_a_constraint_dict():
    assert HiveDialect().get_pk_constraint(None, 't') == {
        'constrained_columns': [], 'name': None}


# Date columns.

def test_date_columns_are_created_as_date():
    table = sa.Table('t', sa.MetaData(), sa.Column('d', sa.Date), sa.Column('d2', sa.DATE),
                     sa.Column('ts', sa.DateTime), sa.Column('t', sa.Time))
    ddl = str(CreateTable(table).compile(dialect=HiveDialect()))
    assert '`d` DATE' in ddl and '`d2` DATE' in ddl
    assert '`ts` TIMESTAMP' in ddl and '`t` TIMESTAMP' in ddl


@pytest.mark.parametrize('raw,expected', [
    ('2000-02-29', datetime.date(2000, 2, 29)),
    (datetime.datetime(2000, 2, 29, 0, 0), datetime.date(2000, 2, 29)),
    (None, None),
])
def test_typed_date_results_are_dates(raw, expected):
    processor = sa.Date()._cached_result_processor(HiveDialect(), None)
    got = processor(raw) if processor else raw
    assert got == expected and type(got) is type(expected)
