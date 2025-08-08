# ========================
# app.py — Best Version (SQLite + Device Lock 2 + ToyyibPay Sandbox + Optional MUX/Email + Admin Auth)
# ========================

import os
import uuid
import hashlib
import sqlite3
import requests
from datetime import datetime
from flask import Flask, request, redirect, render_template, jsonify, abort
from dotenv import load_dotenv
from timezone_utils import generate_expiry, parse_expiry, get_now

# Optional email (Brevo)
try:
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException as BrevoApiException
except Exception:
    sib_api_v3_sdk = None
    BrevoApiException = Exception

# Optional MUX
try:
    import mux_python
    from mux_python.rest import ApiException as MuxApiException
    from mux_python.configuration import Configuration as MuxConfiguration
except Exception:
    mux_python = None
    MuxApiException = Exception
    MuxConfiguration = None

load_dotenv()
app = Flask(__name__)

# -------- Temp logging (remove later) --------
@app.before_request
def _log_req():
    try:
        print(f"➡ {request.method} {request.path} qs={dict(request.args)}")
    except Exception:
        pass

# ======================== ENV / Config ========================
DB_PATH = os.getenv("DB_PATH", "tokens.db")

# Base URL for your deployed app (Render)
BASE_URL = os.getenv("BASE_URL", "https://truboxing-ppv.onrender.com")

# ToyyibPay (KEEP sandbox)
TOYYIB_KEY = os.getenv("TOYYIB_KEY", "")
CATEGORY_CODE = os.getenv("CATEGORY_CODE", "")
TOYYIB_BASE = os.getenv("TOYYIB_BASE", "https://dev.toyyibpay.com")  # prod: https://toyyibpay.com

# Return/Callback (default to BASE_URL if not explicitly set)
RETURN_URL = os.getenv("RETURN_URL", f"{BASE_URL}/generate-token")
CALLBACK_URL = os.getenv("CALLBACK_URL", f"{BASE_URL}/payment-callback")

# Email (optional)
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")

# MUX (optional)
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID", "")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET", "")
FIXED_PLAYBACK_ID = os.getenv("FIXED_PLAYBACK_ID", "")  # If set, we won't create per-buyer streams

# Admin auth (simple)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # set a random strong token; required for admin endpoints

# ======================== DB Setup ========================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                email TEXT,
                expires_at TEXT,
                playback_id TEXT,
                stream_key TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS device_access (
                token TEXT,
                device_hash TEXT,
                ip TEXT,
                user_agent TEXT,
                screen_size TEXT,
                timezone TEXT,
                timestamp TEXT,
                UNIQUE(token, device_hash)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS email_tokens (
                email TEXT PRIMARY KEY,
                token TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_device_token ON device_access(token)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email)")
        conn.commit()

init_db()

# ======================== Utilities ========================
def admin_guard():
    if not ADMIN_TOKEN:
        abort(403, description="Admin disabled: missing ADMIN_TOKEN")
    hdr = request.headers.get("X-Admin-Token", "")
    if hdr != ADMIN_TOKEN:
        abort(403, description="Forbidden")

def send_watch_link(email: str, link: str):
    """Optional Brevo email."""
    if not (BREVO_API_KEY and FROM_EMAIL and sib_api_v3_sdk):
        print("ℹ Email disabled or Brevo SDK missing. Skipping email send.")
        return
    try:
        cfg = sib_api_v3_sdk.Configuration()
        cfg.api_key['api-key'] = BREVO_API_KEY
        api = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(cfg))
        html_content = f"""
        <h2>🎟 Link tontonan anda</h2>
        <p><a href="{link}">{link}</a></p>
        <p><b>⚠️ Akses terhad kepada 2 peranti sahaja.</b></p>
        """
        payload = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": email}],
            html_content=html_content,
            subject="🎥 Akses Truboxing Anda",
            sender={"name": "Truboxing PPV", "email": FROM_EMAIL}
        )
        resp = api.send_transac_email(payload)
        print(f"✅ Email sent to {email}: Message ID {getattr(resp, 'message_id', 'n/a')}")
    except BrevoApiException as e:
        print(f"❌ Email failed to {email}: {e}")

def create_mux_stream_if_needed():
    """Return (playback_id, stream_key or None). Uses fixed playback if provided."""
    if FIXED_PLAYBACK_ID:
        return FIXED_PLAYBACK_ID, None
    if not (MUX_TOKEN_ID and MUX_TOKEN_SECRET and mux_python and MuxConfiguration):
        return "abc123", None  # placeholder/fallback

    cfg = MuxConfiguration()
    cfg.username = MUX_TOKEN_ID
    cfg.password = MUX_TOKEN_SECRET
    client = mux_python.ApiClient(cfg)
    live_api = mux_python.LiveStreamsApi(client)
    try:
        req = mux_python.CreateLiveStreamRequest(
            playback_policy=["public"],
            new_asset_settings=mux_python.CreateAssetRequest(playback_policy=["public"])
        )
        live_stream = live_api.create_live_stream(req)
        playback_id = live_stream.playback_ids[0].id
        stream_key = live_stream.stream_key
        return playback_id, stream_key
    except MuxApiException as e:
        print(f"❌ MUX error: {e}")
        return "abc123", None

def safe_get(d, key, default=""):
    try:
        return (d or {}).get(key, default) or default
    except Exception:
        return default

# ======================== Routes: Payment ========================
@app.route("/")
def home():
    return render_template("payment_form.html")

@app.route("/initiate-payment", methods=["POST"])
def initiate_payment():
    customer_name = request.form.get("name", "").strip()
    customer_email = request.form.get("email", "").strip()

    if not customer_name or not customer_email:
        return "❌ Please fill in both name and email", 400
    if not (TOYYIB_KEY and CATEGORY_CODE and TOYYIB_BASE):
        return "❌ Payment not configured (ToyyibPay ENV missing).", 500

    payload = {
        "userSecretKey": TOYYIB_KEY,
        "categoryCode": CATEGORY_CODE,
        "billName": "Truboxing PPV Ticket",
        "billDescription": "Livestream access to Truboxing event",
        "billPriceSetting": 1,
        "billPayorInfo": 1,
        "billAmount": 790,  # RM7.90 (ToyyibPay expects cents)
        "billReturnUrl": f"{RETURN_URL}?email={customer_email}",
        "billCallbackUrl": CALLBACK_URL,
        "billExternalReferenceNo": f"TRX-{customer_email}",
        "billTo": customer_name,
        "billEmail": customer_email,
        "billPhone": "601118808511",
        "billPaymentChannel": "2",
        "billContentEmail": "Thank you for supporting Truboxing!",
        "billChargeToCustomer": 1,
        "billExpiryDays": 3
    }

    try:
        res = requests.post(f"{TOYYIB_BASE}/index.php/api/createBill", data=payload, timeout=30)
        res.raise_for_status()
        js = res.json()
        if isinstance(js, list) and js and "BillCode" in js[0]:
            bill_code = js[0]["BillCode"]
            return redirect(f"{TOYYIB_BASE}/{bill_code}")
        return f"❌ Unexpected API response: {js}", 500
    except Exception as e:
        return f"❌ Error creating bill: {str(e)}", 500

# “Instant redirect” page – if we can map email -> token, jump straight to /watch/<token>
@app.route("/generate-token", methods=["GET"])
def generate_token_redirect():
    email = request.args.get("email", "").strip()
    if not email:
        return render_template("thank_you.html")
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT token FROM email_tokens WHERE email = ?", (email,))
        row = c.fetchone()
        if row and row[0]:
            return redirect(f"/watch/{row[0]}")
    return render_template("thank_you.html")

# ToyyibPay callback – can be POST or GET; may use `status` or `status_id`
@app.route("/payment-callback", methods=["POST", "GET"])
def payment_callback():
    data = request.form.to_dict() if request.method == "POST" else request.args.to_dict()
    status = data.get("status") or data.get("status_id")
    order_id = data.get("order_id")
    email = order_id[4:] if order_id and order_id.startswith("TRX-") else None

    if status != "1" or not email:
        print(f"🔎 Bad callback payload: method={request.method} data={data}")
        return "❌ Payment not successful or email missing", 400

    token = uuid.uuid4().hex[:8]
    expires_at = generate_expiry(days=7)
    playback_id, stream_key = create_mux_stream_if_needed()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO tokens (token, email, expires_at, playback_id, stream_key) VALUES (?, ?, ?, ?, ?)",
            (token, email, expires_at, playback_id, stream_key)
        )
        c.execute("INSERT OR REPLACE INTO email_tokens (email, token) VALUES (?, ?)", (email, token))
        conn.commit()

    watch_link = f"{request.url_root.rstrip('/')}/watch/{token}"
    send_watch_link(email, watch_link)
    print(f"✅ Callback OK for {email}: token={token}, playback_id={playback_id}")
    return "OK", 200

# ======================== Watch + Verify ========================
@app.route("/watch/<token>")
def watch(token):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT token, email, expires_at, playback_id FROM tokens WHERE token = ?", (token,))
        row = c.fetchone()
        if not row:
            return "❌ Invalid token", 403
        expires_at_str = row[2]
        if get_now() > parse_expiry(expires_at_str):
            return "❌ Token expired", 403
        playback_id = row[3] or "abc123"
    playback_url = f"https://stream.mux.com/{playback_id}.m3u8"
    return render_template("watch.html", playback_url=playback_url, token=token)

# Supports:
# - GET: quick re-check with ?v=<token>&id=<device_hash>
# - POST: fingerprints a new/returning device (JSON: userAgent, screenSize, timezone)
@app.route("/verify", methods=["GET", "POST"])
def verify():
    token = request.args.get("v", "").strip()
    if not token:
        return "❌ No token provided", 400

    if request.method == "GET":
        identity = request.args.get("id", "").strip()
        if not identity:
            return "❌ Missing device id", 400
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM tokens WHERE token = ?", (token,))
            if not c.fetchone():
                return "❌ Invalid token", 403
            c.execute("SELECT 1 FROM device_access WHERE token = ? AND device_hash = ?", (token, identity))
            if c.fetchone():
                return jsonify({"status": "ok"}), 200
        return "🚫 Token locked to another device.", 403

    # POST
    fp = request.get_json(silent=True) or {}
    ua = safe_get(fp, "userAgent")
    ss = safe_get(fp, "screenSize")
    tz = safe_get(fp, "timezone")
    user_ip = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For") or request.remote_addr or ""
    raw = f"{user_ip}|{ua}|{ss}|{tz}"
    device_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = str(get_now())

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # Validate token + expiry
        c.execute("SELECT expires_at FROM tokens WHERE token = ?", (token,))
        row = c.fetchone()
        if not row:
            return "❌ Invalid token", 403
        if get_now() > parse_expiry(row[0]):
            return "❌ Token expired", 403

        # If same device already recorded -> OK (no extra slot)
        c.execute("SELECT 1 FROM device_access WHERE token = ? AND device_hash = ?", (token, device_hash))
        if c.fetchone():
            return jsonify({"status": "ok", "identity": device_hash}), 200

        # Count distinct devices
        c.execute("SELECT COUNT(DISTINCT device_hash) FROM device_access WHERE token = ?", (token,))
        used = c.fetchone()[0] or 0
        if used >= 2:
            return "🚫 Token locked to another device.", 403

        # Insert new device
        c.execute(
            "INSERT OR IGNORE INTO device_access (token, device_hash, ip, user_agent, screen_size, timezone, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, device_hash, user_ip, ua, ss, tz, now)
        )
        conn.commit()

    return jsonify({"status": "ok", "identity": device_hash}), 200

# ======================== Admin (secured) ========================
@app.route("/admin/logs")
def admin_logs():
    admin_guard()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT token, device_hash, ip, user_agent, screen_size, timezone, timestamp FROM device_access ORDER BY timestamp DESC")
        rows = c.fetchall()
    return render_template("admin_logs.html", logs=rows)

@app.route("/admin/kick", methods=["POST"])
def admin_kick():
    admin_guard()
    token = request.form.get("token", "").strip()
    device = request.form.get("device", "").strip()
    if not token or not device:
        return "❌ token/device required", 400
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM device_access WHERE token = ? AND device_hash = ?", (token, device))
        conn.commit()
    return "✅ Device kicked"

@app.route("/admin/add-device", methods=["POST"])
def admin_add_device():
    admin_guard()
    token = request.form.get("token", "").strip()
    device = request.form.get("device", "").strip()
    if not token or not device:
        return "❌ token/device required", 400
    now = str(get_now())
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT device_hash) FROM device_access WHERE token = ?", (token,))
        used = c.fetchone()[0] or 0
        if used >= 2:
            return "🚫 Already at 2 devices.", 403
        c.execute(
            "INSERT OR IGNORE INTO device_access (token, device_hash, ip, user_agent, screen_size, timezone, timestamp) VALUES (?, ?, 'admin', '', '', '', ?)",
            (token, device, now)
        )
        conn.commit()
    return "✅ Device added"

# Simple health
@app.route("/healthz")
def healthz():
    return jsonify(ok=True, time=str(datetime.utcnow()))

# ======================== Run ========================
if __name__ == "__main__":
    # For prod: use gunicorn, e.g. gunicorn -w 2 -k gthread -t 120 app:app
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("DEBUG", "false").lower() == "true")
