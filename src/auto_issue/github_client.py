from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from github import Auth, Github, GithubException

from . import log
from .errors import ApiError

PINNED_ISSUES_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    pinnedIssues(first: 10) {
      nodes {
        issue { title body number url createdAt state }
        pinnedBy { login }
      }
    }
  }
}
"""

TRUNCATION_SUFFIX = "\n\n...(truncated)"


def truncate(text, limit):
    if text is None or limit is None or len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_SUFFIX


@dataclass(frozen=True)
class PinnedIssue:
    number: int
    title: str
    body: str
    state: str
    pinned_by: str

    @property
    def is_open(self):
        return (self.state or "").upper() == "OPEN"


@dataclass(frozen=True)
class FileChange:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str


def format_pinned_issues(issues, limit):
    """get pinned issues formatted for display"""
    open_issues = [issue for issue in issues if issue.is_open]
    if not open_issues:
        return ""
    blocks = [
        f"## {issue.title} (#{issue.number}) - open\nPinned by: {issue.pinned_by}\n\n{issue.body}\n\n---"
        for issue in open_issues
    ]
    return truncate("\n\n".join(blocks), limit)


@dataclass(frozen=True)
class HistoryMatch:
    number: int
    title: str
    body: str
    state: str
    is_pull_request: bool


@dataclass(frozen=True)
class HistoryResult:

    text: str = ""
    numbers: tuple = ()


def format_history(matches, limit):
    if not matches:
        return ""
    blocks = [
        "## {title} (#{number}) - {state}{kind}\n\n{body}\n\n---".format(
            title=match.title,
            number=match.number,
            state=(match.state or "unknown").lower(),
            kind=" (pull request)" if match.is_pull_request else "",
            body=match.body or "",
        )
        for match in matches
    ]
    return truncate("\n\n".join(blocks), limit)


NOISE_PATH_PARTS = (
    ".git/",
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "site-packages/",
)
BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".exe", ".dll", ".so", ".dylib", ".pyc", ".class", ".jar", ".woff", ".woff2",
    ".ttf", ".eot", ".mp3", ".mp4", ".mov", ".bin", ".db", ".sqlite", ".pack", ".idx",
)


def _is_noise_path(path):
    lowered = path.lower()
    return lowered.endswith(BINARY_SUFFIXES) or any(part in lowered for part in NOISE_PATH_PARTS)


def format_file_changes_full(changes, max_lines):
    parts = []
    for change in changes:
        part = f"{change.filename}({change.status},+{change.additions}/-{change.deletions})"
        lines = (change.patch or "").splitlines()
        limited = lines[:max_lines]
        if limited:
            part += "\n" + "\n".join(limited)
            if len(lines) > max_lines:
                part += "\n..."
        parts.append(part)
    return "\n---\n".join(parts)


def format_file_changes(changes, max_patch_lines):
    parts = []
    for change in changes:
        part = f"{change.filename}({change.status},+{change.additions}/-{change.deletions})"
        lines = [line for line in (change.patch or "").splitlines() if line[:1] in ("+", "-")]
        limited = lines[:max_patch_lines]
        if limited:
            part += "\n" + "\n".join(limited)
            if len(lines) > max_patch_lines:
                part += "\n..."
        parts.append(part)
    return "\n---\n".join(parts)


def _describe(exc):
    if isinstance(exc, GithubException):
        data = exc.data if isinstance(exc.data, dict) else {}
        message = data.get("message") or str(exc)
        return f"HTTP {exc.status}: {message}"
    return str(exc)


class GitHubClient:
    def __init__(self, token, repository, config, inputs, api_version="2022-11-28", base_url=None):
        kwargs = {"auth": Auth.Token(token), "api_version": api_version}
        if base_url:
            kwargs["base_url"] = base_url
        self._gh = Github(**kwargs)
        self._config = config
        self._inputs = inputs
        self._repository_name = repository
        self._repo = None

    @property
    def repository_name(self):
        return self._repository_name

    @property
    def repository(self):
        if self._repo is None:
            try:
                self._repo = self._gh.get_repo(self._repository_name)
            except GithubException as exc:
                raise ApiError(f"cannot access repository '{self._repository_name}': {_describe(exc)}") from exc
        return self._repo

    def get_readme(self):
        """GET the README"""
        try:
            content = self.repository.get_readme()
        except GithubException as exc:
            key = "readme_not_found" if exc.status == 404 else "readme_unavailable"
            log.warning(self._config.log_line(key, error=_describe(exc)))
            return None

        if getattr(content, "encoding", None) != "base64":
            log.warning(self._config.logging.readme_too_large)
            return None

        try:
            text = content.decoded_content.decode("utf-8", errors="replace")
        except (AssertionError, ValueError) as exc:
            log.warning(self._config.log_line("readme_decode_failed", error=exc))
            return None

        limit = self._inputs.content_max_chars
        if len(text) > limit:
            log.info(self._config.log_line("readme_truncated", limit=limit))
        text = truncate(text, limit)
        log.info(self._config.log_line("readme_found", length=len(text)))
        return text

    def get_pinned_issues_text(self):
        try:
            nodes = self._fetch_pinned_issue_nodes()
        except GithubException as exc:
            log.warning(self._config.log_line("pinned_issues_unavailable", error=_describe(exc)))
            return ""

        issues = [
            PinnedIssue(
                number=(node.get("issue") or {}).get("number") or 0,
                title=(node.get("issue") or {}).get("title") or "(untitled)",
                body=(node.get("issue") or {}).get("body") or "",
                state=(node.get("issue") or {}).get("state") or "",
                pinned_by=((node.get("pinnedBy") or {}).get("login")) or "unknown",
            )
            for node in nodes
        ]
        text = format_pinned_issues(issues, self._inputs.content_max_chars)
        if text:
            log.info(self._config.log_line("pinned_issues_found", count=len(issues)))
            if len(text) > self._inputs.content_max_chars:
                log.info(self._config.log_line("pinned_issues_truncated", limit=self._inputs.content_max_chars))
        else:
            log.info(self._config.logging.pinned_issues_not_found)
        return text

    def search_history(self, terms, exclude_number=None):
        query = self._history_query(terms)
        if not query:
            return HistoryResult()

        limit = self._config.history.max_results
        try:
            found = []
            for item in self._gh.search_issues(query=query):
                if item.number == exclude_number or item.number in [match.number for match in found]:
                    continue
                found.append(
                    HistoryMatch(
                        number=item.number,
                        title=item.title or "(untitled)",
                        body=item.body or "",
                        state=item.state or "",
                        is_pull_request=bool(getattr(item, "pull_request", None)),
                    )
                )
                if len(found) >= limit:
                    break
        except GithubException as exc:
            log.warning(self._config.log_line("history_unavailable", error=_describe(exc)))
            return HistoryResult()

        if not found:
            return HistoryResult()

        limit_chars = self._config.history.max_chars
        text = format_history(found, limit_chars)
        if len(text) > limit_chars:
            log.info(self._config.log_line("history_truncated", limit=limit_chars))
        log.info(self._config.log_line("history_found", count=len(found)))
        return HistoryResult(text=text, numbers=tuple(match.number for match in found))

    def _history_query(self, terms):
        words = [word for word in re.split(r"[^\w+#.-]+", str(terms or "")) if len(word) > 1]
        words = words[: self._config.history.max_words]
        if not words:
            return ""

        parts = [" ".join(words), f"repo:{self._repository_name}"]
        label = str(self._config.history.exclude_label or "").strip()
        if label:
            quoted = label if re.fullmatch(r"[\w.-]+", label) else f'"{label}"'
            parts.append(f"-label:{quoted}")
        return " ".join(parts)

    def get_pr_file_changes(self, number):
        access = self._inputs.pr_code_access
        if access == "off":
            log.info(self._config.logging.file_analysis_disabled_info)
            return self._config.logging.file_analysis_disabled

        pull = None
        max_files = self._inputs.max_files_to_analyze
        try:
            pull = self._get_pull(number)
            paginated = pull.get_files()
            shown = list(itertools.islice(paginated, max_files))
            total = getattr(paginated, "totalCount", None) or len(shown)
        except GithubException as exc:
            log.warning(self._config.log_line("file_changes_error", error=_describe(exc)))
            return self._config.logging.file_changes_unavailable

        if not shown:
            return self._config.logging.no_file_changes

        changes = [
            FileChange(
                filename=file.filename,
                status=file.status,
                additions=file.additions,
                deletions=file.deletions,
                patch=getattr(file, "patch", None) or "",
            )
            for file in shown
        ]
        if access == "full":
            text = format_file_changes_full(changes, self._config.code_access.diff_lines)
            text += self._changed_files_text(pull, [change.filename for change in changes])
        else:
            text = format_file_changes(changes, self._inputs.max_patch_lines_per_file)
        if total > len(changes):
            text += "\n" + self._config.log_line("file_changes_truncated", total=total, shown=len(changes))
        log.info(self._config.log_line("file_changes_count", count=total))
        return text

    def _changed_files_text(self, pull, filenames):
        ref = getattr(getattr(pull, "head", None), "sha", None)
        text = self.get_files_text(filenames, ref=ref)
        return f"\n\n---\n\n{text}" if text else ""

    def get_file_tree(self):
        paths = self._fetch_tree_paths()
        if not paths:
            return ""

        max_files = self._config.code_access.max_tree_files
        max_chars = self._config.code_access.max_tree_chars
        selected = []
        used = 0
        for path in paths:
            if len(selected) >= max_files or used + len(path) + 1 > max_chars:
                log.info(self._config.log_line("file_tree_capped", count=len(selected), total=len(paths)))
                break
            selected.append(path)
            used += len(path) + 1
        log.info(self._config.log_line("file_tree_found", count=len(selected), total=len(paths)))
        return "\n".join(selected)

    def _fetch_tree_paths(self):
        try:
            tree = self.repository.get_git_tree(self.repository.default_branch, recursive=True)
        except GithubException as exc:
            log.warning(self._config.log_line("file_tree_unavailable", error=_describe(exc)))
            return []

        if getattr(tree, "truncated", False):
            log.warning(self._config.logging.file_tree_truncated)
        return [
            entry.path
            for entry in tree.tree
            if entry.type == "blob" and not _is_noise_path(entry.path)
        ]

    def get_files_text(self, paths, ref=None):
        wanted = [
            path for path in paths if path and not path.startswith("/") and ".." not in path.split("/")
        ]
        if not wanted:
            return ""

        per_file = self._config.code_access.max_chars_per_file
        total = self._config.code_access.max_total_chars
        blocks = []
        used = 0
        for path in wanted:
            if used >= total:
                log.info(self._config.log_line("file_material_truncated", limit=total))
                break
            content = self._file_text(path, ref)
            if content is None:
                continue
            block = f"## {path}\n\n{truncate(content, per_file)}"
            blocks.append(block)
            used += len(block)
        if not blocks:
            return ""
        log.info(self._config.log_line("files_read", count=len(blocks)))
        return truncate("\n\n---\n\n".join(blocks), total)

    def _file_text(self, path, ref):
        try:
            content = self.repository.get_contents(path, ref=ref)
        except GithubException as exc:
            log.warning(self._config.log_line("file_unavailable", path=path, error=_describe(exc)))
            return None
        if isinstance(content, list) or getattr(content, "encoding", None) != "base64":
            return None
        try:
            return content.decoded_content.decode("utf-8", errors="replace")
        except (AssertionError, ValueError) as exc:
            log.warning(self._config.log_line("file_decode_failed", path=path, error=exc))
            return None

    def add_comment(self, number, body):
        self._call(lambda: self._get_issue(number).create_comment(body), f"comment on #{number}")

    def add_labels(self, number, labels):
        self._call(lambda: self._get_issue(number).add_to_labels(*labels), f"labels on #{number}")

    def close_issue(self, number, state_reason):
        self._call(
            lambda: self._get_issue(number).edit(state="closed", state_reason=state_reason),
            f"close issue #{number}",
        )

    def lock_issue(self, number):
        self._call(lambda: self._get_issue(number).lock(self._inputs.lock_reason), f"lock #{number}")

    def close_pr(self, number):
        self._call(lambda: self._get_pull(number).edit(state="closed"), f"close PR #{number}")

    def _get_issue(self, number):
        try:
            return self.repository.get_issue(number)
        except GithubException as exc:
            raise ApiError(f"cannot load #{number}: {_describe(exc)}") from exc

    def _get_pull(self, number):
        try:
            return self.repository.get_pull(number)
        except GithubException as exc:
            raise ApiError(f"cannot load PR #{number}: {_describe(exc)}") from exc

    def _fetch_pinned_issue_nodes(self):
        owner, _, name = self._repository_name.partition("/")
        _, data = self._gh.requester.graphql_query(PINNED_ISSUES_QUERY, {"owner": owner, "repo": name})
        repository = ((data or {}).get("data") or {}).get("repository") or {}
        return (repository.get("pinnedIssues") or {}).get("nodes") or []

    def _call(self, operation, description):
        try:
            return operation()
        except GithubException as exc:
            raise ApiError(f"{description} failed: {_describe(exc)}") from exc
