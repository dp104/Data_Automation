"""Outbound email for OTP codes (signup verification, password reset).
No SMTP configured -> prints to the console (and the API tells the frontend the code directly,
clearly labelled DEV MODE) so the whole flow is testable before real mail is wired up. Set
SMTP_HOST/SMTP_USER/SMTP_PASSWORD in saas.env after deployment and it switches over automatically."""
import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@example.com")

EMAIL_CONFIGURED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send_email(to, subject, body):
    if not EMAIL_CONFIGURED:
        print(f"[DEV MODE - no SMTP configured] email to {to} - {subject}\n{body}", flush=True)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(SMTP_FROM, [to], msg.as_string())


def otp_email_body(otp, purpose):
    action = "verify your account" if purpose == "signup" else "reset your password"
    return (f"Your Data Scraper verification code is: {otp}\n\n"
            f"Use this code to {action}. It expires in 10 minutes.\n"
            "If you didn't request this, you can safely ignore this email.")
