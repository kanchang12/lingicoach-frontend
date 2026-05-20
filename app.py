import os, uuid, time, httpx, hmac, hashlib
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from google import genai
from google.genai import types
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

app = Flask(__name__)
CORS(app)

# ── ENV ────────────────────────────────────────────────────────────────────
GEMINI_KEY        = os.environ.get("GEMINI_API_KEY", "")
LSQ_API_KEY       = os.environ.get("LSQ_API_KEY", "")
LSQ_WEBHOOK_SECRET= os.environ.get("LSQ_WEBHOOK_SECRET", "")
LSQ_MONTHLY_VARIANT= os.environ.get("LSQ_MONTHLY_VARIANT", "")
LSQ_YEARLY_VARIANT = os.environ.get("LSQ_YEARLY_VARIANT", "")
LSQ_MONTHLY_PRICE  = os.environ.get("LSQ_MONTHLY_PRICE", "3.99")
LSQ_YEARLY_PRICE   = os.environ.get("LSQ_YEARLY_PRICE", "29.99")
FRONTEND_URL      = os.environ.get("FRONTEND_URL", "http://localhost:5000")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON     = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE  = os.environ.get("SUPABASE_SERVICE_KEY", "")

gemini  = genai.Client(api_key=GEMINI_KEY)
MODEL = "gemini-3.5-flash"

sessions     = {}
rate_limits  = defaultdict(list)
SESSION_TTL  = 1800

LANGUAGES = {
    "bn":"Bengali","hi":"Hindi","ta":"Tamil","te":"Telugu","mr":"Marathi",
    "ur":"Urdu","ar":"Arabic","sw":"Swahili","es":"Spanish","pt":"Portuguese",
    "fr":"French","id":"Indonesian","tr":"Turkish","vi":"Vietnamese","th":"Thai",
    "ms":"Malay","tl":"Filipino","zh":"Chinese","ja":"Japanese","ko":"Korean",
    "de":"German","it":"Italian","nl":"Dutch","pl":"Polish",
}

# ── Supabase DB helpers ────────────────────────────────────────────────────
def sb(method, path, body=None, token=None):
    key   = token or SUPABASE_SERVICE
    hdrs  = {"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {key}",
              "Content-Type": "application/json", "Prefer": "return=representation"}
    url   = f"{SUPABASE_URL}/rest/v1{path}"
    r = httpx.request(method, url, headers=hdrs, json=body, timeout=10)
    return r.json() if r.text else {}

def sb_auth(method, path, body=None):
    hdrs = {"apikey": SUPABASE_ANON, "Content-Type": "application/json"}
    url  = f"{SUPABASE_URL}/auth/v1{path}"
    r    = httpx.request(method, url, headers=hdrs, json=body, timeout=10)
    return r.json(), r.status_code

def get_user(token):
    """Verify Supabase JWT and return user object."""
    hdrs = {"apikey": SUPABASE_ANON, "Authorization": f"Bearer {token}"}
    r    = httpx.get(f"{SUPABASE_URL}/auth/v1/user", headers=hdrs, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            return jsonify({"error": "Unauthorised"}), 401
        user = get_user(token)
        if not user:
            return jsonify({"error": "Invalid token"}), 401
        request.user = user
        request.token = token
        return f(*args, **kwargs)
    return decorated

def get_stats(user_id):
    rows = sb("GET", f"/user_stats?user_id=eq.{user_id}&select=*")
    return rows[0] if rows else {}

def upsert_stats(user_id, data):
    data["user_id"] = user_id
    sb("POST", "/user_stats", data)

def get_progress(user_id):
    rows = sb("GET", f"/user_progress?user_id=eq.{user_id}&done=eq.true&select=scenario_id")
    return {r["scenario_id"] for r in (rows or [])}

def mark_done(user_id, scenario_id):
    sb("POST", "/user_progress", {"user_id": user_id, "scenario_id": scenario_id, "done": True})

def set_premium(user_id, plan):
    days   = 366 if plan == "yearly" else 32
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    sb("POST", "/user_stats", {"user_id": user_id, "is_premium": True,
                                "premium_expires_at": expiry},)

def is_premium(user_id):
    s = get_stats(user_id)
    if not s.get("is_premium"):
        return False
    exp = s.get("premium_expires_at")
    if exp and datetime.fromisoformat(exp) < datetime.now(timezone.utc):
        return False
    return True

# ── Rate limiting ──────────────────────────────────────────────────────────
def rate_ok(ip):
    now = time.time()
    rate_limits[ip] = [t for t in rate_limits[ip] if now - t < 60]
    if len(rate_limits[ip]) >= 40:
        return False
    rate_limits[ip].append(now)
    return True

def clean_sessions():
    now = time.time()
    dead = [k for k, v in sessions.items() if now - v["last"] > SESSION_TTL]
    for k in dead:
        del sessions[k]

# ── Prompts ────────────────────────────────────────────────────────────────
def learn_prompt(native, title, role, situation):
    return f"""You are Lingi Coach, a warm and patient AI English speaking coach.

## EU AI ACT ARTICLE 50 — MANDATORY
You are an AI (Google Gemini). Never claim to be human.

## YOUR ROLE
Playing: {role}
Scenario: {title}
Situation: {situation}

## LANGUAGE RULES
- All instructions, corrections, encouragement: in {native}.
- Roleplay dialogue: English only (as the {role} would speak in real life).
- After each student attempt: step out of character briefly, correct in {native}, then continue roleplay in English.

## FLOW
1. Set the scene in {native} — two sentences max.
2. Start roleplay immediately as {role}, speaking English.
3. Student responds:
   - Correct → brief praise in {native}, continue.
   - Almost correct → name the error in {native}, write **correct version in bold**, continue.
   - Wrong → correct in {native}, write **correct version in bold**, encourage, continue.
4. Keep replies under 80 words. No lectures.

## PRIVACY
Never ask for personal information."""

def translate_prompt(from_lang, to_lang):
    return f"You are a professional interpreter. Translate between {from_lang} and {to_lang}. Return ONLY the translation, no labels or preamble."

# ── Static / PWA ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

@app.route("/sw.js")
def sw():
    return send_from_directory("static", "sw.js")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL})

@app.route("/config")
def config():
    return jsonify({
        "monthly_price": LSQ_MONTHLY_PRICE,
        "yearly_price":  LSQ_YEARLY_PRICE,
    })

# ── Auth routes (proxy to Supabase) ───────────────────────────────────────
@app.route("/auth/signup", methods=["POST"])
def signup():
    data = request.json
    result, code = sb_auth("POST", "/signup", {
        "email": data.get("email"),
        "password": data.get("password"),
        "data": {"full_name": data.get("name", "")}
    })
    if code not in (200, 201):
        return jsonify({"error": result.get("msg", result.get("message", "Signup failed"))}), 400
    # Create user_stats row
    if result.get("user"):
        uid = result["user"]["id"]
        sb("POST", "/user_stats", {"user_id": uid, "streak": 0})
    return jsonify(result), code

@app.route("/auth/login", methods=["POST"])
def login():
    data = request.json
    result, code = sb_auth("POST", "/token?grant_type=password", {
        "email": data.get("email"),
        "password": data.get("password"),
    })
    if code != 200:
        return jsonify({"error": result.get("msg", result.get("message", "Login failed"))}), 400
    return jsonify(result), 200

@app.route("/auth/forgot", methods=["POST"])
def forgot():
    data = request.json
    sb_auth("POST", "/recover", {"email": data.get("email")})
    return jsonify({"sent": True})

# ── User data ──────────────────────────────────────────────────────────────
@app.route("/user/stats")
@require_auth
def user_stats():
    uid   = request.user["id"]
    stats = get_stats(uid)
    prog  = get_progress(uid)
    pm    = is_premium(uid)
    return jsonify({
        "streak":     stats.get("streak", 0),
        "done":       len(prog),
        "progress":   list(prog),
        "is_premium": pm,
        "language":   stats.get("native_language"),
        "consent":    stats.get("consent_given", False),
    })

@app.route("/user/consent", methods=["POST"])
@require_auth
def set_consent():
    upsert_stats(request.user["id"], {"consent_given": True})
    return jsonify({"ok": True})

@app.route("/user/language", methods=["POST"])
@require_auth
def set_language():
    lang = request.json.get("language")
    if lang not in LANGUAGES:
        return jsonify({"error": "Unsupported language"}), 400
    upsert_stats(request.user["id"], {"native_language": lang})
    return jsonify({"ok": True})

@app.route("/user/streak", methods=["POST"])
@require_auth
def update_streak():
    uid   = request.user["id"]
    stats = get_stats(uid)
    today = datetime.now(timezone.utc).date().isoformat()
    last  = stats.get("last_activity")
    yest  = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    streak = stats.get("streak", 0)
    if last == today:
        pass
    elif last == yest:
        streak += 1
    else:
        streak = 1
    upsert_stats(uid, {"streak": streak, "last_activity": today})
    return jsonify({"streak": streak})

@app.route("/user/progress", methods=["POST"])
@require_auth
def save_progress():
    uid = request.user["id"]
    sid = request.json.get("scenario_id")
    if sid:
        mark_done(uid, sid)
    return jsonify({"ok": True})

# ── Chat session ───────────────────────────────────────────────────────────
@app.route("/session/start", methods=["POST"])
@require_auth
def start_session():
    if not rate_ok(request.remote_addr):
        return jsonify({"error": "Too many requests"}), 429
    data = request.json
    lang_code = data.get("native_language_code", "")
    if lang_code not in LANGUAGES:
        return jsonify({"error": "Unsupported language"}), 400
    clean_sessions()
    native = LANGUAGES[lang_code]
    sid    = str(uuid.uuid4())
    prompt = learn_prompt(native, data["scenario_title"], data["ai_role"], data["situation"])
    try:
        chat = gemini.chats.create(
            model=MODEL,
            config=types.GenerateContentConfig(system_instruction=prompt),
        )
        resp = chat.send_message("Begin the session now.")
        sessions[sid] = {"chat": chat, "last": time.time(), "count": 1}
        return jsonify({"session_id": sid, "message": resp.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/session/chat", methods=["POST"])
@require_auth
def chat_message():
    if not rate_ok(request.remote_addr):
        return jsonify({"error": "Too many requests"}), 429
    data = request.json
    sid  = data.get("session_id", "")
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404
    s = sessions[sid]
    if time.time() - s["last"] > SESSION_TTL:
        del sessions[sid]
        return jsonify({"error": "Session expired"}), 410
    msg = (data.get("message") or "").strip()
    if not msg or len(msg) > 1000:
        return jsonify({"error": "Invalid message"}), 400
    try:
        resp = s["chat"].send_message(msg)
        s["last"] = time.time()
        s["count"] += 1
        return jsonify({"session_id": sid, "message": resp.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/session/<sid>", methods=["DELETE"])
@require_auth
def delete_session(sid):
    sessions.pop(sid, None)
    return jsonify({"deleted": True})

# ── Translate ──────────────────────────────────────────────────────────────
@app.route("/translate", methods=["POST"])
@require_auth
def translate():
    if not rate_ok(request.remote_addr):
        return jsonify({"error": "Too many requests"}), 429
    data      = request.json
    from_lang = LANGUAGES.get(data.get("from_lang_code", ""), data.get("from_lang_code", ""))
    to_lang   = LANGUAGES.get(data.get("to_lang_code", ""), data.get("to_lang_code", ""))
    try:
        resp = gemini.models.generate_content(
            model=MODEL,
            contents=data["text"],
            config=types.GenerateContentConfig(
                system_instruction=translate_prompt(from_lang, to_lang)
            ),
        )
        return jsonify({"translation": resp.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Payment (LemonSqueezy) ────────────────────────────────────────────────
COUPONS = {
    "JUDGE2026": {"discount": 100, "plan": "yearly"},  # 100% free — judges
}

@app.route("/payment/coupon", methods=["POST"])
@require_auth
def apply_coupon():
    code = (request.json.get("code") or "").strip().upper()
    if code not in COUPONS:
        return jsonify({"error": "Invalid or expired coupon code."}), 400
    c = COUPONS[code]
    if c["discount"] == 100:
        set_premium(request.user["id"], c["plan"])
        log_event("/payment/coupon", f"✅ OK — {code}", request.user.get("email","")[:20])
        return jsonify({"premium": True, "message": "Premium activated! Enjoy Lingi Coach."})
    return jsonify({"discount": c["discount"]})

@app.route("/payment/checkout", methods=["POST"])
@require_auth
def create_checkout():
    data      = request.json
    plan      = data.get("plan", "monthly")
    variant   = LSQ_YEARLY_VARIANT if plan == "yearly" else LSQ_MONTHLY_VARIANT
    user_id   = request.user["id"]
    email     = request.user.get("email", "")
    app_url   = os.environ.get("FRONTEND_URL", request.host_url.rstrip("/"))
    try:
        resp = httpx.post(
            "https://api.lemonsqueezy.com/v1/checkouts",
            headers={"Authorization": f"Bearer {LSQ_API_KEY}", "Accept": "application/vnd.api+json",
                     "Content-Type": "application/vnd.api+json"},
            json={"data": {"type": "checkouts", "attributes": {
                "checkout_data": {"email": email, "custom": {"user_id": user_id, "plan": plan}},
                "product_options": {"redirect_url": f"{app_url}/?payment=success",
                                    "receipt_link_url": f"{app_url}/?payment=success"},
                "checkout_options": {"button_color": "#1B7E8C"}
            }, "relationships": {
                "store":   {"data": {"type": "stores",   "id": os.environ.get("LSQ_STORE_ID","1")}},
                "variant": {"data": {"type": "variants", "id": variant}}
            }}},
            timeout=15
        )
        resp.raise_for_status()
        url = resp.json()["data"]["attributes"]["url"]
        return jsonify({"checkout_url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/payment/webhook", methods=["POST"])
def lsq_webhook():
    payload   = request.get_data()
    sig       = request.headers.get("X-Signature", "")
    expected  = hmac.new(LSQ_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return jsonify({"error": "Invalid signature"}), 400
    event = request.json
    if event.get("meta", {}).get("event_name") == "order_created":
        custom  = event.get("meta", {}).get("custom_data", {})
        user_id = custom.get("user_id")
        plan    = custom.get("plan", "monthly")
        if user_id:
            set_premium(user_id, plan)
            log_event("/payment/webhook", f"✅ OK — order_created plan={plan}", user_id[:8])
    return jsonify({"received": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)


# ── ADMIN DASHBOARD ────────────────────────────────────────────────────────
import json
from datetime import datetime, timezone

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme-admin-key")

# In-memory logs (last 200 events)
execution_log = []
api_counters  = {"gemini_calls": 0, "translate_calls": 0, "sessions_started": 0,
                 "sessions_active": 0, "errors": 0}

def log_event(event_type, detail, user_id=None):
    execution_log.append({
        "ts":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "type": event_type,
        "detail": detail,
        "user": user_id or "anon"
    })
    if len(execution_log) > 200:
        execution_log.pop(0)

# Patch start_session and chat_message to emit logs
_orig_start  = app.view_functions.get("start_session")
_orig_chat   = app.view_functions.get("chat_message")
_orig_trans  = app.view_functions.get("translate")

@app.before_request
def track_request():
    if request.path == "/session/start" and request.method == "POST":
        api_counters["sessions_started"] += 1
        api_counters["gemini_calls"] += 1
    elif request.path == "/session/chat" and request.method == "POST":
        api_counters["gemini_calls"] += 1
    elif request.path == "/translate" and request.method == "POST":
        api_counters["translate_calls"] += 1
        api_counters["gemini_calls"] += 1

@app.after_request
def log_response(resp):
    if request.method in ("POST", "DELETE") and any(
        request.path.startswith(p) for p in ["/session", "/translate", "/auth", "/payment", "/user"]
    ):
        status = "✅ OK" if resp.status_code < 400 else f"❌ {resp.status_code}"
        uid = None
        if hasattr(request, "user"):
            uid = request.user.get("email") or request.user.get("id","")[:8]
        log_event(request.path, status, uid)
        if resp.status_code >= 400:
            api_counters["errors"] += 1
    api_counters["sessions_active"] = len(sessions)
    return resp

@app.route("/admin")
def admin_dashboard():
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return "Unauthorised", 401
    return render_template("admin.html", key=key)

@app.route("/admin/data")
def admin_data():
    if request.args.get("key", "") != ADMIN_KEY:
        return jsonify({"error": "Unauthorised"}), 401

    # Get user counts from Supabase
    total_users  = 0
    premium_users = 0
    total_done   = 0
    try:
        rows = sb("GET", "/user_stats?select=is_premium,streak")
        if isinstance(rows, list):
            total_users   = len(rows)
            premium_users = sum(1 for r in rows if r.get("is_premium"))
    except Exception:
        pass
    try:
        prog = sb("GET", "/user_progress?select=id&done=eq.true")
        if isinstance(prog, list):
            total_done = len(prog)
    except Exception:
        pass

    return jsonify({
        "counters": {**api_counters, "total_users": total_users,
                     "premium_users": premium_users, "scenarios_completed": total_done},
        "sessions": [
            {"id": k[:8]+"...", "lang": v.get("lang","?"),
             "msgs": v.get("count",0),
             "age_s": int(time.time() - v["last"])}
            for k, v in sessions.items()
        ],
        "log": list(reversed(execution_log[-50:])),
        "uptime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    })
