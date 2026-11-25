#!/usr/bin/env python3
"""ARIC OS — the ARIC back office (partner pain points, kanban tasks, people management + onboarding).

Python 3 standard library + SQLite only. Runs behind nginx on the ARIC VM:
  https://aric.mfu.ac.th/os/               back office (single-page app, ops/web/app.html)
  https://aric.mfu.ac.th/os/api/...        JSON API
  https://aric.mfu.ac.th/challenge/        public stakeholder form (static page in the website repo)
                                           which posts to /os/api/public/challenge

Sign-in: Firebase Authentication (Google provider) in the browser; the server re-checks every
ID token with Google (Identity Toolkit accounts:lookup), accepts only the configured MFU email
domains, then issues its own HttpOnly session cookie. Roles: the addresses in ARIC_ADMIN_EMAILS
are admins and see everything; everyone else is staff and sees only what they were given.

Usage:
  python3 server.py                      serve (settings from environment, see CFG below)
  python3 server.py --seed-initial       insert ARIC's starting people/boards/tasks if the DB is empty
  python3 server.py --backup DIR         write a consistent copy of the DB to DIR (keeps 14)
"""
import csv, datetime as dt, hashlib, http.server, io, json, os, pathlib, re, secrets, sqlite3, sys, threading, time
import urllib.error, urllib.request
from urllib.parse import urlparse, parse_qs

HERE = pathlib.Path(__file__).resolve().parent
ENV = os.environ.get
CFG = {
    'db': ENV('ARIC_DB', str(HERE / 'data' / 'aric.db')),
    'host': ENV('ARIC_HOST', '127.0.0.1'),
    'port': int(ENV('ARIC_PORT', '8090')),
    'admins': {e.strip().lower() for e in ENV('ARIC_ADMIN_EMAILS', 'nacha.cho@mfu.ac.th').split(',') if e.strip()},
    'domains': tuple(d.strip().lower() for d in ENV('ARIC_EMAIL_DOMAINS', 'mfu.ac.th,lamduan.mfu.ac.th').split(',') if d.strip()),
    'dev_login': ENV('ARIC_DEV_LOGIN') == '1',            # local testing only: sign in by typing an email
    'dev_static': ENV('ARIC_DEV_STATIC', ''),             # local testing only: also serve the website from this folder
    'secure_cookie': ENV('ARIC_SECURE_COOKIE', '1') == '1',
    'autoreload': ENV('ARIC_AUTORELOAD') == '1',          # exit when server.py changes; systemd restarts it
    'session_hours': int(ENV('ARIC_SESSION_HOURS', '168')),
}
FIREBASE = {k: v for k, v in json.loads((HERE / 'firebase_config.json').read_text(encoding='utf-8')).items() if not k.startswith('_')}
APP_HTML = HERE / 'web' / 'app.html'
COOKIE = 'aricos'

# ---------------------------------------------------------------- reference values (kept in step with the app)
PP_STAGES = ['new', 'review', 'shortlist', 'contacted', 'challenge', 'parked']
TASK_COLS = ['backlog', 'todo', 'doing', 'review', 'done']
PRIORITIES = ['h', 'm', 'l']
SECTORS = ['health', 'agri', 'gov', 'tour', 'logi', 'mfg', 'it', 'other']
RTYPES = ['owner', 'industry', 'alumni', 'health', 'gov', 'community']
FREQ = ['daily', 'weekly', 'monthly', 'seasonal', '']
DATA_TYPES = ['img', 'vid', 'doc', 'sheet', 'iot', 'none']
CHANNELS = ['buy', 'refer', 'distribute', 'invest', 'unsure']
INVOLVE = ['owner', 'mentor', 'visit', 'sponsor', 'pilot', 'hire']
KINDS = ['staff', 'intern', 'mentor']
PSTATUS = ['offered', 'confirmed', 'onboarding', 'active', 'alumni']
ONB_ITEMS = [f'o{i}' for i in range(1, 13)]
BOARD_COLORS = ['#C8102E', '#2F5D9A', '#B7791F', '#2E7D5B', '#6B5B95', '#0D3B66', '#8A5A2B', '#3F8F6F', '#7A4E9A', '#6E0B1C']   # same set as the app's swatches
COLORS = ['#6E0B1C', '#2F5D9A', '#B7791F', '#2E7D5B', '#6B5B95', '#C8102E', '#0D3B66', '#8A5A2B', '#3F8F6F', '#7A4E9A']

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE COLLATE NOCASE, name TEXT NOT NULL, name_th TEXT DEFAULT '',
  kind TEXT DEFAULT 'staff', title TEXT DEFAULT '', track TEXT DEFAULT '', sid TEXT DEFAULT '', phone TEXT DEFAULT '',
  status TEXT DEFAULT 'active', start TEXT DEFAULT '', end_ TEXT DEFAULT '', supervisor_id INTEGER, color TEXT DEFAULT '',
  photo TEXT DEFAULT '', created_at TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS boards(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', created_at TEXT, archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS board_members(board_id INTEGER, person_id INTEGER, PRIMARY KEY(board_id, person_id));
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '', status TEXT DEFAULT 'todo',
  assignee_id INTEGER, due TEXT DEFAULT '', priority TEXT DEFAULT 'm', labels TEXT DEFAULT '[]', pp_id INTEGER, ord REAL DEFAULT 0,
  created_by INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, person_id INTEGER, body TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS painpoints(
  id INTEGER PRIMARY KEY, created_at TEXT, status TEXT DEFAULT 'new', priority INTEGER DEFAULT 0, owner_id INTEGER, notes TEXT DEFAULT '',
  ord REAL DEFAULT 0, lang TEXT DEFAULT 'th', name TEXT, org TEXT, position TEXT, email TEXT, phone TEXT, linkedin TEXT, rtype TEXT,
  sector TEXT, alumni TEXT, title TEXT, problem TEXT, affected TEXT, freq TEXT, workaround TEXT, impact TEXT, success TEXT,
  data TEXT DEFAULT '[]', market TEXT, msize TEXT, channels TEXT DEFAULT '[]', price TEXT, competitors TEXT, involve TEXT DEFAULT '[]',
  public_ok INTEGER DEFAULT 0, consent INTEGER DEFAULT 0, ip_hash TEXT, user_agent TEXT, form TEXT DEFAULT 'long');
CREATE TABLE IF NOT EXISTS onboarding(person_id INTEGER, item TEXT, done INTEGER DEFAULT 0, done_at TEXT, done_by INTEGER, PRIMARY KEY(person_id, item));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, person_id INTEGER NOT NULL, expires REAL NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, person_id INTEGER, action TEXT, entity TEXT, entity_id INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_tasks_board ON tasks(board_id);
CREATE INDEX IF NOT EXISTS ix_tasks_assignee ON tasks(assignee_id);
CREATE INDEX IF NOT EXISTS ix_comments_task ON comments(task_id);
"""

def now(): return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec='seconds')
def sha(s): return hashlib.sha256(s.encode()).hexdigest()

def db():
    pathlib.Path(CFG['db']).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(CFG['db'], timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        if 'form' not in {r[1] for r in c.execute('PRAGMA table_info(painpoints)')}:   # short or long public form
            c.execute("ALTER TABLE painpoints ADD COLUMN form TEXT DEFAULT 'long'")
        cols = {r[1] for r in c.execute('PRAGMA table_info(boards)')}   # columns added after release 1
        if 'ord' not in cols: c.execute('ALTER TABLE boards ADD COLUMN ord REAL DEFAULT 0'); c.execute('UPDATE boards SET ord=id')
        if 'color' not in cols:   # give existing boards distinct colours so the tabs are told apart at once
            c.execute("ALTER TABLE boards ADD COLUMN color TEXT DEFAULT ''")
            for i, (bid,) in enumerate(c.execute('SELECT id FROM boards ORDER BY ord, id').fetchall()): c.execute('UPDATE boards SET color=? WHERE id=?', (BOARD_COLORS[i % len(BOARD_COLORS)], bid))

def rows(c, sql, args=()): return [dict(r) for r in c.execute(sql, args).fetchall()]
def row(c, sql, args=()):
    r = c.execute(sql, args).fetchone(); return dict(r) if r else None

def audit(c, who, action, entity, eid, detail=''):
    c.execute('INSERT INTO audit(at,person_id,action,entity,entity_id,detail) VALUES(?,?,?,?,?,?)',
              (now(), who, action, entity, eid, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)))

class HttpError(Exception):
    def __init__(self, status, msg): super().__init__(msg); self.status, self.msg = status, msg

def need(cond, status=400, msg='bad request'):
    if not cond: raise HttpError(status, msg)

def txt(v, n=2000, req=False, name='field'):
    v = '' if v is None else str(v).strip()
    need(not req or v, 400, f'{name} is required')
    need(len(v) <= n, 400, f'{name} is too long')
    return v

def pick(v, allowed, name='field'):
    need(v in allowed, 400, f'invalid {name}'); return v

def picks(v, allowed, name='field'):
    need(isinstance(v, list) and all(x in allowed for x in v), 400, f'invalid {name}'); return json.dumps(sorted(set(v)))

def date_or_blank(v, name='date'):
    v = txt(v, 10, name=name); need(v == '' or re.fullmatch(r'\d{4}-\d{2}-\d{2}', v), 400, f'invalid {name}'); return v

def email_ok(e): return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', e or ''))
def mfu_email(e):
    e = (e or '').lower()
    return any(e.endswith('@' + d) for d in CFG['domains'])

# ---------------------------------------------------------------- auth
def verify_firebase_token(id_token):
    """Ask Google to validate a Firebase ID token; returns (email, name, photo) or raises."""
    req = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE['apiKey']}",
        data=json.dumps({'idToken': id_token}).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise HttpError(401, 'sign-in token was rejected') from e
    users = data.get('users') or []
    need(users, 401, 'sign-in token was rejected')
    u = users[0]
    need(u.get('emailVerified'), 401, 'email is not verified')
    return u.get('email', '').lower(), u.get('displayName', ''), u.get('photoUrl', '')

def start_session(c, email, name='', photo=''):
    need(mfu_email(email), 403, 'only MFU email addresses can sign in')
    p = row(c, 'SELECT * FROM people WHERE email=?', (email,))
    if not p:   # first sign-in: a staff record that sees nothing until the admin gives access
        n = c.execute('SELECT COUNT(*) FROM people').fetchone()[0]
        c.execute('INSERT INTO people(email,name,kind,status,color,photo,created_at) VALUES(?,?,?,?,?,?,?)',
                  (email, name or email.split('@')[0], 'staff', 'active', COLORS[n % len(COLORS)], photo, now()))
        p = row(c, 'SELECT * FROM people WHERE email=?', (email,))
        audit(c, p['id'], 'first_login', 'people', p['id'], email)
    c.execute('UPDATE people SET last_login=?, photo=COALESCE(NULLIF(?,\'\'),photo) WHERE id=?', (now(), photo, p['id']))
    token = secrets.token_urlsafe(32)
    c.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
    c.execute('INSERT INTO sessions VALUES(?,?,?,?)', (sha(token), p['id'], time.time() + CFG['session_hours'] * 3600, now()))
    return token

def is_admin(p): return bool(p) and (p.get('email') or '').lower() in CFG['admins']

# ---------------------------------------------------------------- visibility
def member_board_ids(c, me):
    return {r['board_id'] for r in rows(c, 'SELECT board_id FROM board_members WHERE person_id=?', (me['id'],))}

def can_see_task(c, me, t):
    if is_admin(me): return True
    return t['assignee_id'] == me['id'] or t['board_id'] in member_board_ids(c, me)

def can_see_pp(me, pp): return is_admin(me) or pp['owner_id'] == me['id']

def task_out(t, counts):
    t = dict(t); t['labels'] = json.loads(t['labels'] or '[]'); t['comments'] = counts.get(t['id'], 0); return t

def pp_out(p):
    p = dict(p)
    for k in ('data', 'channels', 'involve'): p[k] = json.loads(p[k] or '[]')
    p['ref'] = f"PP-{p['id']:04d}"; p.pop('ip_hash', None); p.pop('user_agent', None)
    return p

def person_out(p, full):
    keys = ('id', 'name', 'name_th', 'color', 'kind', 'photo') if not full else \
           ('id', 'email', 'name', 'name_th', 'kind', 'title', 'track', 'sid', 'phone', 'status', 'start', 'end_', 'supervisor_id', 'color', 'photo', 'last_login')
    d = {k: p.get(k) for k in keys}; d['admin'] = is_admin(p); return d

def bootstrap(c, me):
    admin = is_admin(me)
    boards = rows(c, 'SELECT * FROM boards WHERE archived=0 ORDER BY ord, id')
    members = {}
    for r in rows(c, 'SELECT * FROM board_members'): members.setdefault(r['board_id'], []).append(r['person_id'])
    counts = {r['task_id']: r['n'] for r in rows(c, 'SELECT task_id, COUNT(*) n FROM comments GROUP BY task_id')}
    tasks = rows(c, 'SELECT * FROM tasks ORDER BY ord, id')
    pps = rows(c, 'SELECT * FROM painpoints ORDER BY ord, id DESC')
    people = rows(c, 'SELECT * FROM people ORDER BY id')
    onb = rows(c, 'SELECT person_id, item, done, done_at FROM onboarding WHERE done=1')
    if not admin:
        mine = member_board_ids(c, me)
        tasks = [t for t in tasks if t['board_id'] in mine or t['assignee_id'] == me['id']]
        vis_b = mine | {t['board_id'] for t in tasks}
        boards = [b for b in boards if b['id'] in vis_b]
        pps = [p for p in pps if p['owner_id'] == me['id']]
        onb = [o for o in onb if o['person_id'] == me['id']]
        ref = {me['id']} | {t['assignee_id'] for t in tasks} | {m for b in boards for m in members.get(b['id'], [])} | {p['owner_id'] for p in pps}
        people = [p for p in people if p['id'] in ref]
    for b in boards: b['members'] = members.get(b['id'], [])
    return {
        'me': person_out(me, True), 'admin': admin,
        'people': [person_out(p, admin or p['id'] == me['id']) for p in people],
        'boards': boards, 'tasks': [task_out(t, counts) for t in tasks], 'pp': [pp_out(p) for p in pps],
        'onb': onb, 'today': dt.date.today().isoformat(),
    }

# ---------------------------------------------------------------- rate limiting (in memory)
_hits, _hits_lock = {}, threading.Lock()
def rate_ok(key, limit, window):
    t = time.time()
    with _hits_lock:
        q = [x for x in _hits.get(key, []) if t - x < window]
        ok = len(q) < limit
        if ok: q.append(t)
        _hits[key] = q
        return ok

# ---------------------------------------------------------------- handlers
ROUTES = []
def route(method, pattern, auth='user'):
    def deco(fn): ROUTES.append((method, re.compile('^' + pattern + '$'), auth, fn)); return fn
    return deco

@route('GET', '/os/api/health', auth=None)
def h_health(rq, c, me, m): return {'ok': True, 'time': now()}

@route('GET', '/os/api/config', auth=None)
def h_config(rq, c, me, m): return {'firebase': FIREBASE, 'dev': CFG['dev_login'], 'domains': CFG['domains']}

@route('POST', '/os/api/auth/session', auth=None)
def h_session(rq, c, me, m):
    need(rate_ok('auth:' + rq.ip, 20, 600), 429, 'too many sign-in attempts, try again later')
    email, name, photo = verify_firebase_token(txt(rq.body.get('idToken'), 5000, True, 'idToken'))
    rq.set_cookie(start_session(c, email, name, photo))
    return {'ok': True}

@route('POST', '/os/api/auth/dev', auth=None)
def h_dev(rq, c, me, m):
    need(CFG['dev_login'], 404, 'not found')
    rq.set_cookie(start_session(c, txt(rq.body.get('email'), 200, True, 'email').lower()))
    return {'ok': True}

@route('POST', '/os/api/auth/logout')
def h_logout(rq, c, me, m):
    c.execute('DELETE FROM sessions WHERE token_hash=?', (sha(rq.cookie or ''),)); rq.set_cookie('', clear=True); return {'ok': True}

@route('GET', '/os/api/bootstrap')
def h_boot(rq, c, me, m): return bootstrap(c, me)

# --- tasks
def task_or_404(c, me, tid):
    t = row(c, 'SELECT * FROM tasks WHERE id=?', (tid,)); need(t and can_see_task(c, me, t), 404, 'task not found'); return t

def board_writable(c, me, bid):
    need(row(c, 'SELECT id FROM boards WHERE id=? AND archived=0', (bid,)), 400, 'unknown board')
    need(is_admin(me) or bid in member_board_ids(c, me), 403, 'you are not a member of that board')

@route('POST', '/os/api/tasks')
def h_task_new(rq, c, me, m):
    b = rq.body; bid = int(b.get('board_id') or 0); board_writable(c, me, bid)
    st = pick(b.get('status', 'todo'), TASK_COLS, 'status')
    mx = c.execute('SELECT COALESCE(MAX(ord),0) FROM tasks WHERE board_id=? AND status=?', (bid, st)).fetchone()[0]
    cur = c.execute('INSERT INTO tasks(board_id,title,description,status,assignee_id,due,priority,pp_id,ord,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (bid, txt(b.get('title'), 300, True, 'title'), txt(b.get('description'), 5000), st, b.get('assignee_id') or me['id'],
         date_or_blank(b.get('due', '')), pick(b.get('priority', 'm'), PRIORITIES, 'priority'), b.get('pp_id') or None, mx + 1, me['id'], now(), now()))
    audit(c, me['id'], 'create', 'task', cur.lastrowid); return {'id': cur.lastrowid}

@route('PATCH', r'/os/api/tasks/(\d+)')
def h_task_edit(rq, c, me, m):
    t = task_or_404(c, me, int(m[1])); b = rq.body; sets = {}
    if 'title' in b: sets['title'] = txt(b['title'], 300, True, 'title')
    if 'description' in b: sets['description'] = txt(b['description'], 5000)
    if 'status' in b: sets['status'] = pick(b['status'], TASK_COLS, 'status')
    if 'priority' in b: sets['priority'] = pick(b['priority'], PRIORITIES, 'priority')
    if 'due' in b: sets['due'] = date_or_blank(b['due'], 'due date')
    if 'assignee_id' in b:
        need(b['assignee_id'] is None or row(c, 'SELECT id FROM people WHERE id=?', (b['assignee_id'],)), 400, 'unknown person'); sets['assignee_id'] = b['assignee_id']
    if 'board_id' in b: board_writable(c, me, int(b['board_id'])); sets['board_id'] = int(b['board_id'])
    if 'labels' in b: need(isinstance(b['labels'], list), 400, 'invalid labels'); sets['labels'] = json.dumps([txt(x, 40) for x in b['labels']][:10], ensure_ascii=False)
    need(sets, 400, 'nothing to change')
    sets['updated_at'] = now()
    c.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), t['id']))
    audit(c, me['id'], 'update', 'task', t['id'], {k: v for k, v in sets.items() if k != 'updated_at'}); return {'ok': True}

@route('DELETE', r'/os/api/tasks/(\d+)', auth='admin')
def h_task_del(rq, c, me, m):
    t = task_or_404(c, me, int(m[1])); c.execute('DELETE FROM comments WHERE task_id=?', (t['id'],)); c.execute('DELETE FROM tasks WHERE id=?', (t['id'],))
    audit(c, me['id'], 'delete', 'task', t['id'], t['title']); return {'ok': True}

@route('POST', '/os/api/tasks/reorder')
def h_task_order(rq, c, me, m):
    """Drag and drop: {status, ids:[...]} = the full new order of that column; every id must be visible."""
    st = pick(rq.body.get('status'), TASK_COLS, 'status'); ids = rq.body.get('ids')
    need(isinstance(ids, list) and len(ids) <= 500, 400, 'invalid ids')
    for i, tid in enumerate(ids):
        t = task_or_404(c, me, int(tid))
        c.execute('UPDATE tasks SET status=?, ord=?, updated_at=? WHERE id=?', (st, i, now() if t['status'] != st else t['updated_at'], t['id']))
        if t['status'] != st: audit(c, me['id'], 'move', 'task', t['id'], {'from': t['status'], 'to': st})
    return {'ok': True}

@route('GET', r'/os/api/tasks/(\d+)/comments')
def h_comments(rq, c, me, m):
    t = task_or_404(c, me, int(m[1]))
    return {'comments': rows(c, 'SELECT id, person_id, body, created_at FROM comments WHERE task_id=? ORDER BY id', (t['id'],))}

@route('POST', r'/os/api/tasks/(\d+)/comments')
def h_comment_new(rq, c, me, m):
    t = task_or_404(c, me, int(m[1]))
    c.execute('INSERT INTO comments(task_id,person_id,body,created_at) VALUES(?,?,?,?)', (t['id'], me['id'], txt(rq.body.get('body'), 4000, True, 'comment'), now()))
    return {'ok': True}

# --- pain points
def pp_or_404(c, me, pid):
    p = row(c, 'SELECT * FROM painpoints WHERE id=?', (pid,)); need(p and can_see_pp(me, p), 404, 'pain point not found'); return p

@route('PATCH', r'/os/api/pp/(\d+)')
def h_pp_edit(rq, c, me, m):
    p = pp_or_404(c, me, int(m[1])); b = rq.body; sets = {}
    if 'status' in b: sets['status'] = pick(b['status'], PP_STAGES, 'status')
    if 'priority' in b: need(b['priority'] in range(0, 6), 400, 'invalid priority'); sets['priority'] = b['priority']
    if 'notes' in b: sets['notes'] = txt(b['notes'], 8000)
    if 'owner_id' in b:
        need(is_admin(me), 403, 'only the admin assigns pain points')
        need(b['owner_id'] is None or row(c, 'SELECT id FROM people WHERE id=?', (b['owner_id'],)), 400, 'unknown person'); sets['owner_id'] = b['owner_id']
    need(sets, 400, 'nothing to change')
    c.execute(f"UPDATE painpoints SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), p['id']))
    audit(c, me['id'], 'update', 'painpoint', p['id'], {k: v for k, v in sets.items() if k != 'notes'}); return {'ok': True}

@route('POST', '/os/api/pp/reorder')
def h_pp_order(rq, c, me, m):
    st = pick(rq.body.get('status'), PP_STAGES, 'status'); ids = rq.body.get('ids')
    need(isinstance(ids, list) and len(ids) <= 1000, 400, 'invalid ids')
    for i, pid in enumerate(ids):
        p = pp_or_404(c, me, int(pid)); c.execute('UPDATE painpoints SET status=?, ord=? WHERE id=?', (st, i, p['id']))
        if p['status'] != st: audit(c, me['id'], 'move', 'painpoint', p['id'], {'from': p['status'], 'to': st})
    return {'ok': True}

@route('DELETE', r'/os/api/pp/(\d+)', auth='admin')
def h_pp_del(rq, c, me, m):
    p = pp_or_404(c, me, int(m[1])); c.execute('UPDATE tasks SET pp_id=NULL WHERE pp_id=?', (p['id'],)); c.execute('DELETE FROM painpoints WHERE id=?', (p['id'],))
    audit(c, me['id'], 'delete', 'painpoint', p['id'], 'PDPA / admin delete'); return {'ok': True}

PP_CSV = ['id', 'created_at', 'form', 'status', 'priority', 'owner_id', 'name', 'org', 'position', 'email', 'phone', 'linkedin', 'rtype', 'sector', 'alumni',
          'title', 'problem', 'affected', 'freq', 'workaround', 'impact', 'success', 'data', 'market', 'msize', 'channels', 'price', 'competitors',
          'involve', 'public_ok', 'notes']

@route('GET', '/os/api/pp.csv', auth='admin')
def h_pp_csv(rq, c, me, m):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(PP_CSV)
    for p in rows(c, 'SELECT * FROM painpoints ORDER BY id'): w.writerow([p.get(k, '') for k in PP_CSV])
    audit(c, me['id'], 'export', 'painpoint', None, 'csv')
    return ('text/csv; charset=utf-8', ('﻿' + buf.getvalue()).encode('utf-8'), 'aric-pain-points.csv')

@route('POST', '/os/api/public/challenge', auth=None)
def h_public(rq, c, me, m):
    b = rq.body
    if txt(b.get('website'), 200): return {'ok': True, 'ref': 'PP-0000'}          # honeypot filled: pretend success
    need(int(b.get('elapsed_ms') or 0) >= 2500, 400, 'please take a moment to fill in the form')
    need(rate_ok('pub:' + rq.ip, 6, 3600), 429, 'too many submissions from this network, please try again later')
    need(b.get('consent') is True, 400, 'PDPA consent is required')
    email, phone = txt(b.get('email'), 200), txt(b.get('phone'), 100)   # the short form needs only one way to reach the owner
    need(email or phone, 400, 'please give a phone number, LINE ID or email'); need(not email or email_ok(email), 400, 'invalid email')
    problem = txt(b.get('problem'), 5000, True, 'problem description')
    title = txt(b.get('title'), 300) or (lambda f: f if len(f) <= 120 else f[:117].rstrip() + '…')(problem.splitlines()[0].strip())
    li = txt(b.get('linkedin'), 300)
    need(li == '' or re.match(r'^https://([a-z]{2,3}\.)?linkedin\.com/', li, re.I), 400, 'LinkedIn link must start with https://www.linkedin.com/')
    vals = dict(
        created_at=now(), lang=pick(b.get('lang', 'th'), ['th', 'en'], 'lang'),
        name=txt(b.get('name'), 200, True, 'name'), org=txt(b.get('org'), 200), position=txt(b.get('position'), 200),
        email=email, phone=phone, linkedin=li, rtype=pick(b.get('rtype') or '', RTYPES + [''], 'submitter type'),
        sector=pick(b.get('sector') or '', SECTORS + [''], 'sector'), alumni=txt(b.get('alumni'), 200),
        title=title, problem=problem, form=pick(b.get('form') or 'long', ['short', 'long'], 'form'),
        affected=txt(b.get('affected'), 500), freq=pick(b.get('freq', ''), FREQ, 'frequency'), workaround=txt(b.get('workaround'), 2000),
        impact=txt(b.get('impact'), 2000), success=txt(b.get('success'), 2000), data=picks(b.get('data', []), DATA_TYPES, 'data'),
        market=txt(b.get('market'), 1000), msize=txt(b.get('msize'), 500), channels=picks(b.get('channels', []), CHANNELS, 'channels'),
        price=txt(b.get('price'), 300), competitors=txt(b.get('competitors'), 1000), involve=picks(b.get('involve', []), INVOLVE, 'involvement'),
        public_ok=1 if b.get('public_ok') is True else 0, consent=1, ip_hash=sha('ip:' + rq.ip)[:16], user_agent=(rq.headers.get('User-Agent') or '')[:200])
    mn = c.execute("SELECT COALESCE(MIN(ord),0) FROM painpoints WHERE status='new'").fetchone()[0]
    vals['ord'] = mn - 1
    cur = c.execute(f"INSERT INTO painpoints({','.join(vals)}) VALUES({','.join('?' * len(vals))})", tuple(vals.values()))
    audit(c, None, 'submit', 'painpoint', cur.lastrowid, vals['org'] or vals['name'])
    return {'ok': True, 'ref': f'PP-{cur.lastrowid:04d}'}

# --- boards (admin)
def set_members(c, bid, ids):
    need(isinstance(ids, list), 400, 'invalid members')
    c.execute('DELETE FROM board_members WHERE board_id=?', (bid,))
    for pid in {int(x) for x in ids}:
        if row(c, 'SELECT id FROM people WHERE id=?', (pid,)): c.execute('INSERT INTO board_members VALUES(?,?)', (bid, pid))

def board_color(v):
    need(not v or re.fullmatch(r'#[0-9A-Fa-f]{6}', v or ''), 400, 'invalid colour'); return v or ''

@route('POST', '/os/api/boards', auth='admin')
def h_board_new(rq, c, me, m):
    ordn = (c.execute('SELECT MAX(ord) FROM boards').fetchone()[0] or 0) + 1
    color = board_color(rq.body.get('color')) or BOARD_COLORS[c.execute('SELECT COUNT(*) FROM boards').fetchone()[0] % len(BOARD_COLORS)]
    cur = c.execute('INSERT INTO boards(name,description,created_at,ord,color) VALUES(?,?,?,?,?)', (txt(rq.body.get('name'), 120, True, 'board name'), txt(rq.body.get('description'), 2000), now(), ordn, color))
    set_members(c, cur.lastrowid, list(rq.body.get('members') or []) + [me['id']])
    audit(c, me['id'], 'create', 'board', cur.lastrowid); return {'id': cur.lastrowid}

@route('PATCH', r'/os/api/boards/(\d+)', auth='admin')
def h_board_edit(rq, c, me, m):
    bid = int(m[1]); need(row(c, 'SELECT id FROM boards WHERE id=?', (bid,)), 404, 'board not found'); b = rq.body
    if 'name' in b: c.execute('UPDATE boards SET name=? WHERE id=?', (txt(b['name'], 120, True, 'board name'), bid))
    if 'description' in b: c.execute('UPDATE boards SET description=? WHERE id=?', (txt(b['description'], 2000), bid))
    if 'archived' in b: c.execute('UPDATE boards SET archived=? WHERE id=?', (1 if b['archived'] else 0, bid))
    if 'color' in b: c.execute('UPDATE boards SET color=? WHERE id=?', (board_color(b['color']), bid))
    if 'members' in b: set_members(c, bid, list(b['members']) + [me['id']])
    audit(c, me['id'], 'update', 'board', bid, {k: b[k] for k in b if k != 'description'}); return {'ok': True}

@route('POST', '/os/api/boards/reorder', auth='admin')
def h_board_order(rq, c, me, m):
    ids = rq.body.get('ids'); need(isinstance(ids, list) and len(ids) <= 500, 400, 'invalid ids')
    for i, bid in enumerate(ids): c.execute('UPDATE boards SET ord=? WHERE id=?', (i + 1, int(bid)))
    audit(c, me['id'], 'reorder', 'board', None, [int(x) for x in ids]); return {'ok': True}

@route('DELETE', r'/os/api/boards/(\d+)', auth='admin')
def h_board_del(rq, c, me, m):
    bid = int(m[1]); b = row(c, 'SELECT * FROM boards WHERE id=?', (bid,)); need(b, 404, 'board not found')
    n = c.execute('SELECT COUNT(*) FROM tasks WHERE board_id=?', (bid,)).fetchone()[0]
    c.execute('DELETE FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE board_id=?)', (bid,))
    c.execute('DELETE FROM tasks WHERE board_id=?', (bid,)); c.execute('DELETE FROM board_members WHERE board_id=?', (bid,)); c.execute('DELETE FROM boards WHERE id=?', (bid,))
    audit(c, me['id'], 'delete', 'board', bid, {'name': b['name'], 'tasks': n}); return {'ok': True}

# --- people (admin) and onboarding
def person_vals(c, b, partial):
    out = {}
    def put(k, v):
        if not partial or k in b: out[k] = v(b.get(k))
    put('name', lambda v: txt(v, 200, True, 'name'))
    put('name_th', lambda v: txt(v, 200))
    put('email', lambda v: (lambda e: (need(e == '' or (email_ok(e) and mfu_email(e)), 400, 'email must be an MFU address') or e.lower() or None))(txt(v, 200)))
    put('kind', lambda v: pick(v or 'staff', KINDS, 'kind'))
    put('title', lambda v: txt(v, 200)); put('track', lambda v: txt(v, 200)); put('sid', lambda v: txt(v, 20)); put('phone', lambda v: txt(v, 50))
    put('status', lambda v: pick(v or 'active', PSTATUS, 'status'))
    put('start', lambda v: date_or_blank(v, 'start date')); put('end_', lambda v: date_or_blank(v, 'end date'))
    put('supervisor_id', lambda v: (need(v in (None, '') or row(c, 'SELECT id FROM people WHERE id=?', (v,)), 400, 'unknown supervisor') or (v or None)))
    put('color', lambda v: (need(not v or re.fullmatch(r'#[0-9A-Fa-f]{6}', v), 400, 'invalid colour') or v or ''))
    return out

@route('POST', '/os/api/people', auth='admin')
def h_person_new(rq, c, me, m):
    v = person_vals(c, rq.body, False)
    if not v.get('color'): v['color'] = COLORS[c.execute('SELECT COUNT(*) FROM people').fetchone()[0] % len(COLORS)]
    v['created_at'] = now()
    try: cur = c.execute(f"INSERT INTO people({','.join(v)}) VALUES({','.join('?' * len(v))})", tuple(v.values()))
    except sqlite3.IntegrityError: raise HttpError(409, 'someone with that email already exists')
    audit(c, me['id'], 'create', 'people', cur.lastrowid, v.get('name')); return {'id': cur.lastrowid}

@route('PATCH', r'/os/api/people/(\d+)', auth='admin')
def h_person_edit(rq, c, me, m):
    pid = int(m[1]); need(row(c, 'SELECT id FROM people WHERE id=?', (pid,)), 404, 'person not found')
    v = person_vals(c, rq.body, True); need(v, 400, 'nothing to change')
    try: c.execute(f"UPDATE people SET {', '.join(k + '=?' for k in v)} WHERE id=?", (*v.values(), pid))
    except sqlite3.IntegrityError: raise HttpError(409, 'someone with that email already exists')
    audit(c, me['id'], 'update', 'people', pid, v); return {'ok': True}

@route('POST', '/os/api/onboarding')
def h_onb(rq, c, me, m):
    pid = int(rq.body.get('person_id') or 0); item = pick(rq.body.get('item'), ONB_ITEMS, 'item')
    need(is_admin(me) or pid == me['id'], 403, 'you can only update your own checklist')
    need(row(c, 'SELECT id FROM people WHERE id=?', (pid,)), 404, 'person not found')
    done = 1 if rq.body.get('done') else 0
    c.execute('INSERT INTO onboarding(person_id,item,done,done_at,done_by) VALUES(?,?,?,?,?) ON CONFLICT(person_id,item) DO UPDATE SET done=excluded.done, done_at=excluded.done_at, done_by=excluded.done_by',
              (pid, item, done, now() if done else None, me['id']))
    audit(c, me['id'], 'onboarding', 'people', pid, {'item': item, 'done': done}); return {'ok': True}

# ---------------------------------------------------------------- HTTP plumbing
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'ARIC-OS/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s %s\n' % (self.ip, self.command, fmt % args))

    @property
    def ip(self):
        xff = self.headers.get('X-Forwarded-For', '')
        return (xff.split(',')[0].strip() if xff else self.headers.get('X-Real-IP') or self.client_address[0])[:64]

    def set_cookie(self, token, clear=False):
        attrs = ['Path=/os/', 'HttpOnly', 'SameSite=Lax'] + (['Secure'] if CFG['secure_cookie'] else [])
        attrs.append('Max-Age=0' if clear else f"Max-Age={CFG['session_hours'] * 3600}")
        self._cookie_out = f'{COOKIE}={token}; ' + '; '.join(attrs)

    def _send(self, status, ctype, body, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        if getattr(self, '_cookie_out', None): self.send_header('Set-Cookie', self._cookie_out)
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)

    def _dev_static(self, path):
        root = pathlib.Path(CFG['dev_static']).resolve()
        target = (root / path.lstrip('/')).resolve()
        if target.is_dir(): target = target / 'index.html'
        if root not in target.parents or not target.is_file(): return self._send(404, 'text/plain', b'not found')
        types = {'.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml', '.js': 'text/javascript', '.css': 'text/css', '.jpg': 'image/jpeg', '.png': 'image/png', '.json': 'application/json'}
        return self._send(200, types.get(target.suffix, 'application/octet-stream'), target.read_bytes())

    def _json(self, status, obj): self._send(status, 'application/json; charset=utf-8', json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def handle_any(self):
        self._cookie_out = None
        path = urlparse(self.path).path
        if self.command in ('GET', 'HEAD') and path in ('/os', '/os/', '/os/index.html'):
            if path == '/os': return self._send(301, 'text/plain', b'', {'Location': '/os/'})
            html = APP_HTML.read_bytes()
            return self._send(200, 'text/html; charset=utf-8', html, {'Content-Security-Policy': "frame-ancestors 'none'"})
        if CFG['dev_static'] and self.command in ('GET', 'HEAD') and not path.startswith('/os/'):
            return self._dev_static(path)   # local testing only; in production nginx serves the website
        for method, rx, auth, fn in ROUTES:
            mt = rx.match(path)
            if not mt or method != self.command: continue
            try:
                self.body = {}
                if self.command in ('POST', 'PATCH', 'DELETE'):
                    # cross-site requests cannot add this custom header without a CORS preflight, which we never allow
                    need(self.headers.get('X-ARIC') == '1', 403, 'missing request header')
                    n = int(self.headers.get('Content-Length') or 0); need(n <= 256_000, 413, 'request too large')
                    raw = self.rfile.read(n) if n else b''
                    self.body = json.loads(raw or b'{}'); need(isinstance(self.body, dict), 400, 'expected a JSON object')
                c = db()
                try:
                    me = None
                    self.cookie = None
                    for part in (self.headers.get('Cookie') or '').split(';'):
                        k, _, v = part.strip().partition('=')
                        if k == COOKIE: self.cookie = v
                    if self.cookie:
                        s = row(c, 'SELECT person_id FROM sessions WHERE token_hash=? AND expires>?', (sha(self.cookie), time.time()))
                        if s: me = row(c, 'SELECT * FROM people WHERE id=?', (s['person_id'],))
                    if auth: need(me, 401, 'please sign in')
                    if auth == 'admin': need(is_admin(me), 403, 'admin only')
                    out = fn(self, c, me, mt)
                    c.commit()
                finally:
                    c.close()
                if isinstance(out, tuple):
                    ctype, data, fname = out
                    return self._send(200, ctype, data, {'Content-Disposition': f'attachment; filename="{fname}"'})
                return self._json(200, out)
            except HttpError as e:
                return self._json(e.status, {'error': e.msg})
            except (ValueError, KeyError, TypeError) as e:
                return self._json(400, {'error': 'bad request'})
            except Exception as e:   # pragma: no cover
                sys.stderr.write(f'ERROR {self.command} {path}: {e!r}\n')
                return self._json(500, {'error': 'server error'})
        return self._json(404, {'error': 'not found'})

    do_GET = do_POST = do_PATCH = do_DELETE = do_HEAD = handle_any

# ---------------------------------------------------------------- CLI extras
def backup(dest):
    d = pathlib.Path(dest); d.mkdir(parents=True, exist_ok=True)
    out = d / f"aric-{dt.datetime.now():%Y%m%d-%H%M}.db"
    src = sqlite3.connect(CFG['db']); dst = sqlite3.connect(out)
    with dst: src.backup(dst)
    src.close(); dst.close()
    for old in sorted(d.glob('aric-*.db'))[:-14]: old.unlink()
    print('backup written', out)

def seed_initial():
    """ARIC's real starting data (2026-11-20). Runs only on an empty database."""
    init_db()
    with db() as c:
        if c.execute('SELECT COUNT(*) FROM boards').fetchone()[0]:
            print('database already has boards; nothing seeded'); return
        def person(email, name, name_th, kind, track, status, sid='', title='', color=''):
            cur = c.execute('INSERT OR IGNORE INTO people(email,name,name_th,kind,title,track,sid,status,start,end_,color,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (email, name, name_th, kind, title, track, sid, status, '2027-01-04' if kind == 'intern' else '', '2027-04-23' if kind == 'intern' else '', color, now()))
            return row(c, 'SELECT id FROM people WHERE email=?', (email,))['id']
        nacha = person('nacha.cho@mfu.ac.th', 'Nacha Chondamrongkul', 'รศ.ดร.นชา ชลดำรงค์กุล', 'staff', '', 'active', title='Director / Dean', color='#6E0B1C')
        wan = person('6631503037@lamduan.mfu.ac.th', 'Wantana Suwannapho', '', 'intern', 'AI & Software', 'confirmed', '6631503037', color='#B7791F')
        nap = person('6631503016@lamduan.mfu.ac.th', 'Napus Samuanpho', 'ณภัทร เสมือนโพธิ์', 'intern', 'AI & Software', 'confirmed', '6631503016', color='#2E7D5B')
        nay = person('6631503079@lamduan.mfu.ac.th', 'Nay Lin Htet', '', 'intern', 'AI & Software', 'confirmed', '6631503079', color='#6B5B95')
        moe = person('6631503128@lamduan.mfu.ac.th', 'Moe Mya Myintzu', '', 'intern', 'Design & Communications', 'confirmed', '6631503128', color='#C8102E')
        pha = person('6631503115@lamduan.mfu.ac.th', 'Phattarapon Kakad', 'ภัทรพล ก๋ากาศ', 'intern', 'AI & Software', 'offered', '6631503115', color='#8A5A2B')
        for pid in (wan, nap, nay, moe, pha): c.execute('UPDATE people SET supervisor_id=? WHERE id=?', (nacha, pid))
        def board(name, desc, members):
            bid = c.execute('INSERT INTO boards(name,description,created_at) VALUES(?,?,?)', (name, desc, now())).lastrowid
            for pid in members: c.execute('INSERT OR IGNORE INTO board_members VALUES(?,?)', (bid, pid))
            return bid
        spark = board('Spark รอบที่ 1', 'โจทย์จากพันธมิตร → Spark Hackathon รอบแรก', [nacha])
        ops = board('งานบริหารศูนย์', 'งานบริหาร การรับนักศึกษาฝึกงาน และเอกสาร', [nacha])
        interns = board('โปรเจกต์นักศึกษาฝึกงาน 2027', 'งานของนักศึกษาฝึกงานรอบ ม.ค.–เม.ย. 2570', [nacha, wan, nap, nay, moe])
        web = board('เว็บไซต์และ ARIC OS', 'เว็บไซต์ aric.mfu.ac.th และระบบหลังบ้าน', [nacha])
        for i, (bid, st, title, who, due, pri) in enumerate([
            (ops, 'doing', 'ส่งแบบตอบรับ CWIE (นักศึกษา 4 คน) ไปที่ Internship@mfu.ac.th', nacha, '2026-11-20', 'h'),
            (ops, 'todo', 'รอคำตอบ Phattarapon (ภายใน 20 พ.ย.)', nacha, '2026-11-20', 'm'),
            (ops, 'backlog', 'หาผู้มาแทนตำแหน่ง Robotics & Systems', nacha, '2026-11-30', 'm'),
            (ops, 'todo', 'เตรียมบัญชีเครื่อง GPU ให้นักศึกษาฝึกงาน', nacha, '2026-12-28', 'm'),
            (ops, 'todo', 'วาระวันปฐมนิเทศ 4 ม.ค. 2570', nacha, '2026-12-20', 'm'),
            (spark, 'todo', 'ส่งแบบฟอร์มสำรวจโจทย์ให้ภาคอุตสาหกรรม เจ้าของกิจการ และศิษย์เก่า', nacha, '2026-11-30', 'h'),
            (spark, 'backlog', 'คัดเลือก 10 โจทย์สำหรับ Spark รอบที่ 1', nacha, '2026-12-15', 'h'),
            (web, 'doing', 'เปิดใช้ ARIC OS รุ่นแรก', nacha, '2026-11-30', 'h'),
            (web, 'todo', 'เพิ่ม aric.mfu.ac.th ใน Firebase Authorized domains', nacha, '2026-11-21', 'h'),
            (interns, 'todo', 'สัปดาห์แรก: ติดตั้งเครื่องมือพัฒนาและเข้าถึง GitHub', wan, '2027-01-08', 'm')]):
            c.execute('INSERT INTO tasks(board_id,title,status,assignee_id,due,priority,ord,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (bid, title, st, who, due, pri, i, nacha, now(), now()))
        for pid in (wan, nap, nay, moe): c.execute('INSERT OR IGNORE INTO onboarding VALUES(?,?,?,?,?)', (pid, 'o1', 1, now(), nacha))
        c.execute('INSERT OR IGNORE INTO onboarding VALUES(?,?,?,?,?)', (nap, 'o3', 1, now(), nacha))   # Napus filed his MIIS request on 13 Nov
        audit(c, nacha, 'seed', 'system', None, 'initial data')
    print('seeded initial data')

def watch_self():
    me = pathlib.Path(__file__); m0 = me.stat().st_mtime
    while True:
        time.sleep(15)
        try:
            if me.stat().st_mtime != m0:
                sys.stderr.write('server.py changed; exiting so systemd restarts the new version\n'); os._exit(0)
        except FileNotFoundError: pass

def main():
    if '--backup' in sys.argv: return backup(sys.argv[sys.argv.index('--backup') + 1])
    if '--seed-initial' in sys.argv: return seed_initial()
    init_db()
    if CFG['autoreload']: threading.Thread(target=watch_self, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((CFG['host'], CFG['port']), Handler)
    srv.daemon_threads = True
    print(f"ARIC OS on http://{CFG['host']}:{CFG['port']}/os/  db={CFG['db']}  dev_login={CFG['dev_login']}", flush=True)
    srv.serve_forever()

if __name__ == '__main__':
    main()
