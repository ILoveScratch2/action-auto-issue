"""
Actions for Auto Issuer
"""

from __future__ import annotations

from . import log
from .errors import ApiError


class _Actions:
    def __init__(self, client, config, inputs, failures):
        self._client = client
        self._config = config
        self._inputs = inputs
        self._failures = failures

    def comment(self, number, body, log_key=None):
        try:
            self._client.add_comment(number, body)
        except ApiError as exc:
            self._fail(f"comment on #{number}", exc)
            return False
        if log_key:
            log.info(self._config.log_line(log_key, number=number))
        return True

    def add_labels(self, number, labels):
        try:
            self._client.add_labels(number, list(labels))
        except ApiError as exc:
            log.warning(self._config.log_line("label_add_failed", error=exc))
            return False
        log.info(self._config.log_line("label_added", number=number, label=", ".join(labels)))
        return True

    def apply_outcome(self, number, body, log_key, policy, state_reason="not_planned"):
        posted = self.comment(number, body) if policy["comment"] else True
        closed = self.close(number, state_reason) if policy["close"] else True
        locked = self.lock(number) if policy["lock"] else True
        if policy["labels"] and self._inputs.apply_labels:
            self.add_labels(number, policy["labels"])
        done = posted and closed and locked
        if done and log_key:
            log.info(self._config.log_line(log_key, number=number))
        return done

    def _fail(self, operation, exc):
        self._failures.record(operation, exc)
        log.error(self._config.log_line("action_failed", operation=operation, error=exc))


class IssueActions(_Actions):
    def close(self, number, state_reason="not_planned"):
        try:
            self._client.close_issue(number, state_reason)
        except ApiError as exc:
            self._fail(f"close issue #{number}", exc)
            return False
        return True

    def lock(self, number):
        try:
            self._client.lock_issue(number)
        except ApiError as exc:
            self._fail(f"lock #{number}", exc)
            return False
        return True

    def prefix_title(self, number, title, prefix):
        if not prefix or title.startswith(prefix):
            return False
        try:
            self._client.edit_issue_title(number, f"{prefix} {title}")
        except ApiError as exc:
            log.warning(self._config.log_line("title_prefix_failed", number=number, error=exc))
            return False
        log.info(self._config.log_line("title_prefixed", number=number, prefix=prefix))
        return True


class PrActions(_Actions):
    def review(self, number, body, log_key=None):
        try:
            self._client.create_review(number, body)
        except ApiError as exc:
            log.warning(self._config.log_line("pr_review_failed", number=number, error=exc))
            return False
        if log_key:
            log.info(self._config.log_line(log_key, number=number))
        return True

    def close(self, number, state_reason=None):
        try:
            self._client.close_pr(number)
        except ApiError as exc:
            self._fail(f"close PR #{number}", exc)
            return False
        return True

    def lock(self, number):
        try:
            self._client.lock_issue(number)
        except ApiError as exc:
            self._fail(f"lock PR #{number}", exc)
            return False
        return True
