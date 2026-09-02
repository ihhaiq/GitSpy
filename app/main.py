from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
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
}


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
    repository: str
    author: str
    subject: str
    comment: str
    url: str


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
    if event not in SUPPORTED_EVENTS or payload.get("action") != "created":
        return None

    owner = str(_nested(payload, "repository", "owner", "login"))
    if owner.casefold() != expected_owner.casefold():
        logger.warning("Ignored event from unexpected owner: %s", owner or "<missing>")
        return None

    repo = str(_nested(payload, "repository", "full_name"))
    comment = payload.get("comment")
    if not repo or not isinstance(comment, Mapping):
        return None

    author = str(_nested(comment, "user", "login", default="unknown"))
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
        repository=repo,
        author=author,
        subject=subject,
        comment=body,
        url=url,
    )


def build_telegram_payload(settings: Settings, notification: Notification) -> dict[str, Any]:
    def cell(
        text: str,
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
        [cell("تعليق جديد", header=True, colspan=2, align="center")],
        [cell("المستودع", header=True), cell(notification.repository)],
        [cell("الكاتب", header=True), cell(notification.author)],
        [cell("المكان", header=True), cell(notification.subject)],
        [cell("التعليق", header=True), cell(notification.comment)],
    ]
    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "rich_message": {
            "blocks": [
                {
                    "type": "table",
                    "cells": rows,
                    "is_bordered": True,
                    "is_striped": True,
                    "is_compact": True,
                }
            ],
            "is_rtl": True,
            "skip_entity_detection": True,
        },
        "reply_markup": {
            "inline_keyboard": [[{"text": "فتح التعليق في GitHub", "url": notification.url}]]
        },
    }
    if settings.telegram_message_thread_id is not None:
        payload["message_thread_id"] = settings.telegram_message_thread_id
    return payload


def send_telegram(settings: Settings, notification: Notification) -> None:
    endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendRichMessage"
    payload = build_telegram_payload(settings, notification)

    request = Request(
        endpoint,
        data=json.dumps(payload).encode(),
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


class GitSpyHandler(BaseHTTPRequestHandler):
    settings: Settings
    deliveries: DeliveryCache

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
            send_telegram(self.settings, notification)
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
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), GitSpyHandler)
    logger.info("GitSpy listening on 0.0.0.0:%s", settings.port)
    server.serve_forever()


if __name__ == "__main__":
    run()
