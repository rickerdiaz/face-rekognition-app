# Face DTR

React/Vite frontend and Python **3.14** FastAPI attendance API for `demo-face.eg-software.com`.

## Features

- Employee enrollment, face identification without typing an ID, camera capture, server-side single-blink check, AWS face verification. All six attendance actions are visible; the kiosk shows the latest successful punch.
- Time in/out, lunch start/end, multiple short breaks, invalid transition protection and one-use challenges.
- UTC timestamps, Asia/Manila display, overnight sessions, configurable paid breaks, provisional open-session totals.
- Admin employee list, daily records and CSV export. SQLite WAL storage for a single-server demo.

## Local development

Use Python 3.14 and Node 22.12+ or Node 24:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
python scripts/download_model.py
uvicorn app.main:app --reload --host 127.0.0.1
```

Set distinct random `ADMIN_TOKEN` and `KIOSK_TOKEN` in `.env`. Missing tokens fail closed. Configure the kiosk once with its token: the API validates it before it is saved in this browser's local storage. Refreshes and browser restarts restore kiosk access automatically. Use **Disconnect device** to remove it. Clearing site data, private browsing, changing browser profiles or changing the site address requires setup again. Admin login exchanges the configured token for a server-signed session that expires exactly 24 hours after login. The browser remembers this session and validates it against the API on each visit; refreshing never extends expiry. Use **Sign out** on the admin page to remove saved administrator access. Only use this on a trusted browser; anyone using that browser profile can open administration until you sign out. Clearing site data or rotating the administrator token requires signing in again. This remembers the shared kiosk credential per browser; it does not create separately revocable device credentials. For development, in another terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173 for the kiosk and http://localhost:5173/admin for administration. In Docker use http://localhost:8000 and http://localhost:8000/admin. The kiosk has no link to administration; admin API access still requires the administrator token. API docs: http://localhost:8000/docs. Camera access requires localhost or HTTPS. Click Time in or another attendance action: the camera opens automatically, starts capturing as soon as the camera is ready, and captures for about 8 seconds. Blink once when prompted and keep exactly one face visible. The API identifies the employee and validates their attendance state before recording. The camera stops after the attempt, including cancellations and failures. Production assets from `npm run build` are served by FastAPI.

## AWS configuration later

Set region and credentials in `.env`, or use an IAM role / standard AWS credentials chain. Temporary credentials also need `AWS_SESSION_TOKEN`. Never commit secrets. IAM needs `rekognition:DetectFaces` and `rekognition:CompareFaces` with Resource `*`. No S3 bucket or face collection is required. Enrollment images are stored in the database. Normal attendance capture frames are not persisted; an AWS connectivity/service failure saves one reference JPEG for administrator review.

Without AWS credentials the UI, health endpoint and empty reports work. Enrollment and punches report unavailable; there is no fake verification bypass. Enrollment invokes DetectFaces. Without an employee ID, each punch invokes CompareFaces once per active enrolled employee after the blink check. Only one match above threshold is accepted; ambiguous matches and unknown faces are rejected. Cost therefore grows with employee count; use a Rekognition collection search before scaling to a large workforce (requires additional IAM permissions and enrollment migration). Retries incur extra usage. Calibrate `FACE_THRESHOLD` on your actual camera; 99 is an initial setting, not an accuracy guarantee.

## Ubuntu deployment

Point the domain DNS A record at the server; add AAAA only if IPv6 works. Install Docker Compose, Nginx and Certbot; expose ports 80/443 only for the app.

```bash
cp .env.example .env
# Edit tokens and configuration; AWS keys can be added later.
chmod 600 .env
docker compose build
docker compose run --rm app python scripts/download_model.py
docker compose run --rm app python -m scripts.check_blink
docker compose up -d
sudo cp deploy/nginx.conf /etc/nginx/sites-available/demo-face.eg-software.com
sudo ln -s /etc/nginx/sites-available/demo-face.eg-software.com /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d demo-face.eg-software.com
sudo certbot renew --dry-run
```

Nginx accepts 16 MB captures. API port 8000 stays bound to localhost. Use one API worker for this demo; the named Docker volume persists the database and model. Keep the server clock synchronized. Protect biometric records, disk and backups. Use SQLite's backup API for a consistent live backup:

```bash
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('data/attendance.db'); d=sqlite3.connect('data/attendance-backup.db'); s.backup(d); d.close(); s.close()"
```

Copy the backup to protected storage, rotate backups and test restores. Migrate to PostgreSQL before multiple app servers or heavier concurrency.

## Server-only outage fallback

If the kiosk can reach this server but AWS times out, has a connection error, or returns a server error, the completed single-blink capture is saved as **pending**. The reference photo lives in `DATA_DIR/pending-attendance/<random-id>.jpg`; action, original server-receipt timestamp, policy snapshot and review history live in SQLite. In Docker both are inside the existing persistent `attendance-data` volume (`/app/data`). Browser storage contains only kiosk configuration, not attendance photos or offline punches. If the server cannot be reached, nothing is saved; the user must receive a server confirmation before treating a capture as queued.

Credential/permission errors, unrecognized or ambiguous faces, invalid actions and failed blink checks do not trigger fallback. Pending captures do not create attendance events, affect totals, or replace the latest confirmed punch. No automatic AWS retry or approval occurs later.

On `/admin`, use **Pending attendance review** to inspect the protected photo, select an enrolled employee, give a review reason and approve or reject. Approval records the original capture timestamp with a manual-review similarity marker of zero and updates interval totals. Review records in capture-time order. Conflicting or incomplete sequences are rejected atomically and remain pending; restore the sequence by reviewing the prerequisite captures first. Some historical break pairs can conflict with already-confirmed later events and require further attendance correction support; do not assign another employee to bypass a conflict. Each capture can be reviewed only once. Reviewed photos/history are retained until an administrator implements the desired retention policy. Back up both SQLite and the photo folder together.

## Verification commands

```bash
python -m pytest -q
cd frontend
npm run build
```

Tests substitute AWS and landmark processing only in the test process. Camera capture, blink calibration and AWS matching require a real kiosk pilot.

## Limits of this first version

Blink once is a movement check, not certified liveness. Replayed video or manipulated uploads may pass. Challenges are one-use but do not prove a physical camera captured the frames. Use a supervised kiosk and test photos and replayed videos. A shared kiosk token is intended for a managed demo; use device credentials and rate limiting before public production use.

Lunch is unpaid and short breaks paid by default; policies are snapshotted at time-in (or capture time for a pending time-in). Reports flag open sessions and do not invent missing punches. Payroll overtime, schedules, general attendance corrections, employee deactivation, individual admin accounts and retention automation are not implemented yet. Pending reviews record decisions and reasons under the shared administrator credential. Totals are interval calculations, not finalized payroll.

References: [AWS CompareFaces](https://docs.aws.amazon.com/rekognition/latest/APIReference/API_CompareFaces.html), [MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker).
