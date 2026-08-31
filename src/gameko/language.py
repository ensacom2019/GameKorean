from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Literal

from .text import guess_language


SourceLanguage = Literal["en", "ja"]


@dataclass(frozen=True)
class LanguageSelection:
    """Original-language choice and the evidence used to make it."""

    selected: SourceLanguage
    en_count: int
    ja_count: int
    reason: str

    @property
    def language(self) -> SourceLanguage:
        """Readable compatibility alias for callers that prefer `language`."""
        return self.selected

    @property
    def counts(self) -> dict[SourceLanguage, int]:
        return {"en": self.en_count, "ja": self.ja_count}


_LANGUAGE_ALIASES: dict[str, SourceLanguage] = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "enus": "en",
    "engb": "en",
    "영어": "en",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "jajp": "ja",
    "日本語": "ja",
    "일본어": "ja",
}

_STRONG_LANGUAGE_KEYS = {
    "originallanguage",
    "originallang",
    "originallocale",
    "sourcelanguage",
    "sourcelang",
    "sourcelocale",
    "baselanguage",
    "baselang",
    "baselocale",
    "nativelanguage",
    "nativelang",
    "developmentlanguage",
}
_DEFAULT_LANGUAGE_KEYS = {
    "defaultlanguage",
    "defaultlang",
    "defaultlocale",
    "primarylanguage",
    "primarylang",
}
_LANGUAGE_VALUE_KEYS = {"language", "lang", "locale", "code", "id"}
_DEFAULT_FLAGS = {"default", "isdefault", "base", "isbase", "original", "isoriginal", "primary", "isprimary"}
_PACK_TOKENS = {"i18n", "lang", "langs", "language", "languages", "locale", "locales", "localization", "localisation", "translation", "translations"}
_BASE_TOKENS = {"base", "default", "original", "source", "native"}


def _normalized_token(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣一-龯ぁ-ヿ]+", "", str(value).strip().lower())


def _language_value(value: Any) -> SourceLanguage | None:
    if not isinstance(value, str):
        return None
    token = _normalized_token(value)
    direct = _LANGUAGE_ALIASES.get(token)
    if direct:
        return direct
    # Locale forms not covered by normalization above, such as en_AU or ja-JP.
    first = re.split(r"[-_.\s]", value.strip().lower(), maxsplit=1)[0]
    return _LANGUAGE_ALIASES.get(_normalized_token(first))


def _entry_value(entry: Any, name: str, default: Any = "") -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _entry_language(entry: Any) -> SourceLanguage | None:
    tagged = _language_value(_entry_value(entry, "source_lang", ""))
    if tagged:
        return tagged
    source = _entry_value(entry, "source", "")
    if not isinstance(source, str):
        return None
    guessed = guess_language(source)
    return guessed if guessed in {"en", "ja"} else None


def _iter_mappings(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        yield path, value
        for key, child in value.items():
            yield from _iter_mappings(child, path + (str(key),))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _iter_mappings(child, path + (str(index),))


def _metadata_language(metadata: Mapping[str, Any] | None) -> tuple[SourceLanguage, str] | None:
    if not isinstance(metadata, Mapping):
        return None
    evidence: list[tuple[int, SourceLanguage, str]] = []
    for path, node in _iter_mappings(metadata):
        normalized_path = {_normalized_token(part) for part in path}
        inside_pack = bool(normalized_path & _PACK_TOKENS)
        for key, value in node.items():
            normalized_key = _normalized_token(key)
            language = _language_value(value)
            location = ".".join((*path, str(key))) or str(key)
            if language and normalized_key in _STRONG_LANGUAGE_KEYS:
                evidence.append((100, language, location))
            elif language and normalized_key in _DEFAULT_LANGUAGE_KEYS and not inside_pack:
                evidence.append((80, language, location))

        # Common language-pack form: {"language": "ja", "isDefault": true}.
        flags = {
            _normalized_token(key)
            for key, value in node.items()
            if value is True and _normalized_token(key) in _DEFAULT_FLAGS
        }
        if flags:
            for key, value in node.items():
                if _normalized_token(key) not in _LANGUAGE_VALUE_KEYS:
                    continue
                language = _language_value(value)
                if language:
                    location = ".".join(path) or "metadata"
                    evidence.append((90, language, f"{location} ({sorted(flags)[0]})"))

    if not evidence:
        return None
    top_score = max(score for score, _, _ in evidence)
    strongest = [(language, location) for score, language, location in evidence if score == top_score]
    languages = {language for language, _ in strongest}
    if len(languages) != 1:
        return None
    language = strongest[0][0]
    locations = ", ".join(location for item_language, location in strongest if item_language == language)
    return language, f"metadata:{locations}={language}"


def _path_tokens(value: str) -> set[str]:
    return {
        _normalized_token(token)
        for token in re.split(r"[\\/._\-\s]+", value)
        if token
    }


def _path_language(value: str) -> SourceLanguage | None:
    languages = {_LANGUAGE_ALIASES[token] for token in _path_tokens(value) if token in _LANGUAGE_ALIASES}
    return next(iter(languages)) if len(languages) == 1 else None


def _file_layout_language(
    entries: list[Any],
    *,
    engine: str | None,
    game_path: str | Path | None,
) -> tuple[SourceLanguage, str] | None:
    # A game/directory explicitly labelled as the original or base language is
    # stronger than localization-pack layout evidence.
    if game_path is not None:
        path_text = str(game_path)
        tokens = _path_tokens(path_text)
        marked = _path_language(path_text)
        if marked and tokens & _BASE_TOKENS:
            return marked, f"path:base/original marker={marked}"

    unmarked = {"en": 0, "ja": 0}
    localized = {"en": 0, "ja": 0}
    explicit_base = {"en": 0, "ja": 0}
    for entry in entries:
        language = _entry_language(entry)
        if not language:
            continue
        file_value = _entry_value(entry, "file", "")
        context_value = _entry_value(entry, "context", "")
        location = " ".join(value for value in (file_value, context_value) if isinstance(value, str))
        tokens = _path_tokens(location)
        marker = _path_language(location)
        if marker == language and tokens & _BASE_TOKENS:
            explicit_base[language] += 1
        elif marker == language and tokens & _PACK_TOKENS:
            localized[language] += 1
        elif marker is None:
            unmarked[language] += 1

    if explicit_base["en"] and not explicit_base["ja"]:
        return "en", "file_layout:English base/original files"
    if explicit_base["ja"] and not explicit_base["en"]:
        return "ja", "file_layout:Japanese base/original files"

    # If one language lives in generic engine data and the other exclusively in
    # a named language pack, generic data is the best available original source.
    if unmarked["en"] and localized["ja"] and not localized["en"]:
        return "en", f"file_layout:{engine or 'generic'} base=en, ja language-pack"
    if unmarked["ja"] and localized["en"] and not localized["ja"]:
        return "ja", f"file_layout:{engine or 'generic'} base=ja, en language-pack"
    return None


def select_original_language(
    entries: Iterable[Any],
    detection: Any = None,
    *,
    preference: Literal["auto", "en", "ja"] = "auto",
    engine: str | None = None,
    game_path: str | Path | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> LanguageSelection:
    """Choose the original language for RPG Maker/GameMaker entry lists.

    Counts are per extracted entry, not per character.  Invalid or undecidable
    entries are ignored.  When mixed/unknown evidence cannot establish the
    original language, Japanese is intentionally preferred.
    """
    materialized = list(entries) if entries is not None else []
    languages = [_entry_language(entry) for entry in materialized]
    en_count = languages.count("en")
    ja_count = languages.count("ja")

    if preference not in {"auto", "en", "ja"}:
        raise ValueError(f"지원하지 않는 원문 언어 선택입니다: {preference}")
    if preference in {"en", "ja"}:
        return LanguageSelection(preference, en_count, ja_count, f"override:user_selected_{preference}")

    detection_details: Mapping[str, Any] | None = None
    if detection is not None:
        if isinstance(detection, Mapping):
            engine = engine or detection.get("engine")
            game_path = game_path or detection.get("root")
            details = detection.get("details")
        else:
            engine = engine or getattr(detection, "engine", None)
            game_path = game_path or getattr(detection, "root", None)
            details = getattr(detection, "details", None)
        if isinstance(details, Mapping):
            detection_details = details

    combined_metadata: dict[str, Any] = {}
    if detection_details:
        combined_metadata.update(detection_details)
    if isinstance(metadata, Mapping):
        combined_metadata.update(metadata)

    if en_count and not ja_count:
        return LanguageSelection("en", en_count, ja_count, "single_language:en")
    if ja_count and not en_count:
        return LanguageSelection("ja", en_count, ja_count, "single_language:ja")

    metadata_choice = _metadata_language(combined_metadata)
    if metadata_choice:
        language, reason = metadata_choice
        return LanguageSelection(language, en_count, ja_count, reason)

    layout_choice = _file_layout_language(materialized, engine=engine, game_path=game_path)
    if layout_choice:
        language, reason = layout_choice
        return LanguageSelection(language, en_count, ja_count, reason)

    return LanguageSelection("ja", en_count, ja_count, "fallback:japanese_priority_insufficient_origin_evidence")
