from __future__ import annotations

import unittest

from gameko.language import LanguageSelection, select_original_language
from gameko.model import Detection, Entry


def make_entry(text: str, language: str, file: str = "data.win", context: str = "") -> Entry:
    entry = Entry.create(text, file, "/Strings/0", context)
    entry.source_lang = language
    return entry


class OriginalLanguageSelectionTests(unittest.TestCase):
    def test_clear_english_only_wins_even_with_conflicting_metadata(self):
        entries = [make_entry("Start game", "en"), make_entry("Options", "en")]
        detection = Detection("gamemaker", "C:/Games/Sample", details={"original_language": "ja"})
        result = select_original_language(entries, detection)
        self.assertEqual(result, LanguageSelection("en", 2, 0, "single_language:en"))
        self.assertEqual(result.counts, {"en": 2, "ja": 0})
        self.assertEqual(result.language, "en")

    def test_clear_japanese_only_is_selected(self):
        result = select_original_language([make_entry("はじめから", "ja")])
        self.assertEqual(result.selected, "ja")
        self.assertEqual((result.en_count, result.ja_count), (0, 1))
        self.assertEqual(result.reason, "single_language:ja")

    def test_user_preference_overrides_counts_and_detection(self):
        entries = [make_entry("はじめから", "ja"), make_entry("Start", "en")]
        result = select_original_language(entries, preference="en")
        self.assertEqual(result.selected, "en")
        self.assertEqual(result.counts, {"en": 1, "ja": 1})
        self.assertEqual(result.reason, "override:user_selected_en")
        with self.assertRaises(ValueError):
            select_original_language(entries, preference="ko")  # type: ignore[arg-type]

    def test_mixed_entries_use_detection_original_language_metadata(self):
        entries = [make_entry("Start", "en"), make_entry("開始", "ja")]
        detection = Detection("gamemaker", "C:/Games/Mixed", details={"source_language": "English"})
        result = select_original_language(entries, detection)
        self.assertEqual(result.selected, "en")
        self.assertEqual(result.counts, {"en": 1, "ja": 1})
        self.assertIn("metadata:source_language=en", result.reason)

    def test_default_language_pack_record_is_original_evidence(self):
        entries = [make_entry("Start", "en"), make_entry("開始", "ja")]
        metadata = {
            "language_packs": [
                {"language": "en", "isDefault": False},
                {"locale": "ja-JP", "isDefault": True},
            ]
        }
        result = select_original_language(entries, metadata=metadata)
        self.assertEqual(result.selected, "ja")
        self.assertIn("isdefault", result.reason)

    def test_rpgmaker_base_japanese_and_english_pack_selects_japanese(self):
        entries = [
            make_entry("開始", "ja", "data/System.json"),
            make_entry("町へようこそ", "ja", "data/Map001.json"),
            make_entry("Start", "en", "languages/en/System.json"),
        ]
        detection = Detection("rpgmaker", "C:/Games/RPG", "MZ", {})
        result = select_original_language(entries, detection)
        self.assertEqual(result.selected, "ja")
        self.assertIn("base=ja, en language-pack", result.reason)

    def test_rpgmaker_base_english_and_japanese_pack_selects_english(self):
        entries = [
            make_entry("Start", "en", "data/System.json"),
            make_entry("Welcome", "en", "data/Map001.json"),
            make_entry("開始", "ja", "locales/ja/System.json"),
        ]
        result = select_original_language(entries, engine="rpgmaker")
        self.assertEqual(result.selected, "en")
        self.assertIn("base=en, ja language-pack", result.reason)

    def test_base_language_marker_in_filename_beats_pack_layout(self):
        entries = [
            make_entry("Start", "en", "locales/en/base_english.json"),
            make_entry("開始", "ja", "locales/ja/dialogue.json"),
        ]
        result = select_original_language(entries, engine="gamemaker")
        self.assertEqual(result.selected, "en")
        self.assertIn("English base/original", result.reason)

    def test_mixed_gamemaker_data_win_without_origin_evidence_prefers_japanese(self):
        entries = [make_entry("Start", "en"), make_entry("開始", "ja")]
        detection = Detection("gamemaker", "C:/Games/Mixed", "data-file", {"data_file": "data.win"})
        result = select_original_language(entries, detection)
        self.assertEqual(result.selected, "ja")
        self.assertEqual(result.reason, "fallback:japanese_priority_insufficient_origin_evidence")

    def test_conflicting_metadata_and_invalid_entries_fall_back_safely(self):
        entries = (
            item
            for item in [
                {"source": "Start", "source_lang": "en", "file": "data.win"},
                {"source": "開始", "source_lang": "ja", "file": "data.win"},
                {"source": None, "source_lang": "unknown"},
                object(),
            ]
        )
        metadata = {"original_language": "en", "source_language": "ja"}
        result = select_original_language(entries, metadata=metadata)
        self.assertEqual(result.selected, "ja")
        self.assertEqual(result.counts, {"en": 1, "ja": 1})
        self.assertTrue(result.reason.startswith("fallback:"))

    def test_empty_entries_can_use_detection_metadata_or_japanese_fallback(self):
        detection = {"engine": "gamemaker", "root": "C:/Games/Empty", "details": {"base_locale": "en-US"}}
        selected = select_original_language([], detection)
        self.assertEqual(selected.selected, "en")
        self.assertEqual(selected.counts, {"en": 0, "ja": 0})
        self.assertEqual(select_original_language([]).selected, "ja")


if __name__ == "__main__":
    unittest.main()
