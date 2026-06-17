"""
Smart PG Electricity Bill Calculator
-------------------------------------
Main Flask application. Run with:

    python app.py

The first run automatically creates database.db with the required
tables (users, readings) via SQLAlchemy's create_all().
"""

import os
from collections import OrderedDict
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
    send_file,
)

from config import Config
from extensions import db
from models import User, Reading
from utils import generate_bill_pdf, build_whatsapp_share_text


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    with app.app_context():
        db.create_all()

    register_routes(app)
    return app


# ---------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Please log in as admin to continue.", "warning")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
def register_routes(app):
    @app.context_processor
    def inject_globals():
        return {
            "current_year": datetime.now().year,
            "rate_per_unit": Config.RATE_PER_UNIT,
        }

    # ---------------- Landing -----------------------------------------
    @app.route("/")
    def index():
        if session.get("user_id"):
            return redirect(url_for("dashboard"))
        return render_template("index.html")

    # ---------------- Registration --------------------------------------
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            room_number = request.form.get("room_number", "").strip()
            mobile_number = request.form.get("mobile_number", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            joining_date_str = request.form.get("joining_date", "")
            initial_reading_str = request.form.get("initial_reading", "")

            errors = []
            if not all(
                [full_name, room_number, mobile_number, password, joining_date_str]
            ):
                errors.append("Please fill in all required fields.")
            if not mobile_number.isdigit() or len(mobile_number) != 10:
                errors.append("Mobile number must be exactly 10 digits.")
            if len(password) < 4:
                errors.append("Password must be at least 4 characters long.")
            if password != confirm_password:
                errors.append("Password and confirm password do not match.")
            try:
                initial_reading = float(initial_reading_str)
                if initial_reading < 0:
                    errors.append("Initial meter reading cannot be negative.")
            except ValueError:
                errors.append("Initial meter reading must be a valid number.")
                initial_reading = None
            try:
                joining_date = datetime.strptime(joining_date_str, "%Y-%m-%d").date()
            except ValueError:
                errors.append("Joining date is invalid.")
                joining_date = None

            if mobile_number and User.query.filter_by(mobile_number=mobile_number).first():
                errors.append("This mobile number is already registered.")

            if errors:
                for e in errors:
                    flash(e, "danger")
                return render_template("register.html", form=request.form)

            user = User(
                full_name=full_name,
                room_number=room_number,
                mobile_number=mobile_number,
                joining_date=joining_date,
                initial_reading=initial_reading,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))

        return render_template("register.html", form={})

    # ---------------- Login / Logout ------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            mobile_number = request.form.get("mobile_number", "").strip()
            password = request.form.get("password", "")

            user = User.query.filter_by(mobile_number=mobile_number).first()
            if user and user.check_password(password):
                session.clear()
                session["user_id"] = user.id
                flash(f"Welcome back, {user.full_name}!", "success")
                return redirect(url_for("dashboard"))

            flash("Invalid mobile number or password.", "danger")

        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been logged out.", "info")
        return redirect(url_for("index"))

    # ---------------- Dashboard ------------------------------------------
    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = current_user()
        today_record = user.reading_for_date(date.today())
        return render_template("dashboard.html", user=user, today_record=today_record)

    # ---------------- Add Reading -----------------------------------------
    @app.route("/add-reading", methods=["GET", "POST"])
    @login_required
    def add_reading():
        user = current_user()
        today = date.today()
        record = user.reading_for_date(today)

        if request.method == "POST":
            slot = request.form.get("slot")
            raw_value = request.form.get("reading", "")

            try:
                value = float(raw_value)
            except ValueError:
                flash("Please enter a valid numeric meter reading.", "danger")
                return redirect(url_for("add_reading"))

            if value < 0:
                flash("Meter reading cannot be negative.", "danger")
                return redirect(url_for("add_reading"))

            if record is None:
                record = Reading(user_id=user.id, date=today)
                db.session.add(record)

            last_value = user.latest_reading_value()

            if slot == "morning":
                if record.morning_reading is not None:
                    flash("Morning reading for today is already recorded.", "warning")
                    return redirect(url_for("add_reading"))
                if value < last_value:
                    flash(
                        f"Reading must be greater than or equal to the last "
                        f"recorded reading ({last_value:.2f}).",
                        "danger",
                    )
                    return redirect(url_for("add_reading"))
                record.morning_reading = value

            elif slot == "night":
                if record.night_reading is not None:
                    flash("Night reading for today is already recorded.", "warning")
                    return redirect(url_for("add_reading"))
                compare_value = (
                    record.morning_reading
                    if record.morning_reading is not None
                    else last_value
                )
                if value < compare_value:
                    flash(
                        f"Night reading must be greater than or equal to "
                        f"{compare_value:.2f}.",
                        "danger",
                    )
                    return redirect(url_for("add_reading"))
                record.night_reading = value

            else:
                flash("Invalid reading type submitted.", "danger")
                return redirect(url_for("add_reading"))

            record.recompute_daily_consumption()
            db.session.commit()
            flash(f"{slot.capitalize()} reading saved successfully.", "success")
            return redirect(url_for("add_reading"))

        return render_template(
            "add_reading.html", user=user, record=record, today=today
        )

    # ---------------- History --------------------------------------------
    @app.route("/history")
    @login_required
    def history():
        user = current_user()
        readings = sorted(user.readings, key=lambda r: r.date, reverse=True)
        return render_template("history.html", user=user, readings=readings)

    # ---------------- Chart data API ---------------------------------------
    @app.route("/api/chart-data")
    @login_required
    def chart_data():
        user = current_user()
        readings = sorted(user.readings, key=lambda r: r.date)

        daily_set = [r for r in readings if r.daily_consumption is not None]
        daily_recent = daily_set[-14:]
        daily = {
            "labels": [r.date.strftime("%d %b") for r in daily_recent],
            "data": [r.daily_consumption for r in daily_recent],
        }

        weekly_map = OrderedDict()
        for r in daily_set:
            year, week, _ = r.date.isocalendar()
            key = f"Week {week}, {year}"
            weekly_map[key] = round(weekly_map.get(key, 0) + r.daily_consumption, 2)
        weekly_items = list(weekly_map.items())[-8:]
        weekly = {
            "labels": [k for k, _ in weekly_items],
            "data": [v for _, v in weekly_items],
        }

        monthly_map = OrderedDict()
        for r in daily_set:
            key = r.date.strftime("%b %Y")
            monthly_map[key] = round(monthly_map.get(key, 0) + r.daily_consumption, 2)
        monthly_items = list(monthly_map.items())[-12:]
        monthly = {
            "labels": [k for k, _ in monthly_items],
            "data": [v for _, v in monthly_items],
        }

        return jsonify({"daily": daily, "weekly": weekly, "monthly": monthly})

    # ---------------- PDF Bill ----------------------------------------------
    @app.route("/bill/download")
    @login_required
    def download_bill():
        user = current_user()
        filepath = generate_bill_pdf(user)
        return send_file(
            filepath,
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype="application/pdf",
        )

    @app.route("/bill/whatsapp-link")
    @login_required
    def whatsapp_link():
        user = current_user()
        return jsonify({"url": build_whatsapp_share_text(user)})

    # ---------------- Admin --------------------------------------------------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            from werkzeug.security import check_password_hash

            if username == Config.ADMIN_USERNAME and check_password_hash(
                Config.ADMIN_PASSWORD_HASH, password
            ):
                session.clear()
                session["is_admin"] = True
                flash("Welcome, admin.", "success")
                return redirect(url_for("admin_dashboard"))

            flash("Invalid admin credentials.", "danger")

        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.clear()
        flash("Admin logged out.", "info")
        return redirect(url_for("index"))

    @app.route("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        users = User.query.order_by(User.id).all()

        total_users = len(users)
        total_rooms = len({u.room_number for u in users})
        total_revenue = round(sum(u.bill_amount() for u in users), 2)

        highest_consumer = None
        lowest_consumer = None
        if users:
            highest_consumer = max(users, key=lambda u: u.total_units_consumed())
            lowest_consumer = min(users, key=lambda u: u.total_units_consumed())

        all_readings = (
            Reading.query.join(User).order_by(Reading.date.desc()).all()
        )

        return render_template(
            "admin_dashboard.html",
            users=users,
            total_users=total_users,
            total_rooms=total_rooms,
            total_revenue=total_revenue,
            highest_consumer=highest_consumer,
            lowest_consumer=lowest_consumer,
            all_readings=all_readings,
        )


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
