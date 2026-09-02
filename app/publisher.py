from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from app.models import CommitEntry, Notification, PublishedNotification, Settings, render_commits
from app.telegram import edit_telegram, send_telegram


PUSH_EDIT_WINDOW_SECONDS = 90


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _merge_commits(
    current: tuple[CommitEntry, ...],
    incoming: tuple[CommitEntry, ...],
) -> tuple[CommitEntry, ...]:
    merged = {commit.sha: commit for commit in current}
    for commit in incoming:
        merged[commit.sha] = commit
    return tuple(merged.values())


def _aggregate(current: Notification, incoming: Notification) -> Notification:
    same_event = bool(incoming.event_id and incoming.event_id == current.event_id)
    keep_merge = same_event and current.event_kind == "merge" and incoming.event_kind == "push"
    visible = current if keep_merge else incoming
    commits = _merge_commits(current.commits, incoming.commits)

    if same_event:
        subjects = current.subjects if keep_merge else incoming.subjects
    else:
        subjects = _unique(current.subjects + incoming.subjects)

    subject = "، ".join(subjects) if subjects else visible.subject
    return replace(
        visible,
        subject_label="الفروع" if len(subjects) > 1 else visible.subject_label,
        subject=subject,
        content=render_commits(commits),
        commits=commits,
        subjects=subjects,
    )


class NotificationPublisher:
    """يجمع تحديثات المستودع المتقاربة داخل رسالة واحدة."""

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
            self._remove_expired(now)
            previous = self._published.get(notification.group_key)
            if previous is None:
                self._send_new(notification, now)
                return

            combined = _aggregate(previous.notification, notification)
            if combined == previous.notification:
                return

            self.editor(self.settings, combined, previous.message_id)
            previous.notification = combined
            previous.updated_at = now

    def _remove_expired(self, now: float) -> None:
        expired = [
            key
            for key, published in self._published.items()
            if now - published.updated_at > self.window_seconds
        ]
        for key in expired:
            self._published.pop(key, None)

    def _send_new(self, notification: Notification, now: float) -> None:
        message_id = self.sender(self.settings, notification)
        assert notification.group_key is not None
        self._published[notification.group_key] = PublishedNotification(
            message_id=message_id,
            notification=notification,
            updated_at=now,
        )
