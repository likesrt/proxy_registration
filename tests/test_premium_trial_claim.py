"""Unit tests for the post-onboarding Premium trial activation request."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import claim_premium_trial
from src.proxyscrape_helpers import PREMIUM_TRIAL_CLAIM_ENDPOINT


class _FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class TestPremiumTrialClaim(unittest.TestCase):
    def test_claim_uses_current_bearer_token_and_requires_success_true(self):
        response = _FakeResponse(
            200,
            {
                "success": True,
                "message": "Premium free trial activated.",
                "account_id": "account-123",
            },
        )
        session = _FakeSession(response)

        result = claim_premium_trial(session, "current-access-token")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["data"]["account_id"], "account-123")
        self.assertEqual(len(session.calls), 1)
        args, kwargs = session.calls[0]
        self.assertEqual(args[0], PREMIUM_TRIAL_CLAIM_ENDPOINT)
        self.assertNotIn("data", kwargs)
        self.assertNotIn("Content-Type", kwargs["headers"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer current-access-token"
        )
        self.assertEqual(
            kwargs["headers"]["referer"],
            "https://dashboard.proxyscrape.com/v2/overview",
        )

    def test_claim_rejects_unsuccessful_payload_or_http_status(self):
        for response in (
            _FakeResponse(200, {"success": False, "message": "Not eligible"}),
            _FakeResponse(403, {"success": True, "message": "Forbidden"}),
            _FakeResponse(200, ValueError("not json"), text="not json"),
        ):
            with self.subTest(status=response.status_code):
                result = claim_premium_trial(_FakeSession(response), "token")
                self.assertFalse(result["ok"])


class TestPremiumTrialRegistrationOrder(unittest.TestCase):
    def test_trial_claim_follows_explicit_typeform_confirmation(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
            source = f.read()

        step_four = source.index("# Step 4: Typeform onboarding")
        confirmation = source.index(
            "if not is_typeform_onboarding_complete(me4):", step_four
        )
        claim = source.index("cres = claim_premium_trial(session, access_token)", step_four)
        self.assertLess(confirmation, claim)


if __name__ == "__main__":
    unittest.main()
