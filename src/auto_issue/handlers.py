from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import log
from .actions import IssueActions, PrActions
from .analyzer import Analyzer
from .classifier import Classifier, match_label
from .config import policy_block
from .errors import AutoIssueError, ContentFilterError
from .results import (
    CoverageVerdict,
    FailureCollector,
    IssueResult,
    IssueStatus,
    PrDecision,
    QualityVerdict,
    SpamVerdict,
    parse_review_report,
    parse_review_request,
)
from .github_client import HistoryResult
from .references import read_reference_material
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
    extra_prompt = policy_block(ctx.config, ctx.inputs)

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
    reference_files = read_reference_material(ctx, analyzer, title=issue.title, body=issue.body)

    log.info(ctx.config.logging.readme_check_start)
    coverage = analyzer.check_readme_coverage(
        title=issue.title,
        body=issue.body,
        readme=readme,
        pinned=pinned,
        history=history.text,
        project_files=project_files,
        reference_files=reference_files,
        extra_prompt=extra_prompt,
    )
    log.info(ctx.config.log_line("readme_check_result", result=coverage.value))
    if coverage is CoverageVerdict.COVERED:
        return _answer_from_docs(
            ctx, actions, analyzer, issue, readme, pinned, history.text, project_files, reference_files
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
        ctx,
        actions,
        analyzer,
        issue,
        quality,
        readme,
        history.text,
        project_files,
        reference_files,
        extra_prompt,
    )


def _answer_from_docs(
    ctx, actions, analyzer, issue, readme, pinned, history="", project_files="", reference_files=""
):
    try:
        answer = analyzer.generate_readme_answer(
            title=issue.title,
            body=issue.body,
            readme=readme,
            pinned=pinned,
            history=history,
            project_files=project_files,
            reference_files=reference_files,
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


def _classify_issue(
    ctx,
    actions,
    analyzer,
    issue,
    quality,
    readme,
    history="",
    project_files="",
    reference_files="",
    extra_prompt="",
):
    log.info(ctx.config.log_line("classification_start", number=issue.number))
    log.info(ctx.config.log_line("available_labels", labels=", ".join(ctx.inputs.labels)))

    content = quality.content.user_content if quality.template.has_template else (issue.body or "")
    try:
        classification = Classifier(ctx.ai, ctx.config).classify_issue(
            title=issue.title,
            content=content,
            labels=ctx.inputs.labels,
            project_files=project_files,
            extra_prompt=extra_prompt,
        )
        label = _apply_label(ctx, actions, issue.number, classification)

        outcome = ctx.inputs.label_outcomes.get(label) if label else None
        prefix = ctx.inputs.title_prefixes.get(label, "")
        if outcome is not None:
            if not outcome.close:
                actions.prefix_title(issue.number, issue.title, prefix)
            actions.apply_outcome(issue.number, outcome.comment, "label_outcome_log", outcome.policy())
            return IssueResult(
                IssueStatus.CLOSED if outcome.close else IssueStatus.KEPT, classification, label
            )
        actions.prefix_title(issue.number, issue.title, prefix)

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
            extra_prompt=extra_prompt,
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
        _handle_unclear(ctx, actions, analyzer, issue, readme, history, project_files, reference_files)
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


def _handle_unclear(
    ctx, actions, analyzer, issue, readme, history="", project_files="", reference_files=""
):
    policy = ctx.config.outcomes.issue_unclear
    if policy["comment"] and (readme or history or project_files or reference_files):
        try:
            answer = analyzer.generate_smart_answer(
                title=issue.title,
                body=issue.body,
                readme=readme,
                history=history,
                project_files=project_files,
                reference_files=reference_files,
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

        _review_pr(ctx, actions, pr, analyzer)
        _classify_pr(ctx, actions, pr, file_changes)
    except ContentFilterError:
        log.warning(ctx.config.log_line("ai_content_filtered", number=pr.number))
        actions.apply_outcome(
            pr.number,
            ctx.config.response("pr_content_filtered"),
            "pr_content_filtered_log",
            ctx.config.outcomes.pr_content_filtered,
        )


def _review_pr(ctx, actions, pr, analyzer):
    """Reviews the diff, letting the model pull more project files in bounded rounds."""
    if ctx.inputs.pr_review != "on":
        return
    if pr.draft:
        log.info(ctx.config.log_line("pr_review_skipped_draft", number=pr.number))
        return
    if ctx.inputs.pr_code_access == "off":
        log.warning(ctx.config.log_line("pr_review_skipped_no_diff", number=pr.number))
        return

    review = ctx.config.review
    log.info(ctx.config.logging.pr_review_start)
    ref = pr.head_sha or None
    material = ""
    read = []
    try:
        diff = ctx.client.get_pr_file_changes(
            pr.number, max_files=review.max_diff_files, max_patch_lines=review.max_diff_lines
        )
        if diff == ctx.config.logging.file_changes_unavailable:
            log.warning(
                ctx.config.log_line(
                    "pr_review_failed", number=pr.number, error="the pull request diff is unavailable"
                )
            )
            return
        commits = ctx.client.get_pr_commits_text(pr.number, review.max_commits)
        tree = ctx.client.get_file_tree(ref=ref)
        reference_files = read_reference_material(ctx, analyzer, title=pr.title, body=pr.body)

        raw = ""
        for round_number in range(review.max_rounds + 1):
            remaining = review.max_files - len(read)
            raw = analyzer.review_pr(
                title=pr.title,
                body=pr.body,
                diff=diff,
                commits=commits,
                file_tree=tree,
                project_files=material,
                reference_files=reference_files,
                max_files=remaining,
            )
            requested = (
                parse_review_request(raw, tree.splitlines(), remaining)
                if round_number < review.max_rounds and tree
                else None
            )
            if not requested:
                break
            log.info(
                ctx.config.log_line(
                    "pr_review_files_requested",
                    round=round_number + 1,
                    count=len(requested),
                    files=", ".join(requested),
                )
            )
            text, read_now = _read_review_files(ctx, requested, ref, read, review, len(material))
            if not read_now:
                break
            material += text
            read.extend(read_now)
    except ContentFilterError:
        log.warning(ctx.config.log_line("pr_review_skipped_filtered", number=pr.number))
        return
    except AutoIssueError as exc:
        log.error(ctx.config.log_line("pr_review_failed", number=pr.number, error=exc))
        return

    verdict, report = parse_review_report(raw)
    if not report or parse_review_request(raw, [], 0) is not None:
        log.info(ctx.config.log_line("pr_review_no_report", number=pr.number))
        return
    if verdict is None:
        log.info(ctx.config.logging.pr_review_no_verdict)
    else:
        log.info(ctx.config.log_line("pr_review_result", result=verdict.value))

    prefix = ctx.config.response("pr_review_prefix", author=pr.author, model=ctx.inputs.model)
    posted = actions.review(pr.number, prefix + "\n\n" + report, log_key="pr_review_posted_log")
    if not posted:
        return
    log.summary(
        f"### PR #{pr.number} code review\n\n"
        f"- Verdict: {verdict.value if verdict else 'unknown'}\n"
        f"- Project files read: {len(read)}\n"
    )


def _read_review_files(ctx, paths, ref, read, review, used_chars):
    """Reads the files the reviewer asked for, or returns nothing when the budget is spent."""
    remaining = review.max_total_chars - used_chars
    if remaining <= 0:
        return "", ()
    text = ctx.client.get_files_text(
        paths,
        ref=ref,
        exclude=set(read),
        max_files=review.max_files - len(read),
        max_chars_per_file=review.max_chars_per_file,
        max_total_chars=remaining,
    )
    if not text:
        return "", ()
    loaded = tuple(
        path for path in paths if any(line == f"## {path}" for line in text.splitlines())
    )
    # GitHubClient prefixes every block with its path. Keep compatibility with lightweight
    # clients that return already-formatted material without those headers.
    if not loaded:
        loaded = tuple(paths)
    return text, loaded


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
