"""
Email Service — HTTP API
========================
Sends transactional emails via Resend or SendGrid HTTP API.
No SMTP, no Gmail App Passwords, no manual server configuration.

Required env vars
-----------------
  EMAIL_PROVIDER   "resend" (default) or "sendgrid"
  EMAIL_API_KEY    API key from your chosen provider
  MAIL_FROM        Sender address — must be verified with the provider
                   e.g. "Taqdeer <noreply@yourdomain.com>"
                   Resend dev/testing shortcut: "Taqdeer <onboarding@resend.dev>"
                     → delivers only to your Resend account's owner email

Dev / no-key mode
-----------------
  When EMAIL_API_KEY is absent or still a placeholder, is_configured() returns
  False.  The caller (auth.py) detects this and falls back to dev-mode behaviour
  (returning dev_code in the response so the flow can be tested locally without
  any real email provider).

Public API
----------
  is_configured()                           → bool
  email_api_diagnostics()                   → list[str]
  send_reset_email(to_email, code)          → None  (raises EmailApiError)
  send_test_email(to_email)                 → None  (raises EmailApiError)

Logs
----
  [EMAIL-API-SEND]   success to=...
  [EMAIL-API-FAILED] provider=... status=... error=...
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


# ── Public exception ──────────────────────────────────────────────────────────

class EmailApiError(Exception):
    """Raised when an email API call fails."""

# Alias — old code that catches EmailError still works after the rename
EmailError = EmailApiError


# ── Helpers ───────────────────────────────────────────────────────────────────

_PLACEHOLDERS = {"your-api-key", "YOUR_API_KEY", "YOUR-API-KEY", ""}


def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def is_configured() -> bool:
    """Return True when EMAIL_API_KEY is set to a real (non-placeholder) value."""
    return _cfg("EMAIL_API_KEY") not in _PLACEHOLDERS


def email_api_diagnostics() -> list[str]:
    """
    Return a list of human-readable config warnings.
    An empty list means the configuration looks plausible.
    """
    warnings: list[str] = []

    provider = _cfg("EMAIL_PROVIDER", "resend").lower()
    if provider not in ("resend", "sendgrid"):
        warnings.append(
            f"EMAIL_PROVIDER='{provider}' is not supported. "
            "Use 'resend' (default) or 'sendgrid'."
        )

    if not is_configured():
        warnings.append(
            "EMAIL_API_KEY is not set. "
            "Sign up at resend.com (free tier: 3,000 emails/month, no credit card). "
            "Create an API key and add EMAIL_API_KEY=re_... to backend/.env. "
            "While testing you can use MAIL_FROM=Taqdeer <onboarding@resend.dev> "
            "and emails arrive at your Resend account email."
        )

    mail_from = _cfg("MAIL_FROM")
    if not mail_from:
        warnings.append(
            "MAIL_FROM is not set. "
            "Set it to your verified sender address, e.g.: "
            "MAIL_FROM=Taqdeer <noreply@yourdomain.com>. "
            "For Resend testing without domain verification, use: "
            "MAIL_FROM=Taqdeer <onboarding@resend.dev>"
        )

    return warnings


# ── Low-level HTTP call ───────────────────────────────────────────────────────

def _post_json(url: str, headers: dict, body: dict) -> dict:
    """
    POST *body* as JSON to *url* with *headers*.
    Returns the parsed response body.
    Raises EmailApiError on HTTP errors or network failures.
    """
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise EmailApiError(
            f"HTTP {exc.code} from email API: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise EmailApiError(
            f"Network error reaching email API: {exc.reason}"
        ) from exc
    except Exception as exc:
        raise EmailApiError(f"Unexpected email API error: {exc}") from exc


# ── Provider implementations ──────────────────────────────────────────────────

def _send_via_resend(to: str, subject: str, html: str, text: str) -> None:
    """
    Resend API — https://resend.com/docs/api-reference/emails/send-email

    Free tier: 3,000 emails/month, 100/day. No credit card required.
    Domain verification required for custom From addresses; while testing
    you may use MAIL_FROM=Taqdeer <onboarding@resend.dev> (delivers only
    to your Resend account email).
    """
    _post_json(
        url="https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {_cfg('EMAIL_API_KEY')}",
            "Content-Type":  "application/json",
        },
        body={
            "from":    _cfg("MAIL_FROM", "Taqdeer <onboarding@resend.dev>"),
            "to":      [to],
            "subject": subject,
            "html":    html,
            "text":    text,
        },
    )


def _send_via_sendgrid(to: str, subject: str, html: str, text: str) -> None:
    """
    SendGrid API — https://docs.sendgrid.com/api-reference/mail-send/mail-send

    Free tier: 100 emails/day permanently. Sender Identity verification required.
    """
    mail_from = _cfg("MAIL_FROM", "noreply@taqdeer.app")
    m = re.match(r'^(.+?)\s*<([^>]+)>$', mail_from)
    from_name  = m.group(1).strip() if m else "Taqdeer"
    from_email = m.group(2).strip() if m else mail_from

    _post_json(
        url="https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {_cfg('EMAIL_API_KEY')}",
            "Content-Type":  "application/json",
        },
        body={
            "personalizations": [{"to": [{"email": to}]}],
            "from":    {"email": from_email, "name": from_name},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": text},
                {"type": "text/html",  "value": html},
            ],
        },
    )


def _dispatch(to: str, subject: str, html: str, text: str) -> None:
    """Route to the configured provider (default: resend)."""
    provider = _cfg("EMAIL_PROVIDER", "resend").lower()
    if provider == "sendgrid":
        _send_via_sendgrid(to, subject, html, text)
    else:
        _send_via_resend(to, subject, html, text)


# ── Public send functions ─────────────────────────────────────────────────────

def send_reset_email(to_email: str, code: str) -> None:
    """
    Send a password-reset code to *to_email*.
    Raises EmailApiError on failure — caller decides whether to surface this.
    """
    _dispatch(
        to      = to_email,
        subject = "Your Taqdeer password reset code",
        html    = _reset_html(code),
        text    = (
            f"Your Taqdeer password reset code is: {code}\n\n"
            "This code expires in 10 minutes.\n"
            "If you did not request this, please ignore this email."
        ),
    )
    print(f"[EMAIL-API-SEND] success to={to_email!r}")


def send_test_email(to_email: str) -> None:
    """Send a plain test email to verify API connectivity and credentials."""
    _dispatch(
        to      = to_email,
        subject = "Taqdeer — email API test",
        html    = (
            "<p style='font-family:Arial,sans-serif;'>"
            "<strong>Email API test</strong><br><br>"
            "This is a test email from Taqdeer. "
            "If you received it, your email API configuration is working correctly."
            "</p>"
        ),
        text    = (
            "Taqdeer email API test\n\n"
            "This is a test email from Taqdeer. "
            "If you received it, your email API configuration is working correctly."
        ),
    )
    print(f"[EMAIL-API-SEND] test email delivered to={to_email!r}")


# Backward-compat alias (old code called send_reset_code)
send_reset_code = send_reset_email


# ── HTML template ─────────────────────────────────────────────────────────────

def _reset_html(code: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f4f7fb;margin:0;padding:0;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f4f7fb;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;
                      box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">
          <tr>
            <td style="background:#28405B;padding:28px 32px;text-align:center;">
              <h1 style="color:#ffffff;margin:0;font-size:22px;letter-spacing:0.5px;">
                Taqdeer
              </h1>
              <p style="color:#98B9D4;margin:6px 0 0;font-size:13px;">
                AI-Powered Exam Grading Platform
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px;">
              <h2 style="color:#28405B;margin:0 0 12px;font-size:18px;">
                Password Reset Request
              </h2>
              <p style="color:#4a5568;margin:0 0 24px;font-size:14px;line-height:1.6;">
                We received a request to reset your password.
                Use the verification code below. This code expires in
                <strong>10 minutes</strong>.
              </p>
              <div style="background:#f4f7fb;border:2px dashed #98B9D4;
                          border-radius:10px;padding:24px;text-align:center;
                          margin:0 0 24px;">
                <span style="font-size:36px;font-weight:700;letter-spacing:10px;
                             color:#28405B;font-family:monospace;">{code}</span>
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
                &copy; 2025 Taqdeer &middot; AI-Powered Exam Grading
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
