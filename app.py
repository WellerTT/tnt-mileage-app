from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

import psycopg2
import psycopg2.extras

import cloudinary
import cloudinary.uploader

from PIL import Image, ImageOps
import imagehash

from datetime import datetime, timedelta, date
from io import BytesIO
from decimal import Decimal, InvalidOperation

import hashlib
import json
import os
import urllib.request
import urllib.error


# =========================================================
# APP SETUP
# =========================================================

app = Flask(__name__)

app.permanent_session_lifetime = timedelta(days=30)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key"
)

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

DATABASE_URL = os.environ.get("DATABASE_URL")

ONESIGNAL_APP_ID = os.environ.get(
    "ONESIGNAL_APP_ID"
)

ONESIGNAL_REST_API_KEY = os.environ.get(
    "ONESIGNAL_REST_API_KEY"
)

cloudinary.config(secure=True)


# =========================================================
# DATABASE CONNECTION
# =========================================================

def get_db():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured."
        )

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


# =========================================================
# DATABASE SETUP
# =========================================================

def init_db():

    conn = get_db()
    cur = conn.cursor()

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
                CHECK(role IN ('admin','driver')),
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    # -----------------------------------------------------
    # MILEAGE
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mileage_entries (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            trip_date TEXT NOT NULL,
            unit_number TEXT,
            pickup_city TEXT,
            delivery_city TEXT,
            beginning_miles REAL NOT NULL,
            ending_miles REAL NOT NULL,
            total_miles REAL NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
    """)

    # -----------------------------------------------------
    # REIMBURSEMENTS
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reimbursements (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expense_date TEXT NOT NULL,
            expense_type TEXT NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            notes TEXT,
            receipt_url TEXT NOT NULL,
            receipt_public_id TEXT,
            receipt_sha256 TEXT,
            receipt_phash TEXT,
            status TEXT NOT NULL DEFAULT 'Unpaid',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
    """)

    cur.execute("""
        ALTER TABLE reimbursements
        ADD COLUMN IF NOT EXISTS receipt_sha256 TEXT
    """)

    cur.execute("""
        ALTER TABLE reimbursements
        ADD COLUMN IF NOT EXISTS receipt_phash TEXT
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_reimbursements_receipt_sha256
        ON reimbursements(receipt_sha256)
    """)

    # -----------------------------------------------------
    # DISPATCHES
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (

            id SERIAL PRIMARY KEY,

            user_id INTEGER NOT NULL,

            pickup_company TEXT,
            pickup_address TEXT NOT NULL,
            pickup_contact TEXT,
            pickup_phone TEXT,

            delivery_company TEXT,
            delivery_address TEXT NOT NULL,
            delivery_contact TEXT,
            delivery_phone TEXT,

            unit_number TEXT,
            vin TEXT,
            truck_type TEXT,

            pickup_datetime TEXT,
            delivery_datetime TEXT,

            instructions TEXT,

            status TEXT NOT NULL
                DEFAULT 'Assigned',

            created_at TEXT NOT NULL,

            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
    """)

    # -----------------------------------------------------
    # SAVED DISPATCH LOCATIONS
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_locations (

            id SERIAL PRIMARY KEY,

            company_name TEXT NOT NULL,
            address TEXT NOT NULL,

            contact_name TEXT,
            phone TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_dispatch_locations_company
        ON dispatch_locations (
            LOWER(company_name)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_dispatch_locations_address
        ON dispatch_locations (
            LOWER(address)
        )
    """)

    conn.commit()

    # -----------------------------------------------------
    # ADMIN ACCOUNT
    # -----------------------------------------------------

    cur.execute(
        """
        SELECT
            id,
            password_hash
        FROM users
        WHERE role=%s
        LIMIT 1
        """,
        ("admin",)
    )

    admin = cur.fetchone()

    admin_password = os.environ.get(
        "ADMIN_PASSWORD"
    )

    if not admin:

        cur.execute(
            """
            INSERT INTO users (
                name,
                username,
                password_hash,
                role
            )
            VALUES (
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                "TNT Admin",
                "admin",
                generate_password_hash(
                    admin_password
                    or "ChangeMeImmediately"
                ),
                "admin"
            )
        )

        conn.commit()

    elif admin_password:

        if not check_password_hash(
            admin["password_hash"],
            admin_password
        ):

            cur.execute(
                """
                UPDATE users
                SET password_hash=%s
                WHERE role='admin'
                """,
                (
                    generate_password_hash(
                        admin_password
                    ),
                )
            )

            conn.commit()

    cur.close()
    conn.close()


@app.before_request
def setup():
    init_db()


# =========================================================
# LOGIN CHECK
# =========================================================

def require_login(role=None):

    if "user_id" not in session:
        return False

    if role and session.get("role") != role:
        return False

    return True


# =========================================================
# ONESIGNAL PUSH NOTIFICATIONS
# =========================================================

def send_push_notification(
    user_id,
    title,
    message,
    dispatch_id=None
):

    if not ONESIGNAL_APP_ID:
        app.logger.warning(
            "ONESIGNAL_APP_ID is not configured."
        )
        return False

    if not ONESIGNAL_REST_API_KEY:
        app.logger.warning(
            "ONESIGNAL_REST_API_KEY is not configured."
        )
        return False

    external_id = (
        f"tnt-user-{user_id}"
    )

    payload = {
        "app_id": ONESIGNAL_APP_ID,

        "target_channel": "push",

        "include_aliases": {
            "external_id": [
                external_id
            ]
        },

        "headings": {
            "en": title
        },

        "contents": {
            "en": message
        }
    }

    if dispatch_id:

        payload["data"] = {
            "type": "dispatch",
            "dispatch_id": dispatch_id
        }

    try:

        body = json.dumps(
            payload
        ).encode("utf-8")

        api_request = urllib.request.Request(
            "https://api.onesignal.com/notifications?c=push",
            data=body,
            headers={
                "Content-Type":
                    "application/json; charset=utf-8",

                "Authorization":
                    f"Key {ONESIGNAL_REST_API_KEY}"
            },
            method="POST"
        )

        with urllib.request.urlopen(
            api_request,
            timeout=15
        ) as response:

            response_body = (
                response.read()
                .decode("utf-8")
            )

            app.logger.info(
                "OneSignal notification sent "
                "to %s: %s",
                external_id,
                response_body
            )

        return True

    except urllib.error.HTTPError as e:

        error_body = ""

        try:
            error_body = (
                e.read()
                .decode("utf-8")
            )
        except Exception:
            pass

        app.logger.error(
            "OneSignal HTTP error %s: %s",
            e.code,
            error_body
        )

        return False

    except Exception:

        app.logger.exception(
            "ONESIGNAL PUSH FAILED"
        )

        return False


# =========================================================
# SAVED LOCATION HELPERS
# =========================================================

def save_dispatch_location(
    company_name,
    address,
    contact_name,
    phone
):

    company_name = (
        company_name or ""
    ).strip()

    address = (
        address or ""
    ).strip()

    contact_name = (
        contact_name or ""
    ).strip()

    phone = (
        phone or ""
    ).strip()

    if not company_name or not address:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id
        FROM dispatch_locations

        WHERE LOWER(company_name)=LOWER(%s)

        AND LOWER(address)=LOWER(%s)

        LIMIT 1
        """,
        (
            company_name,
            address
        )
    )

    existing = cur.fetchone()

    now = datetime.now().isoformat(
        timespec="seconds"
    )

    if existing:

        cur.execute(
            """
            UPDATE dispatch_locations

            SET
                company_name=%s,
                address=%s,
                contact_name=%s,
                phone=%s,
                updated_at=%s

            WHERE id=%s
            """,
            (
                company_name,
                address,
                contact_name,
                phone,
                now,
                existing["id"]
            )
        )

    else:

        cur.execute(
            """
            INSERT INTO dispatch_locations (

                company_name,
                address,
                contact_name,
                phone,
                created_at,
                updated_at

            )

            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                company_name,
                address,
                contact_name,
                phone,
                now,
                now
            )
        )

    conn.commit()

    cur.close()
    conn.close()


# =========================================================
# RECEIPT DUPLICATE SCANNER
# =========================================================

def create_receipt_fingerprints(
    receipt_bytes
):

    sha256_hash = hashlib.sha256(
        receipt_bytes
    ).hexdigest()

    perceptual_hash = None

    try:

        image = Image.open(
            BytesIO(receipt_bytes)
        )

        image = ImageOps.exif_transpose(
            image
        )

        image = image.convert("RGB")

        perceptual_hash = str(
            imagehash.phash(image)
        )

    except Exception as e:

        app.logger.warning(
            "Could not create perceptual hash: %s",
            e
        )

    return (
        sha256_hash,
        perceptual_hash
    )


def find_duplicate_receipt(
    sha256_hash,
    perceptual_hash
):

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            r.id,
            r.expense_date,
            r.expense_type,
            r.amount,
            u.name AS driver_name

        FROM reimbursements r

        JOIN users u
            ON u.id = r.user_id

        WHERE r.receipt_sha256=%s

        LIMIT 1
        """,
        (
            sha256_hash,
        )
    )

    exact_duplicate = cur.fetchone()

    if exact_duplicate:

        cur.close()
        conn.close()

        return {
            "type": "exact",
            "entry": exact_duplicate
        }

    if perceptual_hash:

        cur.execute(
            """
            SELECT
                r.id,
                r.user_id,
                r.expense_date,
                r.expense_type,
                r.amount,
                r.receipt_phash,
                u.name AS driver_name

            FROM reimbursements r

            JOIN users u
                ON u.id = r.user_id

            WHERE r.receipt_phash
                IS NOT NULL
            """
        )

        previous_receipts = cur.fetchall()

        new_hash = imagehash.hex_to_hash(
            perceptual_hash
        )

        for previous in previous_receipts:

            try:

                old_hash = imagehash.hex_to_hash(
                    previous[
                        "receipt_phash"
                    ]
                )

                distance = (
                    new_hash - old_hash
                )

                if distance <= 4:

                    cur.close()
                    conn.close()

                    return {
                        "type": "visual",
                        "entry": previous,
                        "distance": distance
                    }

            except Exception:
                continue

    cur.close()
    conn.close()

    return None


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/",
    methods=["GET", "POST"]
)
def login():

    if (
        request.method == "GET"
        and "user_id" in session
    ):

        if session.get("role") == "admin":

            return redirect(
                url_for(
                    "admin_dashboard"
                )
            )

        return redirect(
            url_for(
                "driver_dashboard"
            )
        )

    if request.method == "POST":

        username = request.form[
            "username"
        ].strip()

        password = request.form[
            "password"
        ]

        remember_me = (
            request.form.get(
                "remember_me"
            ) == "yes"
        )

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *

            FROM users

            WHERE username=%s

            AND active=1
            """,
            (
                username,
            )
        )

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            session.clear()

            session.permanent = (
                remember_me
            )

            session["user_id"] = (
                user["id"]
            )

            session["name"] = (
                user["name"]
            )

            session["role"] = (
                user["role"]
            )

            if user["role"] == "admin":

                return redirect(
                    url_for(
                        "admin_dashboard"
                    )
                )

            return redirect(
                url_for(
                    "driver_dashboard"
                )
            )

        flash(
            "Invalid username or password.",
            "error"
        )

    return render_template(
        "login.html"
    )


# =========================================================
# FORGOT PASSWORD
# =========================================================

@app.route(
    "/forgot-password",
    methods=["GET", "POST"]
)
def forgot_password():

    if request.method == "POST":

        username = request.form[
            "username"
        ].strip()

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *

            FROM users

            WHERE username=%s

            AND active=1
            """,
            (
                username,
            )
        )

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user:

            flash(
                "Your password reset request has been received. "
                "Please contact TNT Admin for a temporary password.",
                "success"
            )

        else:

            flash(
                "Username not found. "
                "Please check your username and try again.",
                "error"
            )

    return render_template(
        "forgot_password.html"
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# DRIVER MILEAGE
# =========================================================

@app.route(
    "/driver",
    methods=["GET", "POST"]
)
def driver_dashboard():

    if not require_login(
        "driver"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":

        trip_date = request.form[
            "trip_date"
        ]

        unit_number = request.form.get(
            "unit_number",
            ""
        ).strip()

        pickup_city = request.form.get(
            "pickup_city",
            ""
        ).strip()

        delivery_city = request.form.get(
            "delivery_city",
            ""
        ).strip()

        notes = request.form.get(
            "notes",
            ""
        ).strip()

        try:

            beginning = float(
                request.form[
                    "beginning_miles"
                ]
            )

            ending = float(
                request.form[
                    "ending_miles"
                ]
            )

        except ValueError:

            cur.close()
            conn.close()

            flash(
                "Beginning and ending miles must be numbers.",
                "error"
            )

            return redirect(
                url_for(
                    "driver_dashboard"
                )
            )

        if ending < beginning:

            cur.close()
            conn.close()

            flash(
                "Ending miles cannot be less than beginning miles.",
                "error"
            )

            return redirect(
                url_for(
                    "driver_dashboard"
                )
            )

        total = (
            ending - beginning
        )

        cur.execute(
            """
            INSERT INTO mileage_entries (

                user_id,
                trip_date,
                unit_number,
                pickup_city,
                delivery_city,
                beginning_miles,
                ending_miles,
                total_miles,
                notes,
                created_at

            )

            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                session[
                    "user_id"
                ],
                trip_date,
                unit_number,
                pickup_city,
                delivery_city,
                beginning,
                ending,
                total,
                notes,
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )

        conn.commit()

        flash(
            f"Mileage submitted: "
            f"{total:,.1f} miles.",
            "success"
        )

    cur.execute(
        """
        SELECT *

        FROM mileage_entries

        WHERE user_id=%s

        ORDER BY
            trip_date DESC,
            id DESC
        """,
        (
            session[
                "user_id"
            ],
        )
    )

    entries = cur.fetchall()

    cur.execute(
        """
        SELECT

            COALESCE(
                SUM(total_miles),
                0
            ) AS total

        FROM mileage_entries

        WHERE user_id=%s
        """,
        (
            session[
                "user_id"
            ],
        )
    )

    total_row = cur.fetchone()

    total_miles = (
        total_row[
            "total"
        ]
    )

    cur.close()
    conn.close()

    return render_template(
        "driver.html",
        entries=entries,
        total_miles=total_miles
    )


# =========================================================
# DRIVER REIMBURSEMENTS
# =========================================================

@app.route(
    "/reimbursements",
    methods=["GET", "POST"]
)
def reimbursements():

    if not require_login(
        "driver"
    ):

        return redirect(
            url_for("login")
        )

    if request.method == "POST":

        expense_date = request.form.get(
            "expense_date",
            ""
        ).strip()

        expense_type = request.form.get(
            "expense_type",
            ""
        ).strip()

        amount_text = request.form.get(
            "amount",
            ""
        ).strip()

        notes = request.form.get(
            "notes",
            ""
        ).strip()

        receipt = request.files.get(
            "receipt"
        )

        if not expense_date:

            flash(
                "Please enter the expense date.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        if not expense_type:

            flash(
                "Please select an expense type.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        try:

            amount = Decimal(
                amount_text
            ).quantize(
                Decimal("0.01")
            )

        except (
            InvalidOperation,
            ValueError
        ):

            flash(
                "Please enter a valid reimbursement amount.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        if amount <= 0:

            flash(
                "Reimbursement amount must be greater than $0.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        try:

            submitted_expense_date = (
                datetime.strptime(
                    expense_date,
                    "%Y-%m-%d"
                ).date()
            )

        except ValueError:

            flash(
                "Please enter a valid expense date.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        today = date.today()

        if submitted_expense_date > today:

            flash(
                "Expense date cannot be in the future.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        oldest_allowed_date = (
            today
            - timedelta(days=7)
        )

        if (
            submitted_expense_date
            < oldest_allowed_date
        ):

            flash(
                "Reimbursements must be submitted within "
                "7 days of the expense date.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id

            FROM reimbursements

            WHERE user_id=%s

            AND expense_date=%s

            AND LOWER(
                expense_type
            )=LOWER(%s)

            AND amount=%s

            LIMIT 1
            """,
            (
                session[
                    "user_id"
                ],
                expense_date,
                expense_type,
                amount
            )
        )

        duplicate = cur.fetchone()

        cur.close()
        conn.close()

        if duplicate:

            flash(
                "Possible duplicate reimbursement. "
                "This expense has already been submitted.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        if (
            not receipt
            or receipt.filename == ""
        ):

            flash(
                "Please upload a receipt photo.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        if not receipt.mimetype.startswith(
            "image/"
        ):

            flash(
                "Receipt must be an image.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        try:

            receipt_bytes = (
                receipt.read()
            )

        except Exception:

            flash(
                "The receipt image could not be read.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        if not receipt_bytes:

            flash(
                "The receipt image is empty.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        (
            receipt_sha256,
            receipt_phash
        ) = create_receipt_fingerprints(
            receipt_bytes
        )

        image_duplicate = (
            find_duplicate_receipt(
                receipt_sha256,
                receipt_phash
            )
        )

        if image_duplicate:

            if (
                image_duplicate[
                    "type"
                ] == "exact"
            ):

                flash(
                    "Duplicate receipt detected. "
                    "This exact receipt image has already "
                    "been submitted.",
                    "error"
                )

            else:

                flash(
                    "Possible duplicate receipt detected. "
                    "This receipt appears to match a receipt "
                    "that was previously submitted.",
                    "error"
                )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        uploaded_public_id = None

        try:

            receipt_file = BytesIO(
                receipt_bytes
            )

            receipt_file.name = (
                receipt.filename
                or "receipt.jpg"
            )

            upload_result = (
                cloudinary.uploader.upload(
                    receipt_file,
                    folder=(
                        "tnt_mileage/"
                        "reimbursements"
                    ),
                    resource_type="image"
                )
            )

            receipt_url = (
                upload_result[
                    "secure_url"
                ]
            )

            uploaded_public_id = (
                upload_result[
                    "public_id"
                ]
            )

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT id

                FROM reimbursements

                WHERE receipt_sha256=%s

                LIMIT 1
                """,
                (
                    receipt_sha256,
                )
            )

            duplicate_after_upload = (
                cur.fetchone()
            )

            if duplicate_after_upload:

                cur.close()
                conn.close()

                try:

                    cloudinary.uploader.destroy(
                        uploaded_public_id
                    )

                except Exception:
                    pass

                flash(
                    "Duplicate receipt detected. "
                    "This receipt has already been submitted.",
                    "error"
                )

                return redirect(
                    url_for(
                        "reimbursements"
                    )
                )

            cur.execute(
                """
                INSERT INTO reimbursements (

                    user_id,
                    expense_date,
                    expense_type,
                    amount,
                    notes,
                    receipt_url,
                    receipt_public_id,
                    receipt_sha256,
                    receipt_phash,
                    status,
                    created_at

                )

                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    session[
                        "user_id"
                    ],
                    expense_date,
                    expense_type,
                    amount,
                    notes,
                    receipt_url,
                    uploaded_public_id,
                    receipt_sha256,
                    receipt_phash,
                    "Unpaid",
                    datetime.now().isoformat(
                        timespec="seconds"
                    )
                )
            )

            conn.commit()

            cur.close()
            conn.close()

            flash(
                "Reimbursement submitted successfully.",
                "success"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

        except Exception:

            if uploaded_public_id:

                try:

                    cloudinary.uploader.destroy(
                        uploaded_public_id
                    )

                except Exception:
                    pass

            app.logger.exception(
                "REIMBURSEMENT UPLOAD FAILED"
            )

            flash(
                "There was a problem uploading your reimbursement. "
                "Please try again.",
                "error"
            )

            return redirect(
                url_for(
                    "reimbursements"
                )
            )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *

        FROM reimbursements

        WHERE user_id=%s

        ORDER BY
            expense_date DESC,
            id DESC
        """,
        (
            session[
                "user_id"
            ],
        )
    )

    reimbursement_entries = (
        cur.fetchall()
    )

    cur.close()
    conn.close()

    return render_template(
        "reimbursements.html",
        reimbursements=(
            reimbursement_entries
        )
    )


# =========================================================
# DRIVER DISPATCHES
# =========================================================

@app.route(
    "/dispatches"
)
def driver_dispatches():

    if not require_login(
        "driver"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *

        FROM dispatches

        WHERE user_id=%s

        ORDER BY

            CASE status

                WHEN 'Assigned'
                    THEN 1

                WHEN 'Picked Up'
                    THEN 2

                WHEN 'Delivered'
                    THEN 3

                ELSE 4

            END,

            id DESC
        """,
        (
            session[
                "user_id"
            ],
        )
    )

    dispatches = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "dispatches.html",
        dispatches=dispatches
    )


# =========================================================
# DRIVER UPDATE DISPATCH STATUS
# =========================================================

@app.route(
    "/dispatch/<int:dispatch_id>/status",
    methods=["POST"]
)
def update_dispatch_status(
    dispatch_id
):

    if not require_login(
        "driver"
    ):

        return redirect(
            url_for("login")
        )

    new_status = request.form.get(
        "status",
        ""
    )

    allowed_statuses = [
        "Assigned",
        "Picked Up",
        "Delivered"
    ]

    if new_status not in allowed_statuses:

        flash(
            "Invalid dispatch status.",
            "error"
        )

        return redirect(
            url_for(
                "driver_dispatches"
            )
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE dispatches

        SET status=%s

        WHERE id=%s

        AND user_id=%s
        """,
        (
            new_status,
            dispatch_id,
            session[
                "user_id"
            ]
        )
    )

    updated = cur.rowcount

    conn.commit()

    cur.close()
    conn.close()

    if updated:

        flash(
            f"Dispatch marked {new_status}.",
            "success"
        )

    else:

        flash(
            "Dispatch was not found.",
            "error"
        )

    return redirect(
        url_for(
            "driver_dispatches"
        )
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
def admin_dashboard():

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            m.*,
            u.name AS driver_name,
            u.username

        FROM mileage_entries m

        JOIN users u
            ON u.id = m.user_id

        ORDER BY
            m.trip_date DESC,
            m.id DESC
    """)

    entries = cur.fetchall()

    cur.execute("""
        SELECT

            u.id,
            u.name,
            u.username,

            COALESCE(
                SUM(m.total_miles),
                0
            ) AS total_miles,

            COUNT(m.id)
                AS entry_count

        FROM users u

        LEFT JOIN mileage_entries m
            ON m.user_id = u.id

        WHERE
            u.role='driver'
            AND u.active=1

        GROUP BY
            u.id,
            u.name,
            u.username

        ORDER BY
            u.name
    """)

    driver_totals = (
        cur.fetchall()
    )

    cur.execute("""
        SELECT

            COALESCE(
                SUM(total_miles),
                0
            ) AS total

        FROM mileage_entries
    """)

    grand_total_row = (
        cur.fetchone()
    )

    grand_total = (
        grand_total_row[
            "total"
        ]
    )

    cur.close()
    conn.close()

    return render_template(
        "admin.html",
        entries=entries,
        driver_totals=driver_totals,
        grand_total=grand_total
    )


# =========================================================
# DELETE MILEAGE
# =========================================================

@app.route(
    "/admin/delete-entry/<int:entry_id>",
    methods=["POST"]
)
def delete_entry(
    entry_id
):

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM mileage_entries

        WHERE id=%s
        """,
        (
            entry_id,
        )
    )

    deleted = cur.rowcount

    conn.commit()

    cur.close()
    conn.close()

    if deleted:

        flash(
            "Mileage entry deleted.",
            "success"
        )

    else:

        flash(
            "Mileage entry was not found.",
            "error"
        )

    return redirect(
        url_for(
            "admin_dashboard"
        )
    )


# =========================================================
# ADMIN REIMBURSEMENTS
# =========================================================

@app.route(
    "/admin/reimbursements"
)
def admin_reimbursements():

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            r.*,
            u.name AS driver_name,
            u.username

        FROM reimbursements r

        JOIN users u
            ON u.id = r.user_id

        ORDER BY
            u.name ASC,
            r.expense_date DESC,
            r.id DESC
    """)

    reimbursement_entries = (
        cur.fetchall()
    )

    cur.execute("""
        SELECT

            COALESCE(
                SUM(amount),
                0
            ) AS total

        FROM reimbursements

        WHERE status='Unpaid'
    """)

    unpaid_row = (
        cur.fetchone()
    )

    unpaid_total = (
        unpaid_row[
            "total"
        ]
    )

    cur.close()
    conn.close()

    return render_template(
        "admin_reimbursements.html",
        reimbursements=(
            reimbursement_entries
        ),
        unpaid_total=unpaid_total
    )


# =========================================================
# UPDATE REIMBURSEMENT STATUS
# =========================================================

@app.route(
    "/admin/reimbursement/"
    "<int:reimbursement_id>/status",
    methods=["POST"]
)
def update_reimbursement_status(
    reimbursement_id
):

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    status = request.form.get(
        "status",
        "Unpaid"
    )

    if status not in [
        "Paid",
        "Unpaid"
    ]:

        status = "Unpaid"

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE reimbursements

        SET status=%s

        WHERE id=%s
        """,
        (
            status,
            reimbursement_id
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    flash(
        f"Reimbursement marked {status}.",
        "success"
    )

    return redirect(
        url_for(
            "admin_reimbursements"
        )
    )


# =========================================================
# DELETE REIMBURSEMENT
# =========================================================

@app.route(
    "/admin/delete-reimbursement/"
    "<int:reimbursement_id>",
    methods=["POST"]
)
def delete_reimbursement(
    reimbursement_id
):

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT receipt_public_id

        FROM reimbursements

        WHERE id=%s
        """,
        (
            reimbursement_id,
        )
    )

    reimbursement = (
        cur.fetchone()
    )

    if not reimbursement:

        cur.close()
        conn.close()

        flash(
            "Reimbursement was not found.",
            "error"
        )

        return redirect(
            url_for(
                "admin_reimbursements"
            )
        )

    public_id = reimbursement[
        "receipt_public_id"
    ]

    cur.execute(
        """
        DELETE FROM reimbursements

        WHERE id=%s
        """,
        (
            reimbursement_id,
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    if public_id:

        try:

            cloudinary.uploader.destroy(
                public_id
            )

        except Exception as e:

            app.logger.warning(
                "Cloudinary delete error: %s",
                e
            )

    flash(
        "Reimbursement deleted.",
        "success"
    )

    return redirect(
        url_for(
            "admin_reimbursements"
        )
    )


# =========================================================
# SAVED LOCATION AUTOCOMPLETE API
# =========================================================

@app.route(
    "/admin/location-search"
)
def location_search():

    if not require_login(
        "admin"
    ):

        return jsonify([]), 403

    query = request.args.get(
        "q",
        ""
    ).strip()

    if len(query) < 1:
        return jsonify([])

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            company_name,
            address,
            contact_name,
            phone

        FROM dispatch_locations

        WHERE
            company_name ILIKE %s

            OR address ILIKE %s

        ORDER BY
            company_name ASC,
            address ASC

        LIMIT 15
        """,
        (
            f"%{query}%",
            f"%{query}%"
        )
    )

    locations = cur.fetchall()

    cur.close()
    conn.close()

    results = []

    for location in locations:

        results.append({
            "id": location["id"],

            "company_name":
                location[
                    "company_name"
                ],

            "address":
                location[
                    "address"
                ],

            "contact_name":
                location[
                    "contact_name"
                ] or "",

            "phone":
                location[
                    "phone"
                ] or ""
        })

    return jsonify(results)


# =========================================================
# ADMIN DISPATCHES
# =========================================================

@app.route(
    "/admin/dispatches",
    methods=["GET", "POST"]
)
def admin_dispatches():

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    # -----------------------------------------------------
    # CREATE DISPATCH
    # -----------------------------------------------------

    if request.method == "POST":

        driver_id = request.form.get(
            "driver_id"
        )

        pickup_company = request.form.get(
            "pickup_company",
            ""
        ).strip()

        pickup_address = request.form.get(
            "pickup_address",
            ""
        ).strip()

        pickup_contact = request.form.get(
            "pickup_contact",
            ""
        ).strip()

        pickup_phone = request.form.get(
            "pickup_phone",
            ""
        ).strip()

        delivery_company = request.form.get(
            "delivery_company",
            ""
        ).strip()

        delivery_address = request.form.get(
            "delivery_address",
            ""
        ).strip()

        delivery_contact = request.form.get(
            "delivery_contact",
            ""
        ).strip()

        delivery_phone = request.form.get(
            "delivery_phone",
            ""
        ).strip()

        unit_number = request.form.get(
            "unit_number",
            ""
        ).strip()

        vin = request.form.get(
            "vin",
            ""
        ).strip()

        truck_type = request.form.get(
            "truck_type",
            ""
        ).strip()

        pickup_datetime = request.form.get(
            "pickup_datetime",
            ""
        ).strip()

        delivery_datetime = request.form.get(
            "delivery_datetime",
            ""
        ).strip()

        instructions = request.form.get(
            "instructions",
            ""
        ).strip()

        if not driver_id:

            flash(
                "Please select a contractor.",
                "error"
            )

            return redirect(
                url_for(
                    "admin_dispatches"
                )
            )

        if not pickup_address:

            flash(
                "Pickup address is required.",
                "error"
            )

            return redirect(
                url_for(
                    "admin_dispatches"
                )
            )

        if not delivery_address:

            flash(
                "Delivery address is required.",
                "error"
            )

            return redirect(
                url_for(
                    "admin_dispatches"
                )
            )

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                name

            FROM users

            WHERE id=%s

            AND role='driver'

            AND active=1
            """,
            (
                driver_id,
            )
        )

        driver = cur.fetchone()

        if not driver:

            cur.close()
            conn.close()

            flash(
                "That contractor could not be found.",
                "error"
            )

            return redirect(
                url_for(
                    "admin_dispatches"
                )
            )

        cur.execute(
            """
            INSERT INTO dispatches (

                user_id,

                pickup_company,
                pickup_address,
                pickup_contact,
                pickup_phone,

                delivery_company,
                delivery_address,
                delivery_contact,
                delivery_phone,

                unit_number,
                vin,
                truck_type,

                pickup_datetime,
                delivery_datetime,

                instructions,

                status,
                created_at

            )

            VALUES (

                %s,

                %s,
                %s,
                %s,
                %s,

                %s,
                %s,
                %s,
                %s,

                %s,
                %s,
                %s,

                %s,
                %s,

                %s,

                %s,
                %s

            )

            RETURNING id
            """,
            (
                driver_id,

                pickup_company,
                pickup_address,
                pickup_contact,
                pickup_phone,

                delivery_company,
                delivery_address,
                delivery_contact,
                delivery_phone,

                unit_number,
                vin,
                truck_type,

                pickup_datetime,
                delivery_datetime,

                instructions,

                "Assigned",

                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )

        new_dispatch = cur.fetchone()

        dispatch_id = (
            new_dispatch["id"]
        )

        conn.commit()

        cur.close()
        conn.close()

        # -------------------------------------------------
        # REMEMBER PICKUP LOCATION
        # -------------------------------------------------

        save_dispatch_location(
            pickup_company,
            pickup_address,
            pickup_contact,
            pickup_phone
        )

        # -------------------------------------------------
        # REMEMBER DELIVERY LOCATION
        # -------------------------------------------------

        save_dispatch_location(
            delivery_company,
            delivery_address,
            delivery_contact,
            delivery_phone
        )

        # -------------------------------------------------
        # SEND PUSH NOTIFICATION
        # -------------------------------------------------

        pickup_name = (
            pickup_company
            or pickup_address
        )

        delivery_name = (
            delivery_company
            or delivery_address
        )

        notification_message = (
            f"You have been assigned a new dispatch: "
            f"{pickup_name} → {delivery_name}."
        )

        if unit_number:

            notification_message += (
                f" Unit {unit_number}."
            )

        notification_sent = (
            send_push_notification(
                driver_id,
                "New TNT Dispatch",
                notification_message,
                dispatch_id
            )
        )

        if notification_sent:

            flash(
                f"Dispatch sent to {driver['name']} "
                "and push notification sent.",
                "success"
            )

        else:

            flash(
                f"Dispatch was saved for {driver['name']}, "
                "but the push notification could not be sent.",
                "error"
            )

        return redirect(
            url_for(
                "admin_dispatches"
            )
        )

    # -----------------------------------------------------
    # GET DRIVERS + DISPATCHES
    # -----------------------------------------------------

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            name,
            username

        FROM users

        WHERE role='driver'

        AND active=1

        ORDER BY name
        """
    )

    drivers = cur.fetchall()

    cur.execute(
        """
        SELECT
            d.*,
            u.name AS driver_name,
            u.username

        FROM dispatches d

        JOIN users u
            ON u.id = d.user_id

        ORDER BY

            CASE d.status

                WHEN 'Assigned'
                    THEN 1

                WHEN 'Picked Up'
                    THEN 2

                WHEN 'Delivered'
                    THEN 3

                ELSE 4

            END,

            d.id DESC
        """
    )

    dispatches = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "admin_dispatches.html",
        drivers=drivers,
        dispatches=dispatches
    )


# =========================================================
# ADMIN DELETE DISPATCH
# =========================================================

@app.route(
    "/admin/delete-dispatch/"
    "<int:dispatch_id>",
    methods=["POST"]
)
def delete_dispatch(
    dispatch_id
):

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM dispatches

        WHERE id=%s
        """,
        (
            dispatch_id,
        )
    )

    deleted = cur.rowcount

    conn.commit()

    cur.close()
    conn.close()

    if deleted:

        flash(
            "Dispatch deleted.",
            "success"
        )

    else:

        flash(
            "Dispatch was not found.",
            "error"
        )

    return redirect(
        url_for(
            "admin_dispatches"
        )
    )


# =========================================================
# ADD DRIVER
# =========================================================

@app.route(
    "/admin/add-driver",
    methods=["GET", "POST"]
)
def add_driver():

    if not require_login(
        "admin"
    ):

        return redirect(
            url_for("login")
        )

    if request.method == "POST":

        name = request.form[
            "name"
        ].strip()

        username = request.form[
            "username"
        ].strip()

        password = request.form[
            "password"
        ]

        if (
            not name
            or not username
            or not password
        ):

            flash(
                "All fields are required.",
                "error"
            )

            return redirect(
                url_for(
                    "add_driver"
                )
            )

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                INSERT INTO users (

                    name,
                    username,
                    password_hash,
                    role

                )

                VALUES (
                    %s,
                    %s,
                    %s,
                    'driver'
                )
                """,
                (
                    name,
                    username,
                    generate_password_hash(
                        password
                    )
                )
            )

            conn.commit()

            flash(
                f"Driver {name} added.",
                "success"
            )

        except psycopg2.IntegrityError:

            conn.rollback()

            flash(
                "That username already exists.",
                "error"
            )

        finally:

            cur.close()
            conn.close()

        return redirect(
            url_for(
                "add_driver"
            )
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            name,
            username,
            active

        FROM users

        WHERE role='driver'

        ORDER BY name
    """)

    drivers = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "add_driver.html",
        drivers=drivers
    )


# =========================================================
# START APP
# =========================================================

if __name__ == "__main__":

    init_db()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
