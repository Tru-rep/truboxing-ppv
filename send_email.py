# send_email.py — FINAL BREVO API VERSION (CLEANED)
import os
import sys
from dotenv import load_dotenv
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# Load environment variables
load_dotenv()

# Validate critical environment variables
required_env = ["FROM_EMAIL", "BREVO_API_KEY"]
for var in required_env:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")

# Main email sending function
def send_watch_link(to_email, stream_link):
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    subject = "🎥 Akses Truboxing Anda"
    sender = {"name": "Truboxing PPV", "email": os.getenv("FROM_EMAIL")}
    html_content = f"""
    <h2>🎟 Link tontonan anda:</h2>
    <p><a href="{stream_link}">Klik sini untuk tonton siaran</a></p>
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
        print(f"❌ API error sending email to {to_email}: {e}")

# Allow standalone terminal test
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 send_email.py email stream_link")
    else:
        send_watch_link(sys.argv[1], sys.argv[2])
