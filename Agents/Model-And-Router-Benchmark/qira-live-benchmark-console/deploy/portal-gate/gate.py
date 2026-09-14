"""
In-page login gate for the Linux Work VM Demo Portal.

Replaces the browser's Basic-Auth popup with a themed sign-in page. nginx keeps
enforcing access with `auth_request`; this service only answers three questions:

    GET  /portal-auth    is this request already signed in?   (nginx subrequest)
    GET  /portal-login   render the sign-in page
    POST /portal-login   check credentials, issue a session cookie
    GET  /portal-logout  drop the session

Credentials are verified against the htpasswd files nginx already used, so the
existing portal password keeps working and nobody is locked out by the switch.
Apache's apr1 (MD5) hashes are verified in pure Python; bcrypt and SHA entries
are delegated to the system `htpasswd` binary, which is already installed.

The session cookie is an HMAC-SHA256 token over the user name and expiry, keyed
by a secret generated on first start. It is HttpOnly and SameSite=Lax, so it is
never sent on a cross-site POST - the same property the console's own Origin
check relies on.

Usage:
    python gate.py --port 8515 [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import os
import re
import secrets
import shutil
import subprocess
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
COOKIE_NAME = "portal_session"
SESSION_TTL_SECONDS = int(os.environ.get("PORTAL_GATE_TTL", str(12 * 3600)))
SECRET_PATH = Path(os.environ.get("PORTAL_GATE_SECRET", "/etc/qira-benchmark/portal-gate.secret"))
USER_FILES = [
    Path(p.strip())
    for p in os.environ.get(
        "PORTAL_GATE_USER_FILES",
        "/etc/nginx/.htpasswd,/etc/nginx/.htpasswd-qira",
    ).split(",")
    if p.strip()
]
PORTAL_TITLE = os.environ.get("PORTAL_GATE_TITLE", "AI Services Hub")
PORTAL_SUBTITLE = os.environ.get(
    "PORTAL_GATE_SUBTITLE", "Microsoft Foundry demo portal - Linux Work VM"
)
MAX_BODY_BYTES = 16 * 1024

# Only the characters a URL path and query may legitimately contain. An
# allowlist is used rather than stripping bad characters, so a redirect target
# can never carry a control character into a response header. The pattern is
# anchored with \A and \Z rather than ^ and $, because in Python `$` also
# matches just before a trailing newline and would let "/path\n" through.
SAFE_NEXT_PATTERN = re.compile(r"\A/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*\Z")

# A failed sign-in must not be cheap to retry, and it must not reveal whether
# the user name exists. This is a deadline rather than an added pause: every
# rejection returns after the same elapsed time regardless of which check
# failed or how expensive the hash comparison was.
FAILURE_DEADLINE_SECONDS = float(os.environ.get("PORTAL_GATE_FAILURE_DEADLINE", "0.5"))


# --------------------------------------------------------------------------
# Secret and session tokens
# --------------------------------------------------------------------------

def load_secret() -> bytes:
    if SECRET_PATH.is_file():
        value = SECRET_PATH.read_bytes().strip()
        if value:
            return value
    secret = base64.urlsafe_b64encode(secrets.token_bytes(48))
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Create the file already private. Writing first and calling chmod after
    # would leave the signing key world-readable for that window.
    handle = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, secret + b"\n")
    finally:
        os.close(handle)
    return secret


SECRET = load_secret()


def issue_token(user: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    expires = int(time.time()) + ttl
    payload = f"{user}|{expires}".encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def verify_token(token: str) -> str | None:
    """Return the signed-in user name, or None when the token is not usable."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    expected = hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        user, _, expires = payload.rpartition("|")
        if not user or time.time() > int(expires):
            return None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return user


# --------------------------------------------------------------------------
# Credential verification
# --------------------------------------------------------------------------

APR1_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _apr1_encode(final: bytes) -> str:
    """Apache's custom base64 ordering for the apr1 digest."""
    order = ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5))
    out = []
    for a, b, c in order:
        value = (final[a] << 16) | (final[b] << 8) | final[c]
        for _ in range(4):
            out.append(APR1_ALPHABET[value & 0x3F])
            value >>= 6
    value = final[11]
    for _ in range(2):
        out.append(APR1_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(out)


def apr1_hash(password: bytes, salt: bytes) -> str:
    """Apache MD5 (``$apr1$``) crypt, the htpasswd default format."""
    magic = b"$apr1$"
    ctx = hashlib.md5(password + magic + salt)
    alternate = hashlib.md5(password + salt + password).digest()

    remaining = len(password)
    while remaining > 0:
        ctx.update(alternate[: min(remaining, 16)])
        remaining -= 16

    remaining = len(password)
    while remaining:
        ctx.update(b"\0" if remaining & 1 else password[:1])
        remaining >>= 1

    final = ctx.digest()
    for i in range(1000):
        step = hashlib.md5()
        step.update(password if i & 1 else final)
        if i % 3:
            step.update(salt)
        if i % 7:
            step.update(password)
        step.update(final if i & 1 else password)
        final = step.digest()

    return magic.decode() + salt.decode() + "$" + _apr1_encode(final)


def _verify_with_htpasswd(path: Path, user: str, password: str) -> bool:
    """
    Delegate bcrypt/SHA entries to the htpasswd binary already on the VM.

    The password is written to stdin rather than passed as an argument: argv is
    world-readable through /proc/<pid>/cmdline and is recorded by process
    accounting, so `-b` would expose every signed-in user's password to any
    local account. Apache's own documentation warns about exactly this.
    """
    binary = shutil.which("htpasswd")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-vi", str(path), user],
            input=password.encode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def check_credentials(user: str, password: str) -> bool:
    if not user or not password or "\n" in user or ":" in user:
        return False
    for path in USER_FILES:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            name, _, stored = line.partition(":")
            if name != user or not stored:
                continue
            if stored.startswith("$apr1$"):
                parts = stored.split("$")
                if len(parts) < 4:
                    continue
                if hmac.compare_digest(
                    apr1_hash(password.encode("utf-8"), parts[2].encode("ascii")), stored
                ):
                    return True
            elif _verify_with_htpasswd(path, user, password):
                return True
    return False


# --------------------------------------------------------------------------
# Sign-in page
# --------------------------------------------------------------------------

def safe_next(raw: str | None) -> str:
    """
    Only allow same-site paths, so the form cannot become an open redirect.

    The value reaches us through parse_qs, which decodes percent escapes, so a
    caller can smuggle real control characters (``%0d%0a``) into it. Those must
    never reach a response header or they would split it, letting an attacker
    append headers of their own to the redirect. Rather than removing bad
    characters, this accepts only the characters a URL path and query may
    contain, so anything unexpected falls back to the portal home page.
    """
    if not raw or not SAFE_NEXT_PATTERN.match(raw):
        return "/"
    if raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    if raw.startswith("/portal-login") or raw.startswith("/portal-logout"):
        return "/"
    return raw


def login_page(next_url: str, error: str | None = None) -> str:
    message = (
        f'<p class="form-error" role="alert">{html.escape(error)}</p>' if error else ""
    )
    hours = SESSION_TTL_SECONDS // 3600
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - {html.escape(PORTAL_TITLE)}</title>
<script>
  (() => {{
    const param = new URLSearchParams(window.location.search).get("clawpilotTheme");
    const theme =
      param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  }})();
</script>
<style>
:root {{
  color-scheme: light;
  --cp-bg: #f7f4ef;
  --cp-surface: #ffffff;
  --cp-border: #dedede;
  --cp-text: #242424;
  --cp-text-muted: #5c5c5c;
  --cp-accent: #b11f4b;
  --cp-accent-hover: #9a1a41;
  --cp-accent-fg: #ffffff;
  --cp-danger: #dc2626;
  --cp-highlight: rgba(177, 31, 75, 0.12);
}}
html[data-theme="dark"] {{
  color-scheme: dark;
  --cp-bg: #3d3b3a;
  --cp-surface: #292929;
  --cp-border: #474747;
  --cp-text: #dedede;
  --cp-text-muted: #919191;
  --cp-accent: #fd8ea1;
  --cp-accent-hover: #fb7b91;
  --cp-accent-fg: #1a1a1a;
  --cp-danger: #f87171;
  --cp-highlight: rgba(253, 142, 161, 0.12);
}}
* {{ box-sizing: border-box; }}
body {{
  display: grid;
  place-items: center;
  min-height: 100vh;
  margin: 0;
  padding: 24px;
  background: var(--cp-bg);
  color: var(--cp-text);
  font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
}}
.card {{
  width: 100%;
  max-width: 400px;
  padding: 32px;
  border: 1px solid var(--cp-border);
  border-radius: 16px;
  background: var(--cp-surface);
  box-shadow: 0 0 2px rgba(0, 0, 0, 0.12), 0 1px 2px rgba(0, 0, 0, 0.14);
}}
.brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }}
.brand-mark {{
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 0.625rem;
  background: var(--cp-accent);
  color: var(--cp-accent-fg);
  font-size: 19px;
  font-weight: 750;
}}
h1 {{ margin: 0; font-size: 18px; font-weight: 650; letter-spacing: -0.015em; }}
.sub {{ margin-top: 2px; color: var(--cp-text-muted); font-size: 11px; }}
label {{
  display: block;
  margin-bottom: 5px;
  color: var(--cp-text-muted);
  font-size: 11px;
  font-weight: 600;
}}
input {{
  width: 100%;
  margin-bottom: 15px;
  padding: 10px 12px;
  border: 1px solid var(--cp-border);
  border-radius: 0.625rem;
  background: var(--cp-surface);
  color: var(--cp-text);
  font-family: inherit;
  font-size: 14px;
  outline: none;
}}
input:focus {{ border-color: var(--cp-accent); box-shadow: 0 0 0 2px var(--cp-highlight); }}
button {{
  width: 100%;
  padding: 11px;
  border: 1px solid var(--cp-accent);
  border-radius: 0.625rem;
  background: var(--cp-accent);
  color: var(--cp-accent-fg);
  font-family: inherit;
  font-size: 14px;
  font-weight: 650;
  cursor: pointer;
}}
button:hover {{ border-color: var(--cp-accent-hover); background: var(--cp-accent-hover); }}
.form-error {{
  margin: 0 0 15px;
  padding: 9px 11px;
  border: 1px solid var(--cp-danger);
  border-left-width: 4px;
  border-radius: 0.625rem;
  background: var(--cp-surface);
  color: var(--cp-danger);
  font-size: 12px;
}}
.foot {{
  margin: 18px 0 0;
  padding-top: 14px;
  border-top: 1px solid var(--cp-border);
  color: var(--cp-text-muted);
  font-size: 10px;
  line-height: 1.6;
}}
</style>
</head>
<body>
  <main class="card">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">AI</div>
      <div>
        <h1>{html.escape(PORTAL_TITLE)}</h1>
        <div class="sub">{html.escape(PORTAL_SUBTITLE)}</div>
      </div>
    </div>
    {message}
    <form method="post" action="/portal-login">
      <input type="hidden" name="next" value="{html.escape(next_url, quote=True)}">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" autofocus required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
    <p class="foot">Demo environment. This browser stays signed in for {hours} hours.</p>
  </main>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PortalGate/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - only log real sign-in traffic
        if self.path.startswith("/portal-auth"):
            return
        super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------
    def _session_user(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is simply not a session
            return None
        morsel = jar.get(COOKIE_NAME)
        return verify_token(morsel.value) if morsel else None

    def _send(self, status: int, body: bytes = b"", content_type: str | None = None,
              headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers or []:
            # Defence in depth. A control character left in a header value would
            # split the response, so build the emitted value by filtering rather
            # than by trusting the caller, and refuse the request if anything had
            # to be removed.
            filtered = "".join(
                ch for ch in value if 0x20 <= ord(ch) != 0x7F
            )
            if filtered != value:
                raise ValueError(f"refusing to send a control character in header {key}")
            self.send_header(key, filtered)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_page(self, status: int, markup: str,
                   headers: list[tuple[str, str]] | None = None) -> None:
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _cookie_header(self, value: str, max_age: int) -> tuple[str, str]:
        parts = [
            f"{COOKIE_NAME}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/portal-auth":
            self._send(204 if self._session_user() else 401)
            return

        if route == "/portal-login":
            target = safe_next(query.get("next", [None])[0])
            if self._session_user():
                self._send(302, headers=[("Location", target)])
                return
            self._send_page(200, login_page(target))
            return

        if route == "/portal-logout":
            self._send(
                302,
                headers=[("Location", "/portal-login"), self._cookie_header("", 0)],
            )
            return

        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path != "/portal-login":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(413, b"Too large", "text/plain; charset=utf-8")
            return
        started = time.monotonic()
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        form = parse_qs(raw, keep_blank_values=True)
        target = safe_next(form.get("next", [None])[0])
        user = (form.get("username", [""])[0] or "").strip()
        password = form.get("password", [""])[0] or ""

        if check_credentials(user, password):
            self._send(
                303,
                headers=[
                    ("Location", target),
                    self._cookie_header(issue_token(user), SESSION_TTL_SECONDS),
                ],
            )
            return

        # Hold every rejection to the same deadline, so an unknown user and a
        # wrong password for a known user are indistinguishable by timing.
        remaining = FAILURE_DEADLINE_SECONDS - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        self._send_page(
            401, login_page(target, "That user name and password did not match.")
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8515)
    args = parser.parse_args()

    present = [str(p) for p in USER_FILES if p.is_file()]
    print(f"Portal sign-in gate on {args.host}:{args.port}")
    print(f"  credential sources: {', '.join(present) if present else 'NONE FOUND'}")
    if not present:
        print("  [warn] no htpasswd file is readable; every sign-in will fail")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
