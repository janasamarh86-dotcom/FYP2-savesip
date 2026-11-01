"""
SaveSip - Student Budgeting System
Application entry point: configuration, extension setup and startup.

Routes live in routes.py and database models live in models.py. Both are
imported at the bottom of this file (see "Circular import handling").
"""

import os
import secrets
import sys
import time
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, flash, redirect, request, session, url_for
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, current_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

# When this file is run directly (python app.py) Python names it "__main__".
# routes.py and models.py will use "from app import app, db", which would
# otherwise load this file a second time as a separate "app" module, creating
# a second Flask instance that the routes attach to instead of this one.
# Registering this module under the name "app" makes both names point to the
# same objects.
if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])

# Load settings such as DATABASE_URL from the .env file next to this one.
# Variables already set in the real environment (e.g. on a hosting
# platform) take priority over .env.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ---------------------------------------------------------------------------
# App initialisation
# ---------------------------------------------------------------------------

app = Flask(__name__, instance_relative_config=True)

# Make sure the instance/ folder exists before the secret key file is
# written into it.
os.makedirs(app.instance_path, exist_ok=True)


def _load_secret_key():
    """Return the SECRET_KEY.

    Uses the SECRET_KEY environment variable when it is set (recommended for
    deployment). Otherwise a random key is generated once and stored in
    instance/secret_key so that sessions stay valid across server restarts.
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key

    key_path = os.path.join(app.instance_path, "secret_key")
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as key_file:
            stored_key = key_file.read().strip()
        if stored_key:
            return stored_key

    new_key = secrets.token_hex(32)
    with open(key_path, "w", encoding="utf-8") as key_file:
        key_file.write(new_key)
    return new_key


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

app.config["SECRET_KEY"] = _load_secret_key()

# PostgreSQL database hosted on Neon, read from DATABASE_URL (.env locally,
# the platform's environment variables in deployment)
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to the .env file or the environment."
    )
# Some providers still give "postgres://" URLs, which SQLAlchemy no longer
# accepts; it needs "postgresql://"
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
# Connect with psycopg2 (psycopg2-binary in requirements.txt). From
# SQLAlchemy 2.1 a plain "postgresql://" URL means the newer psycopg 3
# driver instead, so the driver is named explicitly.
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
# Neon closes idle connections (and suspends the database when unused), so
# test each pooled connection before use instead of failing on a dead one
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# 30-minute inactivity timeout, enforced on the server by
# enforce_inactivity_timeout() below for every logged-in user.
# "Remember Me" makes the session permanent: its cookie then survives closing
# the browser, expires after this same 30 minutes, and is refreshed on every
# request. Without "Remember Me" the cookie is deleted when the browser closes.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

# Session cookie hardening
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Only send cookies over HTTPS in deployment. Left off for local development
# because http://127.0.0.1 would otherwise never receive the session cookie.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"

# Flask-Login's separate remember-me cookie is not used (login_user is always
# called with remember=False), so there is no second long-lived login cookie
# that could outlast the inactivity timeout.

# CSRF protection (Flask-WTF)
app.config["WTF_CSRF_ENABLED"] = True


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"


@app.before_request
def enforce_inactivity_timeout():
    """Log the user out when more than 30 minutes pass between requests.

    The time of the last request is kept in the (signed) session, so the
    check works the same with or without "Remember Me".
    """
    if request.endpoint == "static" or not current_user.is_authenticated:
        return None

    now = time.time()
    last_active = session.get("last_active")
    limit = app.config["PERMANENT_SESSION_LIFETIME"].total_seconds()

    if last_active is not None and now - last_active > limit:
        logout_user()
        session.clear()
        flash("You were logged out after 30 minutes of inactivity.", "warning")
        next_url = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=next_url))

    session["last_active"] = now
    return None


# ---------------------------------------------------------------------------
# Circular import handling
# ---------------------------------------------------------------------------
# Imported here, after app, db, bcrypt and login_manager exist, because
# models.py and routes.py import those objects from this module.
# models must come first so routes can use the model classes.

import models  # noqa: E402,F401
import routes  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Database creation
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()
    models.upgrade_schema()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
