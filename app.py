from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime, timedelta
import os

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=30)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

DB = "tnt_mileage.db"

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','driver')),
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mileage_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.commit()

    admin = cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not admin:
        cur.execute(
            "INSERT INTO users (name, username, password_hash, role) VALUES (?, ?, ?, ?)",
            ("TNT Admin", "admin", generate_password_hash("TNTadmin123!"), "admin")
        )
        conn.commit()
    conn.close()

@app.before_request
def setup():
    init_db()

def require_login(role=None):
    if "user_id" not in session:
        return False
    if role and session.get("role") != role:
        return False
    return True

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET" and "user_id" in session:
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("driver_dashboard"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        remember_me = request.form.get("remember_me") == "yes"
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = remember_me
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            return redirect(url_for("admin_dashboard" if user["role"] == "admin" else "driver_dashboard"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")
    @app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form["username"].strip()

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1",
            (username,)
        ).fetchone()
        conn.close()

        if user:
            flash(
                "Your password reset request has been received. Please contact TNT Admin for a temporary password.",
                "success"
            )
        else:
            flash(
                "Username not found. Please check your username and try again.",
                "error"
            )

    return render_template("forgot_password.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/driver", methods=["GET", "POST"])
def driver_dashboard():
    if not require_login("driver"):
        return redirect(url_for("login"))

    conn = get_db()

    if request.method == "POST":
        trip_date = request.form["trip_date"]
        unit_number = request.form.get("unit_number", "").strip()
        pickup_city = request.form.get("pickup_city", "").strip()
        delivery_city = request.form.get("delivery_city", "").strip()
        notes = request.form.get("notes", "").strip()

        try:
            beginning = float(request.form["beginning_miles"])
            ending = float(request.form["ending_miles"])
        except ValueError:
            flash("Beginning and ending miles must be numbers.", "error")
            conn.close()
            return redirect(url_for("driver_dashboard"))

        if ending < beginning:
            flash("Ending miles cannot be less than beginning miles.", "error")
            conn.close()
            return redirect(url_for("driver_dashboard"))

        total = ending - beginning

        conn.execute("""
            INSERT INTO mileage_entries
            (user_id, trip_date, unit_number, pickup_city, delivery_city,
             beginning_miles, ending_miles, total_miles, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session["user_id"], trip_date, unit_number, pickup_city, delivery_city,
            beginning, ending, total, notes, datetime.now().isoformat(timespec="seconds")
        ))
        conn.commit()
        flash(f"Mileage submitted: {total:,.1f} miles.", "success")

    entries = conn.execute("""
        SELECT * FROM mileage_entries
        WHERE user_id=?
        ORDER BY trip_date DESC, id DESC
    """, (session["user_id"],)).fetchall()

    total_miles = conn.execute("""
        SELECT COALESCE(SUM(total_miles),0) AS total
        FROM mileage_entries WHERE user_id=?
    """, (session["user_id"],)).fetchone()["total"]

    conn.close()
    return render_template("driver.html", entries=entries, total_miles=total_miles)

@app.route("/admin")
def admin_dashboard():
    if not require_login("admin"):
        return redirect(url_for("login"))

    conn = get_db()

    entries = conn.execute("""
        SELECT m.*, u.name AS driver_name, u.username
        FROM mileage_entries m
        JOIN users u ON u.id = m.user_id
        ORDER BY m.trip_date DESC, m.id DESC
    """).fetchall()

    driver_totals = conn.execute("""
        SELECT u.id, u.name, u.username,
               COALESCE(SUM(m.total_miles),0) AS total_miles,
               COUNT(m.id) AS entry_count
        FROM users u
        LEFT JOIN mileage_entries m ON m.user_id = u.id
        WHERE u.role='driver' AND u.active=1
        GROUP BY u.id, u.name, u.username
        ORDER BY u.name
    """).fetchall()

    grand_total = conn.execute("""
        SELECT COALESCE(SUM(total_miles),0) AS total FROM mileage_entries
    """).fetchone()["total"]

    conn.close()
    return render_template("admin.html", entries=entries, driver_totals=driver_totals, grand_total=grand_total)

@app.route("/admin/add-driver", methods=["GET", "POST"])
def add_driver():
    if not require_login("admin"):
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form["name"].strip()
        username = request.form["username"].strip()
        password = request.form["password"]

        if not name or not username or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("add_driver"))

        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO users (name, username, password_hash, role)
                VALUES (?, ?, ?, 'driver')
            """, (name, username, generate_password_hash(password)))
            conn.commit()
            flash(f"Driver {name} added.", "success")
        except sqlite3.IntegrityError:
            flash("That username already exists.", "error")
        finally:
            conn.close()

        return redirect(url_for("add_driver"))

    conn = get_db()
    drivers = conn.execute("""
        SELECT id, name, username, active
        FROM users
        WHERE role='driver'
        ORDER BY name
    """).fetchall()
    conn.close()
    return render_template("add_driver.html", drivers=drivers)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
