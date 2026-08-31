from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error

import gameko
from gameko.detect import detect_game
from gameko.engines import gamemaker, rpgmaker, unity
from gameko.gui import error_reason, format_duration, format_error_message, write_error_report
from gameko.model import Detection, Entry, load_entries, save_project
from gameko.service import apply_game, export_csv, find_game, import_csv
from gameko.text import protect_tokens, restore_tokens
from gameko.translators import (
    GoogleWebTranslator, Translator, make_translator, translate_preserving_tokens,
    translate_project,
)


class GameKoTests(unittest.TestCase):
    MV_ORIGINAL_FONT_CSS = """@font-face {
    font-family: GameFont;
    src: url(\"mplus-1m-regular.ttf\");
}

@font-face {
    font-family: OtherFont;
    src: url(\"other.woff\");
}
"""

    def test_ainforge_branding_assets_and_metadata(self):
        assets = Path(gameko.__file__).parent / "assets"
        self.assertEqual(gameko.__author__, "AINFORGE")
        self.assertEqual(gameko.__creator__, "AINFORGE")
        self.assertGreater((assets / "AINFORGE.png").stat().st_size, 10_000)
        self.assertGreater((assets / "AINFORGE.ico").stat().st_size, 10_000)

    def test_all_engines_use_provided_noto_cjk_regular_otf(self):
        assets = Path(gameko.__file__).parent / "assets"
        font = assets / "NotoSansCJKkr-Regular.otf"
        self.assertEqual(rpgmaker.FONT_FILENAME, font.name)
        self.assertEqual(gamemaker.FONT_FILENAME, font.name)
        self.assertEqual(unity.NOTO_FONT_FILE, font.name)
        self.assertEqual(unity.NOTO_FONT_NAME, "Noto Sans CJK KR")
        self.assertEqual(
            hashlib.sha256(font.read_bytes()).hexdigest().upper(),
            "6BCB2A0703AA137E874FC2DFFA85F6C21BA9A67FA329E81B8C801663AF7E992A",
        )

    def test_progress_duration_format(self):
        self.assertEqual(format_duration(0), "00:00:00")
        self.assertEqual(format_duration(65.9), "00:01:05")
        self.assertEqual(format_duration(3661), "01:01:01")

    def test_error_message_explains_rate_limit_and_shows_report(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "limited", {}, None
        )
        report = Path("C:/Game/gameko_project/error_logs/error.txt")
        message = format_error_message("문장 번역", error, report)
        self.assertIn("실패 단계: 문장 번역", message)
        self.assertIn("HTTPError", message)
        self.assertIn("요청 제한(429)", message)
        self.assertIn(str(report), message)

    def test_error_report_saves_full_traceback_and_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "BrokenGame"
            root.mkdir()
            error = PermissionError("data.win 사용 중")
            report = write_error_report(
                str(root), "번역 적용", error, "TRACEBACK DETAILS",
                {"TMP 버전 무시 강제 적용": True},
            )
            self.assertIsNotNone(report)
            body = report.read_text(encoding="utf-8")
            self.assertIn("파일 쓰기 권한", error_reason(error))
            self.assertIn("data.win 사용 중", body)
            self.assertIn("TRACEBACK DETAILS", body)
            self.assertIn("TMP 버전 무시 강제 적용: True", body)

    def test_web_provider_is_default_no_key_translator(self):
        self.assertIsInstance(make_translator("web"), GoogleWebTranslator)
        self.assertIsInstance(make_translator("google"), GoogleWebTranslator)

    def test_web_rate_limit_reduces_concurrency_and_falls_back(self):
        translator = GoogleWebTranslator()

        def limited(_text, _language):
            raise urllib.error.HTTPError("https://example.invalid", 429, "limited", {}, None)

        translator._google_chrome = limited
        translator._google_web = lambda text, _language: "번역:" + text
        self.assertEqual(translator.translate("hello", "en"), "번역:hello")
        self.assertEqual(translator.current_concurrency, 2)

    def test_translation_project_uses_four_workers_and_checkpoints(self):
        class ConcurrentTranslator(Translator):
            max_workers = 4

            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.high_water = 0

            def translate(self, text, _source_lang):
                with self.lock:
                    self.active += 1
                    self.high_water = max(self.high_water, self.active)
                time.sleep(0.03)
                with self.lock:
                    self.active -= 1
                return "번역:" + text

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ConcurrentGame"
            root.mkdir()
            detection = Detection("rpgmaker", str(root), "MV", {})
            entries = [Entry.create(f"Sentence number {index}.", "Data.json", f"/{index}") for index in range(12)]
            for entry in entries:
                entry.source_lang = "en"
            save_project(root, detection, entries)
            translator = ConcurrentTranslator()
            self.assertEqual(translate_project(root, translator), (12, 12))
            self.assertEqual(translator.high_water, 4)
            self.assertTrue((root / "gameko_project" / "translation_memory.json").is_file())
            self.assertTrue(all(entry.target.startswith("번역:") for entry in load_entries(root)))

    def make_rpg(self, root: Path) -> Detection:
        data = root / "www" / "data"
        data.mkdir(parents=True)
        fonts = root / "www" / "fonts"
        fonts.mkdir(parents=True)
        (fonts / "gamefont.css").write_text(self.MV_ORIGINAL_FONT_CSS, encoding="utf-8")
        (data / "System.json").write_text(json.dumps({
            "gameTitle": "Sample Game", "currencyUnit": "Gold",
            "terms": {"commands": ["Fight", "Escape"]},
            "elements": ["", "Fire"],
        }), encoding="utf-8")
        (data / "Actors.json").write_text(json.dumps([None, {"name": "Harold", "nickname": "Hero", "profile": "A brave hero."}]), encoding="utf-8")
        (data / "Map001.json").write_text(json.dumps({
            "displayName": "First Town",
            "events": [None, {"pages": [{"list": [
                {"code": 101, "parameters": ["Actor1", 0, 0, 2, "Narrator"]},
                {"code": 401, "parameters": ["Hello, \\N[1]!"]},
                {"code": 102, "parameters": [["Yes", "No"], -1, 0, 2, 0]},
            ]}]}],
        }), encoding="utf-8")
        return detect_game(root)

    def test_detect_extract_apply_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "MyGame"
            detection = self.make_rpg(root)
            self.assertEqual(detection.engine, "rpgmaker")
            entries = rpgmaker.extract(detection)
            sources = {e.source for e in entries}
            self.assertTrue({"Sample Game", "Fight", "Fire", "Hello, \\N[1]!", "Yes", "No"} <= sources)
            for entry in entries:
                if entry.source == "Hello, \\N[1]!":
                    entry.target = "안녕, \\N[1]!"
            save_project(root, detection, entries)
            self.assertEqual(rpgmaker.apply(root, entries), 1)
            patched = json.loads((root / "www" / "data" / "Map001.json").read_text(encoding="utf-8"))
            self.assertEqual(patched["events"][1]["pages"][0]["list"][1]["parameters"][0], "안녕, \\N[1]!")
            font = root / "www" / "fonts" / rpgmaker.FONT_FILENAME
            font_css = root / "www" / "fonts" / "gamefont.css"
            self.assertTrue(font.is_file())
            self.assertIn(rpgmaker.FONT_FILENAME, font_css.read_text(encoding="utf-8"))
            self.assertIn("OtherFont", font_css.read_text(encoding="utf-8"))
            self.assertGreater(rpgmaker.restore(root), 0)
            restored = json.loads((root / "www" / "data" / "Map001.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["events"][1]["pages"][0]["list"][1]["parameters"][0], "Hello, \\N[1]!")
            self.assertFalse(font.exists())
            self.assertEqual(font_css.read_text(encoding="utf-8"), self.MV_ORIGINAL_FONT_CSS)

    def test_rpgmaker_detection_reads_default_locale_for_language_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "LocalizedGame"
            detection = self.make_rpg(root)
            system_path = Path(detection.details["data_dir"]) / "System.json"
            system = json.loads(system_path.read_text(encoding="utf-8"))
            system["locale"] = "en_US"
            system_path.write_text(json.dumps(system), encoding="utf-8")
            self.assertEqual(detect_game(root).details["default_language"], "en_US")

    def test_rpgmaker_mz_font_install_and_restore_without_translations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "MZGame"
            data = root / "data"
            data.mkdir(parents=True)
            js = root / "js"
            js.mkdir()
            (js / "rmmz_core.js").write_text("// RPG Maker MZ", encoding="utf-8")
            original_system = {
                "gameTitle": "Sample MZ Game",
                "currencyUnit": "Gold",
                "advanced": {
                    "mainFontFilename": "mplus-1m-regular.woff",
                    "numberFontFilename": "mplus-1m-regular.woff",
                    "fallbackFonts": "Verdana, sans-serif",
                    "fontSize": 26,
                },
                "terms": {"commands": ["Fight", "Escape"]},
            }
            (data / "System.json").write_text(json.dumps(original_system), encoding="utf-8")
            (data / "Map001.json").write_text(json.dumps({"displayName": "First Town", "events": []}), encoding="utf-8")

            detection = detect_game(root)
            self.assertEqual(detection.variant, "MZ")
            entries = rpgmaker.extract(detection)
            save_project(root, detection, entries)
            self.assertEqual(rpgmaker.apply(root, entries), 0)

            patched_system = json.loads((data / "System.json").read_text(encoding="utf-8"))
            self.assertEqual(patched_system["advanced"]["mainFontFilename"], rpgmaker.FONT_FILENAME)
            self.assertEqual(patched_system["advanced"]["numberFontFilename"], "mplus-1m-regular.woff")
            self.assertEqual(patched_system["advanced"]["fallbackFonts"], "Verdana, sans-serif")
            font = root / "fonts" / rpgmaker.FONT_FILENAME
            self.assertTrue(font.is_file())

            # 번역 대상이 없어 일반 JSON 백업 폴더가 생기지 않아도 폰트만 복원됩니다.
            self.assertFalse((root / "gameko_project" / "backup").exists())
            self.assertGreater(rpgmaker.restore(root), 0)
            self.assertEqual(json.loads((data / "System.json").read_text(encoding="utf-8")), original_system)
            self.assertFalse(font.exists())
            self.assertFalse((root / "fonts").exists())

    def test_find_single_child_and_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            detection = self.make_rpg(parent / "Nested")
            self.assertEqual(find_game(parent).root, detection.root)
            tool_folder = parent / "Nested" / "GameKO"
            tool_folder.mkdir()
            self.assertEqual(find_game(tool_folder).root, detection.root)
        protected, tokens = protect_tokens("Hello \\N[1], {name}!")
        self.assertEqual(restore_tokens(protected.replace("Hello", "안녕"), tokens), "안녕 \\N[1], {name}!")
        with self.assertRaises(ValueError):
            restore_tokens("안녕", tokens)

    def test_dropped_control_marker_retries_around_original_tag(self):
        class MarkerDroppingTranslator(Translator):
            def translate(self, text: str, _source_lang: str) -> str:
                if "GKO" in text:
                    return "보호 표식을 삭제한 잘못된 결과"
                if text.startswith("はあ"):
                    return "하아……"
                if text.startswith("この鏡"):
                    return "이 거울은 솔직히 생각하고 싶지 않아."
                return text

        source = "はあ……<waitfor=0.5>この鏡のこと、正直考えたくないな。"
        translated = translate_preserving_tokens(
            MarkerDroppingTranslator(), source, "ja"
        )
        self.assertEqual(
            translated,
            "하아……<waitfor=0.5>이 거울은 솔직히 생각하고 싶지 않아.",
        )

    def test_tmp_rich_text_tags_are_exact_and_ordered(self):
        source = "<color=#FF8800>赤<#00FF00>色</color><sprite=2>"
        protected, tokens = protect_tokens(source)
        self.assertEqual(
            tokens,
            ["<color=#FF8800>", "<#00FF00>", "</color>", "<sprite=2>"],
        )
        self.assertEqual(
            restore_tokens(protected.replace("赤", "빨간").replace("色", "색"), tokens),
            "<color=#FF8800>빨간<#00FF00>색</color><sprite=2>",
        )
        with self.assertRaisesRegex(ValueError, "순서·개수"):
            restore_tokens("⟦GKO1⟧색⟦GKO0⟧⟦GKO2⟧⟦GKO3⟧", tokens)
        with self.assertRaisesRegex(ValueError, "순서·개수"):
            restore_tokens("⟦GKO0⟧⟦GKO0⟧⟦GKO1⟧⟦GKO2⟧⟦GKO3⟧", tokens)

    def test_manual_dialogue_csv_export_import_and_reapply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "MyGame"
            detection = self.make_rpg(root)
            save_project(root, detection, rpgmaker.extract(detection))
            editable = Path(temp) / "my_dialogue.csv"
            self.assertEqual(export_csv(root, editable), editable.resolve())

            def set_target(value: str):
                with editable.open("r", encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.DictReader(fh))
                for row in rows:
                    if row["source"] == "Hello, \\N[1]!":
                        row["target"] = value
                        row["status"] = "edited"
                with editable.open("w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)

            def current_dialogue() -> str:
                data = json.loads((root / "www" / "data" / "Map001.json").read_text(encoding="utf-8"))
                return data["events"][1]["pages"][0]["list"][1]["parameters"][0]

            set_target("안녕, \\N[1]!")
            self.assertGreater(import_csv(root, editable), 0)
            self.assertEqual(apply_game(root), 1)
            self.assertEqual(current_dialogue(), "안녕, \\N[1]!")

            set_target("반가워, \\N[1]!")
            import_csv(root, editable)
            self.assertEqual(apply_game(root), 1)
            self.assertEqual(current_dialogue(), "반가워, \\N[1]!")

            set_target("")
            import_csv(root, editable)
            self.assertEqual(apply_game(root), 0)
            self.assertEqual(current_dialogue(), "Hello, \\N[1]!")

    def test_manual_dialogue_csv_rejects_wrong_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "MyGame"
            detection = self.make_rpg(root)
            save_project(root, detection, rpgmaker.extract(detection))
            editable = export_csv(root, Path(temp) / "dialogue.csv")
            with editable.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            rows[0]["source"] = "다른 게임의 원문"
            with editable.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "다른 게임/이전 추출본"):
                import_csv(root, editable)


if __name__ == "__main__":
    unittest.main()
