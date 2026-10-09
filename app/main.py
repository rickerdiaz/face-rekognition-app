import base64
import csv
import io
import os
import secrets
import sqlite3
import time
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from botocore.exceptions import EndpointConnectionError, ConnectionClosedError, ConnectTimeoutError, ReadTimeoutError, ClientError

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from app.blink import verify_blinks
logger = logging.getLogger('uvicorn.error')

load_dotenv()
DATA = Path(os.getenv('DATA_DIR', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
PENDING_DIR = DATA / 'pending-attendance'
PENDING_DIR.mkdir(parents=True, exist_ok=True)
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
    CREATE TABLE IF NOT EXISTS pending_attendance (
      id TEXT PRIMARY KEY, challenge_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL,
      captured_at TEXT NOT NULL, photo_name TEXT NOT NULL, reason TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', employee_id TEXT REFERENCES employees(id),
      reviewed_at TEXT, review_note TEXT, lunch_paid INTEGER NOT NULL, break_paid INTEGER NOT NULL);
    ''')


def authorize(value, variable):
    expected = os.getenv(variable, '')
    if not expected or expected.startswith('replace-'):
        raise HTTPException(503, f'{variable} is not configured on the server.')
    if not secrets.compare_digest(value or '', f'Bearer {expected}'):
        raise HTTPException(401, 'Invalid access token.')


def admin(authorization: str | None = Header(default=None)):
    secret = os.getenv('ADMIN_TOKEN', '')
    if not secret or secret.startswith('replace-'):
        raise HTTPException(503, 'ADMIN_TOKEN is not configured on the server.')
    try:
        token = (authorization or '').removeprefix('Bearer ')
        payload, signature = token.split('.')
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('Invalid signature')
        session = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if session['role'] != 'admin' or time.time() >= session['expires_at']:
            raise ValueError('Expired session')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, 'Administrator session expired or invalid. Sign in again.')


def issue_admin_session():
    expires_at = int(time.time()) + 24 * 60 * 60
    data = {'role': 'admin', 'expires_at': expires_at, 'nonce': secrets.token_hex(16)}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    signature = hmac.new(os.environ['ADMIN_TOKEN'].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {'token': payload + '.' + signature, 'expires_at': expires_at}


@app.post('/api/admin/session')
def admin_login(authorization: str | None = Header(default=None)):
    authorize(authorization, 'ADMIN_TOKEN')
    return issue_admin_session()


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


class ReviewInput(BaseModel):
    decision: str = Field(pattern='^(approve|reject)$')
    employee_id: str | None = Field(default=None, max_length=40)
    note: str = Field(min_length=1, max_length=500)


def unavailable_aws(exc):
    if isinstance(exc, (EndpointConnectionError, ConnectionClosedError, ConnectTimeoutError, ReadTimeoutError)):
        return True
    return isinstance(exc, ClientError) and exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0) >= 500


def save_pending(challenge, photo, captured_at):
    record_id = secrets.token_hex(16)
    name = record_id + '.jpg'
    path = PENDING_DIR / name
    try:
        with path.open('xb') as file:
            file.write(photo)
        with connection() as con:
            con.execute('INSERT INTO pending_attendance(id,challenge_id,action,captured_at,photo_name,reason,lunch_paid,break_paid) VALUES(?,?,?,?,?,?,?,?)',
                (record_id, challenge['id'], challenge['action'], captured_at, name, 'AWS connection or service unavailable',
                 os.getenv('LUNCH_PAID', 'false').lower() == 'true', os.getenv('SHORT_BREAK_PAID', 'true').lower() == 'true'))
    except (OSError, sqlite3.Error) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(503, 'Could not save the capture on the server. No attendance was recorded; ask an administrator for help.') from exc
    return {'status': 'pending', 'pending_id': record_id, 'action': challenge['action'], 'at': captured_at,
            'message': 'Capture saved on the server. Pending administrator verification; attendance is not yet confirmed.'}


def record_attendance(con, employee, action, at, score, challenge_id, lunch_paid=None, break_paid=None):
    """Replay this employee's ordered events to validate historical approvals atomically."""
    existing = [dict(row) for row in con.execute(
        'SELECT v.*,s.lunch_paid,s.break_paid FROM events v JOIN sessions s ON s.id=v.session_id '
        'WHERE v.employee_id=? ORDER BY v.at,v.id', (employee['id'],))]
    proposed = {'action': action, 'at': at, 'similarity': score, 'challenge_id': challenge_id,
                'lunch_paid': lunch_paid if lunch_paid is not None else os.getenv('LUNCH_PAID', 'false').lower() == 'true',
                'break_paid': break_paid if break_paid is not None else os.getenv('SHORT_BREAK_PAID', 'true').lower() == 'true'}
    timeline = sorted(existing + [proposed], key=lambda event: (event['at'], event.get('id', 2**63)))
    state = 'off'
    previous = None
    for event in timeline:
        if event['action'] not in ACTIONS[state]:
            raise HTTPException(409, f"{employee['name']}: {event['action']} at {event['at']} conflicts with attendance state {state}. Review pending captures in time order; no changes were saved.")
        timestamp = datetime.fromisoformat(event['at'])
        if previous and (timestamp - previous).total_seconds() < 10:
            raise HTTPException(409, 'Attendance events must be at least 10 seconds apart. No changes were saved.')
        previous = timestamp
        state = NEXT[event['action']]
    session_id = None
    # Temporarily close existing open sessions within this transaction so replay
    # can reopen one session at a time without violating the unique index.
    con.execute('UPDATE sessions SET ended=COALESCE(ended,started) WHERE employee_id=?', (employee['id'],))
    for event in timeline:
        if event['action'] == 'time_in':
            if 'id' in event:
                session_id = event['session_id']
                con.execute("UPDATE sessions SET state='working',ended=NULL WHERE id=?", (session_id,))
            else:
                cursor = con.execute('INSERT INTO sessions(employee_id,shift_date,started,state,lunch_paid,break_paid) VALUES(?,?,?,?,?,?)',
                    (employee['id'], datetime.fromisoformat(at).astimezone(TZ).date().isoformat(), at,
                     'working', event['lunch_paid'], event['break_paid']))
                session_id = cursor.lastrowid
        else:
            con.execute('UPDATE sessions SET state=?,ended=? WHERE id=?',
                        (NEXT[event['action']], event['at'] if event['action'] == 'time_out' else None, session_id))
        if 'id' in event:
            con.execute('UPDATE events SET session_id=? WHERE id=?', (session_id, event['id']))
        else:
            con.execute('INSERT INTO events(session_id,employee_id,action,at,similarity,challenge_id) VALUES(?,?,?,?,?,?)',
                        (session_id, employee['id'], action, at, score, challenge_id))


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
                          'FROM events v JOIN employees e ON e.id=v.employee_id ORDER BY v.at DESC,v.id DESC LIMIT 1').fetchone()
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
    captured_at = datetime.now(timezone.utc).isoformat()
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
        blink_started = time.perf_counter()
        reference = verify_blinks([decode_image(frame) for frame in data.frames])
        logger.info('capture_blink action=%s frames=%d duration_ms=%.0f', c['action'], len(data.frames), (time.perf_counter() - blink_started) * 1000)
    except ValueError as exc:
        logger.warning('capture_rejected action=%s stage=blink reason=%s', c['action'], str(exc))
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (OSError, ImportError) as exc:
        raise HTTPException(503, 'Blink detector could not start. Server dependencies need repair; no attendance was recorded.') from exc
    threshold = float(os.getenv('FACE_THRESHOLD', '99'))
    target = jpeg(reference)
    try:
        aws_started = time.perf_counter()
        aws = aws_client()
        def compare(employee):
            result = aws.compare_faces(SourceImage={'Bytes': employee['face']},
                    TargetImage={'Bytes': target}, SimilarityThreshold=threshold, QualityFilter='AUTO')
            scores = [m['Similarity'] for m in result.get('FaceMatches', []) if m['Similarity'] >= threshold]
            return (employee, max(scores)) if scores else None
        # Probe one comparison first so a total outage does not launch a slow
        # failed request for every enrolled employee before queuing the capture.
        first = compare(candidates[0])
        matches = [first] if first else []
        if len(candidates) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(candidates) - 1)) as pool:
                matches.extend(m for m in pool.map(compare, candidates[1:]) if m is not None)
        logger.info('capture_matching action=%s candidates=%d matches=%d duration_ms=%.0f',
                    c['action'], len(candidates), len(matches), (time.perf_counter() - aws_started) * 1000)
    except Exception as exc:
        if unavailable_aws(exc):
            return save_pending(c, target, captured_at)
        raise HTTPException(503, 'AWS verification unavailable. Check credentials and permissions. No capture was queued or attendance recorded.') from exc
    if not matches:
        raise HTTPException(403, 'Face was not recognized. No attendance was recorded.')
    if len(matches) != 1:
        raise HTTPException(403, 'Face matched multiple employees. Ask an administrator to review enrollment photos. No attendance was recorded.')
    employee, score = matches[0]
    at = captured_at
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        record_attendance(con, employee, c['action'], at, score, c['id'])
    return {'status': 'confirmed', 'employee': employee['name'], 'department': employee['department'], 'action': c['action'], 'at': at, 'similarity': round(score, 2)}


@app.get('/api/pending-attendance', dependencies=[Depends(admin)])
def pending_records():
    with connection() as con:
        return [dict(row) for row in con.execute(
            'SELECT p.id,p.action,p.captured_at,p.reason,p.status,p.employee_id,p.reviewed_at,p.review_note,e.name AS employee '
            'FROM pending_attendance p LEFT JOIN employees e ON e.id=p.employee_id '
            "ORDER BY CASE WHEN p.status='pending' THEN 0 ELSE 1 END,p.captured_at LIMIT 500")]


@app.get('/api/pending-attendance/{record_id}/photo', dependencies=[Depends(admin)])
def pending_photo(record_id: str):
    with connection() as con:
        row = con.execute('SELECT photo_name FROM pending_attendance WHERE id=?', (record_id,)).fetchone()
    if not row or not (PENDING_DIR / row['photo_name']).is_file():
        raise HTTPException(404, 'Capture photo unavailable.')
    return FileResponse(PENDING_DIR / row['photo_name'], media_type='image/jpeg',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.post('/api/pending-attendance/{record_id}/review', dependencies=[Depends(admin)])
def review_pending(record_id: str, data: ReviewInput):
    if not data.note.strip():
        raise HTTPException(400, 'A review reason is required.')
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM pending_attendance WHERE id=?', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Pending record not found.')
        if row['status'] != 'pending':
            raise HTTPException(409, 'This capture has already been reviewed.')
        employee = None
        if data.decision == 'approve':
            if not (PENDING_DIR / row['photo_name']).is_file():
                raise HTTPException(409, 'Capture photo is missing. Restore it before approving attendance.')
            employee = con.execute('SELECT * FROM employees WHERE id=? AND active=1', (data.employee_id,)).fetchone()
            if not employee:
                raise HTTPException(400, 'Select an active enrolled employee before approving.')
            record_attendance(con, employee, row['action'], row['captured_at'], 0, row['challenge_id'], row['lunch_paid'], row['break_paid'])
        con.execute('UPDATE pending_attendance SET status=?,employee_id=?,reviewed_at=?,review_note=? WHERE id=?',
                    ('approved' if employee else 'rejected', employee['id'] if employee else None,
                     datetime.now(timezone.utc).isoformat(), data.note.strip(), record_id))
    return {'status': 'approved' if employee else 'rejected', 'id': record_id}


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
        return [summarize(s, con.execute('SELECT action,at,similarity FROM events WHERE session_id=? ORDER BY at,id', (s['id'],)).fetchall()) for s in rows]


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
