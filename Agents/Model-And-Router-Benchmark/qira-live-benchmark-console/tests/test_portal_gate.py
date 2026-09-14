"""Offline tests for the Demo Portal sign-in gate: no network, no real users."""

import http.client
import importlib.util
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
GATE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "portal-gate" / "gate.py"
_SECRET_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("PORTAL_GATE_SECRET", str(Path(_SECRET_DIR.name) / "secret"))

SPEC = importlib.util.spec_from_file_location("portal_gate", GATE_PATH)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

# Generated externally by Apache's own tool, so this fixture is an independent
# reference rather than output of the implementation under test:
#     htpasswd -nbm demo correct-horse
APR1_LINE = "demo:$apr1$koog43vM$cQiCIAlM6zzTKRl5GLBZl1"


class Apr1Verification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".htpasswd"
        self.path.write_text(APR1_LINE + "\n", encoding="utf-8")
        self.patcher = patch.object(GATE, "USER_FILES", [self.path])
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_apr1_reproduces_the_stored_digest(self):
        stored = APR1_LINE.split(":", 1)[1]
        salt = stored.split("$")[2].encode("ascii")
        self.assertEqual(GATE.apr1_hash(b"correct-horse", salt), stored)

    def test_correct_password_is_accepted(self):
        self.assertTrue(GATE.check_credentials("demo", "correct-horse"))

    def test_wrong_password_is_rejected(self):
        self.assertFalse(GATE.check_credentials("demo", "correct-horse "))
        self.assertFalse(GATE.check_credentials("demo", "wrong"))

    def test_unknown_user_is_rejected(self):
        self.assertFalse(GATE.check_credentials("nobody", "correct-horse"))

    def test_malformed_user_names_are_rejected_without_reading_files(self):
        for name in ("", "demo:extra", "demo\nadmin"):
            self.assertFalse(GATE.check_credentials(name, "correct-horse"))

    def test_missing_credential_file_is_not_fatal(self):
        with patch.object(GATE, "USER_FILES", [Path(self.tmp.name) / "absent"]):
            self.assertFalse(GATE.check_credentials("demo", "correct-horse"))


class HtpasswdDelegation(unittest.TestCase):
    """Non-apr1 entries are delegated; the password must stay off the argv."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".htpasswd"
        # A bcrypt-looking entry forces the delegated branch.
        self.path.write_text("bcryptuser:$2y$05$abcdefghijklmnopqrstuv\n", encoding="utf-8")
        self.patcher = patch.object(GATE, "USER_FILES", [self.path])
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_password_is_piped_on_stdin_not_passed_as_an_argument(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(cmd, 0)

        with patch.object(GATE.shutil, "which", return_value="/usr/bin/htpasswd"), \
                patch.object(GATE.subprocess, "run", fake_run):
            self.assertTrue(GATE.check_credentials("bcryptuser", "s3cr3t-value"))

        self.assertIn("-vi", captured["cmd"])
        self.assertNotIn("-vb", captured["cmd"])
        self.assertEqual(captured["input"], b"s3cr3t-value")
        # /proc/<pid>/cmdline exposes argv to every local account.
        self.assertNotIn("s3cr3t-value", captured["cmd"])

    def test_a_nonzero_exit_is_a_rejected_password(self):
        with patch.object(GATE.shutil, "which", return_value="/usr/bin/htpasswd"), \
                patch.object(GATE.subprocess, "run",
                             lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)):
            self.assertFalse(GATE.check_credentials("bcryptuser", "wrong"))

    def test_a_missing_htpasswd_binary_rejects_rather_than_crashes(self):
        with patch.object(GATE.shutil, "which", return_value=None):
            self.assertFalse(GATE.check_credentials("bcryptuser", "anything"))


class Sessions(unittest.TestCase):
    def test_issued_token_round_trips(self):
        self.assertEqual(GATE.verify_token(GATE.issue_token("demo")), "demo")

    def test_expired_token_is_rejected(self):
        self.assertIsNone(GATE.verify_token(GATE.issue_token("demo", ttl=-1)))

    def test_tampered_payload_is_rejected(self):
        token = GATE.issue_token("demo")
        body, _, signature = token.partition(".")
        forged = GATE.base64.urlsafe_b64encode(b"admin|99999999999").decode().rstrip("=")
        self.assertIsNone(GATE.verify_token(f"{forged}.{signature}"))

    def test_token_signed_with_another_secret_is_rejected(self):
        token = GATE.issue_token("demo")
        with patch.object(GATE, "SECRET", b"a-different-secret"):
            self.assertIsNone(GATE.verify_token(token))

    def test_garbage_is_rejected(self):
        for value in ("", "no-dot", "a.b", "....", "%%%.%%%"):
            self.assertIsNone(GATE.verify_token(value))


class RedirectSafety(unittest.TestCase):
    def test_same_site_paths_are_kept(self):
        self.assertEqual(GATE.safe_next("/qira-benchmark/"), "/qira-benchmark/")
        self.assertEqual(GATE.safe_next("/?industry=notebook"), "/?industry=notebook")

    def test_off_site_targets_fall_back_to_the_portal_home(self):
        for value in (None, "", "https://malicious.example", "//malicious.example",
                      "/\\malicious.example", "javascript:alert(1)"):
            self.assertEqual(GATE.safe_next(value), "/")

    def test_login_routes_do_not_redirect_to_themselves(self):
        self.assertEqual(GATE.safe_next("/portal-login?next=/"), "/")
        self.assertEqual(GATE.safe_next("/portal-logout"), "/")

    def test_control_characters_cannot_reach_a_response_header(self):
        # parse_qs decodes %0d%0a, so a smuggled CRLF would otherwise be
        # written straight into the Location header and split the response.
        for value in ("/ok\r\nX-Injected: 1", "/ok\nX-Injected: 1",
                      "/ok\rX-Injected: 1", "/ok\x00", "/ok\x7f"):
            self.assertEqual(GATE.safe_next(value), "/")

    def test_a_trailing_newline_does_not_slip_past_the_allowlist(self):
        # Python's `$` also matches before a trailing newline, so the pattern
        # must be anchored with \Z or "/foo\n" would be accepted.
        for value in ("/foo\n", "/\n", "/a/b/\n"):
            self.assertEqual(GATE.safe_next(value), "/")

    def test_only_valid_url_path_characters_are_accepted(self):
        for value in ("/a b", "/tab\there", "/quote\"x", "/angle<x>", "/back\\slash"):
            self.assertEqual(GATE.safe_next(value), "/")
        for value in ("/qira-benchmark/api/catalog?run_id=abc123",
                      "/path_with.dots~and-dashes/", "/a%20b", "/x?y=1&z=2"):
            self.assertEqual(GATE.safe_next(value), value)


class SecretFile(unittest.TestCase):
    def test_secret_is_created_private_without_a_permissions_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "portal-gate.secret"
            with patch.object(GATE, "SECRET_PATH", target):
                secret = GATE.load_secret()
            self.assertTrue(secret)
            self.assertTrue(target.is_file())
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_existing_secret_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "portal-gate.secret"
            with patch.object(GATE, "SECRET_PATH", target):
                first = GATE.load_secret()
                second = GATE.load_secret()
            self.assertEqual(first, second)


class HttpSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / ".htpasswd"
        path.write_text(APR1_LINE + "\n", encoding="utf-8")
        cls.files_patch = patch.object(GATE, "USER_FILES", [path])
        cls.files_patch.start()
        cls.delay_patch = patch.object(GATE, "FAILURE_DEADLINE_SECONDS", 0)
        cls.delay_patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), GATE.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.delay_patch.stop()
        cls.files_patch.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        head = dict(headers or {})
        if body is not None:
            head.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn.request(method, path, body=body, headers=head)
        response = conn.getresponse()
        payload = response.read().decode("utf-8", errors="replace")
        result = (response.status, dict(response.getheaders()), payload)
        conn.close()
        return result

    def sign_in(self, user="demo", password="correct-horse", next_url="/qira-benchmark/"):
        return self.request(
            "POST", "/portal-login",
            body=f"username={user}&password={password}&next={next_url}",
        )

    def test_auth_endpoint_rejects_an_anonymous_request(self):
        status, _, _ = self.request("GET", "/portal-auth")
        self.assertEqual(status, 401)

    def test_login_page_is_served_without_a_session(self):
        status, headers, body = self.request("GET", "/portal-login?next=/qira-benchmark/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn('name="password"', body)
        self.assertIn("clawpilotTheme", body)
        self.assertIn("--cp-bg: #f7f4ef", body)

    def test_successful_sign_in_sets_a_guarded_cookie_and_redirects(self):
        status, headers, _ = self.sign_in()
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/qira-benchmark/")
        cookie = headers["Set-Cookie"]
        self.assertIn("portal_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)

    def test_session_cookie_satisfies_the_auth_endpoint(self):
        _, headers, _ = self.sign_in()
        token = headers["Set-Cookie"].split(";", 1)[0]
        status, _, _ = self.request("GET", "/portal-auth", headers={"Cookie": token})
        self.assertEqual(status, 204)

    def test_failed_sign_in_redisplays_the_form_without_a_cookie(self):
        status, headers, body = self.sign_in(password="wrong")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn("did not match", body)

    def test_off_site_next_is_neutralised_on_success(self):
        _, headers, _ = self.sign_in(next_url="https://malicious.example")
        self.assertEqual(headers["Location"], "/")

    def test_crlf_in_next_cannot_split_the_redirect_response(self):
        status, headers, body = self.sign_in(next_url="/ok%0d%0aX-Injected:%201")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertNotIn("X-Injected", headers)
        self.assertNotIn("X-Injected", body)

    def test_crlf_in_a_login_page_request_is_not_reflected(self):
        status, headers, body = self.request(
            "GET", "/portal-login?next=/ok%0d%0aX-Injected:%201")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Injected", headers)
        self.assertNotIn("X-Injected", body)

    def test_logout_clears_the_cookie(self):
        status, headers, _ = self.request("GET", "/portal-logout")
        self.assertEqual(status, 302)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_forged_cookie_does_not_authenticate(self):
        status, _, _ = self.request(
            "GET", "/portal-auth", headers={"Cookie": "portal_session=forged.value"})
        self.assertEqual(status, 401)

    def test_unknown_route_is_a_404(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", body="x=1")[0], 404)


class Packaging(unittest.TestCase):
    def test_gate_compiles(self):
        result = subprocess.run(
            ["python", "-m", "py_compile", str(GATE_PATH)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deployment_files_are_shipped(self):
        deploy = GATE_PATH.parent
        for name in ("portal-gate.service", "nginx-portal-gate.conf"):
            self.assertTrue((deploy / name).is_file(), name)

    def test_nginx_snippet_documents_the_auth_request_switch(self):
        conf = (GATE_PATH.parent / "nginx-portal-gate.conf").read_text(encoding="utf-8")
        self.assertIn("auth_request /portal-auth;", conf)
        self.assertIn("error_page 401 = @portal_signin;", conf)
        self.assertIn("location = /portal-login", conf)


if __name__ == "__main__":
    unittest.main()
