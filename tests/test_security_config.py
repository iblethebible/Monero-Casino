import os
import unittest
from unittest.mock import patch

from flask import Flask, session

from security_config import configure_security


GOOD_SECRET = "9YAxN6qJbX4Hm8Vp2Ls7Rc5Tw3Ke0ZfU1DgQ"
OLD_SECRET = "your_super_secret_key"


class SecurityConfigurationTests(unittest.TestCase):
    def make_app(self, secret=GOOD_SECRET, cookie_secure=None):
        environment = {"FLASK_SECRET_KEY": secret}
        if cookie_secure is not None:
            environment["FLASK_SESSION_COOKIE_SECURE"] = cookie_secure

        app = Flask(__name__)
        with patch.dict(os.environ, environment, clear=True):
            configure_security(app)

        @app.get("/session-user")
        def session_user():
            return str(session.get("user_id", "anonymous"))

        return app

    def test_missing_secret_fails_closed(self):
        app = Flask(__name__)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FLASK_SECRET_KEY is required"):
                configure_security(app)

    def test_weak_and_retired_secrets_fail_closed(self):
        for secret in ("short", "a" * 64, OLD_SECRET):
            with self.subTest(secret=secret):
                app = Flask(__name__)
                with patch.dict(os.environ, {"FLASK_SECRET_KEY": secret}, clear=True):
                    with self.assertRaises(RuntimeError):
                        configure_security(app)

    def test_old_signed_cookie_is_rejected_after_key_replacement(self):
        old_app = Flask("old")
        old_app.secret_key = OLD_SECRET
        old_serializer = old_app.session_interface.get_signing_serializer(old_app)
        old_cookie = old_serializer.dumps({"user_id": 1})

        app = self.make_app()
        client = app.test_client()
        client.set_cookie(app.config["SESSION_COOKIE_NAME"], old_cookie)

        response = client.get("/session-user")
        self.assertEqual(response.get_data(as_text=True), "anonymous")

    def test_cookie_defaults_are_hardened(self):
        app = self.make_app()
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertIsNone(app.config["SECRET_KEY_FALLBACKS"])

    def test_local_http_cookie_override_is_explicit(self):
        app = self.make_app(cookie_secure="false")
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])


if __name__ == "__main__":
    unittest.main()
