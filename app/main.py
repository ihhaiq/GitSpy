from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gitspy")

MAX_WEBHOOK_BYTES = 2 * 1024 * 1024
MAX_COMMENT_CHARS = 2600
SUPPORTED_EVENTS = {
    "issue_comment",
    "pull_request_review_comment",
    "discussion_comment",
    "commit_comment",
    "push",
    "pull_request",
}

PUSH_EDIT_WINDOW_SECONDS = 90


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    github_webhook_secret: str
    github_owner: str
    telegram_message_thread_id: int | None
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            "GITHUB_WEBHOOK_SECRET": os.getenv("GITHUB_WEBHOOK_SECRET", "").strip(),
            "GITHUB_OWNER": os.getenv("GITHUB_OWNER", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        raw_thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
        return cls(
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            github_webhook_secret=required["GITHUB_WEBHOOK_SECRET"],
            github_owner=required["GITHUB_OWNER"],
            telegram_message_thread_id=int(raw_thread_id) if raw_thread_id else None,
            port=int(os.getenv("PORT", "8080")),
        )


@dataclass(frozen=True)
class Notification:
    title: str
    repository: str
    repository_url: str
    actor_label: str
    author: str
    author_url: str
    subject_label: str
    subject: str
    content_label: str
    content: str
    url: str
    button_text: str
    group_key: str | None = None
    event_id: str | None = None
    event_kind: str = "comment"


@dataclass
class PublishedNotification:
    message_id: int
    event_id: str | None
    event_kind: str
    updated_at: float


class DeliveryCache:
    """Small in-memory idempotency cache for GitHub delivery IDs."""

    def __init__(self, ttl_seconds: int = 86_400, max_entries: int = 4096) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, delivery_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, timestamp in self._items.items() if now - timestamp > self.ttl_seconds]
            for key in expired:
                self._items.pop(key, None)
            if delivery_id in self._items:
                return False
            if len(self._items) >= self.max_entries:
                oldest = min(self._items, key=self._items.get)  # type: ignore[arg-type]
                self._items.pop(oldest, None)
            self._items[delivery_id] = now
            return True

    def release(self, delivery_id: str) -> None:
        with self._lock:
            self._items.pop(delivery_id, None)


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _nested(mapping: Mapping[str, Any], *keys: str, default: Any = "") -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key, default)
    return current


def _shorten(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    normalized = " ".join(text.replace("\x00", "").split())
    if not normalized:
        return "(تعليق بدون نص)"
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def build_notification(event: str, payload: Mapping[str, Any], expected_owner: str) -> Notification | None:
    if event not in SUPPORTED_EVENTS:
        return None
    if event == "pull_request":
        if payload.get("action") != "closed" or not _nested(payload, "pull_request", "merged", default=False):
            return None
    elif event != "push" and payload.get("action") != "created":
        return None

    owner = str(_nested(payload, "repository", "owner", "login"))
    if owner.casefold() != expected_owner.casefold():
        logger.warning("Ignored event from unexpected owner: %s", owner or "<missing>")
        return None

    repo = str(_nested(payload, "repository", "full_name"))
    if not repo:
        return None

    if event == "push":
        if payload.get("deleted"):
            return None
        ref = str(payload.get("ref") or "")
        branch = ref.removeprefix("refs/heads/").removeprefix("refs/tags/") or "غير معروف"
        author = str(_nested(payload, "sender", "login") or _nested(payload, "pusher", "name") or "unknown")
        author_url = str(_nested(payload, "sender", "html_url") or f"https://github.com/{author}")
        commits = payload.get("commits")
        commit_rows: list[str] = []
        if isinstance(commits, list):
            for commit in commits:
                if not isinstance(commit, Mapping):
                    continue
                commit_id = str(commit.get("id") or "")[:7] or "???????"
                message = _shorten(str(commit.get("message") or "بدون رسالة"), 300)
                commit_rows.append(f"• {commit_id} — {message}")
        if not commit_rows:
            head = payload.get("head_commit")
            if isinstance(head, Mapping):
                commit_id = str(head.get("id") or "")[:7] or "???????"
                message = _shorten(str(head.get("message") or "بدون رسالة"), 300)
                commit_rows.append(f"• {commit_id} — {message}")
        if not commit_rows:
            return None
        content = _shorten("\n".join(commit_rows))
        url = str(payload.get("compare") or _nested(payload, "head_commit", "url") or _nested(payload, "repository", "html_url"))
        return Notification(
            title="Push جديد",
            repository=repo,
            repository_url=str(_nested(payload, "repository", "html_url") or f"https://github.com/{repo}"),
            actor_label="الناشر",
            author=author,
            author_url=author_url,
            subject_label="الفرع",
            subject=branch,
            content_label="الـ Commits",
            content=content,
            url=url,
            button_text="عرض التغييرات في GitHub",
            group_key=repo,
            event_id=str(payload.get("after") or _nested(payload, "head_commit", "id") or "") or None,
            event_kind="push",
        )

    if event == "pull_request":
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, Mapping):
            return None
        merge_sha = str(pull_request.get("merge_commit_sha") or "")
        short_sha = merge_sha[:7] or "???????"
        title = _shorten(str(pull_request.get("title") or "بدون عنوان"), 300)
        head_ref = str(_nested(pull_request, "head", "ref") or "غير معروف")
        base_ref = str(_nested(pull_request, "base", "ref") or "main")
        merged_by = str(
            _nested(pull_request, "merged_by", "login")
            or _nested(payload, "sender", "login")
            or "unknown"
        )
        merged_by_url = str(
            _nested(pull_request, "merged_by", "html_url")
            or _nested(payload, "sender", "html_url")
            or f"https://github.com/{merged_by}"
        )
        return Notification(
            title="Merge جديد",
            repository=repo,
            repository_url=str(_nested(payload, "repository", "html_url") or f"https://github.com/{repo}"),
            actor_label="المدمج",
            author=merged_by,
            author_url=merged_by_url,
            subject_label="الفروع",
            subject=f"{head_ref} → {base_ref}",
            content_label="الـ Commits",
            content=f"• {short_sha} — {title}",
            url=str(pull_request.get("html_url") or _nested(payload, "repository", "html_url")),
            button_text="فتح الدمج في GitHub",
            group_key=repo,
            event_id=merge_sha or None,
            event_kind="merge",
        )

    comment = payload.get("comment")
    if not isinstance(comment, Mapping):
        return None

    author = str(_nested(comment, "user", "login", default="unknown"))
    author_url = str(_nested(comment, "user", "html_url") or f"https://github.com/{author}")
    body = _shorten(str(comment.get("body") or ""))
    url = str(comment.get("html_url") or _nested(payload, "repository", "html_url"))

    if event == "issue_comment":
        item = payload.get("issue") if isinstance(payload.get("issue"), Mapping) else {}
        is_pr = isinstance(item, Mapping) and bool(item.get("pull_request"))
        kind = "طلب سحب" if is_pr else "مشكلة"
        number = item.get("number", "?") if isinstance(item, Mapping) else "?"
        title = str(item.get("title") or "بدون عنوان") if isinstance(item, Mapping) else "بدون عنوان"
        subject = f"{kind} #{number}: {title}"
    elif event == "pull_request_review_comment":
        item = payload.get("pull_request") if isinstance(payload.get("pull_request"), Mapping) else {}
        number = item.get("number", "?") if isinstance(item, Mapping) else "?"
        title = str(item.get("title") or "بدون عنوان") if isinstance(item, Mapping) else "بدون عنوان"
        subject = f"مراجعة طلب سحب #{number}: {title}"
    elif event == "discussion_comment":
        item = payload.get("discussion") if isinstance(payload.get("discussion"), Mapping) else {}
        number = item.get("number", "?") if isinstance(item, Mapping) else "?"
        title = str(item.get("title") or "بدون عنوان") if isinstance(item, Mapping) else "بدون عنوان"
        subject = f"نقاش #{number}: {title}"
    else:
        commit_id = str(comment.get("commit_id") or "")[:7]
        subject = f"تعليق على Commit {commit_id or '?'}"

    return Notification(
        title="تعليق جديد",
        repository=repo,
        repository_url=str(_nested(payload, "repository", "html_url") or f"https://github.com/{repo}"),
        actor_label="الكاتب",
        author=author,
        author_url=author_url,
        subject_label="المكان",
        subject=subject,
        content_label="التعليق",
        content=body,
        url=url,
        button_text="فتح التعليق في GitHub",
    )


def build_rich_message(notification: Notification) -> dict[str, Any]:
    def cell(
        text: Any,
        *,
        header: bool = False,
        colspan: int | None = None,
        align: str = "right",
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "text": text,
            "align": align,
            "valign": "middle" if header else "top",
        }
        if header:
            result["is_header"] = True
        if colspan is not None:
            result["colspan"] = colspan
        return result

    rows = [
        [cell(notification.title, header=True, colspan=2, align="center")],
        [
            cell("المستودع", header=True),
            cell(
                {
                    "type": "button",
                    "button": {
                        "text": notification.repository,
                        "style": "success",
                        "url": notification.repository_url,
                    },
                }
            ),
        ],
        [
            cell(notification.actor_label, header=True),
            cell(
                {
                    "type": "button",
                    "button": {
                        "text": notification.author,
                        "style": "primary",
                        "url": notification.author_url,
                    },
                }
            ),
        ],
        [cell(notification.subject_label, header=True), cell(notification.subject)],
        [cell(notification.content_label, header=True), cell(notification.content)],
    ]
    blocks = [
        {
            "type": "table",
            "cells": rows,
            "is_bordered": True,
            "is_striped": True,
            "is_compact": True,
        },
        {
            "type": "footer",
            "text": {
                "type": "button",
                "button": {
                    "text": notification.button_text,
                    "style": "primary",
                    "url": notification.url,
                },
            },
        },
    ]
    return {
        "blocks": blocks,
        "is_rtl": True,
        "skip_entity_detection": True,
    }


def build_telegram_payload(settings: Settings, notification: Notification) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "rich_message": build_rich_message(notification),
    }
    if settings.telegram_message_thread_id is not None:
        payload["message_thread_id"] = settings.telegram_message_thread_id
    return payload


def _telegram_request(settings: Settings, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    request = Request(
        endpoint,
        data=json.dumps(dict(payload)).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result.get('description', 'unknown error')}")
    response = result.get("result")
    return response if isinstance(response, Mapping) else {}


def send_telegram(settings: Settings, notification: Notification) -> int:
    result = _telegram_request(settings, "sendRichMessage", build_telegram_payload(settings, notification))
    message_id = result.get("message_id")
    if not isinstance(message_id, int):
        raise RuntimeError("Telegram response did not include a message_id")
    return message_id


def edit_telegram(settings: Settings, notification: Notification, message_id: int) -> None:
    payload = {
        "chat_id": settings.telegram_chat_id,
        "message_id": message_id,
        "rich_message": build_rich_message(notification),
    }
    _telegram_request(settings, "editMessageText", payload)


class NotificationPublisher:
    """Combines nearby repository updates into one Telegram message."""

    def __init__(
        self,
        settings: Settings,
        window_seconds: int = PUSH_EDIT_WINDOW_SECONDS,
        sender: Callable[[Settings, Notification], int] = send_telegram,
        editor: Callable[[Settings, Notification, int], None] = edit_telegram,
    ) -> None:
        self.settings = settings
        self.window_seconds = window_seconds
        self.sender = sender
        self.editor = editor
        self._published: dict[str, PublishedNotification] = {}
        self._lock = threading.Lock()

    def publish(self, notification: Notification) -> None:
        if not notification.group_key:
            self.sender(self.settings, notification)
            return

        now = time.monotonic()
        with self._lock:
            expired = [
                key
                for key, published in self._published.items()
                if now - published.updated_at > self.window_seconds
            ]
            for key in expired:
                self._published.pop(key, None)

            previous = self._published.get(notification.group_key)
            if previous:
                same_event = bool(notification.event_id and notification.event_id == previous.event_id)
                if same_event and previous.event_kind == "merge" and notification.event_kind == "push":
                    return
                if same_event and previous.event_kind == notification.event_kind:
                    return
                self.editor(self.settings, notification, previous.message_id)
                previous.event_id = notification.event_id
                previous.event_kind = notification.event_kind
                previous.updated_at = now
                return

            message_id = self.sender(self.settings, notification)
            self._published[notification.group_key] = PublishedNotification(
                message_id=message_id,
                event_id=notification.event_id,
                event_kind=notification.event_kind,
                updated_at=now,
            )


class GitSpyHandler(BaseHTTPRequestHandler):
    settings: Settings
    deliveries: DeliveryCache
    publisher: NotificationPublisher

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.client_address[0], fmt % args)

    def _json_response(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/healthz"}:
            self._json_response(HTTPStatus.OK, {"ok": True, "service": "GitSpy"})
            return
        self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/github/webhook":
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_WEBHOOK_BYTES:
            self._json_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "invalid_size"})
            return

        body = self.rfile.read(length)
        if not verify_signature(
            self.settings.github_webhook_secret,
            body,
            self.headers.get("X-Hub-Signature-256"),
        ):
            self._json_response(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid_signature"})
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self._json_response(HTTPStatus.OK, {"ok": True, "event": "ping"})
            return

        delivery_id = self.headers.get("X-GitHub-Delivery", "").strip()
        if not delivery_id:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_delivery_id"})
            return
        if not self.deliveries.claim(delivery_id):
            self._json_response(HTTPStatus.ACCEPTED, {"ok": True, "duplicate": True})
            return

        try:
            payload = json.loads(body)
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be an object")
            notification = build_notification(event, payload, self.settings.github_owner)
            if notification is None:
                self._json_response(HTTPStatus.ACCEPTED, {"ok": True, "ignored": True})
                return
            self.publisher.publish(notification)
        except (json.JSONDecodeError, ValueError) as exc:
            self.deliveries.release(delivery_id)
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except Exception:
            self.deliveries.release(delivery_id)
            logger.exception("Failed to process GitHub delivery %s", delivery_id)
            self._json_response(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "delivery_failed"})
            return

        logger.info("Forwarded %s delivery %s to Telegram", event, delivery_id)
        self._json_response(HTTPStatus.OK, {"ok": True, "forwarded": True})


def run() -> None:
    settings = Settings.from_env()
    GitSpyHandler.settings = settings
    GitSpyHandler.deliveries = DeliveryCache()
    GitSpyHandler.publisher = NotificationPublisher(settings)
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), GitSpyHandler)
    logger.info("GitSpy listening on 0.0.0.0:%s", settings.port)
    server.serve_forever()


if __name__ == "__main__":
    run()
