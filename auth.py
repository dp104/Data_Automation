"""SaaS accounts: email/password users (admin or subscriber), sessions, password hashing.
Stored in cache/saas.db (own file - kept separate from the price-history db in projects.py)."""
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("UNISCRAPE_DATA_DIR") or HERE
DB = os.path.join(DATA_DIR, "cache", "saas.db")
_db_lock = threading.Lock()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_COOKIE = "uniscrape_sid"
SESSION_TTL = 30 * 24 * 3600  # 30 days
# Render (and any host behind HTTPS) should set this so session cookies carry Secure - never set it
# for a plain-HTTP deployment, since the browser would then silently refuse to send the cookie back.
SECURE_COOKIES = os.environ.get("UNISCRAPE_SECURE_COOKIES") == "1"

_sessions = {}  # token -> {"user_id", "created"}
_sessions_lock = threading.Lock()


OTP_TTL_SECONDS = 600       # 10 minutes
OTP_RESEND_COOLDOWN = 30    # seconds between resend requests
OTP_MAX_ATTEMPTS = 5


def _ensure_column(con, table, col, decl):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        con.commit()


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""create table if not exists users (
        id text primary key, email text unique not null, password_hash text not null,
        name text, role text not null default 'subscriber', created_at text not null, last_login text)""")
    # existing accounts predate email verification and are grandfathered in as verified
    _ensure_column(con, "users", "verified", "integer not null default 1")
    con.execute("""create table if not exists verifications (
        email text not null, purpose text not null, otp_hash text not null,
        expires_at text not null, attempts integer not null default 0, created_at text not null,
        primary key (email, purpose))""")
    return con


# ---------------------------------------------------------------- passwords
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return f"{salt}${h.hex()}"


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


def normalize_email(email):
    return (email or "").strip().lower()


# ---------------------------------------------------------------- users
def get_user(user_id):
    with _db_lock:
        con = db()
        row = con.execute("select * from users where id=?", (user_id,)).fetchone()
        con.close()
    return dict(row) if row else None


def get_user_by_email(email):
    with _db_lock:
        con = db()
        row = con.execute("select * from users where email=?", (normalize_email(email),)).fetchone()
        con.close()
    return dict(row) if row else None


def list_users():
    with _db_lock:
        con = db()
        rows = con.execute("select * from users order by created_at desc").fetchall()
        con.close()
    return [dict(r) for r in rows]


def create_user(email, password, name="", role="subscriber", verified=True):
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    if get_user_by_email(email):
        raise ValueError("An account with this email already exists.")
    uid = secrets.token_hex(8)
    with _db_lock:
        con = db()
        con.execute("insert into users (id,email,password_hash,name,role,created_at,verified) values (?,?,?,?,?,?,?)",
                    (uid, email, hash_password(password), (name or "").strip()[:80], role,
                     time.strftime("%Y-%m-%d %H:%M:%S"), int(bool(verified))))
        con.commit()
        con.close()
    return get_user(uid)


def set_password(user_id, new_password):
    if len(new_password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    with _db_lock:
        con = db()
        con.execute("update users set password_hash=? where id=?", (hash_password(new_password), user_id))
        con.commit()
        con.close()


def mark_verified(user_id):
    with _db_lock:
        con = db()
        con.execute("update users set verified=1 where id=?", (user_id,))
        con.commit()
        con.close()


def authenticate(email, password):
    user = get_user_by_email(email)
    if not user or not verify_password(password or "", user["password_hash"]):
        return None
    with _db_lock:
        con = db()
        con.execute("update users set last_login=? where id=?", (time.strftime("%Y-%m-%d %H:%M:%S"), user["id"]))
        con.commit()
        con.close()
    return user


def ensure_admin(email, password):
    """Create the bootstrap admin account the first time the app runs (no-op once any admin exists)."""
    with _db_lock:
        con = db()
        has_admin = con.execute("select 1 from users where role='admin' limit 1").fetchone()
        con.close()
    if has_admin:
        return None
    email = normalize_email(email)
    existing = get_user_by_email(email)
    if existing:
        with _db_lock:
            con = db()
            con.execute("update users set role='admin' where id=?", (existing["id"],))
            con.commit()
            con.close()
        return get_user(existing["id"])
    return create_user(email, password, name="Admin", role="admin")


# ---------------------------------------------------------------- sessions (in-memory; reset on restart)
def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = {"user_id": user_id, "created": time.time()}
    return token


def destroy_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


def _cookie_value(cookie_header, name):
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k == name:
                return v
    return None


def user_from_cookie(cookie_header):
    token = _cookie_value(cookie_header, SESSION_COOKIE)
    if not token:
        return None
    with _sessions_lock:
        s = _sessions.get(token)
        if s and time.time() - s["created"] > SESSION_TTL:
            del _sessions[token]
            s = None
        if not s:
            return None
        user_id = s["user_id"]
    return get_user(user_id)


def session_cookie_header(token):
    secure = "; Secure" if SECURE_COOKIES else ""
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}{secure}"


def clear_cookie_header():
    secure = "; Secure" if SECURE_COOKIES else ""
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"


def session_token_from_headers(headers):
    return _cookie_value(headers.get("Cookie"), SESSION_COOKIE)


# ---------------------------------------------------------------- OTP verification (signup + password reset)
def create_verification(email, purpose, resend=False):
    """Generate and store a fresh 6-digit OTP for (email, purpose), replacing any earlier one.
    Returns the raw code (caller is responsible for delivering it - see notify.py). Raises
    ValueError if this is a resend requested before the cooldown has passed."""
    email = normalize_email(email)
    now = time.time()
    with _db_lock:
        con = db()
        if resend:
            row = con.execute("select created_at from verifications where email=? and purpose=?",
                               (email, purpose)).fetchone()
            if row:
                created = time.mktime(time.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S"))
                wait = OTP_RESEND_COOLDOWN - (now - created)
                if wait > 0:
                    con.close()
                    raise ValueError(f"Please wait {int(wait) + 1}s before requesting another code.")
        otp = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + OTP_TTL_SECONDS))
        created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        con.execute("""insert into verifications (email,purpose,otp_hash,expires_at,attempts,created_at)
                       values (?,?,?,?,0,?)
                       on conflict(email,purpose) do update set otp_hash=excluded.otp_hash,
                       expires_at=excluded.expires_at, attempts=0, created_at=excluded.created_at""",
                    (email, purpose, hash_password(otp), expires_at, created_at))
        con.commit()
        con.close()
    return otp


def check_verification(email, purpose, otp):
    """Verify a submitted code without consuming it. Returns 'ok', 'not_found', 'expired',
    'too_many' (attempts exhausted - request a new code) or 'invalid'."""
    email = normalize_email(email)
    with _db_lock:
        con = db()
        row = con.execute("select * from verifications where email=? and purpose=?", (email, purpose)).fetchone()
        if not row:
            con.close()
            return "not_found"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if now > row["expires_at"]:
            con.execute("delete from verifications where email=? and purpose=?", (email, purpose))
            con.commit()
            con.close()
            return "expired"
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            con.close()
            return "too_many"
        if not verify_password((otp or "").strip(), row["otp_hash"]):
            con.execute("update verifications set attempts=attempts+1 where email=? and purpose=?", (email, purpose))
            con.commit()
            con.close()
            return "invalid"
        con.close()
    return "ok"


def consume_verification(email, purpose):
    with _db_lock:
        con = db()
        con.execute("delete from verifications where email=? and purpose=?", (normalize_email(email), purpose))
        con.commit()
        con.close()
