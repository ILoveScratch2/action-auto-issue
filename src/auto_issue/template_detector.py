"""Local issue template detection and user content extraction. No API calls here."""

from __future__ import annotations

import re
from dataclasses import dataclass

TITLE_PREFIXES = (
    "[BUG]",
    "[FEATURE]",
    "[Feature Request]",
    "[Bug Report]",
    "[ENHANCEMENT]",
    "[QUESTION]",
)

TEMPLATE_INDICATORS = tuple(
    re.compile(pattern, re.MULTILINE)
    for pattern in (
        r"^### .*",
        r"^## .*",
        r"\*\*(.+)\*\*",
        r"<!--.+-->",
        r"^\s*-\s*\[[\sx]\]",
        r"^>\s*.+",
        r"\|\s*.+\s*\|",
    )
)

COMMON_FIELDS = (
    "description",
    "expected behavior",
    "actual behavior",
    "steps to reproduce",
    "environment",
    "version",
    "browser",
    "additional context",
    "screenshots",
    "描述",
    "预期行为",
    "实际行为",
    "复现步骤",
    "环境信息",
    "版本",
    "浏览器",
    "附加信息",
    "截图",
    "bug描述",
    "功能描述",
    "如何实现",
    "自查",
    "确认",
)
_COMMON_FIELD_PATTERNS = tuple(re.compile(re.escape(field), re.IGNORECASE) for field in COMMON_FIELDS)

EMPTY_PATTERNS = (
    re.compile(r"^_No response_$", re.IGNORECASE),
    re.compile(r"^N/A$"),
    re.compile(r"^None$"),
    re.compile(r"^无$"),
    re.compile(r"^没有$"),
    re.compile(r"^暂无$"),
    re.compile(r"^\s*$"),
    re.compile(r"^\.+$"),
    re.compile(r"^-+$"),
    re.compile(r"^#+$"),
)

CONFIDENCE_CEILING = 85
TEMPLATE_THRESHOLD = 25


@dataclass(frozen=True)
class TemplateInfo:
    has_template: bool
    template_type: str
    confidence: float
    indicators: tuple


@dataclass(frozen=True)
class ContentInfo:
    user_content: str
    is_empty: bool
    total_sections: int
    valid_sections: int


@dataclass(frozen=True)
class QualityInfo:
    score: int
    level: str
    reasons: tuple


@dataclass(frozen=True)
class QualityAnalysis:
    template: TemplateInfo
    content: ContentInfo
    quality: QualityInfo

    def report(self):
        lines = ["Template Analysis:", f"- Has template: {'Yes' if self.template.has_template else 'No'}"]
        if self.template.has_template:
            lines.extend(
                [
                    f"- Type: {self.template.template_type}",
                    f"- Confidence: {self.template.confidence:.1f}%",
                    f"- Indicators: {', '.join(self.template.indicators)}",
                    f"- Valid sections: {self.content.valid_sections}/{self.content.total_sections}",
                    f"- Content: {'Empty' if self.content.is_empty else 'Has content'}",
                ]
            )
        return "\n".join(lines)


def is_empty_content(content):
    text = (content or "").strip()
    if not text:
        return True
    if any(pattern.search(text) for pattern in EMPTY_PATTERNS):
        return True
    stripped = re.sub(r"[-_*#>\s]", "", text)
    stripped = re.sub(r"\[[\sx]\]", "", stripped)
    stripped = stripped.replace("|", "")
    return len(stripped) < 3


def detect_template(title, body):
    if not body:
        return TemplateInfo(False, None, 0.0, ())

    title = title or ""
    indicators = []
    indicator_count = 0
    total_lines = len(body.split("\n"))

    for prefix in TITLE_PREFIXES:
        if prefix in title:
            indicators.append(f"Title prefix: {prefix}")
            indicator_count += 2
            break

    for pattern in TEMPLATE_INDICATORS:
        matches = pattern.findall(body)
        if matches:
            indicators.append(f"Template pattern: {pattern.pattern} ({len(matches)}x)")
            indicator_count += len(matches)

    field_count = sum(1 for pattern in _COMMON_FIELD_PATTERNS if pattern.search(body))
    if field_count >= 2:
        indicators.append(f"Found {field_count} template fields")
        indicator_count += field_count

    min_lines = max(total_lines, 3)
    max_expected = max(min_lines / 3, 2)
    density = min(indicator_count / max_expected, 1.5)
    length_factor = min(total_lines / 10, 1)
    confidence = min(CONFIDENCE_CEILING, density * length_factor * 100)
    has_template = confidence > TEMPLATE_THRESHOLD

    template_type = None
    if has_template:
        lowered_title = title.lower()
        lowered_body = body.lower()
        if "bug" in lowered_title or "bug" in lowered_body:
            template_type = "bug_report"
        elif "feature" in lowered_title or "feature" in lowered_body:
            template_type = "feature_request"
        else:
            template_type = "generic"

    return TemplateInfo(has_template, template_type, confidence, tuple(indicators))


def extract_user_content(body, template):
    if not body or not template.has_template:
        text = body or ""
        return ContentInfo(text, not text.strip(), 0, 0)

    sections = []
    current_title = None
    current_lines = []

    def flush():
        if current_title is None or not current_lines:
            return
        content = "\n".join(current_lines).strip()
        sections.append((current_title, content, bool(content) and not is_empty_content(content)))

    for line in body.split("\n"):
        if any(pattern.search(line) for pattern in TEMPLATE_INDICATORS):
            flush()
            current_title = re.sub(r"[#*>]", "", line).strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()

    filled = [(title, content) for title, content, valid in sections if valid]
    user_content = "\n\n".join(f"{title}: {content}" for title, content in filled)

    return ContentInfo(
        user_content=user_content,
        is_empty=not filled,
        total_sections=len(sections),
        valid_sections=len(filled),
    )


def analyze_issue_quality(title, body):
    template = detect_template(title, body)
    content = extract_user_content(body, template)

    score = 50
    reasons = []
    title = title or ""

    if len(title.strip()) > 10:
        score += 15
        reasons.append("Good title length")
    else:
        score -= 10
        reasons.append("Title too short")

    body_text = body or ""
    if template.has_template:
        if not content.is_empty and content.valid_sections >= 2:
            score += 25
            reasons.append("Template used and well filled")
        elif not content.is_empty:
            score += 10
            reasons.append("Template used but incomplete")
        else:
            score -= 20
            reasons.append("Template used but empty")
    else:
        if len(body_text.strip()) > 50:
            score += 20
            reasons.append("Good free-form description")
        elif len(body_text.strip()) > 10:
            score += 5
            reasons.append("Brief free-form description")
        else:
            score -= 25
            reasons.append("Too short or empty content")

    score = max(0, min(100, score))
    if score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    else:
        level = "low"

    return QualityAnalysis(template, content, QualityInfo(score, level, tuple(reasons)))
