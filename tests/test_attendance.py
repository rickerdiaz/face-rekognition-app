import base64
import io
import os
import tempfile
from datetime import datetime, timezone

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='dtr-test-')
os.environ['ADMIN_TOKEN'] = 'test-admin'
os.environ['KIOSK_TOKEN'] = 'test-kiosk'

from fastapi.testclient import TestClient
from PIL import Image
from app import main
from app.blink import count_blinks

client = TestClient(main.app)
admin = {'Authorization': 'Bearer test-admin'}
kiosk = {'Authorization': 'Bearer test-kiosk'}

def test_blinks_require_complete_cycles():
    assert count_blinks([0, .8, .8, 0, .8, .8, 0]) == 2
    assert count_blinks([.8, .8, 0]) == 0
    assert count_blinks([0, .8, 0]) == 1
    assert count_blinks([0, .8]) == 0
    assert count_blinks([0, 0, 0]) == 0

def test_report_break_policy():
    session = {'started': '2026-10-08T00:00:00+00:00', 'ended': '2026-10-08T09:00:00+00:00', 'lunch_paid': 0, 'break_paid': 1}
    events = [{'action': a, 'at': f'2026-10-08T{t}+00:00'} for a,t in [('break_start','02:00:00'),('break_end','02:15:00'),('lunch_start','04:00:00'),('lunch_end','05:00:00'),('break_start','07:00:00'),('break_end','07:15:00')]]
    report = main.summarize(session, events)
    assert report['payable_minutes'] == 480
    assert report['break_minutes'] == 30
    session['break_paid'] = 0
    assert main.summarize(session, events)['payable_minutes'] == 450

def test_open_overnight_break():
    session = {'started':'2026-10-08T15:00:00+00:00','ended':None,'lunch_paid':0,'break_paid':0}
    report = main.summarize(session,[{'action':'break_start','at':'2026-10-09T00:00:00+00:00'}],datetime(2026,10,9,1,tzinfo=timezone.utc))
    assert report['incomplete']
    assert report['break_minutes'] == 60
    assert report['payable_minutes'] == 540

def test_permissions():
    assert client.get('/api/employees').status_code == 401
    assert client.get('/api/employees',headers=kiosk).status_code == 401
    assert client.get('/api/employees',headers=admin).status_code == 200

def test_punch_transitions_replay_and_failed_match(monkeypatch):
    class AWS:
        matched = True
        def detect_faces(self, **kwargs): return {'FaceDetails':[{}]}
        def compare_faces(self, **kwargs): return {'FaceMatches':[{'Similarity':99.9}] if self.matched else []}
    aws = AWS()
    monkeypatch.setattr(main,'aws_client',lambda:aws)
    monkeypatch.setattr(main,'verify_blinks',lambda images:images[0])
    out=io.BytesIO()
    Image.new('RGB',(64,64)).save(out,'JPEG')
    image=base64.b64encode(out.getvalue()).decode()
    assert client.post('/api/employees',headers=admin,json={'id':'EMP-1','name':'Test','image':image}).status_code == 201
    assert client.post('/api/challenges',headers=kiosk,json={'employee_id':'EMP-1','action':'time_out'}).status_code == 409
    def attempt(action):
        challenge=client.post('/api/challenges',headers=kiosk,json={'employee_id':'EMP-1','action':action}).json()
        with main.connection() as con: con.execute('UPDATE challenges SET issued=issued-4 WHERE id=?',(challenge['id'],))
        return {'challenge_id':challenge['id'],'frames':[image]*24}
    aws.matched=False
    assert client.post('/api/punches',headers=kiosk,json=attempt('time_in')).status_code == 403
    assert client.get('/api/status/EMP-1',headers=kiosk).json()['state'] == 'off'
    aws.matched=True
    data=attempt('time_in')
    assert client.post('/api/punches',headers=kiosk,json=data).status_code == 200
    assert client.post('/api/punches',headers=kiosk,json=data).status_code == 409
    assert client.get('/api/status/EMP-1',headers=kiosk).json()['state'] == 'working'
    assert 'time_in' not in client.get('/api/status/EMP-1',headers=kiosk).json()['actions']


def test_identification_without_id_and_ambiguous_rejection(monkeypatch):
    class AWS:
        ambiguous = False
        recognized = True
        def compare_faces(self, SourceImage, **kwargs):
            matched = self.recognized and (self.ambiguous or SourceImage['Bytes'] == b'unique-face')
            return {'FaceMatches': [{'Similarity': 99.9}] if matched else []}
    aws = AWS()
    monkeypatch.setattr(main, 'aws_client', lambda: aws)
    monkeypatch.setattr(main, 'verify_blinks', lambda images: images[0])
    with main.connection() as con:
        con.execute('INSERT INTO employees(id,name,department,face) VALUES(?,?,?,?)',
                    ('AUTO-1', 'Camera Employee', 'Operations', b'unique-face'))
        con.execute('INSERT INTO employees(id,name,department,face) VALUES(?,?,?,?)',
                    ('AUTO-2', 'Other Employee', 'Operations', b'other-face'))
    out = io.BytesIO()
    Image.new('RGB', (64,64)).save(out, 'JPEG')
    image = base64.b64encode(out.getvalue()).decode()
    def attempt():
        challenge = client.post('/api/challenges', headers=kiosk, json={'action':'time_in'}).json()
        with main.connection() as con:
            con.execute('UPDATE challenges SET issued=issued-4 WHERE id=?', (challenge['id'],))
        return client.post('/api/punches', headers=kiosk,
                           json={'challenge_id':challenge['id'], 'frames':[image]*24})
    aws.ambiguous = True
    assert attempt().status_code == 403
    assert client.get('/api/status/AUTO-1', headers=kiosk).json()['state'] == 'off'
    aws.ambiguous = False
    aws.recognized = False
    assert attempt().status_code == 403
    aws.recognized = True
    result = attempt()
    assert result.status_code == 200
    assert result.json()['employee'] == 'Camera Employee'
    latest = client.get('/api/kiosk/latest', headers=kiosk).json()
    assert latest['employee'] == 'Camera Employee'
    assert latest['action'] == 'time_in'
    assert client.get('/api/kiosk/latest').status_code == 401
    assert attempt().status_code == 409
    assert client.get('/api/status/AUTO-2', headers=kiosk).json()['state'] == 'off'
