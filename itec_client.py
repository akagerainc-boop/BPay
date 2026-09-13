"""ITEC Payment API client — "Request Payment" / "Get Payment Status".

Unlike MTN MoMo and Airtel Money's own developer APIs (momo_client.py /
airtel_client.py), ITEC Payment fronts MTN Mobile Money, Airtel Money and
Spenn behind one endpoint and one API key — so a single client here covers
both networks BPay charges a fee on, once ITEC_API_KEY is configured.

Built directly from ITEC's own documentation (V2 / api2 endpoints). Their
docs only show the "PENDING" status string in worked examples, not a full
enumeration of terminal states — the mapping in check_status is a
defensive best guess at the obvious names (SUCCESSFUL/FAILED and their
common variants) and may need a small adjustment once real transactions
are observed, the same caveat momo_client.py carries for the same reason.
"""

import requests

BASE_URL = "https://pay.itecpay.rw"

_SUCCESS_STATUSES = {"SUCCESSFUL", "SUCCESS", "COMPLETED", "COMPLETE"}
_FAILURE_STATUSES = {
    "FAILED",
    "FAILURE",
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "DECLINED",
    "EXPIRED",
}


class ItecError(Exception):
    pass


def _local_format(phone):
    """ITEC's own docs example a phone as "0798760888" — the local form —
    while every caller here builds MSISDN ("250798760888") for MTN/Airtel's
    own Collections APIs. Forwarding MSISDN to ITEC unchanged is why every
    request here was failing with a generic "Payment request failed" -
    converting back to local form before it's sent is the actual fix."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("250") and len(digits) == 12:
        return "0" + digits[3:]
    return digits


def request_to_pay(config, *, phone, amount, external_id, message):
    """Starts a Request Payment. Returns the reference to track — ITEC has
    no reference of its own to hand back beyond the `req_ref` we send, so
    the caller-generated `external_id` is what's returned and later passed
    to check_status."""
    key = config.get("key")
    if not key:
        raise ItecError("ITEC Payment API key is not configured.")

    response = requests.post(
        f"{BASE_URL}/api2/pay",
        json={
            "amount": amount,
            "phone": _local_format(phone),
            "key": key,
            "req_ref": external_id,
            "note": message,
            "message": message,
        },
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        raise ItecError(
            f"ITEC Payment returned an unreadable response ({response.status_code})."
        )

    if data.get("status") != 200:
        reason = (data.get("data") or {}).get("message") or "Request was rejected."
        raise ItecError(f"ITEC Payment request failed: {reason}")

    return external_id


def check_status(config, reference):
    """Returns (status, reason) where status is 'pending' | 'successful'
    | 'failed'. Raises ItecError on a genuine check failure — the caller
    (get_fee_collection_status) already catches that and reports the
    charge as still pending with the error attached, the same contract
    momo_client.check_status follows."""
    key = config.get("key")
    if not key:
        raise ItecError("ITEC Payment API key is not configured.")

    response = requests.post(
        f"{BASE_URL}/api2/verify",
        json={"action": "status_check", "req_ref": reference, "key": key},
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        raise ItecError(
            f"ITEC Payment status check returned an unreadable response "
            f"({response.status_code})."
        )

    if data.get("status") != 200:
        reason = (data.get("data") or {}).get("message") or "Status check failed."
        raise ItecError(f"ITEC Payment status check failed: {reason}")

    inner = data.get("data") or {}
    status = (inner.get("status") or "").upper()
    if status in _SUCCESS_STATUSES:
        return "successful", None
    if status in _FAILURE_STATUSES:
        return "failed", inner.get("message") or "Payment failed or was declined."
    return "pending", None
