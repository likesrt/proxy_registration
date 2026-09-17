"""Unit tests for .env config helpers (web config page)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.env_config import (
    parse_env_file,
    upsert_env_file,
    get_config_for_ui,
    all_config_keys,
    _format_env_value,
)


class TestEnvConfig(unittest.TestCase):
    def test_format_and_parse(self):
        self.assertEqual(_format_env_value("simple"), "simple")
        self.assertTrue(_format_env_value("a b").startswith('"'))

    def test_upsert_preserves_comments(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment keep\nFOO=1\nBAR=old\n")
            r = upsert_env_file(
                {"BAR": "new", "BAZ": "x"},
                path=path,
                keys_allowlist=["FOO", "BAR", "BAZ"],
            )
            self.assertIn("BAR", r["updated"])
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("# comment keep", text)
            self.assertIn("FOO=1", text)
            self.assertIn("BAR=new", text)
            self.assertIn("BAZ=x", text)
            vals = parse_env_file(path)
            self.assertEqual(vals["BAR"], "new")
            self.assertEqual(vals["BAZ"], "x")

    def test_schema_has_core_keys(self):
        keys = set(all_config_keys())
        for k in (
            "EMAIL_SERVICE_TYPE",
            "TURNSTILE_SOLVER_URL",
            "REGISTER_PASSWORD",
            "WEB_PORT",
            "WEB_PASSWORD",
            "FEED_TOKEN",
            "AUTO_REGISTER_ENABLED",
            "AUTO_REGISTER_INTERVAL",
            "AUTO_REGISTER_TARGET",
            "AUTO_REGISTER_MIN_VALID",
            "AUTO_REGISTER_MAX_PER_ROUND",
            "GPTMAIL_PUBLIC_KEY_URL",
        ):
            self.assertIn(k, keys)
        # Removed with the Resin push chain
        self.assertNotIn("RESIN_SUBSCRIPTION_URL", keys)
        self.assertNotIn("RESIN_API_TOKEN", keys)

    def test_get_config_for_ui(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("WEB_PASSWORD=MyName\nADMIN_PASSWORD=secret\n")
            # Isolate this temporary file from values loaded from the project's .env.
            with patch.dict(os.environ, {}, clear=True):
                data = get_config_for_ui(path, include_secrets=True)
                self.assertTrue(data["exists"])
                self.assertEqual(data["values"].get("WEB_PASSWORD"), "MyName")
                self.assertEqual(data["values"].get("ADMIN_PASSWORD"), "secret")
                self.assertTrue(data["secrets_set"].get("ADMIN_PASSWORD"))
                masked = get_config_for_ui(path, include_secrets=False)
                self.assertEqual(masked["values"].get("ADMIN_PASSWORD"), "")
                self.assertTrue(masked["secrets_set"].get("ADMIN_PASSWORD"))

    def test_feed_token_defaults_disabled(self):
        """Empty FEED_TOKEN is the safe default (route answers 503, never open)."""
        from src.env_config import CONFIG_SCHEMA

        item = next(
            i
            for g in CONFIG_SCHEMA
            for i in g["keys"]
            if i["key"] == "FEED_TOKEN"
        )
        self.assertEqual(item.get("default"), "")
        self.assertTrue(item.get("secret") or item.get("type") == "password")

    def test_auto_register_defaults(self):
        from src.env_config import CONFIG_SCHEMA

        vals = {
            i["key"]: i.get("default")
            for g in CONFIG_SCHEMA
            for i in g["keys"]
            if i["key"].startswith("AUTO_REGISTER_")
        }
        self.assertEqual(vals["AUTO_REGISTER_ENABLED"], "false")
        self.assertEqual(vals["AUTO_REGISTER_INTERVAL"], "1800")
        self.assertEqual(vals["AUTO_REGISTER_TARGET"], "50")
        self.assertEqual(vals["AUTO_REGISTER_MIN_VALID"], "10")
        self.assertEqual(vals["AUTO_REGISTER_MAX_PER_ROUND"], "20")

    def test_secret_true_without_password_type_in_schema(self):
        """Fields with only secret:True (type text) must still be treated as secrets."""
        from src.env_config import CONFIG_SCHEMA

        found = False
        for g in CONFIG_SCHEMA:
            for item in g["keys"]:
                if item.get("secret") and item.get("type") != "password":
                    found = True
                    break
        self.assertTrue(found, "expected at least one secret text field (e.g. WEB_PASSWORD)")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web_app.py"), encoding="utf-8") as f:
            src = f.read()
        # After login auth: config page shows all fields as plaintext
        self.assertIn("always plain text", src)
        self.assertIn("include_secrets=True", src)
        self.assertIn("LOGIN_HTML", src)
        self.assertIn("/api/auth/login", src)


class TestConfigPageSource(unittest.TestCase):
    def test_web_has_config_routes(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "web_app.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('/config"', src)
        self.assertIn("/api/config", src)
        self.assertIn("CONFIG_HTML", src)
        self.assertIn("保存到 .env", src)
        self.assertIn("WEB_PASSWORD", src)
        self.assertIn("_require_web_auth", src)
        self.assertIn("全部明文", src)


if __name__ == "__main__":
    unittest.main()
