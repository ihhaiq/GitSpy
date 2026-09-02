from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from typing import Any

from app.models import CommitEntry, MAX_CONTENT_CHARS, Notification, render_commits


logger = logging.getLogger("gitspy")

SUPPORTED_EVENTS = {
    "issue_comment",
    "pull_request_review_comment",
    "discussion_comment",
    "commit_comment",
    "push",
    "pull_request",
    "fork",
}


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


def _shorten(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    normalized = " ".join(text.replace("\x00", "").split())
    if not normalized:
        return "(تعليق بدون نص)"
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def _repository(payload: Mapping[str, Any], expected_owner: str) -> tuple[str, str] | None:
    owner = str(_nested(payload, "repository", "owner", "login"))
    if owner.casefold() != expected_owner.casefold():
        logger.warning("Ignored event from unexpected owner: %s", owner or "<missing>")
        return None

    name = str(_nested(payload, "repository", "full_name"))
    if not name:
        return None
    url = str(_nested(payload, "repository", "html_url") or f"https://github.com/{name}")
    return name, url


def _build_push(payload: Mapping[str, Any], repo: str, repo_url: str) -> Notification | None:
    if payload.get("deleted"):
        return None

    ref = str(payload.get("ref") or "")
    branch = ref.removeprefix("refs/heads/").removeprefix("refs/tags/") or "غير معروف"
    author = str(_nested(payload, "sender", "login") or _nested(payload, "pusher", "name") or "unknown")
    author_url = str(_nested(payload, "sender", "html_url") or f"https://github.com/{author}")
    entries: list[CommitEntry] = []

    commits = payload.get("commits")
    if isinstance(commits, list):
        for commit in commits:
            if not isinstance(commit, Mapping):
                continue
            entries.append(
                CommitEntry(
                    sha=str(commit.get("id") or ""),
                    message=_shorten(str(commit.get("message") or "بدون رسالة"), 300),
                )
            )

    if not entries:
        head = payload.get("head_commit")
        if isinstance(head, Mapping):
            entries.append(
                CommitEntry(
                    sha=str(head.get("id") or ""),
                    message=_shorten(str(head.get("message") or "بدون رسالة"), 300),
                )
            )
    if not entries:
        return None

    commit_entries = tuple(entries)
    return Notification(
        title="Push جديد",
        repository=repo,
        repository_url=repo_url,
        actor_label="الناشر",
        author=author,
        author_url=author_url,
        subject_label="الفرع",
        subject=branch,
        content_label="الـ Commits",
        content=render_commits(commit_entries),
        url=str(payload.get("compare") or _nested(payload, "head_commit", "url") or repo_url),
        button_text="عرض التغييرات في GitHub",
        group_key=repo,
        event_id=str(payload.get("after") or _nested(payload, "head_commit", "id") or "") or None,
        event_kind="push",
        commits=commit_entries,
        subjects=(branch,),
    )


def _build_merge(payload: Mapping[str, Any], repo: str, repo_url: str) -> Notification | None:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, Mapping):
        return None

    merge_sha = str(pull_request.get("merge_commit_sha") or "")
    title = _shorten(str(pull_request.get("title") or "بدون عنوان"), 300)
    branch = f"{_nested(pull_request, 'head', 'ref') or 'غير معروف'} → {_nested(pull_request, 'base', 'ref') or 'main'}"
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
    commits = (CommitEntry(merge_sha, title),)
    return Notification(
        title="Merge جديد",
        repository=repo,
        repository_url=repo_url,
        actor_label="المدمج",
        author=merged_by,
        author_url=merged_by_url,
        subject_label="الفروع",
        subject=branch,
        content_label="الـ Commits",
        content=render_commits(commits),
        url=str(pull_request.get("html_url") or repo_url),
        button_text="فتح الدمج في GitHub",
        group_key=repo,
        event_id=merge_sha or None,
        event_kind="merge",
        commits=commits,
        subjects=(branch,),
    )


def _build_fork(payload: Mapping[str, Any], repo: str, repo_url: str) -> Notification | None:
    forkee = payload.get("forkee")
    if not isinstance(forkee, Mapping):
        return None

    author = str(
        _nested(payload, "sender", "login")
        or _nested(forkee, "owner", "login")
        or "unknown"
    )
    author_url = str(
        _nested(payload, "sender", "html_url")
        or _nested(forkee, "owner", "html_url")
        or f"https://github.com/{author}"
    )
    fork_name = str(forkee.get("full_name") or forkee.get("name") or "غير معروف")
    default_branch = str(forkee.get("default_branch") or "غير معروف")
    visibility = "خاص" if forkee.get("private") else "عام"

    return Notification(
        title="Fork جديد",
        repository=repo,
        repository_url=repo_url,
        actor_label="صاحب الـFork",
        author=author,
        author_url=author_url,
        subject_label="المستودع الجديد",
        subject=fork_name,
        content_label="التفاصيل",
        content=f"الفرع الافتراضي: {default_branch}\nالظهور: {visibility}",
        url=str(forkee.get("html_url") or repo_url),
        button_text="فتح الـFork في GitHub",
        event_kind="fork",
    )


def _build_comment(event: str, payload: Mapping[str, Any], repo: str, repo_url: str) -> Notification | None:
    comment = payload.get("comment")
    if not isinstance(comment, Mapping):
        return None

    author = str(_nested(comment, "user", "login", default="unknown"))
    body = _shorten(str(comment.get("body") or ""))
    url = str(comment.get("html_url") or repo_url)

    if event == "issue_comment":
        item = payload.get("issue") if isinstance(payload.get("issue"), Mapping) else {}
        is_pr = isinstance(item, Mapping) and bool(item.get("pull_request"))
        kind = "طلب سحب" if is_pr else "مشكلة"
        number = item.get("number", "?") if isinstance(item, Mapping) else "?"
        title = str(item.get("title") or "بدون عنوان") if isinstance(item, Mapping) else "بدون عنوان"
        subject = f"{kind} #{number}: {title}"
    elif event == "pull_request_review_comment":
        item = payload.get("pull_request") if isinstance(payload.get("pull_request"), Mapping) else {}
        subject = f"مراجعة طلب سحب #{item.get('number', '?')}: {item.get('title') or 'بدون عنوان'}"
    elif event == "discussion_comment":
        item = payload.get("discussion") if isinstance(payload.get("discussion"), Mapping) else {}
        subject = f"نقاش #{item.get('number', '?')}: {item.get('title') or 'بدون عنوان'}"
    else:
        commit_id = str(comment.get("commit_id") or "")[:7]
        subject = f"تعليق على Commit {commit_id or '?'}"

    return Notification(
        title="تعليق جديد",
        repository=repo,
        repository_url=repo_url,
        actor_label="الكاتب",
        author=author,
        author_url=str(_nested(comment, "user", "html_url") or f"https://github.com/{author}"),
        subject_label="المكان",
        subject=subject,
        content_label="التعليق",
        content=body,
        url=url,
        button_text="فتح التعليق في GitHub",
    )


def build_notification(event: str, payload: Mapping[str, Any], expected_owner: str) -> Notification | None:
    if event not in SUPPORTED_EVENTS:
        return None
    if event == "pull_request":
        if payload.get("action") != "closed" or not _nested(payload, "pull_request", "merged", default=False):
            return None
    elif event not in {"push", "fork"} and payload.get("action") != "created":
        return None

    repository = _repository(payload, expected_owner)
    if repository is None:
        return None
    repo, repo_url = repository

    if event == "push":
        return _build_push(payload, repo, repo_url)
    if event == "pull_request":
        return _build_merge(payload, repo, repo_url)
    if event == "fork":
        return _build_fork(payload, repo, repo_url)
    return _build_comment(event, payload, repo, repo_url)
