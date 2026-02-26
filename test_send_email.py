import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_SERVER = os.environ.get("SMTP_SERVER", "localhost")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
SMTP_USER = os.environ.get("SMTP_USERNAME")
SMTP_PASS = os.environ.get("SMTP_PASSWORD")
FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", "test@example.com")
TO_EMAIL = os.environ.get("TEST_TO_EMAIL", "recipient@example.com")

subject = "[Test] メール送信テスト"
body = "これはローカルSMTPデバッグサーバに送信されたテストメールです。日本語の本文のテスト。"

msg = MIMEMultipart()
msg["From"] = FROM_EMAIL
msg["To"] = TO_EMAIL
msg["Subject"] = subject
msg.attach(MIMEText(body, "plain", "utf-8"))

try:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
        # If username/password provided, attempt STARTTLS+login
        if SMTP_USER and SMTP_PASS:
            try:
                server.starttls()
            except Exception as e:
                print("starttls failed:", e)
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    print("OK: Email sent to", TO_EMAIL)
except Exception as e:
    print("ERROR:", e)
    raise
