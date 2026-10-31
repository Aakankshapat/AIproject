"""SQLite data layer: fast (WAL, indexes, batched inserts) and dependency-free."""
import hashlib
import hmac
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "portal.db"
_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
    pw_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','client')),
    active INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS tests(
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
    duration_min INTEGER DEFAULT 30, pass_pct REAL DEFAULT 50,
    max_violations INTEGER DEFAULT 3, proctored INTEGER DEFAULT 1,
    published INTEGER DEFAULT 0, shuffle INTEGER DEFAULT 1, created_at REAL,
    category TEXT DEFAULT 'General', difficulty TEXT DEFAULT 'Beginner',
    neg_mark REAL DEFAULT 0, attempts_allowed INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS questions(
    id INTEGER PRIMARY KEY, test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    text TEXT NOT NULL, a TEXT, b TEXT, c TEXT, d TEXT,
    answer TEXT NOT NULL CHECK(answer IN ('A','B','C','D')), marks REAL DEFAULT 1);
CREATE TABLE IF NOT EXISTS attempts(
    id INTEGER PRIMARY KEY, test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at REAL, finished_at REAL, status TEXT DEFAULT 'in_progress',
    score REAL DEFAULT 0, total REAL DEFAULT 0, avg_focus REAL DEFAULT 0, violations INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS answers(
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL, choice TEXT, correct INTEGER DEFAULT 0,
    PRIMARY KEY(attempt_id, question_id));
CREATE TABLE IF NOT EXISTS focus_logs(
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    ts REAL, score REAL, face REAL, eyes REAL, posture REAL, faces INTEGER);
CREATE TABLE IF NOT EXISTS violations(
    id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS i_q_test ON questions(test_id);
CREATE INDEX IF NOT EXISTS i_a_user ON attempts(user_id);
CREATE INDEX IF NOT EXISTS i_a_test ON attempts(test_id);
CREATE INDEX IF NOT EXISTS i_f_att ON focus_logs(attempt_id);
CREATE INDEX IF NOT EXISTS i_v_att ON violations(attempt_id);
"""

DEFAULT_SETTINGS = {
    "allow_signup": "1",
    "low_focus_threshold": "35",   # score below this counts as "low focus"
    "low_focus_seconds": "8",      # ...for this many seconds -> violation
    "no_face_seconds": "4",        # no face for this long -> violation
    "violation_cooldown": "10",    # seconds between repeated violations of same kind
    "flag_integrity_below": "50",  # admin flag threshold (avg focus)
    "show_answers": "0",           # clients may review answers after submit
    "sensitivity": "0.65",         # focus engine sensitivity (0.1 - 1.0)
}


def conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _seed(_conn)
        return _conn


def hash_pw(password, salt=None):
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return f"{salt}${digest}"


def check_pw(password, stored):
    try:
        salt, _ = stored.split("$")
        return hmac.compare_digest(hash_pw(password, salt), stored)
    except ValueError:
        return False


NEW_TEST_COLS = {"category": "TEXT DEFAULT 'General'", "difficulty": "TEXT DEFAULT 'Beginner'",
                 "neg_mark": "REAL DEFAULT 0", "attempts_allowed": "INTEGER DEFAULT 1"}


def _migrate(c):
    have = {r["name"] for r in c.execute("PRAGMA table_info(tests)")}
    for col, ddl in NEW_TEST_COLS.items():
        if col not in have:
            c.execute(f"ALTER TABLE tests ADD COLUMN {col} {ddl}")
    if not c.execute("SELECT 1 FROM settings WHERE key='v2_strikes'").fetchone():
        c.execute("UPDATE tests SET max_violations=3 WHERE max_violations>3")  # 3-strike rule
        c.execute("INSERT INTO settings VALUES('v2_strikes','1')")


def _seed(c):
    from seed_data import TESTS
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        now = time.time()
        c.execute("INSERT INTO users(username,full_name,pw_hash,role,created_at) VALUES(?,?,?,?,?)",
                  ("admin", "Administrator", hash_pw(os.getenv("ADMIN_PASSWORD", "admin123")), "admin", now))
        c.execute("INSERT INTO users(username,full_name,pw_hash,role,created_at) VALUES(?,?,?,?,?)",
                  ("client", "Demo Client", hash_pw("client123"), "client", now))
    for t in TESTS:  # pre-built tests: added once, matched by title (never overwrites admin edits)
        if c.execute("SELECT 1 FROM tests WHERE title=?", (t["title"],)).fetchone():
            continue
        c.execute("INSERT INTO tests(title,description,duration_min,pass_pct,max_violations,published,created_at,"
                  "category,difficulty,neg_mark,attempts_allowed) VALUES(?,?,?,?,3,1,?,?,?,?,2)",
                  (t["title"], t["description"], t["duration_min"], t["pass_pct"], time.time(),
                   t["category"], t["difficulty"], t["neg_mark"]))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        rows_ = []
        for i, (text, right, *wrong) in enumerate(t["questions"]):
            opts = [right, *wrong]
            random.Random(f"{t['title']}{i}").shuffle(opts)
            rows_.append((tid, text, *opts, "ABCD"[opts.index(right)]))
        c.executemany("INSERT INTO questions(test_id,text,a,b,c,d,answer,marks) VALUES(?,?,?,?,?,?,?,1)", rows_)


# ---------- login throttling (in-process, resets on restart) ----------
_fails = {}
MAX_FAILS, LOCK_SECONDS = 5, 60


def lock_remaining(username):
    n, until = _fails.get(username.strip().lower(), (0, 0))
    return max(0, int(until - time.time())) if n >= MAX_FAILS else 0


# ---------- generic helpers ----------
def run(sql, params=()):
    with _lock:
        cur = conn().execute(sql, params)
        return cur.lastrowid


def many(sql, rows):
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            c.executemany(sql, rows)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def rows(sql, params=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


def one(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None


def df(sql, params=()):
    return pd.DataFrame(rows(sql, params))


# ---------- settings ----------
def get_settings():
    s = {r["key"]: r["value"] for r in rows("SELECT * FROM settings")}
    return {**DEFAULT_SETTINGS, **s}


def set_setting(key, value):
    run("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------- users ----------
def authenticate(username, password, role=None):
    """Returns the user, or None. Locks a username for 60s after 5 failures."""
    key = username.strip().lower()
    if lock_remaining(key):
        return None
    u = one("SELECT * FROM users WHERE username=? AND active=1" + (" AND role=?" if role else ""),
            (key, role) if role else (key,))
    if u and check_pw(password, u["pw_hash"]):
        _fails.pop(key, None)
        return u
    n, _ = _fails.get(key, (0, 0))
    _fails[key] = (n + 1, time.time() + LOCK_SECONDS)
    return None


def create_user(username, full_name, password, role="client"):
    username = username.strip().lower()
    if len(username) < 3 or len(password) < 6 or not full_name.strip():
        raise ValueError("Username ≥3 chars, password ≥6 chars and a full name are required.")
    if one("SELECT 1 FROM users WHERE username=?", (username,)):
        raise ValueError("That username is already taken.")
    return run("INSERT INTO users(username,full_name,pw_hash,role,created_at) VALUES(?,?,?,?,?)",
               (username, full_name.strip(), hash_pw(password), role, time.time()))


def set_password(user_id, password):
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    run("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(password), user_id))


# ---------- tests / attempts ----------
def save_answer(attempt_id, question_id, choice):
    run("INSERT OR REPLACE INTO answers(attempt_id,question_id,choice,correct) VALUES(?,?,?,0)",
        (attempt_id, question_id, choice))


def saved_answers(attempt_id):
    return {r["question_id"]: r["choice"] for r in rows("SELECT question_id,choice FROM answers WHERE attempt_id=?", (attempt_id,))}


def grade_attempt(attempt_id, status="completed"):
    """Grade from saved answers (supports negative marking), store aggregates, close the attempt."""
    att = one("SELECT a.*, t.neg_mark FROM attempts a JOIN tests t ON t.id=a.test_id WHERE a.id=?", (attempt_id,))
    if not att or att["status"] != "in_progress":
        return att
    given = saved_answers(attempt_id)
    score = total = 0.0
    batch = []
    for q in rows("SELECT id,answer,marks FROM questions WHERE test_id=?", (att["test_id"],)):
        choice = given.get(q["id"])
        ok = int(choice == q["answer"])
        score += q["marks"] if ok else (-q["marks"] * att["neg_mark"] if choice else 0)
        total += q["marks"]
        batch.append((attempt_id, q["id"], choice, ok))
    score = max(0.0, score)
    many("INSERT OR REPLACE INTO answers(attempt_id,question_id,choice,correct) VALUES(?,?,?,?)", batch)
    avg_focus = one("SELECT COALESCE(AVG(score),0) a FROM focus_logs WHERE attempt_id=?", (attempt_id,))["a"]
    nviol = one("SELECT COUNT(*) n FROM violations WHERE attempt_id=?", (attempt_id,))["n"]
    run("UPDATE attempts SET status=?, finished_at=?, score=?, total=?, avg_focus=?, violations=? WHERE id=?",
        (status, time.time(), score, total, avg_focus, nviol, attempt_id))
    return one("SELECT * FROM attempts WHERE id=?", (attempt_id,))


def rank_of(attempt_id):
    """(rank, participants) of this attempt's user among best scores on the same test."""
    a = one("SELECT test_id, user_id FROM attempts WHERE id=?", (attempt_id,))
    r = one("""WITH best AS (SELECT user_id, MAX(score*100.0/NULLIF(total,0)) p FROM attempts
                             WHERE test_id=? AND status!='in_progress' GROUP BY user_id)
               SELECT (SELECT COUNT(*)+1 FROM best WHERE p > COALESCE((SELECT p FROM best WHERE user_id=?),0)) rank,
                      (SELECT COUNT(*) FROM best) n""", (a["test_id"], a["user_id"]))
    return r["rank"], r["n"]


def leaderboard(test_id, limit=10):
    return df("""SELECT u.full_name AS client, ROUND(MAX(a.score*100.0/NULLIF(a.total,0)),1) AS best_pct,
                        ROUND(AVG(a.avg_focus)) AS avg_focus, COUNT(*) AS attempts
                 FROM attempts a JOIN users u ON u.id=a.user_id
                 WHERE a.test_id=? AND a.status!='in_progress' AND a.total>0
                 GROUP BY u.id ORDER BY best_pct DESC, avg_focus DESC LIMIT ?""", (test_id, limit))


def start_attempt(test_id, user_id):
    return run("INSERT INTO attempts(test_id,user_id,started_at,status) VALUES(?,?,?, 'in_progress')",
               (test_id, user_id, time.time()))
