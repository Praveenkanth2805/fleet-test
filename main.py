"""
MBT Fleet Test — Single-file backend (Supabase / PostgreSQL)
Covers: login, check-in, check-out, location upload, fleet map,
        office/field classification (geofence).

Env var required:
    DATABASE_URL  →  Supabase connection string (postgres://...)

Run locally:    python main.py
Deploy Render:  gunicorn main:app
"""

import os
import math
import uuid
import pytz
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv                    # ← NEW

load_dotenv()                                     # ← NEW (loads .env)

app = Flask(__name__)
CORS(app)

IST = pytz.timezone('Asia/Kolkata')

DATABASE_URL       = os.environ.get('DATABASE_URL', '').strip()
ONLINE_TIMEOUT_MIN = 5
DEFAULT_CHECKIN    = '09:00'
DEFAULT_CHECKOUT   = '18:00'
DEFAULT_RADIUS_M   = 20.0

if not DATABASE_URL:
    print('[WARN] DATABASE_URL not set — using empty connection string.')


# ─────────────────────────────────────────────────────────────────
#  DB LAYER (Supabase / PostgreSQL)
# ─────────────────────────────────────────────────────────────────

@contextmanager
def db_session():
    """Yield a PostgreSQL connection. Commits on success, rolls back on error."""
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_cursor(conn):
    """Return a dict-based cursor so rows behave like dicts."""
    return conn.cursor(cursor_factory=RealDictCursor)


def init_db():
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id              SERIAL PRIMARY KEY,
                employee_code   TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                display_name    TEXT NOT NULL,
                token           TEXT,
                is_admin        INTEGER NOT NULL DEFAULT 0,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS locations (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                latitude    DOUBLE PRECISION NOT NULL,
                longitude   DOUBLE PRECISION NOT NULL,
                ts          TEXT NOT NULL,
                accuracy_m  DOUBLE PRECISION
            );
            CREATE INDEX IF NOT EXISTS idx_loc_user_ts
                ON locations(user_id, ts DESC);

            -- Idempotent migration: add accuracy_m to existing tables
            ALTER TABLE locations
                ADD COLUMN IF NOT EXISTS accuracy_m DOUBLE PRECISION;

            CREATE TABLE IF NOT EXISTS attendance (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                date        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'checked_in',
                checkin_at  TEXT,
                checkout_at TEXT,
                UNIQUE(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS office_settings (
                id                INTEGER PRIMARY KEY,
                office_lat        DOUBLE PRECISION,
                office_lng        DOUBLE PRECISION,
                office_radius_m   DOUBLE PRECISION DEFAULT 20.0,
                geofence_enabled  INTEGER DEFAULT 0,
                updated_at        TIMESTAMPTZ DEFAULT NOW()
            );
        ''')

        # ── Seed test users (idempotent — adds only missing ones) ──
        seeds = [
            ('ADMIN001', 'admin123', 'Admin User',      1),
            ('ADMIN002', 'admin123', 'Admin User 2',    1),
            ('TEST001',  'test123',  'Test Employee',   0),
            ('TEST002',  'test123',  'Test Employee 2', 0),
        ]
        created_codes = []
        for code, pw, name, is_admin in seeds:
            cur.execute(
                "SELECT 1 FROM users WHERE employee_code=%s LIMIT 1",
                (code,)
            )
            if cur.fetchone():
                continue   # already exists → skip
            cur.execute(
                "INSERT INTO users "
                "(employee_code, password_hash, display_name, token, is_admin) "
                "VALUES (%s, %s, %s, %s, %s)",
                (code, generate_password_hash(pw), name,
                 uuid.uuid4().hex, is_admin)
            )
            created_codes.append(code)
        if created_codes:
            print(f'[INIT] Seeded users: {", ".join(created_codes)}')

        # ── Ensure office_settings has the single row ────────────
        cur.execute("SELECT COUNT(*) AS c FROM office_settings WHERE id=1")
        if cur.fetchone()['c'] == 0:
            cur.execute(
                "INSERT INTO office_settings (id, office_radius_m, geofence_enabled) "
                "VALUES (1, %s, 0)",
                (DEFAULT_RADIUS_M,)
            )

        cur.close()


# ─────────────────────────────────────────────────────────────────
#  TIME / GEO HELPERS
# ─────────────────────────────────────────────────────────────────

def now_iso():
    """Current time in IST → 'YYYY-MM-DD HH:MM:SS AM/PM' (attendance only)."""
    return datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p')


def now_iso_utc():
    """ISO UTC — used for location timestamps."""
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
    """IST date (matches Django's _today_ist()).
    Fixes night-shift handling — 9 PM to 5 AM shifts
    cross midnight, so UTC would store the wrong date."""
    return datetime.now(IST).strftime('%Y-%m-%d')


def haversine_meters(lat1, lng1, lat2, lng2):
    """Great-circle distance in meters."""
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


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
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM users WHERE token=%s", (token,))
        row = cur.fetchone()
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

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM users WHERE employee_code=%s", (code,))
        user = cur.fetchone()

        if not user or not check_password_hash(user['password_hash'], pw):
            return jsonify({'error': 'Invalid credentials'}), 401

        token = uuid.uuid4().hex
        cur.execute("UPDATE users SET token=%s WHERE id=%s", (token, user['id']))

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
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET token=NULL WHERE id=%s", (u['id'],))
    return jsonify({'status': 'logged out'})


# ─────────────────────────────────────────────────────────────────
#  PROFILE
# ─────────────────────────────────────────────────────────────────

@app.route('/api/profile/', methods=['GET', 'PATCH'])
def profile():
    u, err = require_auth()
    if err: return err

    if request.method == 'PATCH':
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
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute(
            "SELECT * FROM attendance WHERE user_id=%s AND date=%s",
            (u['id'], today)
        )
        rec = cur.fetchone()
        if not rec:
            cur.execute(
                "SELECT * FROM attendance WHERE user_id=%s AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (u['id'],)
            )
            rec = cur.fetchone()

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
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute(
            "SELECT * FROM attendance WHERE user_id=%s AND date=%s",
            (u['id'], today)
        )
        existing = cur.fetchone()
        if existing:
            return jsonify({
                'error': 'Already checked in today.',
                'checkin_at': existing['checkin_at'],
            }), 400

        now = now_iso()
        cur.execute(
            "INSERT INTO attendance (user_id, date, status, checkin_at) "
            "VALUES (%s, %s, 'checked_in', %s)",
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
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute(
            "SELECT * FROM attendance WHERE user_id=%s AND date=%s",
            (u['id'], today)
        )
        rec = cur.fetchone()
        if not rec:
            cur.execute(
                "SELECT * FROM attendance WHERE user_id=%s AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (u['id'],)
            )
            rec = cur.fetchone()
        if not rec:
            return jsonify({'error': 'You have not checked in today.'}), 400
        if rec['status'] == 'checked_out':
            return jsonify({
                'error': 'Already checked out today.',
                'checkin_at':  rec['checkin_at'],
                'checkout_at': rec['checkout_at'],
            }), 400

        now = now_iso()
        cur.execute(
            "UPDATE attendance SET status='checked_out', checkout_at=%s "
            "WHERE id=%s",
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
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute(
            "SELECT * FROM attendance WHERE user_id=%s AND date=%s",
            (u['id'], today)
        )
        rec = cur.fetchone()
        if not rec:
            cur.execute(
                "SELECT * FROM attendance WHERE user_id=%s AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (u['id'],)
            )
            rec = cur.fetchone()

        if not rec or rec['status'] == 'checked_out':
            return jsonify({
                'status': 'already_checked_out',
                'message': 'You are not currently checked in.',
            })

        now = now_iso()
        cur.execute(
            "UPDATE attendance SET status='checked_out', checkout_at=%s "
            "WHERE id=%s",
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

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute(
            "SELECT * FROM attendance WHERE user_id=%s AND status='checked_in' "
            "ORDER BY date DESC LIMIT 1",
            (u['id'],)
        )
        rec = cur.fetchone()

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
        with db_session() as conn:
            cur = conn.cursor()
            for p in points:
                try:
                    lat = float(p.get('latitude'))
                    lng = float(p.get('longitude'))
                    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                        continue
                    ts = normalize_ts(p.get('timestamp'))
                    # ── accuracy (optional, meters) ──
                    acc_raw = p.get('accuracy')
                    try:
                        acc = float(acc_raw) if acc_raw not in (None, '') else None
                    except (TypeError, ValueError):
                        acc = None
                    cur.execute(
                        "INSERT INTO locations "
                        "(user_id, latitude, longitude, ts, accuracy_m) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (u['id'], lat, lng, ts, acc)
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
    # ── accuracy (optional, meters) ──
    acc_raw = data.get('accuracy')
    try:
        acc = float(acc_raw) if acc_raw not in (None, '') else None
    except (TypeError, ValueError):
        acc = None

    with db_session() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO locations "
            "(user_id, latitude, longitude, ts, accuracy_m) "
            "VALUES (%s, %s, %s, %s, %s)",
            (u['id'], lat, lng, ts, acc)
        )

    return jsonify({
        'status':        'saved',
        'user':          u['employee_code'],
        'stop_tracking': False,
    })


# ─────────────────────────────────────────────────────────────────
#  ADMIN — OFFICE / FIELD SETTINGS  (GEOfence)
# ─────────────────────────────────────────────────────────────────

@app.route('/api/schedule/', methods=['GET'])
def get_schedule():
    u, err = require_auth()
    if err: return err

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM office_settings WHERE id=1")
        row = cur.fetchone()

    if not row:
        return jsonify({
            'expected_checkin':           DEFAULT_CHECKIN,
            'expected_checkout':          DEFAULT_CHECKOUT,
            'office_lat':                 None,
            'office_lng':                 None,
            'office_radius_m':            DEFAULT_RADIUS_M,
            'geofence_enabled':           False,
            'office_location_configured': False,
        })

    return jsonify({
        'expected_checkin':           DEFAULT_CHECKIN,
        'expected_checkout':          DEFAULT_CHECKOUT,
        'office_lat':                 row['office_lat'],
        'office_lng':                 row['office_lng'],
        'office_radius_m':            row['office_radius_m'],
        'geofence_enabled':           bool(row['geofence_enabled']),
        'office_location_configured': (
            row['office_lat'] is not None and row['office_lng'] is not None
        ),
    })


@app.route('/api/schedule/set/', methods=['PATCH', 'POST'])
def set_schedule():
    u, err = require_auth()
    if err: return err
    if not u['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json(silent=True) or {}

    # Coerce / validate inputs
    def _f(v):
        if v in (None, ''):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lat     = _f(data.get('office_lat'))
    lng     = _f(data.get('office_lng'))
    radius  = _f(data.get('office_radius_m'))
    geofence = data.get('geofence_enabled')

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM office_settings WHERE id=1")
        existing = cur.fetchone()

        # Coerce geofence to bool if provided
        geo_flag = None
        if geofence is not None:
            if isinstance(geofence, bool):
                geo_flag = 1 if geofence else 0
            elif isinstance(geofence, (int, float)):
                geo_flag = 1 if geofence else 0
            elif isinstance(geofence, str):
                geo_flag = 1 if geofence.strip().lower() in (
                    'true', '1', 'yes', 'on'
                ) else 0

        # Block enabling geofence without coordinates
        if geo_flag == 1:
            check_lat = lat if lat is not None else (existing['office_lat'] if existing else None)
            check_lng = lng if lng is not None else (existing['office_lng'] if existing else None)
            if check_lat is None or check_lng is None:
                return jsonify({
                    'error': 'Set office location before enabling geofence.'
                }), 400

        if not existing:
            cur.execute(
                "INSERT INTO office_settings "
                "(id, office_lat, office_lng, office_radius_m, geofence_enabled) "
                "VALUES (1, %s, %s, %s, %s)",
                (lat, lng, radius or DEFAULT_RADIUS_M, geo_flag or 0)
            )
        else:
            new_lat    = lat if lat is not None else existing['office_lat']
            new_lng    = lng if lng is not None else existing['office_lng']
            new_radius = radius if radius is not None else existing['office_radius_m']
            new_geo    = geo_flag if geo_flag is not None else existing['geofence_enabled']

            cur.execute(
                "UPDATE office_settings "
                "SET office_lat=%s, office_lng=%s, office_radius_m=%s, "
                "    geofence_enabled=%s, updated_at=NOW() "
                "WHERE id=1",
                (new_lat, new_lng, new_radius, new_geo)
            )

        cur.execute("SELECT * FROM office_settings WHERE id=1")
        row = cur.fetchone()

    return jsonify({
        'status':                     'updated',
        'office_lat':                 row['office_lat'],
        'office_lng':                 row['office_lng'],
        'office_radius_m':            row['office_radius_m'],
        'geofence_enabled':           bool(row['geofence_enabled']),
        'office_location_configured': (
            row['office_lat'] is not None and row['office_lng'] is not None
        ),
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

    with db_session() as conn:
        cur = db_cursor(conn)

        # Office settings
        cur.execute("SELECT * FROM office_settings WHERE id=1")
        office = cur.fetchone()
        office_lat = office['office_lat'] if office else None
        office_lng = office['office_lng'] if office else None
        radius_m   = office['office_radius_m'] if office else DEFAULT_RADIUS_M

        cur.execute("SELECT * FROM users WHERE is_admin=0")
        users = cur.fetchall()

        result = []
        for emp in users:
            cur.execute(
                "SELECT * FROM locations WHERE user_id=%s "
                "ORDER BY ts DESC LIMIT 1",
                (emp['id'],)
            )
            loc = cur.fetchone()

            cur.execute(
                "SELECT * FROM attendance WHERE user_id=%s AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (emp['id'],)
            )
            att = cur.fetchone()

            online = bool(loc and loc['ts'] > cutoff and att is not None)

            distance_m   = None
            is_at_office = None
            acc_m        = None
            if loc:
                acc_m = loc.get('accuracy_m') if 'accuracy_m' in loc.keys() else None

            if loc and office_lat is not None and office_lng is not None:
                distance_m = round(haversine_meters(
                    loc['latitude'], loc['longitude'],
                    office_lat, office_lng,
                ), 1)
                # ── accuracy-aware geofence ──
                # If the GPS fix has a known uncertainty (accuracy_m),
                # allow the point to count as "at office" when the
                # measured distance is within radius + accuracy.
                if acc_m is not None and acc_m > 0:
                    is_at_office = distance_m <= (radius_m + acc_m)
                else:
                    is_at_office = distance_m <= radius_m

            result.append({
                'id':              emp['id'],
                'employee_code':   emp['employee_code'],
                'user':            emp['display_name'],
                'lat':             loc['latitude']  if loc else None,
                'lng':             loc['longitude'] if loc else None,
                'time':            loc['ts']        if loc else None,
                'online':          online,
                'last_seen':       loc['ts']        if loc else None,
                'attendance':      att['status']    if att else 'absent',
                'photo_url':       None,
                'distance_m':      distance_m,
                'is_at_office':    is_at_office,
                'office_radius_m': radius_m,
                'accuracy_m':      acc_m,
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

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM users WHERE is_admin=0")
        users = cur.fetchall()

        trails = []
        for emp in users:
            cur.execute(
                "SELECT * FROM attendance WHERE user_id=%s AND status='checked_in' "
                "ORDER BY date DESC LIMIT 1",
                (emp['id'],)
            )
            att = cur.fetchone()
            if not att:
                continue

            cur.execute(
                "SELECT * FROM locations WHERE user_id=%s AND ts>=%s "
                "ORDER BY ts ASC",
                (emp['id'], cutoff)
            )
            rows = cur.fetchall()

            if not rows:
                cur.execute(
                    "SELECT * FROM locations WHERE user_id=%s "
                    "ORDER BY ts DESC LIMIT 1",
                    (emp['id'],)
                )
                latest = cur.fetchone()
                if latest:
                    rows = [latest]
                else:
                    continue

            trails.append({
                'id':            emp['id'],
                'employee_code': emp['employee_code'],
                'user':          emp['display_name'],
                'points': [
                    {
                        'lat':  r['latitude'],
                        'lng':  r['longitude'],
                        'time': r['ts'],
                        'accuracy_m': (
                            r['accuracy_m']
                            if 'accuracy_m' in r.keys() else None
                        ),
                    }
                    for r in rows
                ],
            })

    return jsonify({'trails': trails})


@app.route('/api/fleet-users/', methods=['GET'])
def fleet_users():
    u, err = require_auth()
    if err: return err

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM users WHERE is_admin=0")
        users = cur.fetchall()

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
            'date_joined':              emp['created_at'].isoformat()
                                        if emp['created_at'] else None,
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

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM users WHERE is_admin=0")
        users = cur.fetchall()

        records = []
        for emp in users:
            cur.execute(
                "SELECT * FROM attendance WHERE user_id=%s AND date=%s",
                (emp['id'], target)
            )
            rec = cur.fetchone()

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
#  ADMIN — LOCATION HISTORY
# ─────────────────────────────────────────────────────────────────

@app.route('/api/location-history/', methods=['GET'])
def location_history():
    u, err = require_auth()
    if err: return err

    username = request.args.get('username')
    if not username:
        return jsonify({'error': 'username param required'}), 400

    with db_session() as conn:
        cur = db_cursor(conn)
        # Primary: employee_code. Fallback: display_name.
        cur.execute("SELECT * FROM users WHERE employee_code=%s", (username,))
        user = cur.fetchone()
        if not user:
            cur.execute("SELECT * FROM users WHERE display_name=%s", (username,))
            user = cur.fetchone()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        limit_param = request.args.get('limit')
        try:
            limit = min(int(limit_param), 5000) if limit_param else None
        except (TypeError, ValueError):
            limit = None

        if limit:
            cur.execute(
                "SELECT * FROM locations WHERE user_id=%s "
                "ORDER BY ts ASC LIMIT %s",
                (user['id'], limit)
            )
        else:
            cur.execute(
                "SELECT * FROM locations WHERE user_id=%s ORDER BY ts ASC",
                (user['id'],)
            )
        rows = cur.fetchall()

    # Sample at 5-minute intervals
    points = []
    last_kept = None
    for r in rows:
        try:
            dt = datetime.fromisoformat(r['ts'].replace('Z', '+00:00'))
        except Exception:
            continue

        if last_kept is None or (dt - last_kept).total_seconds() >= 300:
            points.append({
                'lat':  r['latitude'],
                'lng':  r['longitude'],
                'time': r['ts'],
            })
            last_kept = dt

    if rows:
        last = rows[-1]
        if not points or points[-1]['time'] != last['ts']:
            points.append({
                'lat':  last['latitude'],
                'lng':  last['longitude'],
                'time': last['ts'],
            })

    return jsonify({
        'username': username,
        'points':   points,
    })


@app.route('/api/location-history/clear/', methods=['DELETE'])
def clear_location_history():
    u, err = require_auth()
    if err: return err

    username = request.args.get('username')
    if not username:
        return jsonify({'error': 'username param required'}), 400

    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT * FROM users WHERE employee_code=%s", (username,))
        user = cur.fetchone()
        if not user:
            cur.execute("SELECT * FROM users WHERE display_name=%s", (username,))
            user = cur.fetchone()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        cur.execute("DELETE FROM locations WHERE user_id=%s", (user['id'],))
        deleted = cur.rowcount

    return jsonify({'status': 'cleared', 'deleted': deleted})


# ─────────────────────────────────────────────────────────────────
#  HEALTH + ROOT
# ─────────────────────────────────────────────────────────────────

@app.route('/')
def root():
    return jsonify({
        'status': 'ok',
        'app':    'MBT Fleet Test',
        'time':   now_iso(),
    })


@app.route('/api/health/')
def health():
    with db_session() as conn:
        cur = db_cursor(conn)
        cur.execute("SELECT COUNT(*) AS c FROM users")
        users = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) AS c FROM locations")
        locs = cur.fetchone()['c']
    return jsonify({'status': 'ok', 'users': users, 'locations': locs})


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
