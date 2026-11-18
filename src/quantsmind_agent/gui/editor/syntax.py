"""Lightweight syntax highlighting without a web engine.

The provider maps a file name to a language and a small set of regular
expressions. It is deliberately shallow: it makes source readable, never claims
to parse it, and costs no dependency. A richer provider can implement the same
:class:`SyntaxProvider` protocol later, after a dependency and memory review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

__all__ = [
    "Language",
    "PySideSyntaxHighlighter",
    "SyntaxProvider",
    "language_for_path",
    "language_label",
]


class Language(StrEnum):
    PYTHON = "python"
    JSON = "json"
    YAML = "yaml"
    TOML = "toml"
    MARKDOWN = "markdown"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    SHELL = "shell"
    PLAIN = "plain"


_SUFFIXES: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyw": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".json": Language.JSON,
    ".jsonc": Language.JSON,
    ".yaml": Language.YAML,
    ".yml": Language.YAML,
    ".toml": Language.TOML,
    ".md": Language.MARKDOWN,
    ".markdown": Language.MARKDOWN,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".sh": Language.SHELL,
    ".bash": Language.SHELL,
    ".zsh": Language.SHELL,
    ".ps1": Language.SHELL,
}


def language_for_path(path: Path | str) -> Language:
    return _SUFFIXES.get(Path(path).suffix.lower(), Language.PLAIN)


def language_label(language: Language) -> str:
    return {
        Language.PYTHON: "Python",
        Language.JSON: "JSON",
        Language.YAML: "YAML",
        Language.TOML: "TOML",
        Language.MARKDOWN: "Markdown",
        Language.JAVASCRIPT: "JavaScript",
        Language.TYPESCRIPT: "TypeScript",
        Language.SHELL: "Shell",
        Language.PLAIN: "Plain Text",
    }[language]


class SyntaxProvider(Protocol):
    def language_for_path(self, path: Path | str) -> Language: ...

    def keywords(self, language: Language) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    kind: str


_KEYWORDS: dict[Language, frozenset[str]] = {
    Language.PYTHON: frozenset(
        [
            "False",
            "None",
            "True",
            "and",
            "as",
            "assert",
            "async",
            "await",
            "break",
            "class",
            "continue",
            "def",
            "del",
            "elif",
            "else",
            "except",
            "finally",
            "for",
            "from",
            "global",
            "if",
            "import",
            "in",
            "is",
            "lambda",
            "nonlocal",
            "not",
            "or",
            "pass",
            "raise",
            "return",
            "try",
            "while",
            "with",
            "yield",
            "match",
            "case",
        ]
    ),
    Language.JAVASCRIPT: frozenset(
        [
            "async",
            "await",
            "break",
            "case",
            "catch",
            "class",
            "const",
            "continue",
            "debugger",
            "default",
            "delete",
            "do",
            "else",
            "export",
            "extends",
            "finally",
            "for",
            "from",
            "function",
            "if",
            "import",
            "in",
            "instanceof",
            "let",
            "new",
            "of",
            "return",
            "static",
            "super",
            "switch",
            "this",
            "throw",
            "try",
            "typeof",
            "var",
            "void",
            "while",
            "with",
            "yield",
        ]
    ),
    Language.SHELL: frozenset(
        [
            "break",
            "case",
            "cd",
            "continue",
            "do",
            "done",
            "elif",
            "else",
            "esac",
            "exit",
            "export",
            "fi",
            "for",
            "function",
            "if",
            "in",
            "local",
            "read",
            "return",
            "set",
            "shift",
            "then",
            "until",
            "while",
        ]
    ),
    Language.YAML: frozenset({"true", "false", "null", "yes", "no", "on", "off"}),
    Language.TOML: frozenset({"true", "false"}),
}
_KEYWORDS[Language.TYPESCRIPT] = _KEYWORDS[Language.JAVASCRIPT] | frozenset(
    {
        "interface",
        "type",
        "enum",
        "implements",
        "namespace",
        "declare",
        "private",
        "public",
        "protected",
        "readonly",
        "abstract",
        "as",
        "satisfies",
        "keyof",
        "infer",
        "never",
        "unknown",
        "string",
        "number",
        "boolean",
        "any",
        "void",
    }
)


def _word_pattern(words: frozenset[str]) -> re.Pattern[str] | None:
    if not words:
        return None
    escaped = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{escaped})(?![A-Za-z0-9_])")


_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|"
    r"\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)(?![A-Za-z0-9_])"
)
_CONSTANT = re.compile(r"(?<![A-Za-z0-9_])(?:True|False|None|true|false|null)(?![A-Za-z0-9_])")
_STRING = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')')
_JSON_KEY = re.compile(r'"(?:\\.|[^"\\])*"(?=\s*:)')
_KEY = re.compile(r"(?m)^\s*[A-Za-z0-9_.-]+\s*(?:[:=])")
_MARKDOWN_CODE = re.compile(r"`[^`\n]+`")


def _rules(language: Language) -> list[tuple[re.Pattern[str], str]]:
    rules: list[tuple[re.Pattern[str], str]] = []
    keywords = _word_pattern(_KEYWORDS.get(language, frozenset()))
    if keywords is not None:
        rules.append((keywords, "keyword"))
    rules.append((_NUMBER, "number"))
    if language in (Language.PYTHON, Language.JSON, Language.JAVASCRIPT, Language.TYPESCRIPT):
        rules.append((_CONSTANT, "constant"))
    if language is Language.PYTHON:
        rules.append((re.compile(r"(?m)^\s*@[A-Za-z_][\w.]*"), "decorator"))
        rules.append(
            (
                re.compile(
                    r"\b(?:self|cls|print|len|range|open|super|str|int|float|bool|"
                    r"list|dict|set|tuple|isinstance|enumerate|zip)\b"
                ),
                "builtin",
            )
        )
    if language is Language.MARKDOWN:
        rules.append((re.compile(r"(?m)^#{1,6}\s.*$"), "heading"))
        rules.append((re.compile(r"(?m)^\s*[-*+]\s"), "keyword"))
        rules.append((re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"), "keyword"))
    if language in (Language.YAML, Language.TOML):
        rules.append((_KEY, "key"))
    return rules


class PySideSyntaxHighlighter(QSyntaxHighlighter):
    """A small, stateful highlighter for the supported languages."""

    def __init__(self, document: QTextDocument, language: Language) -> None:
        super().__init__(document)
        self.language = language
        self._rules = _rules(language)
        self._formats = {
            "keyword": _format("#93C5FD", bold=True),
            "constant": _format("#7DD3FC"),
            "number": _format("#FCA5A5"),
            "comment": _format("#64748B", italic=True),
            "string": _format("#86EFAC"),
            "key": _format("#C4B5FD"),
            "heading": _format("#93C5FD", bold=True),
            "decorator": _format("#FCD34D"),
            "builtin": _format("#67E8F9"),
            "code": _format("#C4B5FD"),
        }
        self._enabled = True
        self._covered: list[tuple[int, int]] = []

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if not self._enabled:
            return
        self._covered = []
        self.setCurrentBlockState(0)
        language = self.language

        if language in (Language.YAML, Language.TOML) or language in (
            Language.PYTHON,
            Language.SHELL,
        ):
            self._comment(text)
        elif language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            self._line_comment(text, "//")
            self._block_comment(text)
        elif language is Language.MARKDOWN:
            self._fence(text)
            if self.currentBlockState() == 0:
                for match in _MARKDOWN_CODE.finditer(text):
                    self._span(match.start(), match.end(), "code")

        if language is Language.PYTHON:
            self._triple_string(text)
        elif language is Language.JSON:
            for match in _JSON_KEY.finditer(text):
                self._span(match.start(), match.end(), "key")
        if language is not Language.MARKDOWN or self.currentBlockState() == 0:
            self._strings(text)

        for pattern, kind in self._rules:
            for match in pattern.finditer(text):
                if self._is_covered(match.start(), match.end()):
                    continue
                self._span(match.start(), match.end(), kind)

    # -- pieces ------------------------------------------------------------
    def _comment(self, text: str) -> None:
        self._line_comment(text, "#")

    def _line_comment(self, text: str, marker: str) -> None:
        quote = ""
        escaped = False
        for index, character in enumerate(text):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif quote:
                if character == quote:
                    quote = ""
            elif character in "'\"":
                quote = character
            elif text.startswith(marker, index):
                self._span(index, len(text), "comment")
                return

    def _block_comment(self, text: str) -> None:
        state = self.previousBlockState()
        if state == 1:
            end = text.find("*/")
            if end < 0:
                self._span(0, len(text), "comment")
                self.setCurrentBlockState(1)
                return
            self._span(0, end + 2, "comment")
            start = end + 2
        else:
            start = 0
        while True:
            begin = text.find("/*", start)
            if begin < 0:
                return
            end = text.find("*/", begin + 2)
            if end < 0:
                self._span(begin, len(text), "comment")
                self.setCurrentBlockState(1)
                return
            self._span(begin, end + 2, "comment")
            start = end + 2

    def _triple_string(self, text: str) -> None:
        state = self.previousBlockState()
        start = 0
        if state in (1, 2):
            opener = '"""' if state == 1 else "'''"
            end = text.find(opener)
            if end < 0:
                self._span(0, len(text), "string")
                self.setCurrentBlockState(state)
                return
            self._span(0, end + 3, "string")
            start = end + 3
        while True:
            double = text.find('"""', start)
            single = text.find("'''", start)
            candidates = [(index, 1, '"""') for index in (double,) if index >= 0]
            candidates += [(index, 2, "'''") for index in (single,) if index >= 0]
            if not candidates:
                return
            index, block_state, opener = min(candidates, key=lambda item: item[0])
            end = text.find(opener, index + 3)
            if end < 0:
                self._span(index, len(text), "string")
                self.setCurrentBlockState(block_state)
                return
            self._span(index, end + 3, "string")
            start = end + 3

    def _strings(self, text: str) -> None:
        for match in _STRING.finditer(text):
            if self._is_covered(match.start(), match.end()):
                continue
            self._span(match.start(), match.end(), "string")

    def _fence(self, text: str) -> None:
        if self.previousBlockState() == 1:
            self._span(0, len(text), "code")
            if text.strip().startswith("```"):
                self.setCurrentBlockState(0)
            else:
                self.setCurrentBlockState(1)
        elif text.strip().startswith("```"):
            self._span(0, len(text), "code")
            self.setCurrentBlockState(1)

    # -- intervals ---------------------------------------------------------
    def _span(self, start: int, end: int, kind: str) -> None:
        if end <= start:
            return
        super().setFormat(start, end - start, self._formats[kind])
        self._covered.append((start, end))

    def _is_covered(self, start: int, end: int) -> bool:
        return any(
            start < covered_end and end > covered_start
            for covered_start, covered_end in self._covered
        )


def _format(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt
