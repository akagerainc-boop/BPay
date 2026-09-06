"""Airtel Money Collections API client — "Request to Pay".

Built against Airtel's publicly documented OpenAPI Collections product:
https://developers.airtel.africa/documentation

Not tested against a live sandbox from this codebase — the status codes
below ("TS"/"TF"/...) are Airtel's documented values as of this writing,
but are worth double-checking against a real response once credentials
are in place.
"""

import time

import requests

_token_cache = {}


class AirtelError(Exception):
    pass


def _base_url(config):
    return (config.get("base_url") or "https://openapiuat.airtel.africa").rstrip("/")


def _get_token(config):
    cache_key = config.get("client_id")
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 30:
        return cached[0]

    response = requests.post(
        f"{_base_url(config)}/auth/oauth2/token",
        json={
            "client_id": config.get("client_id"),
            "client_secret": config.get("client_secret"),
            "grant_type": "client_credentials",
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if response.status_code != 200:
        raise AirtelError(
            f"Airtel Money login failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise AirtelError("Airtel Money login returned no access_token")
    _token_cache[cache_key] = (token, time.time() + int(data.get("expires_in", 3600)))
    return token


def request_to_pay(config, *, phone, amount, external_id, message):
    token = _get_token(config)
    response = requests.post(
        f"{_base_url(config)}/merchant/v1/payments/",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Country": "RW",
            "X-Currency": "RWF",
            "Content-Type": "application/json",
        },
        json={
            "reference": message,
            "subscriber": {"country": "RW", "currency": "RWF", "msisdn": phone},
            "transaction": {
                "amount": amount,
                "country": "RW",
                "currency": "RWF",
                "id": external_id,
            },
        },
        timeout=20,
    )
    if response.status_code not in (200, 201, 202):
        raise AirtelError(
            f"Airtel Money request-to-pay failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    data = response.json() if response.content else {}
    txn_id = (data.get("data") or {}).get("transaction", {}).get("id") or external_id
    return txn_id


def check_status(config, reference_id):
    token = _get_token(config)
    response = requests.get(
        f"{_base_url(config)}/standard/v1/payments/{reference_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Country": "RW",
            "X-Currency": "RWF",
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise AirtelError(
            f"Airtel Money status check failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    data = response.json()
    status = ((data.get("data") or {}).get("transaction") or {}).get("status", "")
    status = (status or "").upper()
    if status in ("TS", "SUCCESS", "SUCCESSFUL"):
        return "successful", None
    if status in ("TF", "FAILED", "FAILURE"):
        return "failed", "Payment failed or was declined."
    return "pending", None
