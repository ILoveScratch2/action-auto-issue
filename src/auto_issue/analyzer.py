from __future__ import annotations

from . import log
from .results import (
    CommitVerdict,
    CoverageVerdict,
    Outcome,
    PrDecision,
    PrQualityVerdict,
    QualityVerdict,
    SpamVerdict,
    parse_duplicate,
    parse_file_selection,
    parse_verdict,
)

SMART_ANSWER_PREFIX = "HELPFUL_ANSWER:"
NEED_MORE_INFO = "NEED_MORE_INFO"
MIN_ANSWER_LENGTH = 10


def _context(history="", project_files=""):
    """optional context"""
    material = {}
    if history:
        material["history"] = history
    if project_files:
        material["projectFiles"] = project_files
    return material


class Analyzer:
    def __init__(self, ai, config):
        self._ai = ai
        self._config = config

    def detect_spam(self, *, title, body, template_report):
        return self._verdict(
            instructions=self._config.prompts.spam_detection,
            payload={"type": "issue", "title": title, "body": body, "templateAnalysis": template_report},
            purpose="Spam check",
            enum_cls=SpamVerdict,
            fallback=SpamVerdict.NOT_SPAM,
        )

    def select_files(self, *, title, body, file_tree):
        raw = self._ai.complete(
            instructions=self._config.prompt(
                "select_files", max_files=self._config.code_access.max_files_to_read
            ),
            payload={"issue": {"title": title, "body": body}, "fileList": file_tree},
            purpose="File selection",
            verdict=False,
        )
        return parse_file_selection(raw, file_tree.splitlines(), self._config.code_access.max_files_to_read)

    def advise_fix(self, *, title, body, project_files):
        raw = self._ai.complete(
            instructions=self._config.prompt(
                "issue_fix_suggestion", answer_language=self._config.answer_language
            ),
            payload={"issue": {"title": title, "body": body}, "projectFiles": project_files},
            purpose="Fix suggestion",
            verdict=False,
            max_tokens=self._config.code_access.advise_max_tokens,
        )
        text = raw.strip()
        return None if text.upper().startswith("NO_SUGGESTION") else text

    def check_readme_coverage(self, *, title, body, readme, pinned, history="", project_files=""):
        if not readme and not pinned and not history:
            return CoverageVerdict.NOT_COVERED
        payload = {
            "readme": readme or "",
            "pinnedIssues": pinned or "",
            "issue": {"title": title, "body": body},
            **_context(history, project_files),
        }
        return self._verdict(
            instructions=self._config.prompts.readme_coverage_check,
            payload=payload,
            purpose="README coverage check",
            enum_cls=CoverageVerdict,
            fallback=CoverageVerdict.NOT_COVERED,
        )

    def check_duplicate(self, *, title, body, history, candidates):
        raw = self._ai.complete(
            instructions=self._config.prompt("duplicate_check"),
            payload={"issue": {"title": title, "body": body}, "pastItems": history},
            purpose="Duplicate check",
            verdict=False,
        )
        return parse_duplicate(raw, candidates)

    def check_content_quality(self, *, title, body, template_report, project_files=""):
        return self._verdict(
            instructions=self._config.prompts.content_quality_check,
            payload={
                "title": title,
                "body": body,
                "templateAnalysis": template_report,
                **_context(project_files=project_files),
            },
            purpose="Content quality check",
            enum_cls=QualityVerdict,
            fallback=QualityVerdict.VALID,
        )

    def check_pr_spam(self, *, title, body, file_changes):
        return self._verdict(
            instructions=self._config.prompts.pr_spam_detection,
            payload={"title": title, "body": body, "fileChanges": file_changes},
            purpose="PR spam check",
            enum_cls=SpamVerdict,
            fallback=SpamVerdict.NOT_SPAM,
        )

    def check_pr_commit(self, *, title):
        return self._verdict(
            instructions=self._config.prompts.pr_commit_check,
            payload={"title": title},
            purpose="PR commit title check",
            enum_cls=CommitVerdict,
            fallback=CommitVerdict.VALID,
        )

    def check_pr_quality(self, *, title, body, file_changes):
        return self._verdict(
            instructions=self._config.prompts.pr_quality_check,
            payload={"title": title, "body": body, "fileChanges": file_changes},
            purpose="PR quality check",
            enum_cls=PrQualityVerdict,
            fallback=PrQualityVerdict.VALID,
        )

    def generate_readme_answer(self, *, title, body, readme, pinned, history="", project_files=""):
        payload = {
            "readme": readme or "",
            "pinnedIssues": pinned or "",
            "issue": {"title": title, "body": body},
            **_context(history, project_files),
        }
        return self._ai.complete(
            instructions=self._config.prompt(
                "readme_answer",
                readme_answer_length=self._config.locale.readme_answer_length,
                answer_language=self._config.answer_language,
            ),
            payload=payload,
            purpose="README answer",
            verdict=False,
        )

    def generate_smart_answer(self, *, title, body, readme, history="", project_files=""):
        raw = self._ai.complete(
            instructions=self._config.prompt(
                "unclear_issue_smart_answer",
                smart_answer_length=self._config.locale.smart_answer_length,
                answer_language=self._config.answer_language,
            ),
            payload={
                "readme": readme,
                "issue": {"title": title, "body": body},
                **_context(history, project_files),
            },
            purpose="Unclear issue answer",
            verdict=False,
        )
        text = raw.strip()
        upper = text.upper()
        if upper.startswith(SMART_ANSWER_PREFIX):
            return text[len(SMART_ANSWER_PREFIX) :].strip() or None
        if upper == NEED_MORE_INFO:
            return None
        return text if len(text) > MIN_ANSWER_LENGTH else None

    def review_pr(self, *, title, body, diff, commits, file_tree="", project_files="", max_files):
        return self._ai.complete(
            instructions=self._config.prompt(
                "pr_review", max_files=max_files, answer_language=self._config.answer_language
            ),
            payload={
                "pullRequest": {"title": title, "body": body},
                "fileChanges": diff,
                "commits": commits,
                "fileList": file_tree,
                **_context(project_files=project_files),
            },
            purpose="PR code review",
            verdict=False,
            max_tokens=self._config.review.max_tokens,
        )

    def analyze_pr(self, *, title, body, file_changes):
        log.info(self._config.logging.spam_check_start)
        spam = self.check_pr_spam(title=title, body=body, file_changes=file_changes)
        log.info(self._config.log_line("spam_check_result", result=spam.value))
        if spam is SpamVerdict.SPAM:
            return Outcome(PrDecision.SPAM, 1)

        log.info(self._config.logging.pr_commit_check_start)
        commit = self.check_pr_commit(title=title)
        log.info(self._config.log_line("pr_commit_check_result", result=commit.value))
        if commit is CommitVerdict.INVALID:
            return Outcome(PrDecision.INVALID_COMMIT, 2)

        log.info(self._config.logging.pr_quality_check_start)
        quality = self.check_pr_quality(title=title, body=body, file_changes=file_changes)
        log.info(self._config.log_line("pr_quality_check_result", result=quality.value))
        if quality is PrQualityVerdict.UNCLEAR:
            return Outcome(PrDecision.UNCLEAR, 3)
        if quality is PrQualityVerdict.MALICIOUS:
            return Outcome(PrDecision.MALICIOUS, 3)
        if quality is PrQualityVerdict.TRIVIAL:
            return Outcome(PrDecision.TRIVIAL, 3)
        return Outcome(PrDecision.KEEP, 3)

    def _verdict(self, *, instructions, payload, purpose, enum_cls, fallback):
        raw = self._ai.complete(instructions=instructions, payload=payload, purpose=purpose)
        verdict = parse_verdict(raw, enum_cls)
        if verdict is None:
            log.warning(
                self._config.log_line(
                    "ai_unexpected_verdict", purpose=purpose, verdict=raw, fallback=fallback.value
                )
            )
            return fallback
        return verdict
