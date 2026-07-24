"""Safaricom Daraja C2B.

Customers pay the Paybill and type the invoice number as the account. Safaricom
POSTs the payment to our confirmation URL; we store it and try to match it to
that invoice. Nothing here ever trusts the browser — the money is only ever
recorded from Safaricom's own callback.
"""
import base64
import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.utils import timezone

log = logging.getLogger("mpesa")

SANDBOX = "https://sandbox.safaricom.co.ke"
LIVE = "https://api.safaricom.co.ke"


def config():
    return {
        "key": os.environ.get("MPESA_CONSUMER_KEY", ""),
        "secret": os.environ.get("MPESA_CONSUMER_SECRET", ""),
        "shortcode": os.environ.get("MPESA_SHORTCODE", ""),
        "base": LIVE if os.environ.get("MPESA_ENV", "sandbox") == "live" else SANDBOX,
    }


def access_token():
    """OAuth token. Valid an hour; we just fetch one per call, which is fine
    at this volume and avoids stale-token bugs."""
    c = config()
    if not (c["key"] and c["secret"]):
        raise RuntimeError("MPESA_CONSUMER_KEY / MPESA_CONSUMER_SECRET not set")
    auth = base64.b64encode(f"{c['key']}:{c['secret']}".encode()).decode()
    r = requests.get(
        f"{c['base']}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def register_urls(confirmation_url, validation_url, response_type="Completed"):
    """One-off: tell Safaricom where to send payments for this shortcode."""
    c = config()
    if not c["shortcode"]:
        raise RuntimeError("MPESA_SHORTCODE not set")
    r = requests.post(
        f"{c['base']}/mpesa/c2b/v1/registerurl",
        headers={"Authorization": f"Bearer {access_token()}"},
        json={
            "ShortCode": c["shortcode"],
            "ResponseType": response_type,   # Completed = accept if we don't answer
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url,
        }, timeout=30)
    return r.status_code, r.text


# --- parsing Safaricom's payload -------------------------------------------

def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_time(raw):
    """Safaricom sends yyyyMMddHHmmss in East Africa Time."""
    if not raw:
        return None
    try:
        naive = datetime.strptime(str(raw), "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None
    return timezone.make_aware(naive, timezone.get_current_timezone())


def parse_confirmation(payload):
    """Pull the fields we care about out of a C2B confirmation."""
    name = " ".join(
        str(payload.get(k, "")).strip()
        for k in ("FirstName", "MiddleName", "LastName")
    ).strip()
    name = " ".join(name.split())
    return {
        "trans_id": str(payload.get("TransID", "")).strip(),
        "trans_time": _parse_time(payload.get("TransTime")),
        "amount": _decimal(payload.get("TransAmount")),
        "msisdn": str(payload.get("MSISDN", "")).strip(),
        "bill_ref": str(payload.get("BillRefNumber", "")).strip(),
        "payer_name": name,
        "short_code": str(payload.get("BusinessShortCode", "")).strip(),
    }


def normalise_ref(ref):
    """Customers type invoice numbers loosely: 'sal 2026 0042', 'SAL-2026-0042',
    or just '42'. Strip it to something comparable."""
    return "".join(ch for ch in str(ref or "").upper() if ch.isalnum())