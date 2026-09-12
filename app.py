"""BPay backend API + admin dashboard host.

Two audiences:
  * /api/config    — read-only, consumed by the Flutter app.
  * /api/admin/*   — token-protected, used by the admin dashboard.

The app never hardcodes carrier USSD codes; it renders whatever the admin
has defined in `ussd_templates` and `services`.
"""

import os
import random
import re
import string
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

import db
from auth import issue_token, require_admin, verify_password

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_DIR = os.path.join(HERE, "admin")
UPLOADS_DIR = os.path.join(HERE, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

app = Flask(__name__, static_folder=None)
CORS(app)

try:
    db.ensure_payment_link_schema()
except Exception:  # noqa: BLE001 - a transient DB hiccup at boot must never crash the app
    # Logged, not swallowed silently — a real migration failure (bad DB
    # privileges, an incompatible SQL mode, whatever it turns out to be)
    # used to leave every payment-link endpoint permanently 500ing with no
    # trace of why. `_link_settings()` below also retries this once per
    # process on first actual use, in case this only failed because the
    # database wasn't reachable yet at boot.
    app.logger.exception("ensure_payment_link_schema failed at startup")

# Placeholders an admin may use inside a USSD template. Kept here so the
# dashboard and the app agree on exactly one vocabulary.
PLACEHOLDERS = ["recipient", "amount", "code", "account"]

ALLOWED_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _public_base_url():
    """Where uploaded files / this API are reachable from a phone on the
    same network. Defaults to whatever host the request itself came in
    on, so it works without extra config for the common case (Flutter
    already points at this same host)."""
    configured = os.getenv("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return request.host_url.rstrip("/")


# ------------------------------------------------------------- firebase
# Push notifications (Announcements tab). Optional: the server starts and
# every other feature works without this — only sending a push needs it.
# Get the credentials file from Firebase Console > Project settings >
# Service accounts > "Generate new private key".
_firebase_app = None
_firebase_error = None
try:
    import firebase_admin
    from firebase_admin import credentials, messaging

    cred_path = os.path.join(
        HERE, os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "firebase-service-account.json")
    )
    if os.path.isfile(cred_path):
        _firebase_app = firebase_admin.initialize_app(credentials.Certificate(cred_path))
    else:
        _firebase_error = (
            f"No service account file at {cred_path}. Download one from Firebase "
            "Console > Project settings > Service accounts, and save it there."
        )
except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard, not fatal
    _firebase_error = str(exc)


# ------------------------------------------------------------- dashboard
@app.get("/")
def dashboard_index():
    return send_from_directory(ADMIN_DIR, "index.html")


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOADS_DIR, filename)


@app.get("/<path:filename>")
def dashboard_asset(filename):
    return send_from_directory(ADMIN_DIR, filename)


# ------------------------------------------------------------------ auth
@app.post("/api/admin/login")
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    admin = db.query_one(
        "SELECT id, username, password_hash FROM admins WHERE username = %s",
        (username,),
    )
    # Same message either way, so this can't be used to enumerate usernames.
    if not admin or not verify_password(password, admin["password_hash"]):
        return jsonify({"error": "Incorrect username or password"}), 401

    return jsonify(
        {
            "token": issue_token(admin["id"], admin["username"]),
            "username": admin["username"],
        }
    )


# ------------------------------------------------ app-facing public config
@app.get("/api/config")
def app_config():
    """Everything the Flutter app needs to route a payment."""
    templates = db.query_all(
        """SELECT sim_network, transaction_type, recipient_network, template,
                  completes_payment, guidance
           FROM ussd_templates WHERE active = 1"""
    )
    services = db.query_all(
        """SELECT id, name, description, category, icon,
                  ussd_template_mtn, ussd_template_airtel,
                  account_label, sort_order
           FROM services WHERE active = 1
           ORDER BY sort_order DESC, name ASC"""
    )
    # Only the fields the app needs to *disclose* a fee are public — this
    # is a UI notice, not a payment credential, so it's fine alongside the
    # rest of the routing config. provider_keys never appears here.
    fee_rules = db.query_all(
        """SELECT network, fee_amount, trigger_count, trigger_window, active
           FROM fee_rules"""
    )
    for t in templates:
        t["completes_payment"] = bool(t["completes_payment"])
    for f in fee_rules:
        f["active"] = bool(f["active"])
    return jsonify(
        {
            "version": int(datetime.now().timestamp()),
            "placeholders": PLACEHOLDERS,
            "ussd_templates": templates,
            "services": services,
            "fee_rules": fee_rules,
        }
    )


@app.post("/api/transactions")
def report_transaction():
    """Client-reported attempt. Never treated as proof a payment happened."""
    b = request.get_json(silent=True) or {}
    if not b.get("bpay_id"):
        return jsonify({"error": "bpay_id is required"}), 400

    link_code = (b.get("payment_link_code") or "").strip() or None
    # Only a genuinely new report counts as a "use" of the link — this same
    # bpay_id gets reported again as its status resolves, and that update
    # must never count a second time.
    is_new = db.query_one(
        "SELECT id FROM transactions WHERE bpay_id=%s", (b.get("bpay_id"),)
    ) is None

    db.execute(
        """INSERT INTO transactions
             (bpay_id, device_id, user_phone, sim_network, type, destination,
              destination_name, amount, status, verification, carrier_ref,
              message, payment_link_code, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE
             status = VALUES(status),
             verification = VALUES(verification),
             carrier_ref = VALUES(carrier_ref),
             message = VALUES(message)""",
        (
            b.get("bpay_id"),
            b.get("device_id"),
            b.get("user_phone"),
            b.get("sim_network"),
            b.get("type", "unknown"),
            b.get("destination"),
            b.get("destination_name"),
            int(b.get("amount") or 0),
            b.get("status", "unknown"),
            b.get("verification", "none"),
            b.get("carrier_ref"),
            b.get("message"),
            link_code,
            b.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

    if is_new and link_code:
        try:
            _record_payment_link_use(link_code)
        except Exception:  # noqa: BLE001 - a billing hiccup must never break reporting
            pass

    return jsonify({"ok": True}), 201


@app.get("/api/announcements/<int:aid>")
def get_announcement(aid):
    """Public — lets the app re-fetch full details for a notification it
    already received, in case the push payload was ever incomplete."""
    row = db.query_one("SELECT id, title, message, photo_url, logo_url, created_at FROM announcements WHERE id=%s", (aid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    # Flask's default JSON encoder renders a datetime as an RFC 822 string
    # ("Mon, 07 Sep 2026 06:40:02 GMT"), which Dart's DateTime.parse cannot
    # read — ISO 8601 is what every client here actually expects.
    if row.get("created_at") is not None:
        row["created_at"] = row["created_at"].isoformat() + "Z"
    return jsonify(row)


@app.post("/api/devices/register")
def register_device_token():
    """Called by the app once it has a push token, and again whenever
    Firebase refreshes one. Upserts so reinstalling / a token refresh never
    leaves a stale duplicate row for the same device."""
    b = request.get_json(silent=True) or {}
    device_id = (b.get("device_id") or "").strip()
    fcm_token = (b.get("fcm_token") or "").strip()
    if not device_id or not fcm_token:
        return jsonify({"error": "device_id and fcm_token are required"}), 400

    db.execute(
        """INSERT INTO device_tokens (device_id, fcm_token, platform)
           VALUES (%s,%s,%s)
           ON DUPLICATE KEY UPDATE fcm_token=VALUES(fcm_token), platform=VALUES(platform)""",
        (device_id, fcm_token, b.get("platform", "android")),
    )
    return jsonify({"ok": True}), 201


# -------------------------------------------------- admin: ussd templates
@app.get("/api/admin/ussd-templates")
@require_admin
def list_templates():
    rows = db.query_all(
        "SELECT * FROM ussd_templates ORDER BY sim_network, transaction_type"
    )
    for r in rows:
        r["completes_payment"] = bool(r["completes_payment"])
        r["active"] = bool(r["active"])
    return jsonify(rows)


@app.post("/api/admin/ussd-templates")
@require_admin
def create_template():
    b = request.get_json(silent=True) or {}
    missing = [
        f for f in ("sim_network", "transaction_type", "template") if not b.get(f)
    ]
    if missing:
        return jsonify({"error": "Missing: " + ", ".join(missing)}), 400

    try:
        new_id = db.execute(
            """INSERT INTO ussd_templates
                 (sim_network, transaction_type, recipient_network, template,
                  completes_payment, guidance, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                b["sim_network"],
                b["transaction_type"],
                b.get("recipient_network", "any"),
                b["template"],
                1 if b.get("completes_payment", True) else 0,
                b.get("guidance"),
                1 if b.get("active", True) else 0,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard
        if "Duplicate" in str(exc):
            return (
                jsonify({"error": "A template for that combination already exists"}),
                409,
            )
        raise
    return jsonify({"id": new_id}), 201


@app.put("/api/admin/ussd-templates/<int:tid>")
@require_admin
def update_template(tid):
    b = request.get_json(silent=True) or {}
    db.execute(
        """UPDATE ussd_templates SET
             sim_network=%s, transaction_type=%s, recipient_network=%s,
             template=%s, completes_payment=%s, guidance=%s, active=%s
           WHERE id=%s""",
        (
            b.get("sim_network"),
            b.get("transaction_type"),
            b.get("recipient_network", "any"),
            b.get("template"),
            1 if b.get("completes_payment", True) else 0,
            b.get("guidance"),
            1 if b.get("active", True) else 0,
            tid,
        ),
    )
    return jsonify({"ok": True})


@app.delete("/api/admin/ussd-templates/<int:tid>")
@require_admin
def delete_template(tid):
    db.execute("DELETE FROM ussd_templates WHERE id=%s", (tid,))
    return jsonify({"ok": True})


# -------------------------------------------------------- admin: services
@app.get("/api/admin/services")
@require_admin
def list_services():
    rows = db.query_all("SELECT * FROM services ORDER BY sort_order DESC, name")
    for r in rows:
        r["active"] = bool(r["active"])
    return jsonify(rows)


@app.post("/api/admin/services")
@require_admin
def create_service():
    b = request.get_json(silent=True) or {}
    if not b.get("name"):
        return jsonify({"error": "Missing: name"}), 400
    if not b.get("ussd_template_mtn") and not b.get("ussd_template_airtel"):
        return (
            jsonify({"error": "Set a USSD code for MTN, Airtel, or both"}),
            400,
        )

    new_id = db.execute(
        """INSERT INTO services
             (name, description, category, icon, ussd_template_mtn,
              ussd_template_airtel, account_label, sort_order, active)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            b["name"],
            b.get("description"),
            b.get("category", "other"),
            b.get("icon", "receipt_long_rounded"),
            b.get("ussd_template_mtn") or None,
            b.get("ussd_template_airtel") or None,
            b.get("account_label", "Account number"),
            int(b.get("sort_order") or 0),
            1 if b.get("active", True) else 0,
        ),
    )
    return jsonify({"id": new_id}), 201


@app.put("/api/admin/services/<int:sid>")
@require_admin
def update_service(sid):
    b = request.get_json(silent=True) or {}
    db.execute(
        """UPDATE services SET
             name=%s, description=%s, category=%s, icon=%s,
             ussd_template_mtn=%s, ussd_template_airtel=%s,
             account_label=%s, sort_order=%s, active=%s
           WHERE id=%s""",
        (
            b.get("name"),
            b.get("description"),
            b.get("category", "other"),
            b.get("icon", "receipt_long_rounded"),
            b.get("ussd_template_mtn") or None,
            b.get("ussd_template_airtel") or None,
            b.get("account_label", "Account number"),
            int(b.get("sort_order") or 0),
            1 if b.get("active", True) else 0,
            sid,
        ),
    )
    return jsonify({"ok": True})


@app.delete("/api/admin/services/<int:sid>")
@require_admin
def delete_service(sid):
    db.execute("DELETE FROM services WHERE id=%s", (sid,))
    return jsonify({"ok": True})


# ----------------------------------------------------- admin: fee rules
# A disclosure rule, not a payment mechanism: BPay has no rail of its own to
# collect a fee through, so this only controls the in-app notice shown
# before dialling once a network crosses its transaction threshold.
VALID_NETWORKS = {"mtn", "airtel"}
VALID_WINDOWS = {"day", "week", "month", "year"}


@app.get("/api/admin/fee-rules")
@require_admin
def list_fee_rules():
    # A network with no row yet (a fresh database that was never seeded)
    # still gets a default card here — the only way to create the row is
    # the PUT this same card's Save button sends, so the dashboard must
    # never depend on the row already existing to show that button at all.
    by_network = {r["network"]: r for r in db.query_all("SELECT * FROM fee_rules")}
    out = []
    for network in sorted(VALID_NETWORKS):
        row = by_network.get(network) or {
            "network": network,
            "fee_amount": 0,
            "trigger_count": 5,
            "trigger_window": "month",
            "active": False,
        }
        row["active"] = bool(row["active"])
        out.append(row)
    return jsonify(out)


@app.put("/api/admin/fee-rules/<network>")
@require_admin
def update_fee_rule(network):
    if network not in VALID_NETWORKS:
        return jsonify({"error": "network must be mtn or airtel"}), 400

    b = request.get_json(silent=True) or {}
    window = b.get("trigger_window", "month")
    if window not in VALID_WINDOWS:
        return jsonify({"error": "trigger_window must be day/week/month/year"}), 400

    try:
        fee_amount = int(b.get("fee_amount") or 0)
        trigger_count = int(b.get("trigger_count") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "fee_amount and trigger_count must be numbers"}), 400
    if fee_amount < 0 or trigger_count < 1:
        return (
            jsonify({"error": "fee_amount must be >= 0 and trigger_count >= 1"}),
            400,
        )

    db.execute(
        """INSERT INTO fee_rules (network, fee_amount, trigger_count, trigger_window, active)
           VALUES (%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE
             fee_amount=VALUES(fee_amount),
             trigger_count=VALUES(trigger_count),
             trigger_window=VALUES(trigger_window),
             active=VALUES(active)""",
        (
            network,
            fee_amount,
            trigger_count,
            window,
            1 if b.get("active", False) else 0,
        ),
    )
    return jsonify({"ok": True})


# -------------------------------------------------- admin: provider keys
# Real credentials for the MTN MoMo / Airtel Money Collections APIs — see
# momo_client.py / airtel_client.py for what actually uses them. Never
# exposed on /api/config and secrets are never returned in full once
# saved — only a masked preview, so the dashboard itself can't leak them.
_SECRET_FIELDS = {"api_key", "client_secret", "subscription_key"}
_PROVIDER_FIELDS = {
    "mtn": ["subscription_key", "api_user", "api_key"],
    "airtel": ["client_id", "client_secret"],
}
# Every column that can hold a credential, across both networks — used to
# strip all of them off a row before it goes back to the dashboard,
# regardless of which network the row is for. Without this, a column left
# over from a schema change (e.g. an old single api_key value on an Airtel
# row) would leak unmasked since it isn't in that network's own field list.
_ALL_PROVIDER_FIELDS = sorted({f for fs in _PROVIDER_FIELDS.values() for f in fs})


def _mask_key(key):
    if not key:
        return None
    return ("*" * max(len(key) - 4, 0)) + key[-4:]


@app.get("/api/admin/provider-keys")
@require_admin
def list_provider_keys():
    # Same reasoning as fee-rules above: a network with no row yet must
    # still get a card, since the PUT that would create the row is only
    # reachable from that card's own Save button.
    by_network = {r["network"]: r for r in db.query_all("SELECT * FROM provider_keys")}
    out = []
    for network in sorted(VALID_NETWORKS):
        r = by_network.get(network) or {
            "network": network,
            "environment": "sandbox",
            "base_url": None,
            "target_environment": None,
        }
        relevant = _PROVIDER_FIELDS.get(network, [])
        masked = {}
        for f in _ALL_PROVIDER_FIELDS:
            value = r.pop(f, None)
            if f in relevant:
                masked[f] = _mask_key(value) if f in _SECRET_FIELDS else value
        r["fields"] = masked
        r["configured"] = all(masked.get(f) for f in relevant)
        out.append(r)
    return jsonify(out)


@app.put("/api/admin/provider-keys/<network>")
@require_admin
def update_provider_key(network):
    if network not in VALID_NETWORKS:
        return jsonify({"error": "network must be mtn or airtel"}), 400

    b = request.get_json(silent=True) or {}
    environment = b.get("environment", "sandbox")
    if environment not in ("sandbox", "production"):
        return jsonify({"error": "environment must be sandbox or production"}), 400

    # Only overwrite a secret field when a new value was actually typed —
    # the dashboard sends masked previews back untouched otherwise, and a
    # masked string must never be saved as if it were the real secret.
    existing = db.query_one("SELECT * FROM provider_keys WHERE network=%s", (network,))

    def field(name):
        value = (b.get(name) or "").strip()
        if not value:
            return existing.get(name) if existing else None
        if name in _SECRET_FIELDS and set(value) == {"*"}:
            return existing.get(name) if existing else None
        return value

    db.execute(
        """INSERT INTO provider_keys
             (network, environment, base_url, target_environment,
              subscription_key, api_user, api_key, client_id, client_secret)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE
             environment=VALUES(environment), base_url=VALUES(base_url),
             target_environment=VALUES(target_environment),
             subscription_key=VALUES(subscription_key),
             api_user=VALUES(api_user), api_key=VALUES(api_key),
             client_id=VALUES(client_id), client_secret=VALUES(client_secret)""",
        (
            network,
            environment,
            (b.get("base_url") or "").strip() or None,
            (b.get("target_environment") or "").strip() or None,
            field("subscription_key"),
            field("api_user"),
            field("api_key"),
            field("client_id"),
            field("client_secret"),
        ),
    )
    return jsonify({"ok": True})


# ----------------------------------------------------- fee collections
# The real thing the fee popup leads to: an actual Request-to-Pay against
# MTN MoMo or Airtel Money, charging the user's own wallet and paying it
# into the company's account behind the credentials above. Distinct from
# `transactions`, which is the user's own USSD payment history — this is
# the company's ledger of fee money it has actually requested.
def _provider_config(network):
    row = db.query_one("SELECT * FROM provider_keys WHERE network=%s", (network,))
    return row or {}


def _provider_client(network):
    if network == "mtn":
        import momo_client

        return momo_client
    import airtel_client

    return airtel_client


def _start_fee_collection(
    network, phone, amount, device_id=None, message="BPay service fee", payment_link_code=None
):
    """One Request-to-Pay against a real provider, logged in `fee_collections`
    either way. Shared by the client-initiated service-fee flow and the
    client-initiated payment-link usage fee — same ledger, same provider
    plumbing, only who triggers it differs. Returns the fee_collections
    row id and the outcome dict the caller should respond with.
    Raises ValueError for a caller-fixable problem (bad input, provider not
    configured) so each endpoint can shape its own 400/503 response.
    [payment_link_code] is stamped on the row so `get_fee_collection_status`
    knows which link to credit once the provider confirms it succeeded."""
    if network not in VALID_NETWORKS:
        raise ValueError("network must be mtn or airtel")
    if not phone:
        raise ValueError("phone is required")
    if amount <= 0:
        raise ValueError("amount must be positive")

    config = _provider_config(network)
    fields = _PROVIDER_FIELDS.get(network, [])
    if not all(config.get(f) for f in fields):
        raise LookupError(
            f"{network.upper()} isn't fully configured yet — an administrator "
            "needs to add its API credentials."
        )

    external_id = uuid.uuid4().hex
    new_id = db.execute(
        """INSERT INTO fee_collections
             (network, phone, amount, external_id, status, device_id, payment_link_code)
           VALUES (%s,%s,%s,%s,'pending',%s,%s)""",
        (network, phone, amount, external_id, device_id, payment_link_code),
    )

    client = _provider_client(network)
    try:
        reference = client.request_to_pay(
            config, phone=phone, amount=amount, external_id=external_id, message=message
        )
        db.execute(
            "UPDATE fee_collections SET provider_reference=%s WHERE id=%s",
            (reference, new_id),
        )
        return new_id, {"id": new_id, "status": "pending"}
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not fatal
        db.execute(
            "UPDATE fee_collections SET status='failed', reason=%s WHERE id=%s",
            (str(exc)[:255], new_id),
        )
        return new_id, {"id": new_id, "status": "failed", "reason": str(exc)}


@app.post("/api/fee-collection/request")
def create_fee_collection():
    b = request.get_json(silent=True) or {}
    network = b.get("network")
    phone = (b.get("phone") or "").strip()
    device_id = b.get("device_id")
    try:
        amount = int(b.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400

    try:
        _new_id, outcome = _start_fee_collection(network, phone, amount, device_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 503

    return jsonify(outcome), (502 if outcome["status"] == "failed" else 201)


@app.get("/api/fee-collection/<int:cid>/status")
def get_fee_collection_status(cid):
    row = db.query_one("SELECT * FROM fee_collections WHERE id=%s", (cid,))
    if not row:
        return jsonify({"error": "Not found"}), 404

    if row["status"] != "pending":
        return jsonify({"status": row["status"], "reason": row["reason"]})

    config = _provider_config(row["network"])
    client = _provider_client(row["network"])
    try:
        status, reason = client.check_status(config, row["provider_reference"])
    except Exception as exc:  # noqa: BLE001 - a check failure stays pending, not failed
        return jsonify({"status": "pending", "reason": None, "check_error": str(exc)})

    if status != "pending":
        db.execute(
            "UPDATE fee_collections SET status=%s, reason=%s WHERE id=%s",
            (status, reason, cid),
        )
        # Only a payment-link fee row carries this, and only the moment it
        # resolves — this is the one place a successful charge actually
        # counts against the link's `fee_charges_done`, so a client that
        # started the charge and never polled again still gets credited
        # correctly the next time anyone checks this row's status.
        if status == "successful" and row.get("payment_link_code"):
            db.execute(
                "UPDATE payment_links SET fee_charges_done=fee_charges_done+1 "
                "WHERE code=%s",
                (row["payment_link_code"],),
            )
    return jsonify({"status": status, "reason": reason})


# -------------------------------------------------------- payment links
# A shareable BPay link: tap it, and the app opens with the recipient/
# merchant and amount already filled in and the dial already started
# (Android App Links — see payment_link_settings for the domain/cert that
# makes the OS hand the link straight to the app instead of a browser).
MTN_PREFIXES = ("078", "079")
AIRTEL_PREFIXES = ("072", "073")


def _classify_destination(raw):
    """Mirrors RwandaPhoneValidator on the Flutter side: a recognised
    Rwandan mobile number classifies as a phone (with its network), a
    plain 4-10 digit string otherwise classifies as a merchant code.
    Returns (destination_type, network, normalized) or (None, None, None)
    when neither shape matches."""
    digits = re.sub(r"[^0-9]", "", raw or "")
    if not digits:
        return None, None, None

    d = digits
    if d.startswith("250") and len(d) > 3:
        d = d[3:]
    if len(d) == 9 and d.startswith("7"):
        d = "0" + d
    if len(d) == 10 and d.startswith("0") and d[:3] in MTN_PREFIXES + AIRTEL_PREFIXES:
        network = "mtn" if d[:3] in MTN_PREFIXES else "airtel"
        return "phone", network, d

    if 4 <= len(digits) <= 10:
        return "merchant", "unknown", digits

    return None, None, None


def _generate_link_code():
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(20):
        code = "".join(random.choices(alphabet, k=8))
        if not db.query_one("SELECT id FROM payment_links WHERE code=%s", (code,)):
            return code
    raise RuntimeError("Could not generate a unique payment link code")


_schema_retried = False


def _link_settings():
    global _schema_retried
    try:
        row = db.query_one("SELECT * FROM payment_link_settings WHERE id=1")
    except Exception:
        # Most likely cause: the startup migration failed (see the log
        # line from `ensure_payment_link_schema failed at startup`) and
        # the table was never created — retried once per process rather
        # than on every request, so a genuine, non-transient failure
        # (e.g. the DB user lacking CREATE/ALTER privileges) still
        # surfaces as a clear error instead of retrying forever.
        if _schema_retried:
            raise
        _schema_retried = True
        app.logger.exception(
            "payment_link_settings query failed — retrying schema setup once"
        )
        db.ensure_payment_link_schema()
        row = db.query_one("SELECT * FROM payment_link_settings WHERE id=1")

    if row is None:
        db.execute(
            "INSERT INTO payment_link_settings (id, active) VALUES (1, 0)"
        )
        row = db.query_one("SELECT * FROM payment_link_settings WHERE id=1")
    return row or {}


def _link_url(code, settings=None):
    settings = settings or _link_settings()
    domain = (settings.get("app_domain") or "").strip() or request.host
    return f"https://{domain}/pay/{code}"


def _isoformat_rows(rows, fields=("created_at", "updated_at")):
    """Flask's default JSON encoding renders a raw `datetime` as an
    RFC-822-style HTTP date, which Dart's `DateTime.parse`/`tryParse`
    cannot read (see the same fix on `get_announcement`) — every payment-
    link endpoint the Flutter app calls needs ISO-8601 instead."""
    for row in rows:
        for field in fields:
            value = row.get(field)
            if isinstance(value, datetime):
                row[field] = value.isoformat() + "Z"
    return rows


def _record_payment_link_use(code):
    """Counts one use toward this link's usage fee. Nothing is charged from
    here — a threshold of 5 crossed at the 5th use just becomes a pending
    charge (see `_pending_fee_charge`) the owner's own app notices next
    time it asks, shows a local notification for, and only actually bills
    once its owner taps Pay: the same real Request-to-Pay + on-phone
    approval every other fee in BPay already goes through, never a charge
    silently fired from the server the moment a threshold ticks over."""
    link = db.query_one("SELECT * FROM payment_links WHERE code=%s", (code,))
    if link is None:
        return
    db.execute(
        "UPDATE payment_links SET use_count=use_count+1 WHERE id=%s", (link["id"],)
    )


def _pending_fee_charge(link, settings=None):
    """How much this link's owner currently owes, and the fee amount that
    figure is denominated in — 0 whenever nothing is due, the feature is
    off, or there's no owner phone/network on file to charge at all."""
    settings = settings if settings is not None else _link_settings()
    threshold = settings.get("fee_threshold") or 0
    fee_amount = settings.get("fee_amount") or 0
    if (
        not settings.get("active")
        or threshold <= 0
        or fee_amount <= 0
        or not link.get("owner_phone")
        or not link.get("owner_network")
    ):
        return fee_amount, 0
    charges_due = (link.get("use_count") or 0) // threshold
    already_charged = link.get("fee_charges_done") or 0
    return fee_amount, max(0, charges_due - already_charged)


@app.post("/api/payment-links")
def create_payment_link():
    b = request.get_json(silent=True) or {}
    device_id = (b.get("device_id") or "").strip()
    owner_phone = (b.get("owner_phone") or "").strip() or None
    owner_network = b.get("owner_network")
    # The app sends its SIM's detected network, which can be "unknown" (no
    # SIM chosen yet, or a network BPay couldn't classify) — that's not a
    # reason to refuse generating the link, just a link that can't accrue
    # a billable usage fee until the owner's network is known.
    if owner_network not in ("mtn", "airtel"):
        owner_network = None
    destination_raw = (b.get("destination") or "").strip()
    try:
        amount = int(b.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400

    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be positive"}), 400

    settings = _link_settings()
    if not settings.get("active"):
        return jsonify({"error": "Payment links are not enabled yet."}), 503

    dtype, network, normalized = _classify_destination(destination_raw)
    if dtype is None:
        return jsonify({"error": "Enter a valid phone number or merchant code."}), 400

    code = _generate_link_code()
    db.execute(
        """INSERT INTO payment_links
             (code, device_id, owner_phone, owner_network, destination,
              destination_type, network, amount)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (code, device_id, owner_phone, owner_network, normalized, dtype, network, amount),
    )
    return jsonify({"code": code, "url": _link_url(code, settings)}), 201


@app.get("/api/payment-links")
def list_payment_links():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    rows = db.query_all(
        """SELECT * FROM payment_links
           WHERE device_id=%s AND status != 'deleted'
           ORDER BY created_at DESC""",
        (device_id,),
    )
    settings = _link_settings()
    for r in rows:
        r["url"] = _link_url(r["code"], settings)
        r["fee_amount"], r["pending_fee_charges"] = _pending_fee_charge(r, settings)
    return jsonify(_isoformat_rows(rows))


@app.get("/api/payment-links/<code>")
def resolve_payment_link(code):
    """Public — this is what both the app's deep-link handler and the
    /pay/<code> browser fallback call to find out what to pay."""
    row = db.query_one("SELECT * FROM payment_links WHERE code=%s", (code,))
    if row is None or row["status"] == "deleted":
        return jsonify({"error": "This payment link no longer exists."}), 404
    if row["status"] == "paused":
        return jsonify({"error": "This payment link is currently paused."}), 410
    return jsonify(
        {
            "code": row["code"],
            "destination": row["destination"],
            "destination_type": row["destination_type"],
            "network": row["network"],
            "amount": row["amount"],
            "owner_phone": row["owner_phone"],
        }
    )


@app.get("/api/payment-links/<code>/uses")
def payment_link_uses(code):
    rows = db.query_all(
        """SELECT * FROM transactions WHERE payment_link_code=%s
           ORDER BY created_at DESC LIMIT 200""",
        (code,),
    )
    return jsonify(_isoformat_rows(rows))


def _owned_link_or_error(code, device_id):
    row = db.query_one("SELECT * FROM payment_links WHERE code=%s", (code,))
    if row is None or row["status"] == "deleted":
        return None, (jsonify({"error": "Not found"}), 404)
    if device_id and device_id != row["device_id"]:
        return None, (jsonify({"error": "Not authorized"}), 403)
    return row, None


@app.patch("/api/payment-links/<code>")
def update_payment_link(code):
    b = request.get_json(silent=True) or {}
    row, error = _owned_link_or_error(code, b.get("device_id"))
    if error:
        return error

    result_code = code
    if b.get("regenerate"):
        result_code = _generate_link_code()
        db.execute(
            "UPDATE payment_links SET code=%s WHERE id=%s", (result_code, row["id"])
        )
    if b.get("status") in ("active", "paused"):
        db.execute(
            "UPDATE payment_links SET status=%s WHERE id=%s", (b["status"], row["id"])
        )
    if "amount" in b:
        try:
            amount = int(b["amount"])
        except (TypeError, ValueError):
            amount = None
        if amount and amount > 0:
            db.execute(
                "UPDATE payment_links SET amount=%s WHERE id=%s", (amount, row["id"])
            )

    return jsonify({"code": result_code, "url": _link_url(result_code)})


@app.delete("/api/payment-links/<code>")
def delete_payment_link(code):
    device_id = request.args.get("device_id")
    row, error = _owned_link_or_error(code, device_id)
    if error:
        return error
    db.execute("UPDATE payment_links SET status='deleted' WHERE id=%s", (row["id"],))
    return jsonify({"ok": True})


@app.post("/api/payment-links/<code>/charge-fee")
def charge_payment_link_fee(code):
    """Client-initiated, same as every other fee in BPay: the owner's own
    app noticed (via `pending_fee_charges` on `GET /api/payment-links`)
    that a threshold was crossed, showed a local notification and a pay
    popup, and the owner tapped Pay — only then does a real Request-to-Pay
    go out. Nothing here is ever triggered by the server on its own."""
    b = request.get_json(silent=True) or {}
    row, error = _owned_link_or_error(code, b.get("device_id"))
    if error:
        return error

    fee_amount, pending = _pending_fee_charge(row)
    if pending <= 0:
        return jsonify({"error": "No fee is currently due on this link."}), 400

    phone = (b.get("phone") or row.get("owner_phone") or "").strip()
    network = row.get("owner_network")
    if not network:
        return jsonify({"error": "This link has no owner network on file."}), 400

    try:
        _new_id, outcome = _start_fee_collection(
            network,
            phone,
            fee_amount,
            device_id=row["device_id"],
            message=f"BPay payment-link fee ({code})",
            payment_link_code=code,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 503

    return jsonify(outcome), (502 if outcome["status"] == "failed" else 201)


@app.get("/pay/<code>")
def payment_link_landing(code):
    """What a browser shows when the OS didn't hand the link straight to
    the app (App Links verification failed, or BPay isn't installed) —
    this is the fallback the assetlinks.json / App Links setup exists to
    make unnecessary in the common case."""
    row = db.query_one("SELECT * FROM payment_links WHERE code=%s", (code,))
    settings = _link_settings()
    play_url = settings.get("play_store_url") or "#"

    if row is None or row["status"] == "deleted":
        message = "This payment link no longer exists."
    elif row["status"] == "paused":
        message = "This payment link is currently paused by its owner."
    else:
        who = row["destination"]
        message = f"Pay {row['amount']} RWF to {who} via BPay"

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BPay Payment Link</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f6f6f8; margin: 0; padding: 48px 20px; text-align: center; }}
.card {{ max-width: 360px; margin: 0 auto; background: #fff; border-radius: 20px;
  padding: 28px; box-shadow: 0 12px 40px rgba(0,0,0,.08); }}
.brand {{ font-size: 26px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 8px; }}
.brand .pay {{ color: #ffcc00; }}
p {{ color: #4a4c55; line-height: 1.5; }}
a.btn {{ display: block; margin-top: 18px; padding: 14px; border-radius: 12px;
  background: #ffcc00; color: #3e2116; text-decoration: none; font-weight: 700; }}
</style></head>
<body><div class="card">
  <div class="brand"><span>B</span><span class="pay">Pay</span></div>
  <p>{message}</p>
  <a class="btn" href="{play_url}">Get BPay on Google Play</a>
</div></body></html>"""
    return html


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    """Android App Links verification file — its presence and content
    here (correct package name + this app's real signing certificate
    fingerprint) is what lets the OS hand a bpay /pay/<code> link straight
    to the app instead of opening it in a browser."""
    settings = _link_settings()
    # Comma-separated so both a debug build (for testing) and the real
    # Play Store release signing cert can be trusted at once.
    raw = settings.get("sha256_fingerprint") or ""
    fingerprints = [f.strip() for f in raw.split(",") if f.strip()]
    return jsonify(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": "com.akagerainc.bpay",
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ]
    )


@app.get("/api/admin/payment-link-settings")
@require_admin
def get_link_settings():
    s = _link_settings()
    s["active"] = bool(s.get("active"))
    return jsonify(s)


@app.put("/api/admin/payment-link-settings")
@require_admin
def update_link_settings():
    b = request.get_json(silent=True) or {}
    try:
        fee_amount = int(b.get("fee_amount") or 0)
        fee_threshold = int(b.get("fee_threshold") or 5)
    except (TypeError, ValueError):
        return jsonify({"error": "fee_amount and fee_threshold must be numbers"}), 400
    if fee_amount < 0 or fee_threshold < 1:
        return jsonify({"error": "fee_amount must be >= 0 and fee_threshold >= 1"}), 400

    db.execute(
        """INSERT INTO payment_link_settings
             (id, app_domain, play_store_url, sha256_fingerprint, fee_amount,
              fee_threshold, active)
           VALUES (1,%s,%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE
             app_domain=VALUES(app_domain), play_store_url=VALUES(play_store_url),
             sha256_fingerprint=VALUES(sha256_fingerprint),
             fee_amount=VALUES(fee_amount), fee_threshold=VALUES(fee_threshold),
             active=VALUES(active)""",
        (
            (b.get("app_domain") or "").strip() or None,
            (b.get("play_store_url") or "").strip() or None,
            (b.get("sha256_fingerprint") or "").strip() or None,
            fee_amount,
            fee_threshold,
            1 if b.get("active") else 0,
        ),
    )
    return jsonify({"ok": True})


@app.get("/api/admin/payment-links")
@require_admin
def admin_list_payment_links():
    rows = db.query_all(
        """SELECT * FROM payment_links WHERE status != 'deleted'
           ORDER BY created_at DESC LIMIT 500"""
    )
    settings = _link_settings()
    for r in rows:
        r["url"] = _link_url(r["code"], settings)
        r["fee_amount"], r["pending_fee_charges"] = _pending_fee_charge(r, settings)
    return jsonify(_isoformat_rows(rows))


# ------------------------------------------------------------- uploads
@app.post("/api/admin/uploads")
@require_admin
def upload_file():
    """Used by the Announcements form to upload a photo or logo before the
    push is sent — returns the URL to put in `photo_url`/`logo_url`."""
    file = request.files.get("file")
    if file is None or file.filename == "":
        return jsonify({"error": "No file was uploaded"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return (
            jsonify({"error": "Only image files are allowed (png, jpg, gif, webp)"}),
            400,
        )

    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOADS_DIR, secure_filename(name)))
    return jsonify({"url": f"{_public_base_url()}/uploads/{name}"}), 201


# -------------------------------------------------------- announcements
def _send_push_to_all(announcement_id, title, message, photo_url, logo_url):
    """Sends one push to every registered device token. Returns
    (sent_count, error) — error is a user-facing string when Firebase
    isn't configured or the send itself failed; sent_count is 0 either way
    so the dashboard never claims a push went out when it didn't.

    Deliberately data-only (no `notification` block): that's what makes the
    app render its own notification — with the logo and photo — in every
    state (foreground, background, or killed), instead of Android's default
    plain system rendering only while backgrounded."""
    if _firebase_app is None:
        return 0, _firebase_error or "Firebase is not configured."

    tokens = [r["fcm_token"] for r in db.query_all("SELECT fcm_token FROM device_tokens")]
    if not tokens:
        return 0, "No devices are registered to receive push notifications yet."

    data = {
        "id": str(announcement_id),
        "title": title,
        "body": message,
        "photo_url": photo_url or "",
        "logo_url": logo_url or "",
        # The moment this particular push went out — a resend gets its own
        # fresh timestamp here, distinct from the announcement's original
        # created_at, since that's what "when was this sent" actually means.
        "sent_at": datetime.utcnow().isoformat() + "Z",
    }
    sent = 0
    errors = []
    # Sent one at a time rather than a multicast batch: a single bad/expired
    # token must not stop the rest of the run, and this stays simple to
    # reason about at BPay's scale.
    for token in tokens:
        try:
            messaging.send(
                messaging.Message(
                    data=data,
                    token=token,
                    android=messaging.AndroidConfig(priority="high"),
                )
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one bad token shouldn't stop the rest
            errors.append(str(exc))

    if sent == 0 and errors:
        return 0, errors[0]
    return sent, None


@app.get("/api/admin/announcements")
@require_admin
def list_announcements():
    rows = db.query_all("SELECT * FROM announcements ORDER BY created_at DESC LIMIT 100")
    return jsonify(
        {
            "announcements": rows,
            "firebase_configured": _firebase_app is not None,
            "firebase_error": None if _firebase_app else _firebase_error,
        }
    )


@app.post("/api/admin/announcements")
@require_admin
def create_announcement():
    b = request.get_json(silent=True) or {}
    title = (b.get("title") or "").strip()
    message = (b.get("message") or "").strip()
    if not title or not message:
        return jsonify({"error": "title and message are required"}), 400

    photo_url = b.get("photo_url") or None
    logo_url = b.get("logo_url") or None

    # Inserted before sending so the push payload can carry the real id —
    # the app uses it to fetch full details if it ever needs to refetch.
    new_id = db.execute(
        """INSERT INTO announcements (title, message, photo_url, logo_url, sent_count)
           VALUES (%s,%s,%s,%s,0)""",
        (title, message, photo_url, logo_url),
    )

    sent_count, error = _send_push_to_all(new_id, title, message, photo_url, logo_url)
    if sent_count:
        db.execute(
            "UPDATE announcements SET sent_count=%s WHERE id=%s", (sent_count, new_id)
        )

    return jsonify({"id": new_id, "sent_count": sent_count, "error": error}), 201


@app.post("/api/admin/announcements/<int:aid>/resend")
@require_admin
def resend_announcement(aid):
    """Pushes an already-sent announcement out again, unchanged — for a
    notice that's still relevant (e.g. re-reaching devices that were
    offline the first time), without re-typing it as a new one. Uses the
    same announcement id, so a tap on either send opens the same detail
    page; "Reached" accumulates across every send rather than resetting."""
    row = db.query_one(
        "SELECT title, message, photo_url, logo_url, sent_count FROM announcements WHERE id=%s",
        (aid,),
    )
    if row is None:
        return jsonify({"error": "Announcement not found"}), 404

    sent_count, error = _send_push_to_all(
        aid, row["title"], row["message"], row["photo_url"], row["logo_url"]
    )
    total = (row["sent_count"] or 0) + sent_count
    if sent_count:
        db.execute("UPDATE announcements SET sent_count=%s WHERE id=%s", (total, aid))

    return jsonify({"id": aid, "sent_count": sent_count, "total": total, "error": error})


# ---------------------------------------------------- admin: transactions
@app.get("/api/admin/transactions")
@require_admin
def admin_transactions():
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = db.query_all(
        "SELECT * FROM transactions ORDER BY created_at DESC LIMIT %s", (limit,)
    )
    return jsonify(rows)


# ------------------------------------------------------------- admin: users
# BPay has no user-account system — nobody signs in. "Users" here means
# every device that has either registered for push or reported a real
# transaction, with whatever real activity is on file for it. A device
# that denied push (or predates it) still shows up via its transactions,
# and one that's never made a reported transaction still shows up via its
# push registration — neither side is dropped just because the other is
# missing.
@app.get("/api/admin/users")
@require_admin
def list_users():
    tokens = {r["device_id"]: r for r in db.query_all("SELECT * FROM device_tokens")}
    activity = {
        r["device_id"]: r
        for r in db.query_all(
            """SELECT device_id,
                      MAX(user_phone) AS phone,
                      MAX(sim_network) AS sim_network,
                      COUNT(*) AS transaction_count,
                      COALESCE(SUM(status = 'success'), 0) AS successful_count,
                      COALESCE(SUM(status = 'failed'), 0) AS failed_count,
                      COALESCE(SUM(CASE WHEN status='success' THEN amount ELSE 0 END), 0)
                        AS total_volume,
                      COALESCE(SUM(CASE WHEN status='failed' THEN amount ELSE 0 END), 0)
                        AS failed_volume,
                      MAX(created_at) AS last_transaction_at
               FROM transactions
               WHERE device_id IS NOT NULL
               GROUP BY device_id"""
        )
    }

    users = []
    for device_id in set(tokens) | set(activity):
        token_row = tokens.get(device_id, {})
        tx_row = activity.get(device_id, {})
        users.append(
            {
                "device_id": device_id,
                "phone": tx_row.get("phone"),
                "sim_network": tx_row.get("sim_network"),
                "platform": token_row.get("platform"),
                "has_push_token": bool(token_row.get("fcm_token")),
                "last_seen": token_row.get("updated_at"),
                "transaction_count": int(tx_row.get("transaction_count") or 0),
                "successful_count": int(tx_row.get("successful_count") or 0),
                "failed_count": int(tx_row.get("failed_count") or 0),
                "total_volume": int(tx_row.get("total_volume") or 0),
                "failed_volume": int(tx_row.get("failed_volume") or 0),
                "last_transaction_at": tx_row.get("last_transaction_at"),
            }
        )

    def sort_key(u):
        return str(u["last_transaction_at"] or u["last_seen"] or "")

    users.sort(key=sort_key, reverse=True)
    return jsonify(users)


@app.get("/api/admin/stats")
@require_admin
def admin_stats():
    totals = db.query_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(status = 'success'), 0) AS successful,
                  COALESCE(SUM(CASE WHEN status='success' THEN amount ELSE 0 END), 0)
                    AS volume
           FROM transactions"""
    )
    return jsonify(
        {
            "transactions": int(totals["total"]),
            "successful": int(totals["successful"]),
            "volume": int(totals["volume"]),
            "templates": len(
                db.query_all("SELECT id FROM ussd_templates WHERE active=1")
            ),
            "services": len(db.query_all("SELECT id FROM services WHERE active=1")),
            "devices": len(db.query_all("SELECT device_id FROM device_tokens")),
        }
    )


@app.get("/api/health")
def health():
    try:
        db.query_one("SELECT 1 AS ok")
        return jsonify({"ok": True, "database": "connected"})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "database": str(exc)}), 503


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
