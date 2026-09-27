
from __future__ import annotations


def match_label(classification, labels):
    """
    issue / PR matching
    """
    if not classification:
        return None
    normalized = str(classification).strip().lower()
    if not normalized:
        return None

    for label in labels:
        if label.strip().lower() == normalized:
            return label
    for label in labels:
        if label.strip().lower() in normalized:
            return label
    for label in labels:
        if normalized in label.strip().lower():
            return label
    return None


class Classifier:
    def __init__(self, ai, config):
        self._ai = ai
        self._config = config

    def classify_issue(self, *, title, content, labels, project_files="", extra_prompt=""):
        payload = {"title": title, "content": content}
        if project_files:
            payload["projectFiles"] = project_files
        return self._ai.complete(
            instructions=self._config.prompt(
                "issue_classification",
                labels_options=self._label_options(labels),
                extra_prompt=extra_prompt,
            ),
            payload=payload,
            purpose="Issue classification",
        )

    def classify_pr(self, *, title, body, labels, file_changes):
        return self._ai.complete(
            instructions=self._config.prompt("pr_classification", labels_options=self._label_options(labels)),
            payload={"title": title, "description": body, "fileChanges": file_changes},
            purpose="PR classification",
        )

    @staticmethod
    def _label_options(labels):
        return ", ".join(label.upper() for label in labels)
