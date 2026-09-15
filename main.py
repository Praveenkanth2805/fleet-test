"""
MBT Fleet Test — Single-file backend
Covers: login, check-in, check-out, location upload, fleet map.
Run locally:    python main.py
Deploy Render:  gunicorn main:app
"""

from flask import Flask, request, jsonify
from flask_cors import CORS 
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
import sqlite3
import uuid
import os
import pytz

app = Flask(__name__)
CORS(app)
IST = pytz.timezone('Asia/Kolkata')
DB_PATH              = os.environ.get('DB_PATH', 'fleet_test.db')
ONLINE_TIMEOUT_MIN   = 5     # last GPS ping within N min → online
DEFAULT_CHECKIN      = '09:00'
DEFAULT_CHECKOUT     = '18:00'

# ─────────────────────────────────────────────────────────────────
#  DB LAYER
# ─────────────────────────────────────────────────────────────────

@contextmanager
def db_session():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db_session() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_code   TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                display_name    TEXT NOT NULL,
                token           TEXT,
                is_admin        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS locations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                latitude    REAL NOT NULL,
                longitude   REAL NOT NULL,
                ts          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_loc_user_ts
                ON locations(user_id, ts DESC);

            CREATE TABLE IF NOT EXISTS attendance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                date        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'checked_in',
                checkin_at  TEXT,
                checkout_at TEXT,
                UNIQUE(user_id, date)
            );
        ''')

        # ── Seed test users ──────────────────────────────────────
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seeds = [
                ('ADMIN001', 'admin123', 'Admin User',    1),
                ('TEST001',  'test123',  'Test Employee', 0),
                ('TEST002',  'test123',  'Test Employee 2', 0),
            ]
            for code, pw, name, is_admin in seeds:
                db.execute(
                    "INSERT INTO users "
                    "(employee_code, password_hash, display_name, token, is_admin) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, generate_password_hash(pw), name, uuid.uuid4().hex, is_admin)
                )
            print('[INIT] Seeded: ADMIN001/admin123, TEST001/test123, TEST002/test123')


def now_iso():
    """Return current time in IST formatted as: YYYY-MM-DD HH:MM:SS AM/PM"""
    return datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p')


def now_iso_utc():
    """Location timestamps use ISO — only attendance uses now_iso()"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_ts(raw):
    if not raw:
        return now_iso_utc()
    try:
        s = str(raw).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return now_iso_utc()


def today_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


# ─────────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────────

def current_user():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Token '):
        return None
    token = auth[6:].strip()
    if not token:
        return None
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM users WHERE token=?", (token,)
        ).fetchone()
    return row


def require_auth():
    u = current_user()
    if not u:
        return None, (jsonify({'error': 'Authentication required'}), 401)
    return u, None


# ─────────────────────────────────────────────────────────────────
#  AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route('/api/login/', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    code = str(data.get('employee_code') or data.get('username') or '').strip()
    pw   = str(data.get('password') or '')

    if not code or not pw:
        return jsonify({'error': 'Employee ID and password required'}), 400

    with db_session() as db:
        user = db.execute(
            "SELECT * FROM users WHERE employee_code=?", (code,)
        ).fetchone()

        if not user or not check_password_hash(user['password_hash'], pw):
            return jsonify({'error': 'Invalid credentials'}), 401

        token = uuid.uuid4().hex
        db.execute("UPDATE users SET token=? WHERE id=?", (token, user['id']))

    return jsonify({
        'token':         token,
        'username':      user['display_name'],
        'employee_code': user['employee_code'],
        'is_admin':      bool(user['is_admin']),
        'company_id':    1,
        'company_name':  'Test Company',
        'company_code':  'TEST',
    })


@app.route('/api/logout/', methods=['POST'])
def logout():
    u, err = require_auth()
    if err: return err
    with db_session() as db:
        db.execute("UPDATE users SET token=NULL WHERE id=?", (u['id'],))
    return jsonify({'status': 'logged out'})


# ─────────────────────────────────────────────────────────────────
#  PROFILE
# ─────────────────────────────────────────────────────────────────

@app.route('/api/profile/', methods=['GET', 'PATCH'])
def profile():
    u, err = require_auth()
    if err: return err

    if request.method == 'PATCH':
        # Employee may update display name, email, phone — accept but ignore
        return jsonify({'status': 'updated'})

    return jsonify({
        'username':                 u['display_name'],
        'email':                    '',
        'company_id':               1,
        'company_name':             'Test Company',
        'company_code':             'TEST',
        'employee_code':            u['employee_code'],
        'team_id':                  None,
        'team_name':                '',
        'schedule_source':          'global',
        'use_individual_schedule':  False,
        'role':                     'Employee',
        'phone':                    '',
        'photo_url':                None,
        'expected_checkin':         DEFAULT_CHECKIN,
        'expected_checkout':        DEFAULT_CHECKOUT,
    })


# ─────────────────────────────────────────────────────────────────
#  ATTENDANCE
# ─────────────────────────────────────────────────────────────────

@app.route('/api/attendance/status/', methods=['GET'])
def attendance_status():
    u, err = require_auth()
    if err: return err

    today = today_str()
    with db_session() as db:
        rec = db.execute(
            "SELECT * FROM attendance WHERE user_id=? AND date=?",
            (u['id'], today)
        ).fetchone()
        if not rec:
            rec = db.execute(
                "SELECT * FROM attendance WHERE user_id=? AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (u['id'],)
            ).fetchone()

    if not rec:
        return jsonify({'status': 'not_checked_in'})

    return jsonify({
        'status':           rec['status'],
        'checkin_at':       rec['checkin_at'],
        'checkout_at':      rec['checkout_at'],
        'flag':             'normal',
        'flag_label':       'On Time',
        'hours_worked':     None,
        'checkout_locked':  False,
    })


@app.route('/api/checkin/', methods=['POST'])
def checkin():
    u, err = require_auth()
    if err: return err

    today = today_str()
    with db_session() as db:
        existing = db.execute(
            "SELECT * FROM attendance WHERE user_id=? AND date=?",
            (u['id'], today)
        ).fetchone()
        if existing:
            return jsonify({
                'error': 'Already checked in today.',
                'checkin_at': existing['checkin_at'],
            }), 400

        now = now_iso()
        db.execute(
            "INSERT INTO attendance (user_id, date, status, checkin_at) "
            "VALUES (?, ?, 'checked_in', ?)",
            (u['id'], today, now)
        )

    return jsonify({
        'status':            'checked_in',
        'checkin_at':        now,
        'flag':              'normal',
        'flag_label':        'On Time',
        'late_notification': None,
        'message':           f"Welcome, {u['display_name']}!",
    })


@app.route('/api/checkout/', methods=['POST'])
def checkout():
    u, err = require_auth()
    if err: return err

    today = today_str()
    with db_session() as db:
        rec = db.execute(
            "SELECT * FROM attendance WHERE user_id=? AND date=?",
            (u['id'], today)
        ).fetchone()
        if not rec:
            rec = db.execute(
                "SELECT * FROM attendance WHERE user_id=? AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (u['id'],)
            ).fetchone()
        if not rec:
            return jsonify({'error': 'You have not checked in today.'}), 400
        if rec['status'] == 'checked_out':
            return jsonify({
                'error': 'Already checked out today.',
                'checkin_at':  rec['checkin_at'],
                'checkout_at': rec['checkout_at'],
            }), 400

        now = now_iso()
        db.execute(
            "UPDATE attendance SET status='checked_out', checkout_at=? WHERE id=?",
            (now, rec['id'])
        )
        checkin_at = rec['checkin_at']

    return jsonify({
        'status':        'checked_out',
        'checkin_at':    checkin_at,
        'checkout_at':   now,
        'hours_worked':  None,
        'flag':          'normal',
        'flag_label':    'On Time',
        'message':       'Checked out successfully.',
    })


@app.route('/api/auto-checkout/', methods=['POST'])
def auto_checkout():
    u, err = require_auth()
    if err: return err

    today = today_str()
    with db_session() as db:
        rec = db.execute(
            "SELECT * FROM attendance WHERE user_id=? AND date=?",
            (u['id'], today)
        ).fetchone()
        if not rec:
            rec = db.execute(
                "SELECT * FROM attendance WHERE user_id=? AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (u['id'],)
            ).fetchone()

        if not rec or rec['status'] == 'checked_out':
            return jsonify({
                'status': 'already_checked_out',
                'message': 'You are not currently checked in.',
            })

        now = now_iso()
        db.execute(
            "UPDATE attendance SET status='checked_out', checkout_at=? WHERE id=?",
            (now, rec['id'])
        )
        checkin_at = rec['checkin_at']

    return jsonify({
        'status':        'checked_out',
        'reason':        'location_disabled',
        'checkin_at':    checkin_at,
        'checkout_at':   now,
        'message':       'Auto checked out because location was turned off.',
    })


# ─────────────────────────────────────────────────────────────────
#  LOCATION UPLOAD
# ─────────────────────────────────────────────────────────────────

@app.route('/api/location/', methods=['POST'])
def save_location():
    u, err = require_auth()
    if err: return err

    data = request.get_json(silent=True) or {}

    # Is this user currently checked in?
    with db_session() as db:
        rec = db.execute(
            "SELECT * FROM attendance WHERE user_id=? AND status='checked_in' "
            "ORDER BY date DESC LIMIT 1",
            (u['id'],)
        ).fetchone()

    if not rec:
        return jsonify({
            'status': 'ignored',
            'user': u['employee_code'],
            'stop_tracking': True,
        }), 202

    # Batch upload
    points = data.get('points')
    if isinstance(points, list):
        saved = 0
        with db_session() as db:
            for p in points:
                try:
                    lat = float(p.get('latitude'))
                    lng = float(p.get('longitude'))
                    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                        continue
                    ts = normalize_ts(p.get('timestamp'))
                    db.execute(
                        "INSERT INTO locations (user_id, latitude, longitude, ts) "
                        "VALUES (?, ?, ?, ?)",
                        (u['id'], lat, lng, ts)
                    )
                    saved += 1
                except (TypeError, ValueError):
                    continue
        return jsonify({
            'status':        'saved_bulk',
            'count':         saved,
            'user':          u['employee_code'],
            'stop_tracking': False,
        })

    # Single point
    try:
        lat = float(data.get('latitude'))
        lng = float(data.get('longitude'))
    except (TypeError, ValueError):
        return jsonify({'error': 'latitude and longitude required'}), 400

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({'error': 'coordinates out of range'}), 400

    ts = normalize_ts(data.get('timestamp'))
    with db_session() as db:
        db.execute(
            "INSERT INTO locations (user_id, latitude, longitude, ts) "
            "VALUES (?, ?, ?, ?)",
            (u['id'], lat, lng, ts)
        )

    return jsonify({
        'status':        'saved',
        'user':          u['employee_code'],
        'stop_tracking': False,
    })


# ─────────────────────────────────────────────────────────────────
#  ADMIN — FLEET
# ─────────────────────────────────────────────────────────────────

@app.route('/api/latest-locations/', methods=['GET'])
def latest_locations():
    u, err = require_auth()
    if err: return err

    cutoff = (
        datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(minutes=ONLINE_TIMEOUT_MIN)
    ).isoformat()

    with db_session() as db:
        users = db.execute(
            "SELECT * FROM users WHERE is_admin=0"
        ).fetchall()

        result = []
        for emp in users:
            loc = db.execute(
                "SELECT * FROM locations WHERE user_id=? "
                "ORDER BY ts DESC LIMIT 1",
                (emp['id'],)
            ).fetchone()

            att = db.execute(
                "SELECT * FROM attendance WHERE user_id=? AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (emp['id'],)
            ).fetchone()

            online = bool(
                loc and loc['ts'] > cutoff and att is not None
            )

            result.append({
                'id':              emp['id'],
                'user':            emp['display_name'],
                'lat':             loc['latitude']  if loc else None,
                'lng':             loc['longitude'] if loc else None,
                'time':            loc['ts']        if loc else None,
                'online':          online,
                'last_seen':       loc['ts']        if loc else None,
                'attendance':      att['status']    if att else 'absent',
                'photo_url':       None,
                'distance_m':      None,
                'is_at_office':    None,
                'office_radius_m': 20.0,
            })

    return jsonify(result)


@app.route('/api/fleet-trails/', methods=['GET'])
def fleet_trails():
    u, err = require_auth()
    if err: return err

    try:
        minutes = min(int(request.args.get('minutes', 5)), 60)
    except (TypeError, ValueError):
        minutes = 5

    cutoff = (
        datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(minutes=minutes)
    ).isoformat()

    with db_session() as db:
        users = db.execute(
            "SELECT * FROM users WHERE is_admin=0"
        ).fetchall()

        trails = []
        for emp in users:
            att = db.execute(
                "SELECT * FROM attendance WHERE user_id=? AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (emp['id'],)
            ).fetchone()
            if not att:
                continue

            rows = db.execute(
                "SELECT * FROM locations WHERE user_id=? AND ts>=? "
                "ORDER BY ts ASC",
                (emp['id'], cutoff)
            ).fetchall()

            if not rows:
                latest = db.execute(
                    "SELECT * FROM locations WHERE user_id=? "
                    "ORDER BY ts DESC LIMIT 1",
                    (emp['id'],)
                ).fetchone()
                if latest:
                    rows = [latest]
                else:
                    continue

            trails.append({
                'user': emp['display_name'],
                'points': [
                    {
                        'lat':  r['latitude'],
                        'lng':  r['longitude'],
                        'time': r['ts'],
                    }
                    for r in rows
                ],
            })

    return jsonify({'trails': trails})


@app.route('/api/fleet-users/', methods=['GET'])
def fleet_users():
    u, err = require_auth()
    if err: return err

    with db_session() as db:
        users = db.execute(
            "SELECT * FROM users WHERE is_admin=0"
        ).fetchall()

    return jsonify({'users': [
        {
            'id':                       emp['id'],
            'username':                 emp['display_name'],
            'display_name':             emp['display_name'],
            'employee_code':            emp['employee_code'],
            'email':                    '',
            'role':                     'Employee',
            'phone':                    '',
            'team_id':                  None,
            'team_name':                '',
            'schedule_source':          'global',
            'use_individual_schedule':  False,
            'photo_url':                None,
            'expected_checkin':         DEFAULT_CHECKIN,
            'expected_checkout':        DEFAULT_CHECKOUT,
            'date_joined':              emp['created_at'],
            'leave_used':               0,
            'leave_allowance':          0,
        }
        for emp in users
    ]})


@app.route('/api/attendance/list/', methods=['GET'])
def attendance_list():
    u, err = require_auth()
    if err: return err

    target = request.args.get('date') or today_str()

    with db_session() as db:
        users = db.execute(
            "SELECT * FROM users WHERE is_admin=0"
        ).fetchall()

        records = []
        for emp in users:
            rec = db.execute(
                "SELECT * FROM attendance WHERE user_id=? AND date=?",
                (emp['id'], target)
            ).fetchone()

            if rec:
                records.append({
                    'id':               emp['id'],
                    'username':         emp['display_name'],
                    'photo_url':        None,
                    'status':           rec['status'],
                    'checkin_at':       rec['checkin_at'],
                    'checkout_at':      rec['checkout_at'],
                    'hours_worked':     None,
                    'flag':             'normal',
                    'flag_label':       'On Time',
                    'late_by_minutes':  0,
                    'early_by_minutes': 0,
                })
            else:
                records.append({
                    'id':               emp['id'],
                    'username':         emp['display_name'],
                    'photo_url':        None,
                    'status':           'absent',
                    'checkin_at':       None,
                    'checkout_at':      None,
                    'hours_worked':     None,
                    'flag':             'normal',
                    'flag_label':       '—',
                    'late_by_minutes':  0,
                    'early_by_minutes': 0,
                })

    return jsonify({'date': target, 'records': records})


# ─────────────────────────────────────────────────────────────────
#  HEALTH + ROOT
# ─────────────────────────────────────────────────────────────────

@app.route('/')
def root():
    return jsonify({
        'status':  'ok',
        'app':     'MBT Fleet Test',
        'time':    now_iso(),
    })


@app.route('/api/health/')
def health():
    with db_session() as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        locs  = db.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
    return jsonify({'status': 'ok', 'users': users, 'locations': locs})


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
