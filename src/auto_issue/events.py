from __future__ import annotations

from dataclasses import dataclass

OPENED = "opened"


@dataclass(frozen=True)
class IssueEvent:
    number: int
    title: str
    body: str
    author: str


@dataclass(frozen=True)
class PullRequestEvent:
    number: int
    title: str
    body: str
    author: str


def parse_issue_event(payload):
    if payload.get("action") != OPENED or not payload.get("issue"):
        return None
    return _from_payload(payload["issue"], IssueEvent)


def parse_pr_event(payload):
    if payload.get("action") != OPENED or not payload.get("pull_request"):
        return None
    return _from_payload(payload["pull_request"], PullRequestEvent)


def _from_payload(raw, cls):
    return cls(
        number=raw.get("number"),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        author=str((raw.get("user") or {}).get("login") or "").lower(),
    )
