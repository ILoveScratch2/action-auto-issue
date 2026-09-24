"""Verdicts, decisions and result types exchanged between the layers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class SpamVerdict(str, Enum):
    SPAM = "SPAM"
    NOT_SPAM = "NOT_SPAM"


class CoverageVerdict(str, Enum):
    COVERED = "COVERED"
    NOT_COVERED = "NOT_COVERED"


class QualityVerdict(str, Enum):
    UNCLEAR = "UNCLEAR"
    BASIC = "BASIC"
    VALID = "VALID"


class CommitVerdict(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


class PrQualityVerdict(str, Enum):
    UNCLEAR = "UNCLEAR"
    MALICIOUS = "MALICIOUS"
    TRIVIAL = "TRIVIAL"
    VALID = "VALID"


class PrDecision(str, Enum):
    SPAM = "SPAM"
    INVALID_COMMIT = "INVALID_COMMIT"
    MALICIOUS = "MALICIOUS"
    TRIVIAL = "TRIVIAL"
    UNCLEAR = "UNCLEAR"
    KEEP = "KEEP"


class IssueStatus(str, Enum):
    KEPT = "KEPT"
    NEEDS_INFO = "NEEDS_INFO"
    CLOSED = "CLOSED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"


class ReviewVerdict(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    CLOSE = "CLOSE"


def parse_verdict(raw, enum_cls):
    """Maps a model answer onto an enum member, or None when it is not recognized."""
    if raw is None:
        return None
    try:
        return enum_cls(str(raw).strip().upper())
    except ValueError:
        return None


DUPLICATE_PATTERN = re.compile(r"\bDUPLICATE\s*[:#]?\s*(\d+)\b")


def parse_duplicate(raw, candidates):
    """Maps a model answer onto one of the searched candidates, or None when it is not usable.

    The answer is validated against the candidates so a number the model invented, or the number of
    the item being checked, can never end up posted as a duplicate link.
    """
    match = DUPLICATE_PATTERN.search(str(raw or "").upper())
    if match is None:
        return None
    number = int(match.group(1))
    return number if number in candidates else None


def parse_file_selection(raw, available, limit):
    """Maps a model answer onto real repository paths, in the order the model chose them.

    The answer is matched against the fetched tree, so a path the model invented, or a plain
    sentence, can never turn into a file read.
    """
    chosen = []
    for line in str(raw or "").splitlines():
        for path in available:
            if path in chosen:
                continue
            if re.search(rf"(?<![\w/]){re.escape(path)}(?![\w/])", line):
                chosen.append(path)
                break
        if len(chosen) >= limit:
            break
    return tuple(chosen)


REVIEW_VERDICT_PREFIX = "VERDICT:"
REVIEW_FILES_PREFIX = "REQUEST_FILES:"


def parse_review_verdict(marker):
    """Maps the text after ``VERDICT:`` onto a member, tolerating spacing and trailing notes."""
    candidate = str(marker).strip().upper()
    normalized = re.sub(r"[\s-]+", "_", candidate)
    for member in ReviewVerdict:
        if normalized == member.value or normalized.startswith(member.value + "_"):
            return member
    return None


def parse_review_report(raw):
    """Splits the verdict marker off the report, keeping the whole answer when there is none."""
    text = str(raw or "").strip()
    lines = text.splitlines()
    if not lines or not lines[0].strip().upper().startswith(REVIEW_VERDICT_PREFIX):
        return None, text
    body = "\n".join(lines[1:]).strip()
    if not body:
        return None, text
    marker = lines[0].strip().upper()[len(REVIEW_VERDICT_PREFIX) :]
    return parse_review_verdict(marker), body


def parse_review_request(raw, available, limit):
    """Maps an answer onto the paths the reviewer asked to read, or None when it is a report.

    The paths are matched against the fetched tree, so a path the model invented can never turn
    into a file read. An empty tuple means the answer asked for files that do not exist.
    """
    lines = [line.strip() for line in str(raw or "").splitlines()]
    if not lines or not lines[0].upper().startswith(REVIEW_FILES_PREFIX):
        return None
    return parse_file_selection("\n".join(lines[1:]), available, limit)


@dataclass(frozen=True)
class Outcome:
    decision: Enum
    step: int


@dataclass(frozen=True)
class IssueResult:
    status: IssueStatus
    classification: str | None = None
    label: str | None = None

    @property
    def closed(self):
        return self.status is IssueStatus.CLOSED

    @property
    def needs_info(self):
        return self.status is IssueStatus.NEEDS_INFO


@dataclass
class FailureCollector:
    """Collects GitHub write failures so the step can end red instead of silently green."""

    items: list = field(default_factory=list)

    def record(self, operation, error):
        self.items.append((operation, str(error)))

    def __bool__(self):
        return bool(self.items)

    def __len__(self):
        return len(self.items)
