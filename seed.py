"""Creates the schema, the first admin, and a starting set of routes.

Safe to re-run: it only inserts what is missing, so it never overwrites
templates an admin has since edited in the dashboard.

    python seed.py
"""

import os

import mysql.connector
from dotenv import load_dotenv

import db
from auth import hash_password

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "db", "schema.sql")

# Starting routes. These are the codes BPay used before the backend
# existed — the admin can change any of them without an app update.
# {recipient}, {amount}, {code} and {account} are substituted by the app.
TEMPLATES = [
    # MTN SIM
    ("mtn", "phone_transfer", "mtn", "*182*1*1*{recipient}*{amount}#", 1, None),
    ("mtn", "phone_transfer", "airtel", "*182*1*2*{recipient}*{amount}#", 1, None),
    ("mtn", "merchant_payment", "any", "*182*8*1*{code}*{amount}#", 1, None),
    (
        "mtn",
        "bill_payment",
        "any",
        "*182#",
        0,
        "BPay will open the MTN MoMo menu. Choose the service you need and "
        "follow the prompts.",
    ),
    ("mtn", "airtime", "any", "*182*2*1*{amount}#", 1, None),
    # MTN MoKash
    ("mtn", "mokash_send", "any", "*182*1*1*{recipient}*{amount}#", 1, None),
    (
        "mtn",
        "mokash_withdraw",
        "any",
        "*182*8*3*{amount}#",
        1,
        None,
    ),
    # Airtel SIM — no verified inline transfer shortcut, so the menu is
    # opened rather than a code being guessed at.
    (
        "airtel",
        "phone_transfer",
        "any",
        "*500#",
        0,
        "BPay will open the Airtel Money menu. Choose Send Money, then enter "
        "the number and amount shown above.",
    ),
    (
        "airtel",
        "merchant_payment",
        "any",
        "*182*8*1#",
        0,
        "BPay will open Airtel Money merchant payment. Enter the merchant "
        "code and amount shown above when prompted.",
    ),
    (
        "airtel",
        "bill_payment",
        "any",
        "*500#",
        0,
        "BPay will open the Airtel Money menu. Choose the service you need.",
    ),
    # Balance check — no recipient, no amount, just a menu code.
    ("mtn", "check_balance", "any", "*182*6*1#", 1, None),
    (
        "airtel",
        "check_balance",
        "any",
        "*500#",
        0,
        "BPay will open the Airtel Money menu. Choose My Account, then "
        "Balance to see it.",
    ),
]

## name, description, category, icon, mtn template (or None), airtel
## template (or None), account label, sort order. A None side means BPay
## doesn't offer that service on that network until an admin adds a code
## for it — never a guessed one.
SERVICES = [
    (
        "Electricity (EUCL)",
        "Buy prepaid electricity tokens",
        "electricity",
        "bolt_rounded",
        "*182*2*6*{account}*{amount}#",
        None,
        "Meter number",
        100,
    ),
    (
        "Water (WASAC)",
        "Pay your water bill",
        "water",
        "water_drop_rounded",
        "*182*3*3*{account}*{amount}#",
        None,
        "Customer number",
        90,
    ),
    (
        "Buy Airtime",
        "Top up your own line",
        "airtime",
        "phone_android_rounded",
        "*182*2*1*{amount}#",
        None,
        "Phone number",
        80,
    ),
    (
        "Irembo Services",
        "Government e-services",
        "government",
        "account_balance_outlined",
        "*909#",
        "*909#",
        "Reference number",
        70,
    ),
    (
        "Bank of Kigali",
        "BK mobile banking",
        "banks",
        "account_balance_rounded",
        "*334#",
        "*334#",
        "Account number",
        60,
    ),
    (
        "RSSB",
        "Social security contributions",
        "insurance",
        "shield_outlined",
        "*909#",
        "*909#",
        "Member number",
        50,
    ),
    (
        "TV Subscription",
        "Renew Canal+ / StarTimes",
        "tv",
        "tv_rounded",
        "*182*2*5*{account}*{amount}#",
        None,
        "Decoder number",
        40,
    ),
    (
        "School Fees",
        "Pay tuition via Irembo",
        "education",
        "school_outlined",
        "*909#",
        "*909#",
        "Student code",
        30,
    ),
]


def run_schema():
    """Applies schema.sql, connecting without a database first."""
    cfg = {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
    }
    conn = mysql.connector.connect(**cfg)
    cursor = conn.cursor()
    with open(SCHEMA, encoding="utf-8") as fh:
        raw = fh.read()

    # Strip line comments first: splitting on ";" alone can leave a chunk
    # that is nothing but comments, which the server rejects as empty.
    stripped = chr(10).join(
        line for line in raw.splitlines() if not line.strip().startswith("--")
    )
    for statement in (s.strip() for s in stripped.split(";")):
        if statement:
            cursor.execute(statement)
    conn.commit()
    cursor.close()
    conn.close()
    print("schema applied")


def seed_admin():
    existing = db.query_one("SELECT id FROM admins LIMIT 1")
    if existing:
        print("admin already exists — left untouched")
        return
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    db.execute(
        "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
        (username, hash_password(password)),
    )
    print(f"admin created: {username}")


def seed_templates():
    added = 0
    for sim, ttype, rnet, template, completes, guidance in TEMPLATES:
        exists = db.query_one(
            """SELECT id FROM ussd_templates
               WHERE sim_network=%s AND transaction_type=%s
                 AND recipient_network=%s""",
            (sim, ttype, rnet),
        )
        if exists:
            continue
        db.execute(
            """INSERT INTO ussd_templates
                 (sim_network, transaction_type, recipient_network, template,
                  completes_payment, guidance)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (sim, ttype, rnet, template, completes, guidance),
        )
        added += 1
    print(f"templates added: {added}")


def seed_services():
    added = 0
    for name, desc, cat, icon, mtn_tpl, airtel_tpl, label, order in SERVICES:
        if db.query_one("SELECT id FROM services WHERE name=%s", (name,)):
            continue
        db.execute(
            """INSERT INTO services
                 (name, description, category, icon, ussd_template_mtn,
                  ussd_template_airtel, account_label, sort_order)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (name, desc, cat, icon, mtn_tpl, airtel_tpl, label, order),
        )
        added += 1
    print(f"services added: {added}")


def seed_fee_rules():
    """Inactive, zero-amount rows for both networks — the admin turns them
    on and sets real numbers in the dashboard. Never enabled by default."""
    added = 0
    for network in ("mtn", "airtel"):
        if db.query_one("SELECT id FROM fee_rules WHERE network=%s", (network,)):
            continue
        db.execute(
            """INSERT INTO fee_rules
                 (network, fee_amount, trigger_count, trigger_window, active)
               VALUES (%s, 0, 5, 'month', 0)""",
            (network,),
        )
        added += 1
    print(f"fee rules added: {added}")


def seed_provider_keys():
    """Empty placeholder rows so the admin dashboard always has a row per
    network to edit, instead of conjuring one on first save."""
    added = 0
    for network in ("mtn", "airtel"):
        if db.query_one("SELECT id FROM provider_keys WHERE network=%s", (network,)):
            continue
        db.execute(
            "INSERT INTO provider_keys (network, api_key) VALUES (%s, NULL)",
            (network,),
        )
        added += 1
    print(f"provider key rows added: {added}")


if __name__ == "__main__":
    run_schema()
    seed_admin()
    seed_templates()
    seed_services()
    seed_fee_rules()
    seed_provider_keys()
    print("\nSeed complete. Start the API with:  python app.py")
