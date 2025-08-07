from datetime import datetime, timedelta
from dateutil import parser
import pytz

# Global timezone for the whole app
MALAYSIA_TZ = pytz.timezone("Asia/Kuala_Lumpur")

def get_now():
    """Return current datetime in Malaysia timezone (aware)."""
    return datetime.now(MALAYSIA_TZ)

def generate_expiry(days=1):
    """Return future expiry time in ISO format with Malaysia timezone."""
    return (get_now() + timedelta(days=days)).isoformat()

def parse_expiry(expiry_str):
    """Parse ISO datetime string into Malaysia-aware datetime."""
    return parser.isoparse(expiry_str).astimezone(MALAYSIA_TZ)
