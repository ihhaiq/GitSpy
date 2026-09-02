import hashlib
import hmac
import unittest

from app.main import build_notification, verify_signature


class SignatureTests(unittest.TestCase):
    def test_valid_signature(self) -> None:
        body = b'{"hello":"world"}'
        secret = "test-secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(secret, body, signature))

    def test_invalid_signature(self) -> None:
        self.assertFalse(verify_signature("secret", b"body", "sha256=bad"))


class NotificationTests(unittest.TestCase):
    def test_issue_comment(self) -> None:
        payload = {
            "action": "created",
            "repository": {"full_name": "ihhaiq/GitSpy", "owner": {"login": "ihhaiq"}},
            "issue": {"number": 7, "title": "Fix <this>"},
            "comment": {
                "body": "Hello <b>world</b>",
                "html_url": "https://github.com/ihhaiq/GitSpy/issues/7#issuecomment-1",
                "user": {"login": "someone"},
            },
        }
        notification = build_notification("issue_comment", payload, "ihhaiq")
        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertIn("ihhaiq/GitSpy", notification.text)
        self.assertIn("Hello &lt;b&gt;world&lt;/b&gt;", notification.text)
        self.assertNotIn("<this>", notification.text)

    def test_ignores_edits_and_other_owners(self) -> None:
        payload = {
            "action": "edited",
            "repository": {"full_name": "other/repo", "owner": {"login": "other"}},
            "issue": {"number": 1, "title": "x"},
            "comment": {"body": "x", "html_url": "https://example.com", "user": {"login": "x"}},
        }
        self.assertIsNone(build_notification("issue_comment", payload, "ihhaiq"))


if __name__ == "__main__":
    unittest.main()
