import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def millis(value):
    return int(value.timestamp() * 1000)


@pytest.fixture
def env(monkeypatch):
    uri = os.getenv('TEST_MONGO_URI')
    connection = MongoClient(uri, serverSelectionTimeoutMS=3000) if uri else mongomock.MongoClient()
    if uri:
        connection.admin.command('ping')
    name = 'punch_out_test_' + uuid4().hex
    database = connection[name]
    monkeypatch.setattr(main, 'db', database)
    database.employees.insert_one({
        'emp_code': 'EMP0001', 'shift_start': '09:30', 'shift_end': '18:30',
    })
    start = instant('2026-07-06T04:00:00')
    doc = {
        'emp_code': 'EMP0001', 'date': '2026-07-06', 'status': 'PRESENT',
        'punch_in': start, 'punch_out': None, 'work_hours': None,
        'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False, 'history': [],
    }
    database.attendance_logs.insert_one(doc)
    with TestClient(main.app) as client:
        yield database, client, start
    connection.drop_database(name)
    connection.close()


def punch(client, time, **extra):
    return client.post('/attendance/punch-out', json={
        'emp_code': 'EMP0001', 'punched_at': millis(time), **extra,
    })


def test_normal_and_repeat(env):
    db, client, start = env
    response = punch(client, start + timedelta(hours=9))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['work_hours'] == 9
    assert result['half_day'] is False
    assert result['punch_out'] == millis(start + timedelta(hours=9))
    assert '_id' not in result and 'id' not in result
    assert punch(client, start + timedelta(hours=10)).status_code == 409


@pytest.mark.parametrize('value', [None, True, '1783310400000', 1783310400000.0,
                                  1783310400, 99999999999, 4102444800001])
def test_invalid_input(env, value):
    _, client, _ = env
    response = client.post('/attendance/punch-out', json={
        'emp_code': 'EMP0001', 'punched_at': value,
    })
    assert response.status_code == 422
    assert isinstance(response.json()['detail'], list)


@pytest.mark.parametrize('seconds,status', [(0, 422), (86400, 200), (86401, 422)])
def test_duration_limits(env, seconds, status):
    _, client, start = env
    response = punch(client, start + timedelta(seconds=seconds))
    assert response.status_code == status, response.text
    if status == 422:
        assert isinstance(response.json()['detail'], list)


@pytest.mark.parametrize('seconds,hours,half', [(18, 0.01, True),
                                              (16181, 4.49, True),
                                              (16182, 4.50, False)])
def test_rounding_and_half_day(env, seconds, hours, half):
    _, client, start = env
    result = punch(client, start + timedelta(seconds=seconds)).json()
    assert result['work_hours'] == hours
    assert result['half_day'] is half


@pytest.mark.parametrize('extra,expected', [(1799, 0), (1800, 30), (1859, 30)])
def test_overtime_boundary(env, extra, expected):
    _, client, _ = env
    result = punch(client, instant('2026-07-06T13:00:00') + timedelta(seconds=extra))
    assert result.status_code == 200, result.text
    assert result.json()['overtime_minutes'] == expected


def test_overnight(env):
    db, client, _ = env
    db.employees.update_one({}, {'$set': {'shift_start': '22:00', 'shift_end': '06:00'}})
    db.attendance_logs.update_one({}, {'$set': {'punch_in': instant('2026-07-06T16:25:00')}})
    response = punch(client, instant('2026-07-07T01:10:00'))
    assert response.status_code == 200, response.text
    assert response.json()['date'] == '2026-07-06'
    assert response.json()['work_hours'] == 8.75
    assert response.json()['overtime_minutes'] == 40


def test_missing_employee_or_record(env):
    db, client, start = env
    assert punch(client, start + timedelta(hours=1), emp_code='UNKNOWN').status_code == 404
    db.attendance_logs.delete_many({})
    assert punch(client, start + timedelta(hours=1)).status_code == 404


def test_latest_closed_record_not_older_open(env):
    db, client, start = env
    db.attendance_logs.insert_one({
        'emp_code': 'EMP0001', 'date': '2026-07-07',
        'punch_in': start + timedelta(days=1),
        'punch_out': start + timedelta(days=1, hours=1),
    })
    assert punch(client, start + timedelta(days=1, hours=2)).status_code == 409


def test_history_and_truncation(env):
    db, client, start = env
    history = [{'at': start, 'by': 'HR', 'reason': 'correction',
                'changes': {'punch_in': {'from': None, 'to': start}}}]
    db.attendance_logs.update_one({}, {'$set': {
        'history': history, 'punch_in': start + timedelta(milliseconds=900),
    }})
    response = punch(client, start + timedelta(hours=1, milliseconds=999))
    assert response.status_code == 200, response.text
    assert response.json()['work_hours'] == 1
    assert response.json()['punch_in'] == millis(start)
    assert response.json()['history'][0]['changes']['punch_in']['to'] == millis(start)
    assert db.attendance_logs.find_one({})['history'] == history_as_naive(history)


def history_as_naive(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dict):
        return {k: history_as_naive(v) for k, v in value.items()}
    return [history_as_naive(v) for v in value] if isinstance(value, list) else value


def test_legacy_defaults_and_ignore_derived_input(env):
    db, client, start = env
    db.attendance_logs.update_one({}, {'$unset': {'history': '', 'late_minutes': '', 'half_day': ''}})
    result = punch(client, start + timedelta(hours=1), work_hours=99).json()
    assert result['history'] == [] and result['late_minutes'] == 0
    assert result['work_hours'] == 1 and result['half_day'] is True


def test_omitted_timestamp(env):
    db, client, _ = env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.attendance_logs.update_one({}, {'$set': {
        'punch_in': now - timedelta(hours=1), 'date': now.astimezone(main.IST).date().isoformat(),
    }})
    response = client.post('/attendance/punch-out', json={'emp_code': 'EMP0001'})
    assert response.status_code == 200, response.text
    assert abs(response.json()['punch_out'] - millis(now)) < 5000


def test_simultaneous_punch_outs(env, monkeypatch):
    if not os.getenv('TEST_MONGO_URI'):
        pytest.skip('requires real MongoDB atomic find_one_and_update')
    db, client, start = env
    # Force both requests to read the open record before either writes.
    barrier = Barrier(2)
    collection_type = type(db.attendance_logs)
    original = collection_type.find_one

    def synchronized_find(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.full_name == db.attendance_logs.full_name and 'sort' in kwargs:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(collection_type, 'find_one', synchronized_find)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: punch(client, start + timedelta(hours=9)), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_concurrent_correction_rejected(env, monkeypatch):
    db, client, start = env
    collection_type = type(db.attendance_logs)
    original = collection_type.find_one_and_update

    def concurrent_edit(self, *args, **kwargs):
        if self.full_name == db.attendance_logs.full_name:
            db.attendance_logs.update_one({}, {'$set': {'status': 'WFH'}})
        return original(self, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'find_one_and_update', concurrent_edit)
    assert punch(client, start + timedelta(hours=1)).status_code == 409
    doc = db.attendance_logs.find_one({})
    assert doc['punch_out'] is None and doc['status'] == 'WFH'
