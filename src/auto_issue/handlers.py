from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import log
from .actions import IssueActions, PrActions
from .analyzer import Analyzer
from .classifier import Classifier, match_label
from .errors import AutoIssueError, ContentFilterError
from .results import (
    CoverageVerdict,
    FailureCollector,
    IssueResult,
    IssueStatus,
    PrDecision,
    QualityVerdict,
    SpamVerdict,
)
from .github_client import HistoryResult
from .template_detector import analyze_issue_quality

if TYPE_CHECKING:
    from .ai import AiClient
    from .config import Config, Inputs
    from .github_client import GitHubClient

BUG_KEYWORDS = ("bug", "error", "fix")


@dataclass
class RunContext:
    config: "Config"
    inputs: "Inputs"
    client: "GitHubClient"
    ai: "AiClient"
    failures: FailureCollector


def handle_new_issue(ctx, issue):
    log.info(ctx.config.log_line("issue_check_start", title=issue.title))

    if issue.author in ctx.inputs.blocked_users:
        IssueActions(ctx.client, ctx.config, ctx.inputs, ctx.failures).apply_outcome(
            issue.number,
            ctx.config.response("issue_blocked"),
            "issue_blocked_log",
            ctx.config.outcomes.issue_blocked,
        )
        return

    try:
        _triage_issue(ctx, issue)
    except ContentFilterError:
        log.warning(ctx.config.log_line("ai_content_filtered", number=issue.number))
        IssueActions(ctx.client, ctx.config, ctx.inputs, ctx.failures).apply_outcome(
            issue.number,
            ctx.config.response("issue_content_filtered"),
            "issue_content_filtered_log",
            ctx.config.outcomes.issue_content_filtered,
        )


def _search_history(ctx, terms, number):
    if not ctx.inputs.search_history:
        return HistoryResult()
    log.info(ctx.config.logging.history_search_start)
    return ctx.client.search_history(terms, exclude_number=number)


def _duplicate_of(ctx, analyzer, issue, history):
    if not history.numbers:
        return None
    duplicate = analyzer.check_duplicate(
        title=issue.title, body=issue.body, history=history.text, candidates=history.numbers
    )
    if duplicate:
        log.info(
            ctx.config.log_line("duplicate_detected", number=issue.number, duplicate=duplicate)
        )
    return duplicate


def _read_project_files(ctx, analyzer, issue):
    if ctx.inputs.issue_code_access == "off":
        return ""
    tree = ctx.client.get_file_tree()
    if not tree:
        return ""

    log.info(ctx.config.logging.file_selection_start)
    paths = analyzer.select_files(title=issue.title, body=issue.body, file_tree=tree)
    if not paths:
        log.info(ctx.config.logging.files_none_selected)
        return ""

    log.info(ctx.config.log_line("files_selected", count=len(paths), files=", ".join(paths)))
    return ctx.client.get_files_text(paths)


def _triage_issue(ctx, issue):
    actions = IssueActions(ctx.client, ctx.config, ctx.inputs, ctx.failures)
    analyzer = Analyzer(ctx.ai, ctx.config)

    readme = ctx.client.get_readme()
    pinned = ctx.client.get_pinned_issues_text()
    quality = analyze_issue_quality(issue.title, issue.body)
    _log_quality(ctx, quality)

    log.info(ctx.config.logging.spam_check_start)
    spam = analyzer.detect_spam(title=issue.title, body=issue.body, template_report=quality.report())
    log.info(ctx.config.log_line("spam_check_result", result=spam.value))
    if spam is SpamVerdict.SPAM:
        actions.apply_outcome(
            issue.number,
            ctx.config.response("issue_spam"),
            "issue_spam_log",
            ctx.config.outcomes.issue_spam,
        )
        return IssueResult(IssueStatus.CLOSED)

    # search history
    history = _search_history(ctx, issue.title, issue.number)
    project_files = _read_project_files(ctx, analyzer, issue)

    log.info(ctx.config.logging.readme_check_start)
    coverage = analyzer.check_readme_coverage(
        title=issue.title,
        body=issue.body,
        readme=readme,
        pinned=pinned,
        history=history.text,
        project_files=project_files,
    )
    log.info(ctx.config.log_line("readme_check_result", result=coverage.value))
    if coverage is CoverageVerdict.COVERED:
        return _answer_from_docs(
            ctx, actions, analyzer, issue, readme, pinned, history.text, project_files
        )

    duplicate = _duplicate_of(ctx, analyzer, issue, history)
    if duplicate:
        actions.apply_outcome(
            issue.number,
            ctx.config.response("issue_duplicate", number=duplicate),
            "issue_duplicate_log",
            ctx.config.outcomes.issue_duplicate,
        )
        return IssueResult(IssueStatus.CLOSED)

    return _classify_issue(
        ctx, actions, analyzer, issue, quality, readme, history.text, project_files
    )


def _answer_from_docs(ctx, actions, analyzer, issue, readme, pinned, history="", project_files=""):
    try:
        answer = analyzer.generate_readme_answer(
            title=issue.title,
            body=issue.body,
            readme=readme,
            pinned=pinned,
            history=history,
            project_files=project_files,
        )
    except ContentFilterError:
        raise
    except AutoIssueError as exc:
        log.error(ctx.config.log_line("readme_answer_failed", number=issue.number, error=exc))
        return IssueResult(IssueStatus.ANALYSIS_FAILED)

    prefix = "history_answer_prefix" if history else "readme_answer_prefix"
    posted = actions.comment(
        issue.number, ctx.config.response(prefix) + answer, log_key="readme_answer_generated"
    )
    if not posted:
        return IssueResult(IssueStatus.ANALYSIS_FAILED)

    actions.apply_outcome(
        issue.number,
        ctx.config.response("issue_readme_covered"),
        "issue_readme_covered_log",
        ctx.config.outcomes.issue_readme_covered,
        state_reason="completed",
    )
    return IssueResult(IssueStatus.CLOSED)


def _classify_issue(ctx, actions, analyzer, issue, quality, readme, history="", project_files=""):
    log.info(ctx.config.log_line("classification_start", number=issue.number))
    log.info(ctx.config.log_line("available_labels", labels=", ".join(ctx.inputs.labels)))

    content = quality.content.user_content if quality.template.has_template else (issue.body or "")
    try:
        classification = Classifier(ctx.ai, ctx.config).classify_issue(
            title=issue.title, content=content, labels=ctx.inputs.labels, project_files=project_files
        )
        label = _apply_label(ctx, actions, issue.number, classification)

        if not _needs_details(classification, ctx.inputs.labels):
            log.info(ctx.config.log_line("issue_passed_log", number=issue.number))
            _advise(ctx, actions, analyzer, issue, project_files)
            return IssueResult(IssueStatus.KEPT, classification, label)

        log.info(ctx.config.logging.quality_check_start)
        verdict = analyzer.check_content_quality(
            title=issue.title,
            body=issue.body,
            template_report=quality.report(),
            project_files=project_files,
        )
    except ContentFilterError:
        raise
    except AutoIssueError as exc:
        log.error(ctx.config.log_line("classification_failed", number=issue.number, error=exc))
        return IssueResult(IssueStatus.ANALYSIS_FAILED)

    log.info(ctx.config.log_line("quality_check_result", result=verdict.value))

    if verdict is QualityVerdict.BASIC:
        actions.apply_outcome(
            issue.number,
            ctx.config.response("issue_basic"),
            "issue_basic_log",
            ctx.config.outcomes.issue_basic,
        )
        return IssueResult(IssueStatus.CLOSED, classification, label)

    if verdict is QualityVerdict.UNCLEAR:
        _handle_unclear(ctx, actions, analyzer, issue, readme, history, project_files)
        return IssueResult(IssueStatus.NEEDS_INFO, classification, label)

    log.info(ctx.config.log_line("issue_passed_log", number=issue.number))
    _advise(ctx, actions, analyzer, issue, project_files)
    return IssueResult(IssueStatus.KEPT, classification, label)


def _advise(ctx, actions, analyzer, issue, project_files):
    if ctx.inputs.issue_code_access != "advise" or not project_files:
        return
    try:
        advice = analyzer.advise_fix(
            title=issue.title, body=issue.body, project_files=project_files
        )
    except ContentFilterError:
        raise
    except AutoIssueError as exc:
        log.error(ctx.config.log_line("advise_failed", number=issue.number, error=exc))
        return
    if advice:
        actions.comment(
            issue.number, ctx.config.response("advise_prefix") + advice, log_key="advise_generated"
        )


def _apply_label(ctx, actions, number, classification):
    label = match_label(classification, ctx.inputs.labels)
    if not label:
        log.info(
            ctx.config.log_line("label_no_match", number=number, classification=classification)
        )
        return None
    if ctx.inputs.apply_labels:
        actions.add_labels(number, [label])
    return label


def _handle_unclear(ctx, actions, analyzer, issue, readme, history="", project_files=""):
    policy = ctx.config.outcomes.issue_unclear
    if policy["comment"] and (readme or history or project_files):
        try:
            answer = analyzer.generate_smart_answer(
                title=issue.title,
                body=issue.body,
                readme=readme,
                history=history,
                project_files=project_files,
            )
        except ContentFilterError:
            raise
        except AutoIssueError as exc:
            log.error(
                ctx.config.log_line("unclear_smart_answer_failed", number=issue.number, error=exc)
            )
            answer = None

        if answer and actions.comment(
            issue.number,
            ctx.config.response("unclear_answer_prefix") + answer,
            log_key="unclear_smart_answer_generated",
        ):
            return

    if policy["comment"]:
        log.info(ctx.config.log_line("unclear_fallback_to_standard", number=issue.number))
        actions.apply_outcome(
            issue.number, ctx.config.response("issue_unclear"), "issue_unclear_log", policy
        )


def handle_new_pr(ctx, pr):
    log.info(ctx.config.log_line("pr_check_start", title=pr.title))
    actions = PrActions(ctx.client, ctx.config, ctx.inputs, ctx.failures)

    if pr.author in ctx.inputs.blocked_users:
        actions.apply_outcome(
            pr.number, ctx.config.response("pr_blocked"), "pr_blocked_log", ctx.config.outcomes.pr_blocked
        )
        return

    try:
        file_changes = ctx.client.get_pr_file_changes(pr.number)
        analyzer = Analyzer(ctx.ai, ctx.config)
        outcome = analyzer.analyze_pr(title=pr.title, body=pr.body, file_changes=file_changes)

        if outcome.decision is PrDecision.SPAM:
            actions.apply_outcome(
                pr.number, ctx.config.response("pr_spam"), "pr_spam_log", ctx.config.outcomes.pr_spam
            )
            return
        if outcome.decision is PrDecision.INVALID_COMMIT:
            actions.apply_outcome(
                pr.number,
                ctx.config.response("pr_invalid_commit"),
                "pr_commit_rule_log",
                ctx.config.outcomes.pr_invalid_commit,
            )
            return
        if outcome.decision is PrDecision.MALICIOUS:
            actions.apply_outcome(
                pr.number,
                ctx.config.response("pr_malicious"),
                "pr_malicious_log",
                ctx.config.outcomes.pr_malicious,
            )
            return
        if outcome.decision is PrDecision.TRIVIAL:
            actions.apply_outcome(
                pr.number,
                ctx.config.response("pr_trivial"),
                "pr_trivial_log",
                ctx.config.outcomes.pr_trivial,
            )
            return

        if outcome.decision is PrDecision.UNCLEAR:
            log.info(ctx.config.log_line("pr_unclear_log", number=pr.number))
        else:
            log.info(ctx.config.log_line("pr_passed_log", number=pr.number))

        history = _search_history(ctx, pr.title, pr.number)
        duplicate = _duplicate_of(ctx, analyzer, pr, history)
        if duplicate:
            actions.apply_outcome(
                pr.number,
                ctx.config.response("pr_duplicate", number=duplicate),
                "pr_duplicate_log",
                ctx.config.outcomes.pr_duplicate,
            )
            return

        _classify_pr(ctx, actions, pr, file_changes)
    except ContentFilterError:
        log.warning(ctx.config.log_line("ai_content_filtered", number=pr.number))
        actions.apply_outcome(
            pr.number,
            ctx.config.response("pr_content_filtered"),
            "pr_content_filtered_log",
            ctx.config.outcomes.pr_content_filtered,
        )


def _classify_pr(ctx, actions, pr, file_changes):
    if not ctx.inputs.labels:
        return

    log.info(ctx.config.log_line("classification_start", number=pr.number))
    log.info(ctx.config.log_line("available_labels", labels=", ".join(ctx.inputs.labels)))

    try:
        classification = Classifier(ctx.ai, ctx.config).classify_pr(
            title=pr.title, body=pr.body, labels=ctx.inputs.labels, file_changes=file_changes
        )
    except ContentFilterError:
        raise
    except AutoIssueError as exc:
        log.error(ctx.config.log_line("classification_failed", number=pr.number, error=exc))
        return

    _apply_label(ctx, actions, pr.number, classification)


def _needs_details(classification, labels):
    if not classification:
        return False
    lowered = str(classification).lower()
    if any(keyword in lowered for keyword in BUG_KEYWORDS):
        return True
    return any(
        label.lower() == lowered
        for label in labels
        if any(keyword in label.lower() for keyword in BUG_KEYWORDS)
    )


def _log_quality(ctx, quality):
    if quality.template.has_template:
        log.info(
            ctx.config.log_line(
                "template_detected",
                type=quality.template.template_type,
                confidence=f"{quality.template.confidence:.1f}",
            )
        )
        log.info(ctx.config.log_line("template_analysis", sections=quality.content.valid_sections))
        if quality.content.user_content:
            log.info(
                ctx.config.log_line("user_content_extracted", length=len(quality.content.user_content))
            )
    log.info(
        ctx.config.log_line("quality_analysis", level=quality.quality.level, score=quality.quality.score)
    )
