"""MTN MoMo Collections API client — "Request to Pay".

Charges a payer's own MoMo wallet and pays it into the company's MoMo
account tied to the API user configured in the admin dashboard. Built
against MTN's publicly documented Collections API:
https://momodeveloper.mtn.com/api-documentation/api-description/

Not tested against a live sandbox from this codebase — once real
credentials are in place, a status string or header value may need a
small adjustment; the shapes here follow MTN's documented contract.
"""

import base64
import time
import uuid

import requests

# Cached per API user for its declared lifetime, refetched once expired.
# Process-local: fine for a single dev/small-deployment Flask process:
# restarting it just means the next call re-authenticates.
_token_cache = {}


class MomoError(Exception):
    pass


def _base_url(config):
    return (config.get("base_url") or "https://sandbox.momodeveloper.mtn.com").rstrip(
        "/"
    )


def _target_environment(config):
    return config.get("target_environment") or config.get("environment") or "sandbox"


def _get_token(config):
    cache_key = config.get("api_user")
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 30:
        return cached[0]

    basic = base64.b64encode(
        f"{config.get('api_user')}:{config.get('api_key')}".encode()
    ).decode()
    response = requests.post(
        f"{_base_url(config)}/collection/token/",
        headers={
            "Authorization": f"Basic {basic}",
            "Ocp-Apim-Subscription-Key": config.get("subscription_key") or "",
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise MomoError(
            f"MTN MoMo login failed ({response.status_code}): {response.text[:200]}"
        )
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise MomoError("MTN MoMo login returned no access_token")
    _token_cache[cache_key] = (token, time.time() + int(data.get("expires_in", 3600)))
    return token


def request_to_pay(config, *, phone, amount, external_id, message):
    """Starts a Request-to-Pay. Returns the reference id it's tracked
    under — the one we generate and send as X-Reference-Id, since MTN's
    202 response itself carries no body to read one back from."""
    reference_id = str(uuid.uuid4())
    token = _get_token(config)
    response = requests.post(
        f"{_base_url(config)}/collection/v1_0/requesttopay",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Reference-Id": reference_id,
            "X-Target-Environment": _target_environment(config),
            "Ocp-Apim-Subscription-Key": config.get("subscription_key") or "",
            "Content-Type": "application/json",
        },
        json={
            "amount": str(amount),
            "currency": "RWF",
            "externalId": external_id,
            "payer": {"partyIdType": "MSISDN", "partyId": phone},
            "payerMessage": message,
            "payeeNote": message,
        },
        timeout=20,
    )
    if response.status_code not in (200, 202):
        raise MomoError(
            f"MTN MoMo request-to-pay failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    return reference_id


def check_status(config, reference_id):
    """Returns (status, reason) where status is 'pending' | 'successful'
    | 'failed'."""
    token = _get_token(config)
    response = requests.get(
        f"{_base_url(config)}/collection/v1_0/requesttopay/{reference_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Target-Environment": _target_environment(config),
            "Ocp-Apim-Subscription-Key": config.get("subscription_key") or "",
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise MomoError(
            f"MTN MoMo status check failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    data = response.json()
    status = (data.get("status") or "").upper()
    if status == "SUCCESSFUL":
        return "successful", None
    if status == "FAILED":
        reason = data.get("reason")
        return "failed", str(reason) if reason else "Payment failed or was declined."
    return "pending", None
