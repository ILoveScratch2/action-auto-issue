from __future__ import annotations

import json
import os
import sys
import traceback

from . import log
from .ai import AiClient, AiSettings
from .config import apply_locale, load_config, parse_inputs
from .errors import AutoIssueError, ConfigurationError
from .events import parse_issue_event, parse_pr_event
from .github_client import GitHubClient
from .handlers import RunContext, handle_new_issue, handle_new_pr
from .results import FailureCollector

ISSUE_EVENT = "issues"
PR_EVENT = "pull_request_target"


def build_context(environ=None):
    environ = os.environ if environ is None else environ
    base = load_config()
    config = apply_locale(
        base,
        environ.get("INPUT_LANGUAGE") or base.defaults.language,
        answer_language=environ.get("INPUT_ANSWER_LANGUAGE"),
    )
    inputs = parse_inputs(environ, config)

    settings = AiSettings(
        base_url=inputs.ai_base_url,
        api_key=inputs.ai_key,
        model=inputs.model,
        api_type=inputs.ai_api_type,
        max_tokens=inputs.max_tokens,
        temperature=float(config.ai_settings.temperature),
        timeout=inputs.request_timeout_seconds,
    )
    failures = FailureCollector()
    client = GitHubClient(
        token=inputs.token,
        repository=environ.get("GITHUB_REPOSITORY", ""),
        config=config,
        inputs=inputs,
        base_url=environ.get("GITHUB_API_URL") or None,
    )
    return RunContext(config=config, inputs=inputs, client=client, ai=AiClient(settings, config), failures=failures)


def dispatch(ctx, event_name, payload):
    if event_name == ISSUE_EVENT:
        issue = parse_issue_event(payload)
        if issue is not None:
            handle_new_issue(ctx, issue)
            return
    elif event_name == PR_EVENT:
        pr = parse_pr_event(payload)
        if pr is not None:
            handle_new_pr(ctx, pr)
            return

    log.info(
        ctx.config.log_line("event_no_match", event=event_name, action=payload.get("action") or "unknown")
    )


def run(environ=None):
    environ = os.environ if environ is None else environ

    try:
        ctx = build_context(environ)
    except ConfigurationError as exc:
        log.set_failed(str(exc))
        return 1

    _log_context(ctx)

    try:
        dispatch(ctx, environ.get("GITHUB_EVENT_NAME", ""), _load_payload(environ))
    except AutoIssueError as exc:
        log.set_failed(str(exc))
        return 1
    except Exception as exc:
        traceback.print_exc()
        log.set_failed(f"unexpected error: {exc}")
        return 1

    if ctx.failures:
        for operation, error in ctx.failures.items:
            log.error(ctx.config.log_line("action_failed", operation=operation, error=error))
        log.set_failed(ctx.config.log_line("failures_summary", count=len(ctx.failures)))
        return 1

    return 0


def _load_payload(environ):
    path = environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"cannot read the event payload at {path}: {exc}")
        return {}


def _log_context(ctx):
    log.info(ctx.config.log_line("target_repo", owner=_owner(ctx), repo=_name(ctx)))
    log.info(ctx.config.log_line("using_ai_model", model=ctx.inputs.model))
    log.info(
        ctx.config.log_line(
            "using_api_endpoint", endpoint=ctx.inputs.ai_base_url, api_type=ctx.inputs.ai_api_type
        )
    )
    log.info(ctx.config.logging.config_info)
    log.info(ctx.config.log_line("comment_language_info", language=ctx.inputs.language))
    log.info(ctx.config.log_line("answer_language_info", answer_language=ctx.inputs.answer_language))
    log.info(ctx.config.log_line("analysis_depth_info", access=ctx.inputs.pr_code_access))
    log.info(
        ctx.config.log_line(
            "analysis_depth_details",
            depth=ctx.inputs.analysis_depth,
            files=ctx.inputs.max_files_to_analyze,
            lines=ctx.inputs.max_patch_lines_per_file,
        )
    )


def _owner(ctx):
    return getattr(ctx.client, "repository_name", "").partition("/")[0]


def _name(ctx):
    return getattr(ctx.client, "repository_name", "").partition("/")[2]


def main():
    return run()


if __name__ == "__main__":
    sys.exit(main())
