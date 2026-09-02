from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.models import Notification, Settings


def _cell(
    text: Any,
    *,
    header: bool = False,
    colspan: int | None = None,
    align: str = "right",
) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "text": text,
        "align": align,
        "valign": "middle" if header else "top",
    }
    if header:
        cell["is_header"] = True
    if colspan is not None:
        cell["colspan"] = colspan
    return cell


def _button(text: str, url: str, style: str) -> dict[str, Any]:
    return {
        "type": "button",
        "button": {
            "text": text,
            "style": style,
            "url": url,
        },
    }


def build_rich_message(notification: Notification) -> dict[str, Any]:
    rows = [
        [_cell(notification.title, header=True, colspan=2, align="center")],
        [
            _cell("المستودع", header=True),
            _cell(_button(notification.repository, notification.repository_url, "success")),
        ],
        [
            _cell(notification.actor_label, header=True),
            _cell(_button(notification.author, notification.author_url, "primary")),
        ],
        [_cell(notification.subject_label, header=True), _cell(notification.subject)],
        [_cell(notification.content_label, header=True), _cell(notification.content)],
    ]
    return {
        "blocks": [
            {
                "type": "table",
                "cells": rows,
                "is_bordered": True,
                "is_striped": True,
                "is_compact": True,
            },
            {
                "type": "footer",
                "text": _button(notification.button_text, notification.url, "primary"),
            },
        ],
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


def _request(settings: Settings, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    request = Request(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
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
    result = _request(settings, "sendRichMessage", build_telegram_payload(settings, notification))
    message_id = result.get("message_id")
    if not isinstance(message_id, int):
        raise RuntimeError("Telegram response did not include a message_id")
    return message_id


def edit_telegram(settings: Settings, notification: Notification, message_id: int) -> None:
    _request(
        settings,
        "editMessageText",
        {
            "chat_id": settings.telegram_chat_id,
            "message_id": message_id,
            "rich_message": build_rich_message(notification),
        },
    )
