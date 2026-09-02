import hashlib
import hmac
import unittest

from app.main import Settings, build_notification, build_telegram_payload, verify_signature


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
        self.assertEqual(notification.repository, "ihhaiq/GitSpy")
        self.assertEqual(notification.comment, "Hello <b>world</b>")
        self.assertIn("Fix <this>", notification.subject)

    def test_ignores_edits_and_other_owners(self) -> None:
        payload = {
            "action": "edited",
            "repository": {"full_name": "other/repo", "owner": {"login": "other"}},
            "issue": {"number": 1, "title": "x"},
            "comment": {"body": "x", "html_url": "https://example.com", "user": {"login": "x"}},
        }
        self.assertIsNone(build_notification("issue_comment", payload, "ihhaiq"))

    def test_rich_table_layout(self) -> None:
        payload = {
            "action": "created",
            "repository": {"full_name": "ihhaiq/GitSpy", "owner": {"login": "ihhaiq"}},
            "issue": {"number": 7, "title": "A title"},
            "comment": {
                "body": "A comment",
                "html_url": "https://github.com/ihhaiq/GitSpy/issues/7#issuecomment-1",
                "user": {"login": "someone"},
            },
        }
        notification = build_notification("issue_comment", payload, "ihhaiq")
        assert notification is not None
        settings = Settings("token", "-1001", "secret", "ihhaiq", None, 8080)
        telegram_payload = build_telegram_payload(settings, notification)
        table = telegram_payload["rich_message"]["blocks"][0]

        self.assertEqual(table["type"], "table")
        self.assertTrue(table["is_bordered"])
        self.assertEqual(table["cells"][0][0]["text"], "تعليق جديد")
        self.assertEqual(table["cells"][0][0]["colspan"], 2)
        self.assertEqual(table["cells"][4][1]["text"], "A comment")


if __name__ == "__main__":
    unittest.main()
