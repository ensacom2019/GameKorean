from __future__ import annotations

import re


JA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
KO_RE = re.compile(r"[\uac00-\ud7a3]")
LATIN_RE = re.compile(r"[A-Za-z]")
TOKEN_RE = re.compile(
    r"(\\[A-Za-z]+\[[^\]]*\]|\\[.$|!><^{}]|%\d*\$?[#0 +\-]?[0-9.*]*[a-zA-Z]|"
    r"\{[^{}\r\n]+\}|</?[A-Za-z][^>]*>|<#[0-9A-Fa-f]{3,8}>|"
    r"\$\{[^{}]+\}|\\[nrt]|\[[A-Z_][A-Z0-9_]*\])"
)
MARKER_RE = re.compile(r"⟦\s*GKO\s*(\d+)\s*⟧")


def guess_language(text: str) -> str:
    if JA_RE.search(text):
        return "ja"
    if LATIN_RE.search(text):
        return "en"
    return "auto"


def is_translatable(text: str) -> bool:
    value = text.strip()
    if not value or len(value) > 5000:
        return False
    if KO_RE.search(value) and not JA_RE.search(value) and len(LATIN_RE.findall(value)) < 3:
        return False
    if not (JA_RE.search(value) or LATIN_RE.search(value)):
        return False
    if re.fullmatch(r"(?:https?://|www\.)\S+", value, re.I):
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./\\:-]*", value) and ("_" in value or "/" in value or "\\" in value):
        return False
    if re.fullmatch(r"[A-Fa-f0-9-]{16,}", value):
        return False
    return True


def protect_tokens(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"⟦GKO{len(tokens) - 1}⟧"

    return TOKEN_RE.sub(replace, text), tokens


def split_tokens(text: str) -> list[tuple[bool, str]]:
    """Split text into ordinary text and exact game-control tokens."""
    parts: list[tuple[bool, str]] = []
    cursor = 0
    for match in TOKEN_RE.finditer(text):
        if match.start() > cursor:
            parts.append((False, text[cursor:match.start()]))
        parts.append((True, match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        parts.append((False, text[cursor:]))
    return parts


def restore_tokens(text: str, tokens: list[str]) -> str:
    result = text
    marker_ids = [int(match.group(1)) for match in MARKER_RE.finditer(result)]
    expected_ids = list(range(len(tokens)))
    if marker_ids:
        if marker_ids != expected_ids:
            raise ValueError(
                f"번역기가 보호 토큰 순서·개수를 변경했습니다: {marker_ids} (필요: {expected_ids})"
            )
        result = MARKER_RE.sub(lambda match: tokens[int(match.group(1))], result)
    found_tokens = [match.group(0) for match in TOKEN_RE.finditer(result)]
    if found_tokens != tokens:
        missing = next(
            (index for index, token in enumerate(tokens) if index >= len(found_tokens) or found_tokens[index] != token),
            len(found_tokens),
        )
        raise ValueError(f"번역기가 보호 토큰 #{missing}의 내용·순서·개수를 변경했습니다.")
    if re.search(r"GKO\d+", result):
        raise ValueError("번역기가 보호 토큰 형식을 손상했습니다.")
    return result
