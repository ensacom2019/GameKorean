from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gameko.engines import gamemaker
from gameko.model import Detection, Entry, project_path


class GameMakerFontTests(unittest.TestCase):
    def make_game(self, parent: Path) -> tuple[Path, Detection, list[Entry]]:
        root = parent / "GameMakerGame"
        root.mkdir()
        data_file = root / "data.win"
        data_file.write_bytes(b"ORIGINAL-DATA")
        project = project_path(root)
        project.mkdir()
        (project / "gamemaker_strings.json").write_text(
            json.dumps(["Hello, player!"], ensure_ascii=False), encoding="utf-8"
        )
        entry = Entry.create("Hello, player!", "data.win", "/Strings/0")
        entry.target = "안녕, 플레이어!"
        detection = Detection("gamemaker", str(root), "data-file", {"data_file": str(data_file)})
        return root, detection, [entry]

    def test_required_font_characters_collects_printable_bmp_including_punctuation(self):
        entry = Entry.create("Hello", "data.win", "/Strings/0")
        entry.target = "안녕 안녕—世界ㄱᄀ"
        self.assertEqual(gamemaker._required_font_characters([entry]), "ᄀ—ㄱ世界녕안")

    def test_apply_injects_font_in_separate_verified_patch_and_restore_cleans_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root, detection, entries = self.make_game(Path(temp))
            commands: list[list[str]] = []

            def fake_run(command, **kwargs):
                command = [str(value) for value in command]
                commands.append(command)
                if command[1] == "info":
                    return subprocess.CompletedProcess(command, 0, "ok", "")
                output = Path(command[command.index("-o") + 1])
                if command[2].endswith("data.win"):
                    output.write_bytes(b"STRINGS-PATCHED")
                else:
                    output.write_bytes(b"FONT-PATCHED")
                    report = Path(kwargs["env"]["GAMEKO_FONT_REPORT"])
                    report.write_text(json.dumps({
                        "format": 1,
                        "required_glyphs": 7,
                        "patched": ["fnt_main: Noto Sans CJK KR 글리프 7개 추가"],
                        "covered": [],
                        "skipped": [],
                        "errors": [],
                    }), encoding="utf-8")
                    self.assertTrue(Path(kwargs["env"]["GAMEKO_FONT"]).is_file())
                    self.assertTrue(Path(kwargs["env"]["GAMEKO_FONT_CHARS"]).is_file())
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch.object(gamemaker, "_ensure_tool", return_value=Path(temp) / "UndertaleModCli.exe"), \
                 patch.object(gamemaker.subprocess, "run", side_effect=fake_run):
                self.assertEqual(gamemaker.apply(root, detection, entries), 1)

            self.assertEqual((root / "data.win").read_bytes(), b"FONT-PATCHED")
            self.assertTrue((root / gamemaker.FONT_DIRNAME / gamemaker.FONT_FILENAME).is_file())
            self.assertFalse((root / gamemaker.FONT_DIRNAME / gamemaker.FONT_WARNING_FILENAME).exists())
            self.assertIn("ImageMagick.Drawing", (project_path(root) / "gm_font_patch.csx").read_text(encoding="utf-8"))
            self.assertEqual([command[1] for command in commands], ["load", "info", "load", "info"])

            self.assertGreater(gamemaker.restore(root, detection), 1)
            self.assertEqual((root / "data.win").read_bytes(), b"ORIGINAL-DATA")
            self.assertFalse((root / gamemaker.FONT_DIRNAME).exists())

    def test_font_stage_failure_keeps_translated_data_and_writes_clear_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            root, detection, entries = self.make_game(Path(temp))

            def fake_run(command, **kwargs):
                command = [str(value) for value in command]
                if command[1] == "info":
                    return subprocess.CompletedProcess(command, 0, "ok", "")
                output = Path(command[command.index("-o") + 1])
                if command[2].endswith("data.win"):
                    output.write_bytes(b"STRINGS-PATCHED")
                    return subprocess.CompletedProcess(command, 0, "ok", "")
                return subprocess.CompletedProcess(command, 1, "", "font script compile failed")

            with patch.object(gamemaker, "_ensure_tool", return_value=Path(temp) / "UndertaleModCli.exe"), \
                 patch.object(gamemaker.subprocess, "run", side_effect=fake_run):
                self.assertEqual(gamemaker.apply(root, detection, entries), 1)

            self.assertEqual((root / "data.win").read_bytes(), b"STRINGS-PATCHED")
            warning = root / gamemaker.FONT_DIRNAME / gamemaker.FONT_WARNING_FILENAME
            self.assertTrue(warning.is_file())
            warning_text = warning.read_text(encoding="utf-8")
            self.assertIn("문자열은 data.win에 적용", warning_text)
            self.assertIn("실제 화면에서 한글 표시를 확인", warning_text)
            report = json.loads((project_path(root) / gamemaker.FONT_REPORT_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue(report["errors"])
            self.assertIn("문자열 패치만 사용", report["skipped"][0])

            gamemaker.restore(root, detection)
            self.assertFalse((root / gamemaker.FONT_DIRNAME).exists())


if __name__ == "__main__":
    unittest.main()
