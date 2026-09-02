from __future__ import annotations

import os
from dataclasses import dataclass


MAX_CONTENT_CHARS = 2600
MAX_AGGREGATED_COMMITS = 20


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
class CommitEntry:
    sha: str
    message: str

    @property
    def text(self) -> str:
        return f"• {self.sha[:7] or '???????'} — {self.message}"


def render_commits(commits: tuple[CommitEntry, ...]) -> str:
    visible = list(commits[-MAX_AGGREGATED_COMMITS:])
    hidden = len(commits) - len(visible)

    while visible:
        prefix = f"• … و{hidden} Commits أقدم\n" if hidden else ""
        content = prefix + "\n".join(commit.text for commit in visible)
        if len(content) <= MAX_CONTENT_CHARS:
            return content
        visible.pop(0)
        hidden += 1
    return "(ماكو Commits قابلة للعرض)"


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
    commits: tuple[CommitEntry, ...] = ()
    subjects: tuple[str, ...] = ()


@dataclass
class PublishedNotification:
    message_id: int
    notification: Notification
    updated_at: float
