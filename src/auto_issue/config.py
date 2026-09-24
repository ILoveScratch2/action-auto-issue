from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import log
from .errors import ConfigurationError

# action.yml installs the package instead of importing it in place, which puts this file in site-packages 
ACTION_ROOT = Path(os.environ.get("AUTO_ISSUE_ROOT") or Path(__file__).resolve().parents[2])
SUPPORTED_LANGUAGES = ("en", "zh-cn")
SUPPORTED_API_TYPES = ("chat-completions", "responses")
REQUIRED_SECTIONS = (
    "prompts",
    "responses",
    "outcomes",
    "code_access",
    "history",
    "answer_languages",
    "logging",
    "ai_settings",
    "defaults",
    "analysis_depths",
)
OUTCOMES = (
    "issue_blocked",
    "issue_spam",
    "issue_duplicate",
    "issue_readme_covered",
    "issue_basic",
    "issue_unclear",
    "issue_content_filtered",
    "pr_blocked",
    "pr_spam",
    "pr_duplicate",
    "pr_invalid_commit",
    "pr_malicious",
    "pr_trivial",
    "pr_content_filtered",
)
OUTCOME_KEYS = ("comment", "close", "lock", "labels")
HISTORY_NUMBERS = ("max_results", "max_words", "max_chars")
ISSUE_CODE_ACCESS = ("off", "read", "advise")
PR_CODE_ACCESS = ("off", "patch", "full")
CODE_ACCESS_NUMBERS = (
    "max_files_to_read",
    "max_chars_per_file",
    "max_total_chars",
    "max_tree_files",
    "max_tree_chars",
    "diff_lines",
    "advise_max_tokens",
)
REQUIRED_PROMPTS = ("spam_detection", "readme_coverage_check", "content_quality_check", "pr_spam_detection")
TRUE_VALUES = ("true", "1", "yes")
FALSE_VALUES = ("false", "0", "no")
ANSWER_LANGUAGE_MODES = ("auto", "locale", "en", "zh", "both")

ANSWER_LANGUAGE_ALIASES = {
    "auto": "auto",
    "locale": "locale",
    "en": "en",
    "zh": "zh",
    "zh-cn": "zh",
    "both": "both",
}
ANSWER_LANGUAGE_FALLBACK = "auto"
ANSWER_LANGUAGE_PLACEHOLDERS = ("language",)


class Section(dict):

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise ConfigurationError(f"missing configuration key '{name}'") from exc


@dataclass(frozen=True)
class Config:
    prompts: Section
    responses: Section
    outcomes: Section
    code_access: Section
    history: Section
    answer_languages: Section
    logging: Section
    ai_settings: Section
    defaults: Section
    analysis_depths: Section
    locale: Section
    answer_language: str = ""  # the phrase the answer prompts use, resolved by apply_locale

    def log_line(self, key, **fields):
        return log.render(self.logging[key], **fields)

    def response(self, key, **fields):
        return log.render(self.responses[key], **fields)

    def prompt(self, key, **fields):
        return log.render(self.prompts[key], **fields)


@dataclass(frozen=True)
class Inputs:
    token: str
    ai_base_url: str
    ai_key: str
    model: str
    ai_api_type: str
    labels: tuple
    language: str
    answer_language: str
    apply_labels: bool
    search_history: bool
    issue_code_access: str
    pr_code_access: str
    analysis_depth: str
    blocked_users: tuple
    max_tokens: int
    content_max_chars: int
    max_files_to_analyze: int
    max_patch_lines_per_file: int
    request_timeout_seconds: int
    lock_reason: str


def load_config(root=ACTION_ROOT):
    path = Path(root) / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"config.json not found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config.json is not valid JSON: {exc}") from exc
    validate_config(raw)
    return Config(
        prompts=Section(raw["prompts"]),
        responses=Section(raw["responses"]),
        outcomes=Section(raw["outcomes"]),
        code_access=Section(raw["code_access"]),
        history=Section(raw["history"]),
        answer_languages=Section(raw["answer_languages"]),
        logging=Section(raw["logging"]),
        ai_settings=Section(raw["ai_settings"]),
        defaults=Section(raw["defaults"]),
        analysis_depths=Section(raw["analysis_depths"]),
        locale=Section({}),
    )


def validate_config(raw):
    for section in REQUIRED_SECTIONS:
        if not isinstance(raw.get(section), dict):
            raise ConfigurationError(f"config.json is missing the '{section}' section")
    for name in REQUIRED_PROMPTS:
        if not raw["prompts"].get(name):
            raise ConfigurationError(f"config.json is missing the '{name}' prompt")
    for name in ("max_tokens", "temperature", "request_timeout_seconds", "content_max_chars"):
        if not isinstance(raw["ai_settings"].get(name), (int, float)):
            raise ConfigurationError(f"config.json ai_settings.{name} must be a number")
    if raw["ai_settings"]["max_tokens"] <= 0:
        raise ConfigurationError("config.json ai_settings.max_tokens must be positive")
    if not 0 <= raw["ai_settings"]["temperature"] <= 2:
        raise ConfigurationError("config.json ai_settings.temperature must be between 0 and 2")
    if set(raw["outcomes"]) != set(OUTCOMES):
        unknown = ", ".join(sorted(set(raw["outcomes"]) - set(OUTCOMES))) or "none"
        missing = ", ".join(sorted(set(OUTCOMES) - set(raw["outcomes"]))) or "none"
        raise ConfigurationError(
            f"config.json outcomes must list exactly the known outcomes "
            f"(unknown: {unknown}; missing: {missing})"
        )
    for name, policy in raw["outcomes"].items():
        if not isinstance(policy, dict) or set(policy) != set(OUTCOME_KEYS):
            raise ConfigurationError(
                f"config.json outcomes.{name} must set exactly: {', '.join(OUTCOME_KEYS)}"
            )
        for flag in ("comment", "close", "lock"):
            if not isinstance(policy[flag], bool):
                raise ConfigurationError(f"config.json outcomes.{name}.{flag} must be true or false")
        labels = policy["labels"]
        if not isinstance(labels, list) or not all(
            isinstance(label, str) and label.strip() for label in labels
        ):
            raise ConfigurationError(
                f"config.json outcomes.{name}.labels must be a list of label names"
            )
    for name in CODE_ACCESS_NUMBERS:
        if not isinstance(raw["code_access"].get(name), int) or raw["code_access"][name] <= 0:
            raise ConfigurationError(f"config.json code_access.{name} must be a positive integer")
    analysis_depths = raw["analysis_depths"]
    if not isinstance(analysis_depths, dict) or not analysis_depths:
        raise ConfigurationError("config.json analysis_depths must be a non-empty object")
    for depth, settings in analysis_depths.items():
        if not isinstance(settings, dict):
            raise ConfigurationError(f"config.json analysis_depths.{depth} must be an object")
        for name in ("max_files", "max_lines"):
            value = settings.get(name)
            if not isinstance(value, int) or value <= 0:
                raise ConfigurationError(
                    f"config.json analysis_depths.{depth}.{name} must be a positive integer"
                )
    for name, allowed in (("issue", ISSUE_CODE_ACCESS), ("pr", PR_CODE_ACCESS)):
        if raw["code_access"].get(name) not in allowed:
            raise ConfigurationError(
                f"config.json code_access.{name} must be one of: {', '.join(allowed)}"
            )
    for name in HISTORY_NUMBERS:
        if not isinstance(raw["history"].get(name), int) or raw["history"][name] <= 0:
            raise ConfigurationError(f"config.json history.{name} must be a positive integer")
    if not isinstance(raw["history"].get("enabled"), bool):
        raise ConfigurationError("config.json history.enabled must be true or false")
    exclude_label = raw["history"].get("exclude_label")
    if not isinstance(exclude_label, str) or '"' in exclude_label:
        raise ConfigurationError('config.json history.exclude_label must be a string without quotes')
    for mode in ANSWER_LANGUAGE_MODES:
        phrase = raw["answer_languages"].get(mode)
        if not isinstance(phrase, str) or not phrase.strip():
            raise ConfigurationError(f"config.json answer_languages is missing the '{mode}' phrase")
        unsupported = set(re.findall(r"\{([a-z_]+)\}", phrase)) - set(ANSWER_LANGUAGE_PLACEHOLDERS)
        if unsupported:
            raise ConfigurationError(
                f"config.json answer_languages.{mode} uses unsupported placeholder(s): "
                f"{', '.join(sorted(unsupported))}"
            )
    if raw["defaults"].get("answer_language") not in ANSWER_LANGUAGE_MODES:
        raise ConfigurationError(
            "config.json defaults.answer_language must be one of: "
            f"{', '.join(ANSWER_LANGUAGE_MODES)}"
        )


def apply_locale(config, requested, root=ACTION_ROOT, answer_language=None):
    language = normalize_language(requested)
    path = Path(root) / "locales" / f"{language}.json"
    try:
        locale = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot load locale '{language}': {exc}") from exc

    section = Section(locale)
    if not str(section.get("answer_language") or "").strip():
        raise ConfigurationError(f"locale '{language}' is missing 'answer_language'")

    merged = dict(config.responses)
    merged.update(section.get("responses", {}))
    requested_mode = answer_language if str(answer_language or "").strip() else config.defaults.answer_language
    phrase = answer_language_instruction(
        config.answer_languages, section.answer_language, normalize_answer_language(requested_mode)
    )
    return replace(config, responses=Section(merged), locale=section, answer_language=phrase)


def normalize_language(requested):
    return "zh-CN" if str(requested or "").strip().lower() == "zh-cn" else "en"


def normalize_code_access(requested, allowed, fallback):
    value = str(requested or "").strip().lower()
    return value if value in allowed else fallback


def normalize_answer_language(requested):
    return ANSWER_LANGUAGE_ALIASES.get(str(requested or "").strip().lower(), ANSWER_LANGUAGE_FALLBACK)


def answer_language_instruction(answer_languages, locale_name, mode):
    return log.render(answer_languages[mode], language=locale_name)


def parse_inputs(environ, config):
    requested_language = _text(environ, "language") or config.defaults.language
    language = normalize_language(requested_language)
    if requested_language.strip().lower() not in SUPPORTED_LANGUAGES:
        log.warning(config.log_line("unsupported_language", language=requested_language))

    requested_answer_language = _text(environ, "answer-language") or config.defaults.answer_language
    answer_language = normalize_answer_language(requested_answer_language)
    if requested_answer_language.strip().lower() not in ANSWER_LANGUAGE_ALIASES:
        log.warning(
            config.log_line(
                "unsupported_answer_language",
                value=requested_answer_language,
                fallback=answer_language,
            )
        )

    requested_issue_access = _text(environ, "issue-code-access") or config.code_access.issue
    issue_code_access = normalize_code_access(requested_issue_access, ISSUE_CODE_ACCESS, "off")
    if issue_code_access != requested_issue_access.strip().lower():
        log.warning(
            config.log_line(
                "unknown_code_access",
                input="issue-code-access",
                value=requested_issue_access,
                fallback=issue_code_access,
            )
        )

    requested_pr_access = _text(environ, "pr-code-access") or config.code_access.pr
    pr_code_access = normalize_code_access(requested_pr_access, PR_CODE_ACCESS, "patch")
    if pr_code_access != requested_pr_access.strip().lower():
        log.warning(
            config.log_line(
                "unknown_code_access",
                input="pr-code-access",
                value=requested_pr_access,
                fallback=pr_code_access,
            )
        )

    depth = (_text(environ, "analysis-depth") or config.defaults.analysis_depth).lower()
    if depth not in config.analysis_depths:
        log.warning(config.log_line("unknown_analysis_depth", depth=depth))
        depth = "normal"
    depth_settings = config.analysis_depths[depth]

    api_type = (_text(environ, "ai-api-type") or config.defaults.ai_api_type).strip().lower()
    if api_type not in SUPPORTED_API_TYPES:
        raise ConfigurationError(
            f"unsupported ai-api-type '{api_type}', expected one of: {', '.join(SUPPORTED_API_TYPES)}"
        )

    base_url = _required(environ, "ai-base-url", config)
    parsed_url = urlsplit(base_url if "://" in base_url else f"//{base_url}")
    hostname = (parsed_url.hostname or "").lower().rstrip(".")
    if hostname == "models.github.ai" or hostname.endswith(".models.github.ai"):
        raise ConfigurationError(
            "the GitHub Models endpoint was retired on 2026-07-30; configure another ai-base-url"
        )

    return Inputs(
        token=_required(environ, "token", config),
        ai_base_url=base_url,
        ai_key=_required(environ, "ai-key", config),
        model=_text(environ, "model") or config.defaults.model,
        ai_api_type=api_type,
        labels=_split_list(_text(environ, "labels") or config.defaults.labels),
        language=language,
        answer_language=answer_language,
        apply_labels=_boolean(environ, "apply-labels", config.defaults.apply_labels),
        search_history=_boolean(environ, "search-history", config.history.enabled),
        issue_code_access=issue_code_access,
        pr_code_access=pr_code_access,
        analysis_depth=depth,
        blocked_users=tuple(user.lower() for user in _split_list(_text(environ, "blocked-users"))),
        max_tokens=_positive_int(environ, "max-tokens", config.ai_settings.max_tokens),
        content_max_chars=_positive_int(environ, "content-max-chars", config.ai_settings.content_max_chars),
        max_files_to_analyze=depth_settings["max_files"],
        max_patch_lines_per_file=depth_settings["max_lines"],
        request_timeout_seconds=int(config.ai_settings.request_timeout_seconds),
        lock_reason=config.defaults.lock_reason,
    )


def _text(environ, name):
    return str(environ.get(f"INPUT_{name.upper().replace('-', '_')}", "")).strip()


def _required(environ, name, config):
    value = _text(environ, name)
    if not value:
        raise ConfigurationError(
            f"the '{name}' input is required. Set it in the workflow "
            f"(see the README for provider examples); available inputs are documented in action.yml."
        )
    return value


def _split_list(value):
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


def _positive_int(environ, name, default):
    raw = _text(environ, name)
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"the '{name}' input must be an integer, got '{raw}'") from exc
    if value <= 0:
        raise ConfigurationError(f"the '{name}' input must be positive, got {value}")
    return value


def _boolean(environ, name, default):
    raw = _text(environ, name).lower()
    if not raw:
        return bool(default)
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ConfigurationError(f"the '{name}' input must be true or false, got '{raw}'")
