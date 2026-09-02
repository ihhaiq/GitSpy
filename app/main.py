from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.github_events import build_notification, verify_signature
from app.models import Notification, Settings
from app.publisher import NotificationPublisher
from app.telegram import build_telegram_payload


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gitspy")

MAX_WEBHOOK_BYTES = 2 * 1024 * 1024


class DeliveryCache:
    """يمنع معالجة GitHub Delivery نفسه أكثر من مرة."""

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
