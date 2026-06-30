"""
WhatsApp notifications via Twilio's REST API.
Uses plain urllib (no twilio SDK dependency) — same approach as the Google
OAuth integration in web.py.

Each app-user saves their own WhatsApp number in the dashboard; when one of
their monitored accounts posts something new, a message is sent to that
number. Twilio credentials (shared, app-level) live in .env.
"""

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request

import config

logger = logging.getLogger(__name__)

_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def is_configured() -> bool:
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN)


def _normalize_number(number: str) -> str:
    """Twilio WhatsApp numbers need a 'whatsapp:' prefix and a leading '+'."""
    number = number.strip()
    if number.startswith("whatsapp:"):
        return number
    if not number.startswith("+"):
        number = "+" + number.lstrip("0")
    return f"whatsapp:{number}"


def send_whatsapp(to_number: str, message: str) -> bool:
    """
    Send *message* to *to_number* via Twilio WhatsApp.
    Returns False (and logs) on any failure — never raises, so a broken
    notification never crashes the monitoring loop.
    """
    if not is_configured():
        return False
    if not to_number or not to_number.strip():
        return False

    url = _API_URL.format(sid=config.TWILIO_ACCOUNT_SID)
    data = urllib.parse.urlencode({
        "From": config.TWILIO_WHATSAPP_FROM,
        "To":   _normalize_number(to_number),
        "Body": message,
    }).encode()

    creds = base64.b64encode(
        f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()
    ).decode()

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True
            logger.warning("Twilio WhatsApp send failed: status=%s", resp.status)
            return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:300]
        logger.warning("Twilio WhatsApp send failed: %s — %s", exc, body)
        return False
    except Exception as exc:
        logger.warning("Twilio WhatsApp send failed: %s", exc)
        return False
