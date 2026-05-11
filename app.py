"""
pdf-sender — веб-сервис для отправки PDF-файлов по email.

Загружаешь один или несколько PDF, указываешь получателя — каждый файл
улетает отдельным письмом через SMTP mail.ru.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, flash, redirect, url_for

load_dotenv()

SMTP_HOST = "smtp.mail.ru"
SMTP_PORT = 465
SMTP_USER = os.environ.get("SMTP_USER", "myshsender@mail.ru")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

ALLOWED_EXT = {"pdf"}
MAX_UPLOAD_MB = 50

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me-in-prod")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def is_allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def is_valid_email(addr: str) -> bool:
    if not addr or "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    return bool(local) and "." in domain


def send_pdf(recipient: str, filename: str, data: bytes, subject: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = recipient
    msg["Subject"] = subject or filename
    msg.set_content(f"Файл: {filename}")
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/send", methods=["POST"])
def send():
    recipient = (request.form.get("recipient") or "").strip()
    if not is_valid_email(recipient):
        flash("Укажи корректный email получателя.", "error")
        return redirect(url_for("index"))

    files = request.files.getlist("pdfs")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("Не выбрано ни одного PDF-файла.", "error")
        return redirect(url_for("index"))

    sent, failed = [], []
    for f in files:
        name = Path(f.filename).name
        if not is_allowed(name):
            failed.append((name, "не PDF"))
            continue
        try:
            data = f.read()
            if not data:
                failed.append((name, "пустой файл"))
                continue
            send_pdf(recipient, name, data)
            sent.append(name)
        except Exception as e:
            failed.append((name, str(e)))

    if sent:
        flash(f"Отправлено писем: {len(sent)} → {recipient}", "ok")
        for n in sent:
            flash(f"  ✓ {n}", "ok")
    if failed:
        flash(f"Ошибок: {len(failed)}", "error")
        for n, reason in failed:
            flash(f"  ✗ {n} — {reason}", "error")

    return redirect(url_for("index"))


@app.route("/health")
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
