"""Subscription plans, Razorpay payments and usage/admin stats for the SaaS layer.
Uses the REST API directly via `requests` (no razorpay SDK dependency) and stores state
in the same cache/saas.db file auth.py uses."""
import hmac
import hashlib
import os
import sqlite3
import threading
import time

import requests

import auth

HERE = os.path.dirname(os.path.abspath(__file__))
DB = auth.DB
_db_lock = threading.Lock()

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "rzp_test_SAMPLEKEYID00")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "SAMPLE_TEST_SECRET_REPLACE_ME")
RAZORPAY_API = "https://api.razorpay.com/v1"

TRIAL_DAYS = int(os.environ.get("UNISCRAPE_TRIAL_DAYS", "1"))
PLAN_PERIOD_DAYS = 30

# Sample pricing (INR, amounts in paise as Razorpay expects) - adjust freely, these are placeholders.
PLANS = {
    "starter": {"name": "Starter", "price": 99900, "currency": "INR",
                "tagline": "For a single recruiter scanning a handful of sites",
                "features": ["Up to 5 saved sites/projects", "University + Any-website modes",
                             "Excel export in your own format", "Email support"]},
    "pro": {"name": "Pro", "price": 299900, "currency": "INR",
            "tagline": "For a small team running scans every week",
            "features": ["Up to 30 saved sites/projects", "Price-history tracking", "Priority crawl speed",
                         "Priority support"]},
    "business": {"name": "Business", "price": 799900, "currency": "INR",
                 "tagline": "For agencies running this across many brands",
                 "features": ["Unlimited saved sites/projects", "All project types", "Dedicated onboarding",
                              "Priority support"]},
}
PLAN_ORDER = ["starter", "pro", "business"]


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""create table if not exists subscriptions (
        user_id text primary key, plan text, status text not null, trial_end text,
        period_start text, period_end text, razorpay_order_id text, razorpay_payment_id text,
        amount integer, currency text, updated_at text not null)""")
    con.execute("""create table if not exists payments (
        id integer primary key autoincrement, user_id text not null, plan text, amount integer,
        currency text, razorpay_order_id text, razorpay_payment_id text, status text, created_at text not null)""")
    con.execute("""create table if not exists usage_events (
        id integer primary key autoincrement, user_id text, action text, detail text, at text not null)""")
    return con


# ---------------------------------------------------------------- subscription lifecycle
def start_trial(user_id):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    trial_end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + TRIAL_DAYS * 86400))
    with _db_lock:
        con = db()
        con.execute("""insert into subscriptions (user_id, plan, status, trial_end, updated_at)
                       values (?,?,?,?,?)
                       on conflict(user_id) do update set plan=excluded.plan, status=excluded.status,
                       trial_end=excluded.trial_end, updated_at=excluded.updated_at""",
                    (user_id, "trial", "trialing", trial_end, now))
        con.commit()
        con.close()


def get_subscription(user_id):
    with _db_lock:
        con = db()
        row = con.execute("select * from subscriptions where user_id=?", (user_id,)).fetchone()
        con.close()
    return dict(row) if row else None


def has_access(user):
    """True if this user may use the scraping tool right now: an admin, or a subscriber
    whose trial/paid period hasn't ended."""
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    sub = get_subscription(user["id"])
    if not sub:
        return False
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if sub["status"] == "trialing":
        return bool(sub["trial_end"]) and now <= sub["trial_end"]
    if sub["status"] == "active":
        return bool(sub["period_end"]) and now <= sub["period_end"]
    return False


# ---------------------------------------------------------------- Razorpay
def create_order(user_id, plan):
    if plan not in PLANS:
        raise ValueError("Unknown plan.")
    p = PLANS[plan]
    receipt = f"{plan}-{user_id}-{int(time.time())}"
    try:
        r = requests.post(f"{RAZORPAY_API}/orders", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                           json={"amount": p["price"], "currency": p["currency"], "receipt": receipt,
                                 "payment_capture": 1, "notes": {"plan": plan, "user_id": user_id}},
                           timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach Razorpay: {e}") from e
    if r.status_code >= 400:
        # with placeholder sample keys this is the expected response until real keys are set
        raise RuntimeError(f"Razorpay rejected the order request ({r.status_code}): {r.text[:300]}")
    order = r.json()
    with _db_lock:
        con = db()
        con.execute("insert into payments (user_id,plan,amount,currency,razorpay_order_id,status,created_at) "
                    "values (?,?,?,?,?,?,?)",
                    (user_id, plan, p["price"], p["currency"], order["id"], "created",
                     time.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        con.close()
    return {"order_id": order["id"], "amount": p["price"], "currency": p["currency"],
            "key_id": RAZORPAY_KEY_ID, "plan": plan, "plan_name": p["name"]}


def verify_payment(user_id, plan, order_id, payment_id, signature):
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(),
                         hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        with _db_lock:
            con = db()
            con.execute("update payments set status='failed', razorpay_payment_id=? where razorpay_order_id=?",
                        (payment_id, order_id))
            con.commit()
            con.close()
        raise ValueError("Payment signature did not match. The payment was not accepted.")
    p = PLANS[plan]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    period_end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + PLAN_PERIOD_DAYS * 86400))
    with _db_lock:
        con = db()
        con.execute("update payments set status='paid', razorpay_payment_id=? where razorpay_order_id=?",
                    (payment_id, order_id))
        con.execute("""insert into subscriptions (user_id, plan, status, period_start, period_end,
                       razorpay_order_id, razorpay_payment_id, amount, currency, updated_at)
                       values (?,?,?,?,?,?,?,?,?,?)
                       on conflict(user_id) do update set plan=excluded.plan, status=excluded.status,
                       period_start=excluded.period_start, period_end=excluded.period_end,
                       razorpay_order_id=excluded.razorpay_order_id, razorpay_payment_id=excluded.razorpay_payment_id,
                       amount=excluded.amount, currency=excluded.currency, updated_at=excluded.updated_at""",
                    (user_id, plan, "active", now, period_end, order_id, payment_id, p["price"], p["currency"], now))
        con.commit()
        con.close()
    return get_subscription(user_id)


# ---------------------------------------------------------------- usage tracking
def log_usage(user_id, action, detail=""):
    with _db_lock:
        con = db()
        con.execute("insert into usage_events (user_id, action, detail, at) values (?,?,?,?)",
                    (user_id, action, detail[:200], time.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        con.close()


# ---------------------------------------------------------------- admin stats
def _last_n_days_counts(con, table, date_col, n=14):
    rows = con.execute(f"select substr({date_col},1,10) d, count(*) c from {table} "
                        f"where {date_col} >= ? group by d", (
                            time.strftime("%Y-%m-%d", time.localtime(time.time() - (n - 1) * 86400)),)).fetchall()
    by_day = {r["d"]: r["c"] for r in rows}
    days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(n - 1, -1, -1)]
    return [{"day": d, "count": by_day.get(d, 0)} for d in days]


def admin_stats():
    users = auth.list_users()
    subscribers = [u for u in users if u["role"] != "admin"]
    with _db_lock:
        con = db()
        subs = {r["user_id"]: dict(r) for r in con.execute("select * from subscriptions").fetchall()}
        paid_total = con.execute("select coalesce(sum(amount),0) t from payments where status='paid'").fetchone()["t"]
        payment_count = con.execute("select count(*) c from payments where status='paid'").fetchone()["c"]
        usage_by_day = _last_n_days_counts(con, "usage_events", "at")
        con.close()
    day_range = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(13, -1, -1)]
    signup_days = {d: 0 for d in day_range}
    for u in users:
        d = (u["created_at"] or "")[:10]
        if d in signup_days:
            signup_days[d] += 1
    signups_by_day = [{"day": d, "count": signup_days[d]} for d in day_range]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    counts = {"trialing": 0, "active": 0, "past_due": 0, "none": 0}
    mrr = 0
    rows = []
    for u in subscribers:
        sub = subs.get(u["id"])
        status = "none"
        plan = None
        expires = None
        if sub:
            plan = sub["plan"]
            if sub["status"] == "trialing" and sub["trial_end"] and now <= sub["trial_end"]:
                status, expires = "trialing", sub["trial_end"]
            elif sub["status"] == "active" and sub["period_end"] and now <= sub["period_end"]:
                status, expires = "active", sub["period_end"]
                mrr += PLANS.get(plan, {}).get("price", 0)
            elif sub["status"] in ("trialing", "active"):
                status = "past_due"  # trial or paid period lapsed
                expires = sub["trial_end"] if sub["status"] == "trialing" else sub["period_end"]
        counts[status] = counts.get(status, 0) + 1
        rows.append({"id": u["id"], "email": u["email"], "name": u["name"], "plan": plan, "status": status,
                     "expires": expires, "created_at": u["created_at"], "last_login": u["last_login"]})
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {
        "total_users": len(users), "total_subscribers": len(subscribers), "admins": len(users) - len(subscribers),
        "status_counts": counts, "mrr_paise": mrr, "revenue_paise": paid_total, "payment_count": payment_count,
        "signups_by_day": signups_by_day, "usage_by_day": usage_by_day, "subscribers": rows,
    }
