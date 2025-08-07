# ========================
# app.py — Full Production with Device Lock 2.1 + MUX ✅
# ========================

import os
import uuid
import json
import requests
import hashlib
from flask import Flask, request, redirect, render_template, jsonify
from dotenv import load_dotenv
from timezone_utils import generate_expiry, parse_expiry, get_now

import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# MUX imports
import mux_python
from mux_python.rest import ApiException as MuxApiException
from mux_python.configuration import Configuration

# Load environment variables
load_dotenv()
app = Flask(__name__)

REQUIRED_ENV = ["TOYYIB_KEY", "CATEGORY_CODE", "FROM_EMAIL", "BREVO_API_KEY", "MUX_TOKEN_ID", "MUX_TOKEN_SECRET"]
for var in REQUIRED_ENV:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")

TOYYIB_KEY = os.getenv('TOYYIB_KEY')
CATEGORY_CODE = os.getenv('CATEGORY_CODE')
CALLBACK_URL = 'https://watch.truboxing.co/payment-callback'
RETURN_URL = 'https://watch.truboxing.co/generate-token'

# MUX Setup
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
mux_config = Configuration()
mux_config.username = MUX_TOKEN_ID
mux_config.password = MUX_TOKEN_SECRET
mux_api = mux_python.ApiClient(mux_config)
live_api = mux_python.LiveStreamsApi(mux_api)

# ========================
# Email sending (Brevo API)
# ========================
def send_watch_link(to_email, stream_link):
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    subject = "🎥 Akses Truboxing Anda"
    sender = {"name": "Truboxing PPV", "email": os.getenv("FROM_EMAIL")}
    html_content = f"""
    <h2>🎟 Link tontonan anda:</h2>
    <p><a href="{stream_link}">{stream_link}</a></p>
    <p><b>⚠ Gunakan hanya di 2 peranti.</b></p>
    """

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to_email}],
        html_content=html_content,
        subject=subject,
        sender=sender
    )

    try:
        api_response = api_instance.send_transac_email(send_smtp_email)
        print(f"✅ Email sent to {to_email}: Message ID {api_response.message_id}")
    except ApiException as e:
        print(f"❌ Email failed to {to_email}: {e}")

# ========================
# Payment Pages
# ========================
@app.route('/')
def home():
    return render_template('payment_form.html')

@app.route('/initiate-payment', methods=['POST'])
def initiate_payment():
    customer_name = request.form.get('name')
    customer_email = request.form.get('email')
    if not customer_name or not customer_email:
        return "❌ Please provide both name and email.", 400

    payload = {
        'userSecretKey': TOYYIB_KEY,
        'categoryCode': CATEGORY_CODE,
        'billName': 'Truboxing PPV Ticket',
        'billDescription': 'Livestream access to Truboxing event',
        'billPriceSetting': 1,
        'billPayorInfo': 1,
        'billAmount': 790,
        'billReturnUrl': f"{RETURN_URL}?email={customer_email}",
        'billCallbackUrl': CALLBACK_URL,
        'billExternalReferenceNo': f"TRX-{customer_email}",
        'billTo': customer_name,
        'billEmail': customer_email,
        'billPhone': '601118808511',
        'billPaymentChannel': '2',
        'billContentEmail': 'Thank you for supporting Truboxing!',
        'billChargeToCustomer': 1,
        'billExpiryDays': 3
    }

    try:
        res = requests.post("https://dev.toyyibpay.com/index.php/api/createBill", data=payload)
        res.raise_for_status()
        response_json = res.json()
        if isinstance(response_json, list) and "BillCode" in response_json[0]:
            bill_code = response_json[0]["BillCode"]
            return redirect(f"https://dev.toyyibpay.com/{bill_code}")
        else:
            return f"❌ Unexpected API response: {response_json}", 500
    except Exception as e:
        return f"❌ Error creating bill: {str(e)}", 500

@app.route('/generate-token')
def generate_token_redirect():
    return render_template("thank_you.html")

# ========================
# Device Lock 2.1 Fingerprinting
# ========================
@app.route('/verify', methods=['POST', 'GET'])
def verify():
    token = request.args.get('v')
    if not token:
        return "❌ No token provided", 400

    try:
        with open('access_db.json', 'r+') as f:
            db = json.load(f)
            if token not in db:
                return "❌ Invalid token", 403

            info = db[token]
            expires_at = parse_expiry(info.get("expires_at"))
            now = get_now()
            if now > expires_at:
                return "❌ Token expired.", 403

            devices = info.get('devices', [])

            cookie_identity = request.args.get('id')
            if cookie_identity:
                if cookie_identity in devices:
                    return jsonify({"status": "ok"}), 200
                else:
                    return "🚫 Token locked to another device.", 403

            fingerprint = request.json
            user_ip = request.headers.get('X-Real-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr
            raw_identity = f"{user_ip}_{fingerprint['userAgent']}_{fingerprint['screenSize']}_{fingerprint['timezone']}"
            identity = hashlib.sha256(raw_identity.encode()).hexdigest()

            if identity not in devices:
                if devices:
                    return "🚫 Token locked to another device.", 403
                devices.append(identity)
                db[token]['devices'] = devices
                db[token]['device_info'] = {
                    "ip": user_ip,
                    "userAgent": fingerprint['userAgent'],
                    "screenSize": fingerprint['screenSize'],
                    "timezone": fingerprint['timezone'],
                    "identity": identity,
                    "timestamp": str(now)
                }
                f.seek(0)
                f.truncate()
                json.dump(db, f, indent=2)

            return jsonify({"status": "ok", "identity": identity}), 200

    except Exception as e:
        return f"⚠ Server error: {str(e)}", 500

# ========================
# Watch Route — Load correct MUX playback URL
# ========================
@app.route('/watch')
def watch():
    token = request.args.get('v')
    if not token:
        return "❌ No token provided", 400

    with open('access_db.json', 'r') as f:
        db = json.load(f)

    if token not in db:
        return "❌ Invalid token", 403

    info = db[token]
    playback_id = info.get("playback_id")
    playback_url = f"https://stream.mux.com/{playback_id}.m3u8"
    
    return render_template("watch.html", playback_url=playback_url)

# ========================
# Payment Callback — Generate token + Create MUX stream automatically
# ========================
@app.route('/payment-callback', methods=['POST'])
def payment_callback():
    data = request.form.to_dict()
    status = data.get('status')
    order_id = data.get('order_id')
    email = order_id[4:] if order_id and order_id.startswith("TRX-") else None

    if status == "1" and email:
        token = str(uuid.uuid4())[:8]

        try:
            # ✅ MUX API: Create live stream automatically
            create_request = mux_python.CreateLiveStreamRequest(
                playback_policy=["public"],
                new_asset_settings=mux_python.CreateAssetRequest(playback_policy=["public"])
            )
            live_stream = live_api.create_live_stream(create_request)

            stream_key = live_stream.stream_key
            playback_id = live_stream.playback_ids[0].id

            # ✅ Store token with full stream info
            with open('access_db.json', 'r+') as f:
                try: db = json.load(f)
                except json.JSONDecodeError: db = {}

                db[token] = {
                    "email": email,
                    "devices": [],
                    "expires_at": generate_expiry(days=7),
                    "playback_id": playback_id,
                    "stream_key": stream_key
                }
                f.seek(0)
                f.truncate()
                json.dump(db, f, indent=2)

            # ✅ Store email-to-token mapping
            try:
                with open('email_to_token.json', 'r+') as f:
                    try: mapping = json.load(f)
                    except json.JSONDecodeError: mapping = {}
                    mapping[email] = token
                    f.seek(0)
                    f.truncate()
                    json.dump(mapping, f, indent=2)
            except Exception as e:
                print(f"⚠ Failed email_to_token.json: {e}")

            stream_link = f"https://watch.truboxing.co/watch?v={token}"
            send_watch_link(email, stream_link)

            print(f"✅ Token + Mux stream created for {email}: {token}")
            return "✅ Token + Stream Created", 200

        except Exception as e:
            return f"❌ Error during Mux stream creation: {str(e)}", 500

    else:
        return "❌ Payment not successful or email missing", 400

# ========================
# Run Local Dev
# ========================
if __name__ == "__main__":
    app.run(debug=True)

