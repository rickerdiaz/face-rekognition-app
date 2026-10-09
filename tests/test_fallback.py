import base64
import io
import time

import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from PIL import Image
from fastapi.testclient import TestClient
from app import main

client = TestClient(main.app)
admin = {'Authorization': 'Bearer ' + main.issue_admin_session()['token']}
kiosk = {'Authorization': 'Bearer test-kiosk'}


def prepare(monkeypatch, failure):
    class AWS:
        def compare_faces(self, **kwargs):
            raise failure
    monkeypatch.setattr(main, 'aws_client', lambda: AWS())
    monkeypatch.setattr(main, 'verify_blinks', lambda images: images[0])
    with main.connection() as con:
        con.execute('INSERT OR IGNORE INTO employees(id,name,department,face) VALUES(?,?,?,?)',
                    ('OFFLINE-1', 'Offline Employee', 'Demo', b'photo'))
    out = io.BytesIO()
    Image.new('RGB', (64,64)).save(out, 'JPEG')
    frame = base64.b64encode(out.getvalue()).decode()
    challenge = client.post('/api/challenges', headers=kiosk, json={'action':'time_in'}).json()
    with main.connection() as con:
        con.execute('UPDATE challenges SET issued=issued-4 WHERE id=?', (challenge['id'],))
    return {'challenge_id':challenge['id'], 'frames':[frame]*24}


def test_network_failure_queue_review_and_replay(monkeypatch):
    data = prepare(monkeypatch, EndpointConnectionError(endpoint_url='https://rekognition.test'))
    with main.connection() as con:
        count = con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    response = client.post('/api/punches', headers=kiosk, json=data)
    assert response.status_code == 200
    pending = response.json()
    assert pending['status'] == 'pending'
    path = f"/api/pending-attendance/{pending['pending_id']}"
    assert client.get(path+'/photo', headers=admin).headers['content-type'] == 'image/jpeg'
    assert client.get(path+'/photo', headers=kiosk).status_code == 401
    assert client.get('/api/pending-attendance', headers=kiosk).status_code == 401
    with main.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM events').fetchone()[0] == count
    assert client.post('/api/punches', headers=kiosk, json=data).status_code == 409
    review = {'decision':'approve', 'employee_id':'OFFLINE-1', 'note':'Photo checked by administrator'}
    assert client.post(path+'/review', headers=admin, json=review).status_code == 200
    assert client.post(path+'/review', headers=admin, json=review).status_code == 409
    with main.connection() as con:
        event = con.execute('SELECT * FROM events WHERE challenge_id=?', (data['challenge_id'],)).fetchone()
        assert event['at'] == pending['at']
        assert event['employee_id'] == 'OFFLINE-1'
        assert event['similarity'] == 0


def test_credentials_do_not_trigger_fallback(monkeypatch):
    data = prepare(monkeypatch, NoCredentialsError())
    response = client.post('/api/punches', headers=kiosk, json=data)
    assert response.status_code == 503
    with main.connection() as con:
        assert not con.execute('SELECT 1 FROM pending_attendance WHERE challenge_id=?', (data['challenge_id'],)).fetchone()


def test_rejection_and_invalid_approval_leave_attendance_unchanged():
    entry = main.save_pending({'id':'reject-test', 'action':'time_out'}, b'jpeg', '2026-10-09T00:00:00+00:00')
    path = f"/api/pending-attendance/{entry['pending_id']}/review"
    assert client.post(path, headers=admin, json={'decision':'approve','employee_id':'AUTO-2','note':'Check'}).status_code == 409
    assert client.post(path, headers=admin, json={'decision':'reject','note':'Unrecognized photo'}).status_code == 200
    with main.connection() as con:
        assert not con.execute('SELECT 1 FROM events WHERE challenge_id=?', ('reject-test',)).fetchone()


def test_replay_preserves_closed_sessions_with_later_open_session():
    with main.connection() as con:
        con.execute('INSERT INTO employees(id,name,department,face) VALUES(?,?,?,?)', ('HISTORY-1','History','','photo'))
        employee = con.execute('SELECT * FROM employees WHERE id=?', ('HISTORY-1',)).fetchone()
        for action, at, identity in [('time_in','2026-10-07T00:00:00+00:00','h1'),
                                     ('time_out','2026-10-07T09:00:00+00:00','h2'),
                                     ('time_in','2026-10-08T00:00:00+00:00','h3')]:
            main.record_attendance(con, employee, action, at, 99, identity)
        main.record_attendance(con, employee, 'time_out','2026-10-08T09:00:00+00:00',0,'h4')
        assert con.execute('SELECT COUNT(*) FROM sessions WHERE employee_id=? AND ended IS NULL', ('HISTORY-1',)).fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM events WHERE employee_id=?', ('HISTORY-1',)).fetchone()[0] == 4
