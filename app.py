"""BPay backend API + admin dashboard host.

Two audiences:
  * /api/config    — read-only, consumed by the Flutter app.
  * /api/admin/*   — token-protected, used by the admin dashboard.

The app never hardcodes carrier USSD codes; it renders whatever the admin
has defined in `ussd_templates` and `services`.
"""

import os
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

    db.execute(
        """INSERT INTO transactions
             (bpay_id, device_id, user_phone, sim_network, type, destination,
              destination_name, amount, status, verification, carrier_ref,
              message, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            b.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return jsonify({"ok": True}), 201


@app.get("/api/announcements/<int:aid>")
def get_announcement(aid):
    """Public — lets the app re-fetch full details for a notification it
    already received, in case the push payload was ever incomplete."""
    row = db.query_one("SELECT id, title, message, photo_url, logo_url, created_at FROM announcements WHERE id=%s", (aid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
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

    if network not in VALID_NETWORKS:
        return jsonify({"error": "network must be mtn or airtel"}), 400
    if not phone:
        return jsonify({"error": "phone is required"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be positive"}), 400

    config = _provider_config(network)
    fields = _PROVIDER_FIELDS.get(network, [])
    if not all(config.get(f) for f in fields):
        return (
            jsonify(
                {
                    "error": f"{network.upper()} isn't fully configured yet — an "
                    "administrator needs to add its API credentials."
                }
            ),
            503,
        )

    external_id = uuid.uuid4().hex
    new_id = db.execute(
        """INSERT INTO fee_collections
             (network, phone, amount, external_id, status, device_id)
           VALUES (%s,%s,%s,%s,'pending',%s)""",
        (network, phone, amount, external_id, device_id),
    )

    client = _provider_client(network)
    try:
        reference = client.request_to_pay(
            config,
            phone=phone,
            amount=amount,
            external_id=external_id,
            message="BPay service fee",
        )
        db.execute(
            "UPDATE fee_collections SET provider_reference=%s WHERE id=%s",
            (reference, new_id),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the app, not fatal
        db.execute(
            "UPDATE fee_collections SET status='failed', reason=%s WHERE id=%s",
            (str(exc)[:255], new_id),
        )
        return jsonify({"id": new_id, "status": "failed", "reason": str(exc)}), 502

    return jsonify({"id": new_id, "status": "pending"}), 201


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
    return jsonify({"status": status, "reason": reason})


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
                      COALESCE(SUM(CASE WHEN status='success' THEN amount ELSE 0 END), 0)
                        AS total_volume,
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
                "total_volume": int(tx_row.get("total_volume") or 0),
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
