"""
pdf-sender — веб-сервис для отправки PDF-файлов по email.

Авторизация по логин/пароль. Получатель = email авторизованного пользователя
(вручную не выбирается). Админ создаёт/удаляет пользователей.
"""
import os
import sqlite3
import smtplib
import ssl
import time
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, flash, redirect, url_for, session, g, abort
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

SMTP_HOST = "smtp.mail.ru"
SMTP_PORT = 465
SMTP_USER = os.environ.get("SMTP_USER", "myshsender@mail.ru")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

DB_PATH = os.environ.get("DB_PATH", "/data/pdf-sender.db")

MAX_UPLOAD_MB = 50
PDF_MAGIC = b"%PDF-"
MIN_PASSWORD_LEN = 6

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET") or ""
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET env var is required")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# ============================== DB ==============================

def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g._db = db
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
        admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
        admin_pw = os.environ.get("ADMIN_PASSWORD") or ""
        if admin_email and admin_pw:
            cur = db.execute(
                "INSERT OR IGNORE INTO users (email, password_hash, is_admin) VALUES (?, ?, 1)",
                (admin_email, generate_password_hash(admin_pw)),
            )
            db.commit()
            if cur.rowcount:
                app.logger.info("Bootstrapped admin: %s", admin_email)


# ============================== Auth ==============================

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not current_user():
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u["is_admin"]:
            abort(403)
        return f(*a, **k)
    return wrap


def user_only(f):
    """Доступ только обычным юзерам (не админам)."""
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if u["is_admin"]:
            return redirect(url_for("admin"))
        return f(*a, **k)
    return wrap


@app.context_processor
def inject_user():
    return {"user": current_user()}


def is_valid_email(addr: str) -> bool:
    if not addr or "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    return bool(local) and "." in domain


# ============================== SMTP ==============================

def send_pdf(recipient: str, filename: str, data: bytes) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = recipient
    msg["Subject"] = filename
    msg.set_content(f"Файл: {filename}")
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# ============================== Routes ==============================

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("admin" if current_user()["is_admin"] else "index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], pw):
            session.clear()
            session["user_id"] = row["id"]
            return redirect(url_for("admin" if row["is_admin"] else "index"))
        time.sleep(1)  # лёгкий тормоз против перебора
        flash("Неверный email или пароль.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@user_only
def index():
    return render_template("index.html")


@app.route("/send", methods=["POST"])
@user_only
def send():
    recipient = current_user()["email"]
    files = [f for f in request.files.getlist("pdfs") if f and f.filename]
    if not files:
        flash("Не выбрано ни одного PDF-файла.", "error")
        return redirect(url_for("index"))

    sent, failed = [], []
    for f in files:
        name = Path(f.filename).name
        if "." not in name or name.rsplit(".", 1)[1].lower() != "pdf":
            failed.append((name, "не PDF"))
            continue
        try:
            data = f.read()
            if not data:
                failed.append((name, "пустой файл"))
                continue
            if not data.startswith(PDF_MAGIC):
                failed.append((name, "не похоже на PDF"))
                continue
            send_pdf(recipient, name, data)
            sent.append(name)
        except Exception as e:
            failed.append((name, str(e)))

    if sent:
        flash(f"Отправлено: {len(sent)} → {recipient}", "ok")
        for n in sent:
            flash(f"  ✓ {n}", "ok")
    if failed:
        flash(f"Ошибок: {len(failed)}", "error")
        for n, reason in failed:
            flash(f"  ✗ {n} — {reason}", "error")

    return redirect(url_for("index"))


@app.route("/admin", methods=["GET"])
@admin_required
def admin():
    rows = get_db().execute(
        "SELECT id, email, is_admin, created_at FROM users ORDER BY is_admin DESC, id"
    ).fetchall()
    return render_template("admin.html", users=rows)


@app.route("/admin/users", methods=["POST"])
@admin_required
def admin_create_user():
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    if not is_valid_email(email):
        flash("Неверный email.", "error")
    elif len(pw) < MIN_PASSWORD_LEN:
        flash(f"Пароль минимум {MIN_PASSWORD_LEN} символов.", "error")
    else:
        try:
            db = get_db()
            db.execute(
                "INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 0)",
                (email, generate_password_hash(pw)),
            )
            db.commit()
            flash(f"Пользователь {email} создан.", "ok")
        except sqlite3.IntegrityError:
            flash(f"Пользователь {email} уже существует.", "error")
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id: int):
    me = current_user()
    if user_id == me["id"]:
        flash("Нельзя удалить самого себя.", "error")
        return redirect(url_for("admin"))
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash("Пользователь удалён.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_reset_password(user_id: int):
    pw = request.form.get("password") or ""
    if len(pw) < MIN_PASSWORD_LEN:
        flash(f"Пароль минимум {MIN_PASSWORD_LEN} символов.", "error")
    else:
        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(pw), user_id),
        )
        db.commit()
        flash("Пароль обновлён.", "ok")
    return redirect(url_for("admin"))


@app.errorhandler(403)
def forbidden(e):
    return ("403 Forbidden", 403)


@app.route("/health")
def health():
    return {"status": "ok"}, 200


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
