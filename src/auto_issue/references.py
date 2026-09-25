from __future__ import annotations

from typing import TYPE_CHECKING

from . import log
from .errors import AutoIssueError, ContentFilterError

if TYPE_CHECKING:
    from .analyzer import Analyzer
    from .handlers import RunContext


def read_reference_material(ctx, analyzer, *, title, body):
    """Material read from the configured reference repositories
    """
    if not ctx.inputs.reference_repos:
        return ""

    settings = ctx.config.references
    remaining_files = settings.max_files
    remaining_chars = settings.max_total_chars
    parts = []
    target = str(getattr(ctx.client, "repository_name", "")).lower()

    for name in ctx.inputs.reference_repos:
        if name == target:
            log.info(ctx.config.log_line("reference_repo_is_target", repo=name))
            continue
        if remaining_files <= 0 or remaining_chars <= 0:
            log.info(ctx.config.log_line("reference_budget_spent", repo=name))
            break

        limit = min(settings.max_files_per_repo, remaining_files)
        try:
            text, files = _read_one(ctx, analyzer, name, title, body, limit, remaining_chars)
        except ContentFilterError:
            raise
        except AutoIssueError as exc:
            log.warning(ctx.config.log_line("reference_failed", repo=name, error=exc))
            continue

        if not text:
            continue
        remaining_files -= files
        remaining_chars -= len(text)
        parts.append(text)

    return "\n\n---\n\n".join(parts)


def _read_one(ctx, analyzer, name, title, body, file_limit, char_budget):
    tree = ctx.client.get_reference_tree(name)
    if not tree:
        return "", 0

    paths = analyzer.select_reference_files(
        repo=name, title=title, body=body, file_tree=tree, max_files=file_limit
    )
    if not paths:
        log.info(ctx.config.log_line("reference_none_selected", repo=name))
        return "", 0

    log.info(
        ctx.config.log_line(
            "reference_files_selected", repo=name, count=len(paths), files=", ".join(paths)
        )
    )
    text = ctx.client.get_reference_files_text(
        name,
        paths,
        max_files=file_limit,
        max_chars_per_file=ctx.config.references.max_chars_per_file,
        max_total_chars=char_budget,
    )
    if not text:
        return "", 0

    # GitHubClient heads every block with `<repository>:<path>`; a lighter client that
    # returns already-formatted material without those headers falls back to the request.
    loaded = sum(1 for line in text.splitlines() if line.startswith(f"## {name}:"))
    if not loaded:
        loaded = len(paths)
    log.info(ctx.config.log_line("reference_files_read", count=loaded, repo=name))
    return text, loaded
