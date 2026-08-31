from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gameko import service
from gameko.model import Detection, Entry, load_entries, load_manifest, save_project
from gameko.translators import Translator


class RecordingTranslator(Translator):
    max_workers = 1

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def translate(self, text: str, source_lang: str) -> str:
        self.calls.append((text, source_lang))
        return f"번역:{text}"


def mixed_entries(*, with_targets: bool = False) -> list[Entry]:
    japanese = Entry.create("開始", "data.win", "/Strings/0", "Japanese dialogue")
    japanese.source_lang = "ja"
    english = Entry.create("Start", "data.win", "/Strings/1", "English dialogue")
    english.source_lang = "en"
    if with_targets:
        japanese.target = "시작(일본어)"
        english.target = "시작(영어)"
    return [japanese, english]


class ServiceLanguageIntegrationTests(unittest.TestCase):
    def make_project(self, parent: Path, engine: str = "rpgmaker", *, with_targets: bool = False) -> Path:
        root = parent / f"{engine}-game"
        root.mkdir()
        details = {"data_dir": str(root / "data")} if engine == "rpgmaker" else {"data_file": str(root / "data.win")}
        detection = Detection(engine, str(root), "MV" if engine == "rpgmaker" else "data-file", details)
        save_project(root, detection, mixed_entries(with_targets=with_targets))
        return root

    def test_auto_mixed_project_falls_back_to_japanese_and_leaves_english_untranslated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_project(Path(temp))
            translator = RecordingTranslator()
            with patch.object(service, "make_translator", return_value=translator):
                self.assertEqual(
                    service.translate_game(root, "mock", {}, source_language="auto"),
                    (1, 1),
                )

            self.assertEqual(translator.calls, [("開始", "ja")])
            entries = {entry.source_lang: entry for entry in load_entries(root)}
            self.assertEqual(entries["ja"].target, "번역:開始")
            self.assertEqual(entries["en"].target, "")
            manifest = load_manifest(root)
            self.assertEqual(manifest["source_language"], "ja")
            self.assertEqual(manifest["language_counts"], {"en": 1, "ja": 1})
            self.assertTrue(manifest["language_reason"].startswith("fallback:"))

    def test_explicit_english_translates_only_english(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_project(Path(temp))
            translator = RecordingTranslator()
            with patch.object(service, "make_translator", return_value=translator):
                self.assertEqual(
                    service.translate_game(root, "mock", {}, source_language="en"),
                    (1, 1),
                )

            self.assertEqual(translator.calls, [("Start", "en")])
            entries = {entry.source_lang: entry for entry in load_entries(root)}
            self.assertEqual(entries["en"].target, "번역:Start")
            self.assertEqual(entries["ja"].target, "")
            manifest = load_manifest(root)
            self.assertEqual(manifest["source_language"], "en")
            self.assertEqual(manifest["source_language_preference"], "en")
            self.assertEqual(manifest["language_reason"], "override:user_selected_en")

    def test_switching_back_to_auto_recomputes_instead_of_reusing_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_project(Path(temp))
            english = RecordingTranslator()
            with patch.object(service, "make_translator", return_value=english):
                service.translate_game(root, "mock", {}, source_language="en")

            automatic = RecordingTranslator()
            with patch.object(service, "make_translator", return_value=automatic):
                self.assertEqual(
                    service.translate_game(root, "mock", {}, source_language="auto"),
                    (1, 1),
                )

            self.assertEqual(automatic.calls, [("開始", "ja")])
            manifest = load_manifest(root)
            self.assertEqual(manifest["source_language"], "ja")
            self.assertEqual(manifest["source_language_preference"], "auto")

    def test_apply_filters_out_existing_targets_from_unselected_language(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            for engine in ("rpgmaker", "gamemaker"):
                with self.subTest(engine=engine):
                    root = self.make_project(parent, engine, with_targets=True)
                    received: list[Entry] = []

                    def fake_apply(*args):
                        entries = args[-1]
                        received.extend(entries)
                        return len(entries)

                    engine_module = service.rpgmaker if engine == "rpgmaker" else service.gamemaker
                    with patch.object(engine_module, "apply", side_effect=fake_apply) as apply_mock:
                        self.assertEqual(service.apply_game(root, source_language="auto"), 1)

                    self.assertEqual([entry.source_lang for entry in received], ["ja"])
                    self.assertEqual([entry.target for entry in received], ["시작(일본어)"])
                    self.assertEqual(apply_mock.call_count, 1)
                    # Filtering is for the engine boundary only; the other
                    # language remains in the editable translation project.
                    stored = {entry.source_lang: entry.target for entry in load_entries(root)}
                    self.assertEqual(stored["en"], "시작(영어)")


if __name__ == "__main__":
    unittest.main()
