"""Contract integration checks. Aggregations deliberately require a real MongoDB."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from app import main


def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def employee(code='EMP0001', department='Engineering', joined='2026-07-01', **fields):
    return dict(emp_code=code, name=code, email=f'{code}@example.com',
                department=department, joined_on=joined, **fields)


@pytest.fixture
def api(monkeypatch):
    uri = os.getenv('TEST_MONGO_URI')
    if not uri:
        pytest.skip('full contract tests require TEST_MONGO_URI (MongoDB 6+)')
    connection = MongoClient(uri, serverSelectionTimeoutMS=3000)
    name = 'full_api_test_' + uuid4().hex
    db = connection[name]
    monkeypatch.setattr(main, 'db', db)
    with TestClient(main.app) as client:
        yield db, client
    connection.drop_database(name)
    connection.close()


def create(client, **kwargs):
    response = client.post('/employees', json=employee(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def seed(db, code, day, status='PRESENT', hours=8, late=0, overtime=0, half=False):
    doc = dict(emp_code=code, date=day, status=status, punch_in=None, punch_out=None,
               work_hours=hours, late_minutes=late, overtime_minutes=overtime, half_day=half)
    db.attendance_logs.insert_one(doc)


def test_employee_defaults_and_filtered_pagination(api):
    db, client = api
    for code, department in [('EMP0003', 'Engineering'), ('EMP0001', 'Engineering'), ('EMP0002', 'HR')]:
        result = create(client, code=code, department=department)
        assert isinstance(result['created_at'], int)
        assert result['shift_start'] == '09:30' and '_id' not in result
    result = client.get('/employees', params={'department':'Engineering', 'page_size':1, 'page':2}).json()
    assert result['total'] == 2
    assert [item['emp_code'] for item in result['items']] == ['EMP0003']
    assert client.get('/health').json() == {'status':'ok'}


@pytest.mark.parametrize('change', [dict(emp_code='foo'), dict(name=''), dict(email='bad'),
    dict(department=''), dict(joined_on='2026-02-30'), dict(shift_start='24:00'),
    dict(shift_start='09:30', shift_end='09:30')])
def test_employee_validation(api, change):
    _, client = api
    assert client.post('/employees', json={**employee(), **change}).status_code == 422


def test_concurrent_employee_creation(api):
    db, client = api
    barrier = Barrier(4)
    def request(_):
        barrier.wait()
        return client.post('/employees', json=employee()).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(request, range(4))) == [201,409,409,409]
    assert db.employees.count_documents({}) == 1


@pytest.mark.parametrize('time,late', [('09:40:00.999',0),('09:40:01',10),('10:15:59',45)])
def test_punchin_grace_and_truncation(api, time, late):
    db, client = api
    create(client)
    value = ms(f'2026-07-06T{time}+05:30')
    response = client.post('/attendance/punch-in', json={'emp_code':'EMP0001', 'punched_at':value, 'late_minutes':999})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['late_minutes'] == late and result['punch_in'] == value // 1000 * 1000
    assert result['work_hours'] is None and result['history'] == []
    assert db.attendance_logs.find_one({})['punch_in'].microsecond == 0


@pytest.mark.parametrize('time,day', [('05:59:59','2026-07-05'),('06:00:00','2026-07-06')])
def test_overnight_date_boundary(api, time, day):
    _, client = api
    create(client, shift_start='22:00', shift_end='06:00')
    response = client.post('/attendance/punch-in', json={'emp_code':'EMP0001', 'punched_at':ms(f'2026-07-06T{time}+05:30')})
    assert response.status_code == 201, response.text
    assert response.json()['date'] == day


def test_concurrent_punchin(api):
    db, client = api
    create(client)
    barrier = Barrier(4)
    def request(_):
        barrier.wait()
        return client.post('/attendance/punch-in', json={'emp_code':'EMP0001', 'punched_at':ms('2026-07-06T09:30:00+05:30')}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(request, range(4))) == [201,409,409,409]
    assert db.attendance_logs.count_documents({}) == 1


def test_correction_audit_and_leave(api):
    db, client = api
    create(client)
    initial = ms('2026-07-06T10:05:00+05:30')
    corrected = ms('2026-07-06T09:28:00+05:30')
    assert client.post('/attendance/punch-in', json={'emp_code':'EMP0001', 'punched_at':initial}).status_code == 201
    body = dict(punch_in=corrected, reason='Clock was incorrect', regularized_by='HR')
    response = client.patch('/attendance/EMP0001/2026-07-06', json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['late_minutes'] == 0 and len(result['history']) == 1
    changes = result['history'][0]['changes']
    assert changes == {'punch_in':{'from':initial,'to':corrected}, 'late_minutes':{'from':35,'to':0}}
    assert isinstance(db.attendance_logs.find_one({})['history'][0]['changes']['punch_in']['to'], datetime)
    assert client.patch('/attendance/EMP0001/2026-07-06', json=body).status_code == 422
    leave = dict(status='LEAVE', reason='Approved annual leave', regularized_by='HR')
    response = client.patch('/attendance/EMP0001/2026-07-06', json=leave)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['punch_in'] is None and result['work_hours'] is None
    assert len(result['history']) == 2
    assert client.patch('/attendance/EMP0001/2026-07-06', json={**leave, 'punch_in':initial}).status_code == 422
    assert client.patch('/attendance/EMP0001/2026-07-06', json={**leave,'status':'WFH'}).status_code == 422


def test_attendance_filters_and_legacy_defaults(api):
    db, client = api
    for code, day, status in [('EMP0002','2026-07-07','WFH'),('EMP0001','2026-07-07','PRESENT'),('EMP0001','2026-07-06','PRESENT')]:
        seed(db,code,day,status)
    response = client.get('/attendance', params={'date_from':'2026-07-07','date_to':'2026-07-07','page_size':1,'page':2})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['total'] == 2 and result['items'][0]['emp_code'] == 'EMP0002'
    assert result['items'][0]['history'] == []
    assert client.get('/attendance', params={'date_from':'2026-07-08','date_to':'2026-07-07'}).status_code == 422


def test_analytics_stored_values_headcount_and_ranking(api):
    db, client = api
    for n in range(1,6):
        create(client,code=f'EMP{n:04}',joined='2026-08-01' if n==5 else '2026-07-01')
    seed(db,'EMP0001','2026-07-06',hours=1.005,late=20,half=True)
    seed(db,'EMP0001','2026-07-11',hours=9,late=10,overtime=30)
    seed(db,'EMP0002','2026-07-06',hours=2,late=20)
    seed(db,'EMP0003','2026-07-06','ON_DUTY',hours=None,late=20)
    seed(db,'EMP0002','2026-07-07','LEAVE',hours=None)
    seed(db,'GHOST','2026-07-06',late=999)
    response = client.get('/analytics/employees/EMP0001/monthly',params={'month':'2026-07'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['working_days'] == 23 and result['present_days'] == 0.5
    assert result['attendance_pct'] == 2.17 and result['total_late_minutes'] == 30
    assert result['total_overtime_minutes'] == 30
    response = client.get('/analytics/departments/summary',params={'month':'2026-07'})
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['headcount'] == 4 and item['present_days'] == 2.5
    assert item['avg_work_hours'] == 4 and item['leave_count'] == 1 and item['on_duty_count'] == 1
    response = client.get('/analytics/leaderboard/late',params={'month':'2026-07','limit':2})
    assert response.status_code == 200, response.text
    items = response.json()['items']
    assert [(x['emp_code'],x['rank']) for x in items] == [('EMP0001',1),('EMP0002',2),('EMP0003',2)]


def test_trend_gap_fill_joining_and_window(api):
    db, client = api
    create(client,joined='2026-07-06')
    create(client,code='EMP0002',joined='2026-07-08')
    seed(db,'EMP0001','2026-07-06',half=True)
    seed(db,'EMP0001','2026-07-11')
    response = client.get('/analytics/departments/Engineering/trend',params={'from':'2026-07-04','to':'2026-07-12'})
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert len(rows)==9 and rows[0]['date']=='2026-07-04' and rows[-1]['date']=='2026-07-12'
    assert rows[0]['headcount']==0 and rows[0]['attendance_rate'] is None
    assert rows[2]['attendance_rate']==0.5 and rows[2]['moving_avg_7d']==0.5
    assert rows[3]['attendance_rate']==0 and rows[3]['moving_avg_7d']==0.25
    assert rows[4]['headcount']==2 and rows[4]['moving_avg_7d']==0.1667
    assert rows[7]['present_count']==1 and rows[7]['attendance_rate'] is None
    assert rows[8]['moving_avg_7d']==0.1
    assert client.get('/analytics/departments/Unknown/trend',params={'from':'2026-07-01','to':'2026-07-01'}).status_code==404


@pytest.mark.parametrize('path,params', [('/employees',{'page':0}),('/attendance',{'page_size':101}),
    ('/analytics/departments/summary',{'month':'2026-13'}),('/analytics/leaderboard/late',{'month':'2026-07','limit':51}),
    ('/analytics/departments/Engineering/trend',{'from':'2026-07-02','to':'2026-07-01'}),
    ('/analytics/departments/Engineering/trend',{'from':'2026-01-01','to':'2026-04-04'}),
    ('/admin/explain/employee_monthly',{'month':'2026-07'})])
def test_query_validation(api,path,params):
    _, client = api
    create(client)
    assert client.get(path,params=params).status_code==422


@pytest.mark.parametrize('endpoint,params', [('attendance_list',{'date_from':'2026-07-01','date_to':'2026-07-31','status':'PRESENT','page':2,'page_size':3}),
    ('employee_monthly',{'emp_code':'EMP0001','month':'2026-07'}),
    ('department_summary',{'month':'2026-07'}),('late_leaderboard',{'month':'2026-07'}),
    ('department_trend',{'department':'Engineering','from':'2026-07-01','to':'2026-07-10'})])
def test_explain_uses_indexes(api,endpoint,params):
    db, client = api
    create(client)
    # A modest dataset avoids optimizing an empty collection into EOF.
    db.attendance_logs.insert_many([dict(emp_code=f'EMP{n:04}',date='2026-07-06',status='PRESENT',late_minutes=20) for n in range(1,301)])
    response = client.get('/admin/explain/'+endpoint,params=params)
    assert response.status_code==200, response.text
    data=response.json()
    assert data['endpoint']==endpoint and data['collection'] in ['attendance_logs','employees']
    text=json.dumps(data['explain'])
    assert 'IXSCAN' in text and 'COLLSCAN' not in text, text


def test_trend_department_with_no_logs(api):
    _, client = api
    create(client, joined='2026-07-06')
    response = client.get('/analytics/departments/Engineering/trend',params={'from':'2026-07-04','to':'2026-07-08'})
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert len(rows) == 5
    assert [r['headcount'] for r in rows] == [0,0,1,1,1]
    assert [r['attendance_rate'] for r in rows] == [None,None,0,0,0]
    assert [r['moving_avg_7d'] for r in rows] == [None,None,0,0,0]


def test_monthly_joining_and_no_logs(api):
    _, client = api
    create(client, joined='2026-07-16')
    result = client.get('/analytics/employees/EMP0001/monthly',params={'month':'2026-07'})
    assert result.status_code == 200, result.text
    assert result.json()['working_days'] == 12 and result.json()['attendance_pct'] == 0
    future = client.get('/analytics/employees/EMP0001/monthly',params={'month':'2026-06'}).json()
    assert future['working_days'] == 0 and future['attendance_pct'] is None


def test_concurrent_corrections_preserve_history(api, monkeypatch):
    db, client = api
    create(client)
    client.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00+05:30')})
    barrier = Barrier(2)
    collection_type = type(db.attendance_logs)
    original = collection_type.find_one
    def synchronized(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.full_name == db.attendance_logs.full_name:
            barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(collection_type,'find_one',synchronized)
    def request(status):
        return client.patch('/attendance/EMP0001/2026-07-06',json={'status':status,'reason':'Manual status correction','regularized_by':'HR'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(request,['WFH','ON_DUTY']))
    assert sorted(r.status_code for r in responses) in ([200,200],[200,409])
    stored=original(db.attendance_logs,{})
    assert len(stored['history']) == sum(r.status_code==200 for r in responses)


def test_correction_date_and_duration_constraints(api):
    _, client = api
    create(client)
    start=ms('2026-07-06T09:30:00+05:30')
    client.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':start})
    base={'reason':'Manual time correction','regularized_by':'HR'}
    for change in [{'punch_in':ms('2026-07-07T09:30:00+05:30')},
                   {'punch_out':start},{'punch_out':start+86401000},
                   {'punch_in':None},{'punch_out':True}]:
        response=client.patch('/attendance/EMP0001/2026-07-06',json={**base,**change})
        assert response.status_code==422, response.text


@pytest.mark.parametrize('value',[None, True, '1783310400000', 1783310400000.0, 1783310400, 4102444800001])
def test_punchin_rejects_invalid_instants(api,value):
    _,client=api
    create(client)
    response=client.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':value})
    assert response.status_code==422, response.text


def test_unknown_employee_and_invalid_presence_status(api):
    _,client=api
    assert client.post('/attendance/punch-in',json={'emp_code':'UNKNOWN'}).status_code==404
    assert client.patch('/attendance/UNKNOWN/2026-07-06',json={'reason':'Correction reason','regularized_by':'HR','status':'WFH'}).status_code==404
    assert client.get('/analytics/employees/UNKNOWN/monthly',params={'month':'2026-07'}).status_code==404
    create(client)
    for status in ['LEAVE','ABSENT','present']:
        assert client.post('/attendance/punch-in',json={'emp_code':'EMP0001','status':status}).status_code==422


def test_analytics_decimal_half_up_and_department_scope(api):
    db,client=api
    create(client,department='A')
    create(client,code='EMP0002',department='B')
    seed(db,'EMP0001','2026-07-06',hours=1.005,late=10)
    seed(db,'EMP0002','2026-07-06',hours=9,late=20)
    response=client.get('/analytics/departments/summary',params={'month':'2026-07','department':'A'})
    assert response.status_code==200,response.text
    assert len(response.json()['items'])==1
    assert response.json()['items'][0]['avg_work_hours']==1.01
    rows=client.get('/analytics/leaderboard/late',params={'month':'2026-07','department':'A'}).json()['items']
    assert len(rows)==1 and rows[0]['rank']==1 and rows[0]['emp_code']=='EMP0001'


def test_health_reports_database_outage(api, monkeypatch):
    from pymongo.errors import ConnectionFailure
    db, client = api
    def unavailable(*args, **kwargs):
        raise ConnectionFailure('simulated unavailable database')
    with monkeypatch.context() as outage:
        outage.setattr(type(db), 'command', unavailable)
        response = client.get('/health')
    assert response.status_code == 503
    assert response.json() == {'detail': 'database unavailable'}


def test_trend_inclusive_range_limits(api):
    _, client = api
    create(client)
    path = '/analytics/departments/Engineering/trend'
    response = client.get(path, params={'from':'2026-01-01','to':'2026-04-02'})
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 92
    assert client.get(path, params={'from':'2026-01-01','to':'2026-04-03'}).status_code == 422


def test_trend_maximum_calendar_date(api):
    _, client = api
    create(client)
    response = client.get('/analytics/departments/Engineering/trend',
                          params={'from':'9999-12-31','to':'9999-12-31'})
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 1
    assert response.json()['items'][0]['date'] == '9999-12-31'


@pytest.mark.parametrize('field,value', [
    ('shift_start', '0\u0661:30'), ('shift_start', '09:3\u0660'),
    ('shift_end', '1\u0668:30'), ('shift_end', '18:3\u0660'),
])
def test_shift_clocks_reject_unicode_digits(api, field, value):
    db, client = api
    response = client.post('/employees', json={**employee(), field: value})
    assert response.status_code == 422, response.text
    assert db.employees.count_documents({}) == 0


def test_final_calendar_month_includes_last_day(api):
    db, client = api
    create(client, joined='9999-12-31')
    create(client, code='EMP0002', joined='9999-11-30')
    seed(db, 'EMP0001', '9999-12-31', hours=8, late=10)
    # A real calendar date after the year-9999 boundary is never constructed.
    monthly = client.get('/analytics/employees/EMP0001/monthly', params={'month':'9999-12'})
    assert monthly.status_code == 200, monthly.text
    assert monthly.json()['working_days'] == 1
    assert monthly.json()['present_days'] == 1
    assert monthly.json()['attendance_pct'] == 100
    summary = client.get('/analytics/departments/summary', params={'month':'9999-12'})
    assert summary.status_code == 200, summary.text
    assert summary.json()['items'][0]['headcount'] == 2
    assert summary.json()['items'][0]['present_days'] == 1
    ranked = client.get('/analytics/leaderboard/late', params={'month':'9999-12'})
    assert ranked.status_code == 200, ranked.text
    assert ranked.json()['items'][0]['total_late_minutes'] == 10
    for endpoint, extra in [
        ('employee_monthly', {'emp_code':'EMP0001'}),
        ('department_summary', {}), ('late_leaderboard', {}),
    ]:
        explained = client.get('/admin/explain/' + endpoint,
                               params={'month':'9999-12', **extra})
        assert explained.status_code == 200, explained.text
