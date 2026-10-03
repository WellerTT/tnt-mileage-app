from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

import psycopg2
import psycopg2.extras

import cloudinary
import cloudinary.uploader

from datetime import datetime, timedelta
import os


app = Flask(__name__)

app.permanent_session_lifetime = timedelta(days=30)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

# Maximum upload size: 10 MB
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

DATABASE_URL = os.environ.get("DATABASE_URL")

# Cloudinary automatically reads CLOUDINARY_URL
# from your Render environment variables.
cloudinary.config(secure=True)


# ---------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------

def get_db():

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


# ---------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------

def init_db():

    conn = get_db()
    cur = conn.cursor()

    # USERS TABLE
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','driver')),
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    # MILEAGE TABLE
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
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # REIMBURSEMENTS TABLE
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
            status TEXT NOT NULL DEFAULT 'Unpaid',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.commit()

    # CHECK FOR ADMIN
    cur.execute(
        """
        SELECT id, password_hash
        FROM users
        WHERE role=%s
        LIMIT 1
        """,
        ("admin",)
    )

    admin = cur.fetchone()

    admin_password = os.environ.get("ADMIN_PASSWORD")

    # CREATE ADMIN IF NEEDED
    if not admin:

        cur.execute(
            """
            INSERT INTO users (
                name,
                username,
                password_hash,
                role
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                "TNT Admin",
                "admin",
                generate_password_hash(
                    admin_password or "ChangeMeImmediately"
                ),
                "admin"
            )
        )

        conn.commit()

    # UPDATE ADMIN PASSWORD ONLY IF IT CHANGED
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
                    generate_password_hash(admin_password),
                )
            )

            conn.commit()

    cur.close()
    conn.close()


@app.before_request
def setup():

    init_db()


# ---------------------------------------------------------
# LOGIN HELPER
# ---------------------------------------------------------

def require_login(role=None):

    if "user_id" not in session:
        return False

    if role and session.get("role") != role:
        return False

    return True


# ---------------------------------------------------------
# LOGIN
# ---------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def login():

    if request.method == "GET" and "user_id" in session:

        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))

        return redirect(url_for("driver_dashboard"))

    if request.method == "POST":

        username = request.form["username"].strip()
        password = request.form["password"]

        remember_me = (
            request.form.get("remember_me") == "yes"
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
            (username,)
        )

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            session.clear()

            session.permanent = remember_me

            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]

            if user["role"] == "admin":
                return redirect(
                    url_for("admin_dashboard")
                )

            return redirect(
                url_for("driver_dashboard")
            )

        flash(
            "Invalid username or password.",
            "error"
        )

    return render_template("login.html")


# ---------------------------------------------------------
# FORGOT PASSWORD
# ---------------------------------------------------------

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
            (username,)
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


# ---------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ---------------------------------------------------------
# DRIVER MILEAGE DASHBOARD
# ---------------------------------------------------------

@app.route(
    "/driver",
    methods=["GET", "POST"]
)
def driver_dashboard():

    if not require_login("driver"):
        return redirect(
            url_for("login")
        )

    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":

        trip_date = request.form["trip_date"]

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
                request.form["beginning_miles"]
            )

            ending = float(
                request.form["ending_miles"]
            )

        except ValueError:

            cur.close()
            conn.close()

            flash(
                "Beginning and ending miles must be numbers.",
                "error"
            )

            return redirect(
                url_for("driver_dashboard")
            )

        if ending < beginning:

            cur.close()
            conn.close()

            flash(
                "Ending miles cannot be less than beginning miles.",
                "error"
            )

            return redirect(
                url_for("driver_dashboard")
            )

        total = ending - beginning

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
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                session["user_id"],
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
        ORDER BY trip_date DESC, id DESC
        """,
        (
            session["user_id"],
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
            session["user_id"],
        )
    )

    total_row = cur.fetchone()

    total_miles = total_row["total"]

    cur.close()
    conn.close()

    return render_template(
        "driver.html",
        entries=entries,
        total_miles=total_miles
    )


# ---------------------------------------------------------
# DRIVER REIMBURSEMENTS
# ---------------------------------------------------------

@app.route(
    "/reimbursements",
    methods=["GET", "POST"]
)
def reimbursements():

    if not require_login("driver"):
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

        receipt = request.files.get("receipt")

        # VALIDATE REQUIRED FIELDS
        if not expense_date:

            flash(
                "Please enter the expense date.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

        if not expense_type:

            flash(
                "Please select an expense type.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

        try:

            amount = float(amount_text)

        except ValueError:

            flash(
                "Please enter a valid reimbursement amount.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

        if amount <= 0:

            flash(
                "Reimbursement amount must be greater than $0.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

        if not receipt or receipt.filename == "":

            flash(
                "Please upload a receipt photo.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

        # ONLY ALLOW IMAGE FILES
        if not receipt.mimetype.startswith("image/"):

            flash(
                "Receipt must be an image.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

        uploaded_public_id = None

        try:

            # UPLOAD RECEIPT TO CLOUDINARY
            upload_result = cloudinary.uploader.upload(
                receipt,
                folder="tnt_mileage/reimbursements",
                resource_type="image"
            )

            receipt_url = upload_result[
                "secure_url"
            ]

            uploaded_public_id = upload_result[
                "public_id"
            ]

            conn = get_db()
            cur = conn.cursor()

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
                    status,
                    created_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    session["user_id"],
                    expense_date,
                    expense_type,
                    amount,
                    notes,
                    receipt_url,
                    uploaded_public_id,
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
                url_for("reimbursements")
            )

        except Exception as e:

            # REMOVE IMAGE IF DATABASE SAVE FAILED
            if uploaded_public_id:

                try:

                    cloudinary.uploader.destroy(
                        uploaded_public_id
                    )

                except Exception:
                    pass

            print(
                "Reimbursement upload error:",
                e
            )

            flash(
                "There was a problem uploading your reimbursement. "
                "Please try again.",
                "error"
            )

            return redirect(
                url_for("reimbursements")
            )

    # SHOW DRIVER'S OWN REIMBURSEMENTS
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM reimbursements
        WHERE user_id=%s
        ORDER BY expense_date DESC, id DESC
        """,
        (
            session["user_id"],
        )
    )

    reimbursement_entries = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "reimbursements.html",
        reimbursements=reimbursement_entries
    )


# ---------------------------------------------------------
# ADMIN DASHBOARD
# ---------------------------------------------------------

@app.route("/admin")
def admin_dashboard():

    if not require_login("admin"):
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
            COUNT(m.id) AS entry_count
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

    driver_totals = cur.fetchall()

    cur.execute("""
        SELECT
            COALESCE(
                SUM(total_miles),
                0
            ) AS total
        FROM mileage_entries
    """)

    grand_total_row = cur.fetchone()

    grand_total = grand_total_row["total"]

    cur.close()
    conn.close()

    return render_template(
        "admin.html",
        entries=entries,
        driver_totals=driver_totals,
        grand_total=grand_total
    )


# ---------------------------------------------------------
# DELETE MILEAGE ENTRY
# ---------------------------------------------------------

@app.route(
    "/admin/delete-entry/<int:entry_id>",
    methods=["POST"]
)
def delete_entry(entry_id):

    if not require_login("admin"):
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
        url_for("admin_dashboard")
    )


# ---------------------------------------------------------
# ADMIN REIMBURSEMENTS
# ---------------------------------------------------------

@app.route("/admin/reimbursements")
def admin_reimbursements():

    if not require_login("admin"):
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
            r.expense_date DESC,
            r.id DESC
    """)

    reimbursement_entries = cur.fetchall()

    cur.execute("""
        SELECT
            COALESCE(
                SUM(amount),
                0
            ) AS total
        FROM reimbursements
        WHERE status='Unpaid'
    """)

    unpaid_row = cur.fetchone()

    unpaid_total = unpaid_row["total"]

    cur.close()
    conn.close()

    return render_template(
        "admin_reimbursements.html",
        reimbursements=reimbursement_entries,
        unpaid_total=unpaid_total
    )


# ---------------------------------------------------------
# UPDATE REIMBURSEMENT STATUS
# ---------------------------------------------------------

@app.route(
    "/admin/reimbursement/<int:reimbursement_id>/status",
    methods=["POST"]
)
def update_reimbursement_status(
    reimbursement_id
):

    if not require_login("admin"):
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
        url_for("admin_reimbursements")
    )


# ---------------------------------------------------------
# DELETE REIMBURSEMENT
# ---------------------------------------------------------

@app.route(
    "/admin/delete-reimbursement/<int:reimbursement_id>",
    methods=["POST"]
)
def delete_reimbursement(
    reimbursement_id
):

    if not require_login("admin"):
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

    reimbursement = cur.fetchone()

    if not reimbursement:

        cur.close()
        conn.close()

        flash(
            "Reimbursement was not found.",
            "error"
        )

        return redirect(
            url_for("admin_reimbursements")
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

    # DELETE RECEIPT FROM CLOUDINARY
    if public_id:

        try:

            cloudinary.uploader.destroy(
                public_id
            )

        except Exception as e:

            print(
                "Cloudinary delete error:",
                e
            )

    flash(
        "Reimbursement deleted.",
        "success"
    )

    return redirect(
        url_for("admin_reimbursements")
    )


# ---------------------------------------------------------
# ADD DRIVER
# ---------------------------------------------------------

@app.route(
    "/admin/add-driver",
    methods=["GET", "POST"]
)
def add_driver():

    if not require_login("admin"):
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

        if not name or not username or not password:

            flash(
                "All fields are required.",
                "error"
            )

            return redirect(
                url_for("add_driver")
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
            url_for("add_driver")
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


# ---------------------------------------------------------
# START APP
# ---------------------------------------------------------

if __name__ == "__main__":

    init_db()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
