import base64
import csv
import io
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from app.blink import verify_blinks

load_dotenv()
DATA = Path(os.getenv('DATA_DIR', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'attendance.db'
TZ = ZoneInfo(os.getenv('BUSINESS_TIMEZONE', 'Asia/Manila'))
app = FastAPI(title='Face DTR API', version='0.1.0')
ACTIONS = {
    'off': ['time_in'],
    'working': ['lunch_start', 'break_start', 'time_out'],
    'lunch': ['lunch_end'],
    'break': ['break_end'],
}
NEXT = {'time_in': 'working', 'lunch_start': 'lunch', 'break_start': 'break',
        'lunch_end': 'working', 'break_end': 'working', 'time_out': 'off'}


@contextmanager
def connection():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        with con:
            yield con
    finally:
        con.close()


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > 16_000_000:
                await Response('Request too large', status_code=413)(scope, receive, send)
                return
            chunks.append(message)
            if not message.get('more_body', False):
                break
        async def buffered_receive():
            return chunks.pop(0) if chunks else await receive()
        await self.app(scope, buffered_receive, send)


app.add_middleware(BodyLimitMiddleware)


with connection() as con:
    con.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS employees (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL,
      face BLOB NOT NULL, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES employees(id),
      shift_date TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
      state TEXT NOT NULL, lunch_paid INTEGER NOT NULL, break_paid INTEGER NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS one_open_session ON sessions(employee_id) WHERE ended IS NULL;
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
      employee_id TEXT NOT NULL REFERENCES employees(id), action TEXT NOT NULL,
      at TEXT NOT NULL, similarity REAL NOT NULL, challenge_id TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS challenges (
      id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, action TEXT NOT NULL,
      issued REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
    ''')


def authorize(value, variable):
    expected = os.getenv(variable, '')
    if not expected or expected.startswith('replace-'):
        raise HTTPException(503, f'{variable} is not configured on the server.')
    if not secrets.compare_digest(value or '', f'Bearer {expected}'):
        raise HTTPException(401, 'Invalid access token.')


def admin(authorization: str | None = Header(default=None)):
    authorize(authorization, 'ADMIN_TOKEN')


def kiosk(authorization: str | None = Header(default=None)):
    authorize(authorization, 'KIOSK_TOKEN')


def decode_image(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 1_000_000:
            raise ValueError('Image is too large.')
        img = Image.open(io.BytesIO(raw))
        if img.format not in ('JPEG', 'PNG') or img.width * img.height > 2_000_000:
            raise ValueError('Use a JPEG or PNG up to 2 megapixels.')
        img.load()
        return img.convert('RGB')
    except (ValueError, UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, 'Invalid image: use a JPEG/PNG up to 1 MB and 2 megapixels.') from exc


def jpeg(img):
    out = io.BytesIO()
    img.save(out, format='JPEG', quality=90)
    return out.getvalue()


def aws_client():
    import boto3
    from botocore.config import Config
    return boto3.client('rekognition', region_name=os.getenv('AWS_DEFAULT_REGION', 'ap-southeast-1'),
                        config=Config(connect_timeout=5, read_timeout=15, retries={'max_attempts': 1}))


class EmployeeInput(BaseModel):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    name: str = Field(min_length=1, max_length=100)
    department: str = Field(default='', max_length=100)
    image: str = Field(max_length=1_400_000)


class ChallengeInput(BaseModel):
    employee_id: str = Field(default='', max_length=40)
    action: str = Field(max_length=20)


class PunchInput(BaseModel):
    challenge_id: str = Field(max_length=100)
    frames: list[str] = Field(min_length=24, max_length=90)


@app.get('/api/health')
def health():
    return {'status': 'ok', 'timezone': str(TZ),
            'blink_model_ready': Path(os.getenv('FACE_MODEL_PATH', 'data/face_landmarker.task')).is_file(),
            'aws_region': os.getenv('AWS_DEFAULT_REGION', 'ap-southeast-1'),
            'notice': 'Blink once is a movement check, not strong anti-spoofing.'}


@app.get('/api/employees', dependencies=[Depends(admin)])
def employees():
    with connection() as con:
        return [dict(row) for row in con.execute('SELECT id,name,department,active FROM employees ORDER BY name')]


@app.get('/api/kiosk/access', dependencies=[Depends(kiosk)])
def kiosk_access():
    return {'authorized': True}


@app.get('/api/kiosk/latest', dependencies=[Depends(kiosk)])
def latest_punch():
    with connection() as con:
        row = con.execute('SELECT e.name AS employee,e.department,v.action,v.at '
                          'FROM events v JOIN employees e ON e.id=v.employee_id ORDER BY v.id DESC LIMIT 1').fetchone()
        return dict(row) if row else None


@app.post('/api/employees', dependencies=[Depends(admin)], status_code=201)
def enroll(data: EmployeeInput):
    image = jpeg(decode_image(data.image))
    try:
        faces = aws_client().detect_faces(Image={'Bytes': image}, Attributes=['DEFAULT'])['FaceDetails']
        if len(faces) != 1:
            raise HTTPException(400, 'Enrollment photo must contain exactly one face.')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, 'AWS enrollment unavailable. Configure credentials and region, then retry.') from exc
    try:
        with connection() as con:
            con.execute('INSERT INTO employees(id,name,department,face) VALUES(?,?,?,?)',
                        (data.id, data.name.strip(), data.department.strip(), image))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Employee ID already exists.')
    return {'id': data.id, 'name': data.name}


@app.get('/api/status/{employee_id}', dependencies=[Depends(kiosk)])
def status(employee_id: str):
    with connection() as con:
        employee = con.execute('SELECT id,name FROM employees WHERE id=? AND active=1', (employee_id,)).fetchone()
        if not employee:
            raise HTTPException(404, 'Employee is not enrolled.')
        session = con.execute('SELECT state FROM sessions WHERE employee_id=? AND ended IS NULL', (employee_id,)).fetchone()
        state = session['state'] if session else 'off'
        return {**dict(employee), 'state': state, 'actions': ACTIONS[state]}


@app.post('/api/challenges', dependencies=[Depends(kiosk)])
def challenge(data: ChallengeInput):
    if data.action not in NEXT:
        raise HTTPException(400, 'Unknown attendance action.')
    if data.employee_id:
        info = status(data.employee_id)
        if data.action not in info['actions']:
            raise HTTPException(409, 'Action is not valid for the current attendance state.')
    token = secrets.token_urlsafe(24)
    now = time.time()
    with connection() as con:
        con.execute('DELETE FROM challenges WHERE issued < ?', (now - 300,))
        con.execute('INSERT INTO challenges(id,employee_id,action,issued) VALUES(?,?,?,?)',
                    (token, data.employee_id, data.action, now))
    return {'id': token, 'instruction': 'Blink once, then open your eyes', 'expires_in': 45}


@app.post('/api/punches', dependencies=[Depends(kiosk)])
def punch(data: PunchInput):
    if sum(map(len, data.frames)) > 12_000_000:
        raise HTTPException(413, 'Capture too large.')
    with connection() as con:
        c = con.execute('SELECT * FROM challenges WHERE id=?', (data.challenge_id,)).fetchone()
        if not c or c['consumed'] or not 3 <= time.time() - c['issued'] <= 45:
            raise HTTPException(409, 'Challenge expired, used, or capture was too short. Start again.')
        changed = con.execute('UPDATE challenges SET consumed=1 WHERE id=? AND consumed=0', (c['id'],)).rowcount
        if not changed:
            raise HTTPException(409, 'Challenge already used.')
        candidates = con.execute('SELECT * FROM employees WHERE active=1' + (' AND id=?' if c['employee_id'] else ''),
                                 (c['employee_id'],) if c['employee_id'] else ()).fetchall()
    if not candidates:
        raise HTTPException(404, 'No enrolled employees are available.')
    try:
        reference = verify_blinks([decode_image(frame) for frame in data.frames])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (OSError, ImportError) as exc:
        raise HTTPException(503, 'Blink detector could not start. Server dependencies need repair; no attendance was recorded.') from exc
    threshold = float(os.getenv('FACE_THRESHOLD', '99'))
    target = jpeg(reference)
    try:
        aws = aws_client()
        def compare(employee):
            result = aws.compare_faces(SourceImage={'Bytes': employee['face']},
                    TargetImage={'Bytes': target}, SimilarityThreshold=threshold, QualityFilter='AUTO')
            scores = [m['Similarity'] for m in result.get('FaceMatches', []) if m['Similarity'] >= threshold]
            return (employee, max(scores)) if scores else None
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            matches = [m for m in pool.map(compare, candidates) if m is not None]
    except Exception as exc:
        raise HTTPException(503, 'AWS verification unavailable. No attendance was recorded.') from exc
    if not matches:
        raise HTTPException(403, 'Face was not recognized. No attendance was recorded.')
    if len(matches) != 1:
        raise HTTPException(403, 'Face matched multiple employees. Ask an administrator to review enrollment photos. No attendance was recorded.')
    employee, score = matches[0]
    now = datetime.now(timezone.utc)
    at = now.isoformat()
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        session = con.execute('SELECT * FROM sessions WHERE employee_id=? AND ended IS NULL', (employee['id'],)).fetchone()
        state = session['state'] if session else 'off'
        if c['action'] not in ACTIONS[state]:
            raise HTTPException(409, f"{employee['name']} is currently {state}. Choose a valid attendance action. No attendance was recorded.")
        recent = con.execute('SELECT at FROM events WHERE employee_id=? ORDER BY id DESC LIMIT 1', (employee['id'],)).fetchone()
        if recent and (now - datetime.fromisoformat(recent['at'])).total_seconds() < 10:
            raise HTTPException(409, 'A punch was just recorded. Wait before the next action.')
        if c['action'] == 'time_in':
            cursor = con.execute('INSERT INTO sessions(employee_id,shift_date,started,state,lunch_paid,break_paid) VALUES(?,?,?,?,?,?)',
                (employee['id'], now.astimezone(TZ).date().isoformat(), at, 'working',
                 os.getenv('LUNCH_PAID', 'false').lower() == 'true', os.getenv('SHORT_BREAK_PAID', 'true').lower() == 'true'))
            session_id = cursor.lastrowid
        else:
            session_id = session['id']
            con.execute('UPDATE sessions SET state=?, ended=? WHERE id=?',
                        (NEXT[c['action']], at if c['action'] == 'time_out' else None, session_id))
        con.execute('INSERT INTO events(session_id,employee_id,action,at,similarity,challenge_id) VALUES(?,?,?,?,?,?)',
                    (session_id, employee['id'], c['action'], at, score, c['id']))
    return {'employee': employee['name'], 'department': employee['department'], 'action': c['action'], 'at': at, 'similarity': round(score, 2)}


def summarize(session, events, now=None):
    end = datetime.fromisoformat(session['ended']) if session['ended'] else (now or datetime.now(timezone.utc))
    elapsed = (end - datetime.fromisoformat(session['started'])).total_seconds()
    totals = {'lunch': 0, 'break': 0}
    opened = {}
    for event in events:
        for kind in totals:
            if event['action'] == kind + '_start':
                opened[kind] = datetime.fromisoformat(event['at'])
            elif event['action'] == kind + '_end' and kind in opened:
                totals[kind] += (datetime.fromisoformat(event['at']) - opened.pop(kind)).total_seconds()
    for kind, start in opened.items():
        totals[kind] += (end - start).total_seconds()
    unpaid = (0 if session['lunch_paid'] else totals['lunch']) + (0 if session['break_paid'] else totals['break'])
    return {**dict(session), 'elapsed_minutes': round(elapsed / 60, 2),
            'lunch_minutes': round(totals['lunch'] / 60, 2), 'break_minutes': round(totals['break'] / 60, 2),
            'payable_minutes': round(max(0, elapsed - unpaid) / 60, 2),
            'incomplete': session['ended'] is None, 'events': [dict(e) for e in events]}


@app.get('/api/records', dependencies=[Depends(admin)])
def records(date: str | None = None):
    if date:
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(400, 'Use YYYY-MM-DD.')
    with connection() as con:
        rows = con.execute('SELECT s.*,e.name FROM sessions s JOIN employees e ON e.id=s.employee_id '
                           + ('WHERE shift_date=? ' if date else '') + 'ORDER BY s.id DESC LIMIT 1000',
                           (date,) if date else ()).fetchall()
        return [summarize(s, con.execute('SELECT action,at,similarity FROM events WHERE session_id=? ORDER BY id', (s['id'],)).fetchall()) for s in rows]


@app.get('/api/records.csv', dependencies=[Depends(admin)])
def export(date: str | None = None):
    output = io.StringIO()
    fields = ['employee_id', 'name', 'shift_date', 'started', 'ended', 'lunch_minutes', 'break_minutes', 'payable_minutes', 'incomplete']
    writer = csv.DictWriter(output, fields, extrasaction='ignore')
    writer.writeheader()
    for record in records(date):
        for key in ('employee_id', 'name'):
            if record[key].startswith(('=', '+', '-', '@')):
                record[key] = "'" + record[key]
        writer.writerow(record)
    return Response(output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="attendance.csv"'})


if Path('frontend/dist').is_dir():
    @app.get('/admin', include_in_schema=False)
    @app.get('/admin/', include_in_schema=False)
    def admin_page():
        return FileResponse('frontend/dist/index.html', headers={'Cache-Control': 'no-store'})

    app.mount('/', StaticFiles(directory='frontend/dist', html=True), name='frontend')
