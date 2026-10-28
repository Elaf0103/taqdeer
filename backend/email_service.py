"""
Email Service
=============
Sends transactional emails via SMTP using stdlib smtplib.
Configuration is read from environment variables (see .env).

Required env vars:
  SMTP_HOST     — SMTP server hostname  (e.g. smtp.gmail.com)
  SMTP_PORT     — SMTP port             (587 for STARTTLS, 465 for SSL)
  SMTP_USER     — Login username        (your actual Gmail address)
  SMTP_PASSWORD — Gmail App Password    (16-char, spaces optional)
  MAIL_FROM     — "From" header         (e.g. "Taqdeer <you@gmail.com>")

Public API:
  send_reset_code(to_email, code)  — raises EmailError on failure
  send_test_email(to_email)        — smoke-test; raises EmailError on failure
  smtp_diagnostics()               — returns list[str] of config warnings
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Placeholder sentinel — if any SMTP var still has this exact value,
# email will never work.
_PLACEHOLDER = "your-email@gmail.com"


class EmailError(Exception):
    """Raised when an email cannot be sent."""


def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def smtp_diagnostics() -> list[str]:
    """
    Return a list of human-readable warnings about the SMTP configuration.
    Empty list means config looks plausible (does NOT guarantee delivery).
    """
    warnings: list[str] = []

    host = _cfg("SMTP_HOST")
    port = _cfg("SMTP_PORT", "587")
    user = _cfg("SMTP_USER")
    pwd  = _cfg("SMTP_PASSWORD")
    frm  = _cfg("MAIL_FROM")

    if not host:
        warnings.append("SMTP_HOST is not set.")
    if not user:
        warnings.append("SMTP_USER is not set.")
    elif user == _PLACEHOLDER:
        warnings.append(
            f"SMTP_USER is still the placeholder value '{_PLACEHOLDER}'. "
            "Set it to your actual Gmail address."
        )
    if not pwd:
        warnings.append("SMTP_PASSWORD is not set.")
    elif len(pwd.replace(" ", "")) < 8:
        warnings.append(
            "SMTP_PASSWORD looks too short. "
            "For Gmail, use a 16-character App Password (not your regular password)."
        )
    if frm and _PLACEHOLDER in frm:
        warnings.append(
            f"MAIL_FROM still contains the placeholder '{_PLACEHOLDER}'. "
            "Update it to your actual sender address."
        )

    try:
        port_int = int(port)
        if port_int not in (25, 465, 587):
            warnings.append(f"SMTP_PORT={port_int} is unusual. Expected 587 (STARTTLS) or 465 (SSL).")
    except ValueError:
        warnings.append(f"SMTP_PORT='{port}' is not a valid integer.")

    return warnings


def _build_smtp_connection(smtp_host: str, smtp_port: int,
                           smtp_user: str, smtp_pass: str):
    """
    Open and authenticate an SMTP connection.
    Returns the live smtplib.SMTP / SMTP_SSL instance.
    Caller is responsible for closing it.
    Raises EmailError on any connection or auth failure.
    """
    try:
        if smtp_port == 465:
            print(f"[SMTP] opening SSL connection to {smtp_host}:{smtp_port}")
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            server.ehlo()
        else:
            print(f"[SMTP] opening STARTTLS connection to {smtp_host}:{smtp_port}")
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()
        print(f"[SMTP] connection established — attempting login …")
        server.login(smtp_user, smtp_pass)
        print(f"[SMTP] login successful")
        return server
    except smtplib.SMTPAuthenticationError as exc:
        _log_error("SMTPAuthenticationError", str(exc))
        raise EmailError(
            "SMTP authentication failed. "
            "For Gmail: make sure 2-Step Verification is on and you are using a "
            "16-character App Password (not your regular Gmail password). "
            f"(server said: {exc})"
        ) from exc
    except smtplib.SMTPConnectError as exc:
        _log_error("SMTPConnectError", str(exc))
        raise EmailError(
            f"Could not connect to SMTP server {smtp_host}:{smtp_port}. "
            f"Check SMTP_HOST and SMTP_PORT. ({exc})"
        ) from exc
    except smtplib.SMTPServerDisconnected as exc:
        _log_error("SMTPServerDisconnected", str(exc))
        raise EmailError(f"SMTP server disconnected unexpectedly. ({exc})") from exc
    except TimeoutError as exc:
        _log_error("TimeoutError", str(exc))
        raise EmailError(
            f"Connection to {smtp_host}:{smtp_port} timed out. "
            "Check that the host and port are correct and reachable."
        ) from exc
    except OSError as exc:
        _log_error("OSError", str(exc))
        raise EmailError(
            f"Network error connecting to {smtp_host}:{smtp_port}: {exc}"
        ) from exc
    except Exception as exc:
        _log_error(type(exc).__name__, str(exc))
        raise EmailError(f"Unexpected SMTP error: {exc}") from exc


def _log_error(exc_type: str, message: str) -> None:
    """Print a safe [EMAIL-SEND-ERROR] log line (never logs credentials)."""
    host = _cfg("SMTP_HOST", "(not set)")
    port = _cfg("SMTP_PORT", "587")
    user = _cfg("SMTP_USER", "(not set)")
    # Show only last 4 chars of SMTP_USER to aid debugging without exposing address
    user_hint = f"…{user[-10:]}" if len(user) > 10 else user or "(not set)"
    print(
        f"[EMAIL-SEND-ERROR] "
        f"type={exc_type} "
        f"host={host}:{port} "
        f"smtp_user={user_hint} "
        f"message={message!r}"
    )


def send_reset_code(to_email: str, code: str) -> None:
    """
    Send a password-reset verification code to *to_email*.
    Raises EmailError on any SMTP or configuration failure.

    Debug logs emitted (password is never printed):
      [SMTP] send_reset_code called
      [SMTP] config loaded: host port user from
      [SMTP] connecting …
      [SMTP] message sent successfully
      [SMTP-ERROR] on any failure
    """
    print(f"[SMTP] send_reset_code called — to={to_email!r}")

    # Pre-flight: warn about obvious misconfigurations
    diag = smtp_diagnostics()
    if diag:
        for d in diag:
            print(f"[SMTP-CONFIG-WARN] {d}")

    smtp_host = _cfg("SMTP_HOST")
    smtp_port = int(_cfg("SMTP_PORT", "587"))
    smtp_user = _cfg("SMTP_USER")
    smtp_pass = _cfg("SMTP_PASSWORD")
    mail_from = _cfg("MAIL_FROM") or smtp_user

    # Log loaded config — never print the password
    user_hint = (f"…{smtp_user[-10:]}" if len(smtp_user) > 10 else smtp_user) or "(not set)"
    print(f"[SMTP] config loaded — host={smtp_host!r} port={smtp_port} "
          f"user={user_hint!r} from={mail_from!r}")

    if not smtp_host or not smtp_user or not smtp_pass:
        missing = [k for k, v in [("SMTP_HOST", smtp_host),
                                   ("SMTP_USER", smtp_user),
                                   ("SMTP_PASSWORD", smtp_pass)] if not v]
        print(f"[SMTP-ERROR] missing required env vars: {missing}")
        raise EmailError(
            "Email service is not configured. "
            "Set SMTP_HOST, SMTP_USER, and SMTP_PASSWORD in your .env file."
        )

    subject    = "Your Taqdeer password reset code"
    html_body  = _reset_html(code)
    plain_body = (
        f"Your Taqdeer password reset code is: {code}\n\n"
        f"This code expires in 20 minutes.\n"
        f"If you did not request this, please ignore this email."
    )

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = mail_from
    msg["To"]      = to_email
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    print(f"[SMTP] connecting to {smtp_host}:{smtp_port} …")
    server = _build_smtp_connection(smtp_host, smtp_port, smtp_user, smtp_pass)
    try:
        print(f"[SMTP] sending message …")
        server.sendmail(mail_from, [to_email], msg.as_string())
        print(f"[SMTP] message sent successfully — to={to_email!r}")
    except smtplib.SMTPRecipientsRefused as exc:
        _log_error("SMTPRecipientsRefused", str(exc))
        raise EmailError(f"Recipient address refused by server: {to_email}") from exc
    except Exception as exc:
        _log_error(type(exc).__name__, str(exc))
        raise EmailError(f"Failed to send message: {exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_test_email(to_email: str) -> None:
    """
    Send a plain test email to verify SMTP connectivity independently
    of the reset-password flow.  Raises EmailError on failure.
    """
    smtp_host = _cfg("SMTP_HOST")
    smtp_port = int(_cfg("SMTP_PORT", "587"))
    smtp_user = _cfg("SMTP_USER")
    smtp_pass = _cfg("SMTP_PASSWORD")
    mail_from = _cfg("MAIL_FROM") or smtp_user

    if not smtp_host or not smtp_user or not smtp_pass:
        raise EmailError(
            "SMTP is not configured (SMTP_HOST / SMTP_USER / SMTP_PASSWORD missing)."
        )

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = "Taqdeer SMTP test email"
    msg["From"]    = mail_from
    msg["To"]      = to_email
    msg.attach(MIMEText(
        "This is a test email from Taqdeer.\n"
        "If you received it, your SMTP configuration is working correctly.",
        "plain",
    ))

    server = _build_smtp_connection(smtp_host, smtp_port, smtp_user, smtp_pass)
    try:
        server.sendmail(mail_from, [to_email], msg.as_string())
    except Exception as exc:
        _log_error(type(exc).__name__, str(exc))
        raise EmailError(f"Failed to send test message: {exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_reset_link(to_email: str, reset_url: str) -> None:
    """
    Send a password-reset link to *to_email*.
    The link contains the one-time URL token and expires in 20 minutes.
    Raises EmailError on any SMTP or configuration failure.
    """
    diag = smtp_diagnostics()
    if diag:
        for d in diag:
            print(f"[EMAIL-CONFIG-WARN] {d}")

    smtp_host = _cfg("SMTP_HOST")
    smtp_port = int(_cfg("SMTP_PORT", "587"))
    smtp_user = _cfg("SMTP_USER")
    smtp_pass = _cfg("SMTP_PASSWORD")
    mail_from = _cfg("MAIL_FROM") or smtp_user

    if not smtp_host or not smtp_user or not smtp_pass:
        raise EmailError(
            "Email service is not configured. "
            "Set SMTP_HOST, SMTP_USER, and SMTP_PASSWORD in your .env file."
        )

    subject    = "Reset your Taqdeer password"
    html_body  = _reset_link_html(reset_url)
    plain_body = (
        f"You requested a password reset for your Taqdeer account.\n\n"
        f"Click the link below to set a new password:\n{reset_url}\n\n"
        f"This link expires in 20 minutes.\n"
        f"If you did not request this, please ignore this email."
    )

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = mail_from
    msg["To"]      = to_email
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    server = _build_smtp_connection(smtp_host, smtp_port, smtp_user, smtp_pass)
    try:
        server.sendmail(mail_from, [to_email], msg.as_string())
    except smtplib.SMTPRecipientsRefused as exc:
        _log_error("SMTPRecipientsRefused", str(exc))
        raise EmailError(f"Recipient address refused by server: {to_email}") from exc
    except Exception as exc:
        _log_error(type(exc).__name__, str(exc))
        raise EmailError(f"Failed to send message: {exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ── HTML template ─────────────────────────────────────────────────────────────

def _reset_html(code: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f4f7fb;margin:0;padding:0;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fb;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">
          <tr>
            <td style="background:#28405B;padding:28px 32px;text-align:center;">
              <h1 style="color:#ffffff;margin:0;font-size:22px;letter-spacing:0.5px;">Taqdeer</h1>
              <p style="color:#98B9D4;margin:6px 0 0;font-size:13px;">AI-Powered Exam Grading Platform</p>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px;">
              <h2 style="color:#28405B;margin:0 0 12px;font-size:18px;">Password Reset Request</h2>
              <p style="color:#4a5568;margin:0 0 24px;font-size:14px;line-height:1.6;">
                We received a request to reset your password. Use the verification code below.
                This code expires in <strong>20 minutes</strong>.
              </p>
              <div style="background:#f4f7fb;border:2px dashed #98B9D4;border-radius:10px;
                          padding:24px;text-align:center;margin:0 0 24px;">
                <span style="font-size:36px;font-weight:700;letter-spacing:10px;color:#28405B;
                             font-family:monospace;">{code}</span>
              </div>
              <p style="color:#718096;font-size:13px;margin:0 0 8px;">
                If you did not request a password reset, please ignore this email.
                Your password will not change.
              </p>
              <p style="color:#718096;font-size:13px;margin:0;">
                For security, never share this code with anyone.
              </p>
            </td>
          </tr>
          <tr>
            <td style="background:#f4f7fb;padding:20px 40px;text-align:center;">
              <p style="color:#a0aec0;font-size:12px;margin:0;">
                &copy; 2025 Taqdeer · AI-Powered Exam Grading
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _reset_link_html(reset_url: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f4f7fb;margin:0;padding:0;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fb;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">
          <tr>
            <td style="background:#28405B;padding:28px 32px;text-align:center;">
              <h1 style="color:#ffffff;margin:0;font-size:22px;letter-spacing:0.5px;">Taqdeer</h1>
              <p style="color:#98B9D4;margin:6px 0 0;font-size:13px;">AI-Powered Exam Grading Platform</p>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px;">
              <h2 style="color:#28405B;margin:0 0 12px;font-size:18px;">Password Reset Request</h2>
              <p style="color:#4a5568;margin:0 0 24px;font-size:14px;line-height:1.6;">
                We received a request to reset the password for your Taqdeer account.
                Click the button below to choose a new password.
                This link expires in <strong>20 minutes</strong>.
              </p>
              <div style="text-align:center;margin:0 0 28px;">
                <a href="{reset_url}"
                   style="display:inline-block;background:#28405B;color:#ffffff;text-decoration:none;
                          font-size:15px;font-weight:600;padding:14px 32px;border-radius:8px;
                          letter-spacing:0.3px;">
                  Reset My Password
                </a>
              </div>
              <p style="color:#718096;font-size:13px;margin:0 0 8px;">
                If the button does not work, copy and paste this link into your browser:
              </p>
              <p style="font-size:12px;color:#446B95;word-break:break-all;margin:0 0 20px;">
                {reset_url}
              </p>
              <hr style="border:none;border-top:1px solid #e8edf2;margin:0 0 16px;">
              <p style="color:#718096;font-size:13px;margin:0 0 8px;">
                If you did not request a password reset, you can safely ignore this email.
                Your password will not change.
              </p>
              <p style="color:#718096;font-size:13px;margin:0;">
                For security, never share this link with anyone.
              </p>
            </td>
          </tr>
          <tr>
            <td style="background:#f4f7fb;padding:20px 40px;text-align:center;">
              <p style="color:#a0aec0;font-size:12px;margin:0;">
                &copy; 2025 Taqdeer · AI-Powered Exam Grading
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
