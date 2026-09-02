import hashlib
import hmac
import unittest

from app.main import (
    NotificationPublisher,
    Settings,
    build_notification,
    build_telegram_payload,
    verify_signature,
)


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
                "user": {"login": "someone", "html_url": "https://github.com/someone"},
            },
        }
        notification = build_notification("issue_comment", payload, "ihhaiq")
        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertEqual(notification.repository, "ihhaiq/GitSpy")
        self.assertEqual(notification.content, "Hello <b>world</b>")
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
                "user": {"login": "someone", "html_url": "https://github.com/someone"},
            },
        }
        notification = build_notification("issue_comment", payload, "ihhaiq")
        assert notification is not None
        settings = Settings("token", "-1001", "secret", "ihhaiq", None, 8080)
        telegram_payload = build_telegram_payload(settings, notification)
        table = telegram_payload["rich_message"]["blocks"][0]
        footer = telegram_payload["rich_message"]["blocks"][1]

        self.assertEqual(table["type"], "table")
        self.assertTrue(table["is_bordered"])
        self.assertEqual(table["cells"][0][0]["text"], "تعليق جديد")
        self.assertEqual(table["cells"][0][0]["colspan"], 2)
        self.assertEqual(table["cells"][4][1]["text"], "A comment")
        repository_button = table["cells"][1][1]["text"]["button"]
        self.assertEqual(repository_button["text"], "ihhaiq/GitSpy")
        self.assertEqual(repository_button["style"], "success")
        self.assertEqual(repository_button["url"], "https://github.com/ihhaiq/GitSpy")
        author_button = table["cells"][2][1]["text"]["button"]
        self.assertEqual(author_button["text"], "someone")
        self.assertEqual(author_button["style"], "primary")
        self.assertEqual(author_button["url"], "https://github.com/someone")
        self.assertEqual(footer["type"], "footer")
        footer_button = footer["text"]["button"]
        self.assertEqual(footer_button["text"], "فتح التعليق في GitHub")
        self.assertEqual(footer_button["style"], "primary")
        self.assertEqual(len(telegram_payload["rich_message"]["blocks"]), 2)
        self.assertNotIn("reply_markup", telegram_payload)

    def test_push_notification_groups_commits(self) -> None:
        payload = {
            "ref": "refs/heads/main",
            "deleted": False,
            "compare": "https://github.com/ihhaiq/GitSpy/compare/old...new",
            "repository": {"full_name": "ihhaiq/GitSpy", "owner": {"login": "ihhaiq"}},
            "sender": {"login": "ihhaiq", "html_url": "https://github.com/ihhaiq"},
            "commits": [
                {"id": "1234567890", "message": "First change", "author": {"name": "HUSSEIN"}},
                {"id": "abcdef1234", "message": "Second change", "author": {"name": "HUSSEIN"}},
            ],
        }
        notification = build_notification("push", payload, "ihhaiq")
        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertEqual(notification.title, "Push جديد")
        self.assertEqual(notification.subject, "main")
        self.assertIn("1234567 — First change", notification.content)
        self.assertIn("abcdef1 — Second change", notification.content)
        self.assertNotIn("HUSSEIN", notification.content)
        self.assertEqual(notification.button_text, "عرض التغييرات في GitHub")
        self.assertEqual(notification.group_key, "ihhaiq/GitSpy")
        self.assertEqual(notification.event_kind, "push")
        self.assertEqual(notification.author_url, "https://github.com/ihhaiq")

    def test_merged_pull_request_notification(self) -> None:
        payload = {
            "action": "closed",
            "repository": {
                "full_name": "ihhaiq/GitSpy",
                "html_url": "https://github.com/ihhaiq/GitSpy",
                "owner": {"login": "ihhaiq"},
            },
            "sender": {"login": "ihhaiq", "html_url": "https://github.com/ihhaiq"},
            "pull_request": {
                "merged": True,
                "merge_commit_sha": "1234567890abcdef",
                "title": "إضافة ميزة جديدة",
                "html_url": "https://github.com/ihhaiq/GitSpy/pull/8",
                "merged_by": {"login": "ihhaiq", "html_url": "https://github.com/ihhaiq"},
                "head": {"ref": "feature/new"},
                "base": {"ref": "main"},
            },
        }
        notification = build_notification("pull_request", payload, "ihhaiq")
        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertEqual(notification.title, "Merge جديد")
        self.assertEqual(notification.subject, "feature/new → main")
        self.assertEqual(notification.content, "• 1234567 — إضافة ميزة جديدة")
        self.assertEqual(notification.event_id, "1234567890abcdef")
        self.assertEqual(notification.event_kind, "merge")
        self.assertEqual(notification.author_url, "https://github.com/ihhaiq")

    def test_nearby_updates_edit_the_same_message(self) -> None:
        sent: list[object] = []
        edited: list[tuple[object, int]] = []
        settings = Settings("token", "-1001", "secret", "ihhaiq", None, 8080)

        def sender(_settings: Settings, notification: object) -> int:
            sent.append(notification)
            return 77

        def editor(_settings: Settings, notification: object, message_id: int) -> None:
            edited.append((notification, message_id))

        publisher = NotificationPublisher(settings, sender=sender, editor=editor)
        base_payload = {
            "ref": "refs/heads/main",
            "deleted": False,
            "repository": {"full_name": "ihhaiq/GitSpy", "owner": {"login": "ihhaiq"}},
            "sender": {"login": "ihhaiq"},
        }
        first = build_notification(
            "push",
            {**base_payload, "after": "aaa1111", "commits": [{"id": "aaa1111", "message": "First"}]},
            "ihhaiq",
        )
        second = build_notification(
            "push",
            {**base_payload, "after": "bbb2222", "commits": [{"id": "bbb2222", "message": "Second"}]},
            "ihhaiq",
        )
        assert first is not None and second is not None

        publisher.publish(first)
        publisher.publish(second)

        self.assertEqual(len(sent), 1)
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0][1], 77)
        self.assertEqual(
            edited[0][0].content,
            "• aaa1111 — First\n• bbb2222 — Second",
        )

    def test_aggregation_keeps_branches_and_removes_duplicate_commits(self) -> None:
        sent: list[object] = []
        edited: list[tuple[object, int]] = []
        settings = Settings("token", "-1001", "secret", "ihhaiq", None, 8080)

        def sender(_settings: Settings, notification: object) -> int:
            sent.append(notification)
            return 88

        def editor(_settings: Settings, notification: object, message_id: int) -> None:
            edited.append((notification, message_id))

        publisher = NotificationPublisher(settings, sender=sender, editor=editor)
        common = {
            "deleted": False,
            "repository": {"full_name": "ihhaiq/GitSpy", "owner": {"login": "ihhaiq"}},
            "sender": {"login": "ihhaiq"},
        }
        first = build_notification(
            "push",
            {
                **common,
                "ref": "refs/heads/feature/one",
                "after": "aaa1111",
                "commits": [{"id": "aaa1111", "message": "First"}],
            },
            "ihhaiq",
        )
        second = build_notification(
            "push",
            {
                **common,
                "ref": "refs/heads/feature/two",
                "after": "bbb2222",
                "commits": [
                    {"id": "aaa1111", "message": "First"},
                    {"id": "bbb2222", "message": "Second"},
                ],
            },
            "ihhaiq",
        )
        assert first is not None and second is not None

        publisher.publish(first)
        publisher.publish(second)

        combined = edited[0][0]
        self.assertEqual(combined.subject_label, "الفروع")
        self.assertEqual(combined.subject, "feature/one، feature/two")
        self.assertEqual(combined.content.count("aaa1111"), 1)
        self.assertEqual(combined.content.count("bbb2222"), 1)


if __name__ == "__main__":
    unittest.main()
