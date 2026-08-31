from __future__ import annotations

import configparser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gameko.engines import unity
from gameko.model import Detection, project_path


class UnityFontTests(unittest.TestCase):
    def test_config_uses_noto_for_ugui_and_tmp(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "AutoTranslatorConfig.ini"
            config_path.write_text(
                "[Behaviour]\nFallbackFontTextMeshPro=korean-tmp-font-bundle\n",
                encoding="utf-8",
            )

            unity._write_config(config_path)

            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read(config_path, encoding="utf-8")
            self.assertEqual(config["Behaviour"]["OverrideFont"], unity.NOTO_FONT_NAME)
            self.assertEqual(
                config["Behaviour"]["OverrideFontTextMeshPro"],
                unity.NOTO_FONT_NAME,
            )
            self.assertEqual(
                config["Behaviour"]["FallbackFontTextMeshPro"],
                unity.NOTO_FONT_NAME,
            )
            self.assertEqual(config["General"]["FromLanguage"], "auto")

    def test_config_accepts_selected_source_language(self):
        with tempfile.TemporaryDirectory() as temp:
            for source_language in ("ja", "en"):
                config_path = Path(temp) / f"Config-{source_language}.ini"
                unity._write_config(config_path, source_language)
                config = configparser.ConfigParser(interpolation=None)
                config.optionxform = str
                config.read(config_path, encoding="utf-8")
                self.assertEqual(
                    config["General"]["FromLanguage"], source_language
                )
            with self.assertRaisesRegex(ValueError, "auto, ja, en"):
                unity._write_config(Path(temp) / "invalid.ini", "ko")

    def test_config_uses_external_tmp_bundle_when_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "Config.ini"
            unity._write_config(config_path, "ja", unity.TMP_FONT_BUNDLE_FILE)
            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read(config_path, encoding="utf-8")
            self.assertEqual(config["Behaviour"]["OverrideFontTextMeshPro"], "")
            self.assertEqual(
                config["Behaviour"]["FallbackFontTextMeshPro"],
                unity.TMP_FONT_BUNDLE_FILE,
            )

    def test_tmp_bundle_requires_exact_unity_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            data_dir = root / "Demo_Data"
            data_dir.mkdir(parents=True)
            project = project_path(root)
            project.mkdir()
            managers = data_dir / "globalgamemanagers"
            managers.write_bytes(b"header\x002020.3.48f1\x00payload")
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(data_dir)}
            )
            self.assertEqual(unity.detect_unity_version(detection), "2020.3.48f1")
            mismatch = unity._install_experimental_tmp_font(
                detection, project, set(), True
            )
            self.assertEqual(mismatch["status"], "version_mismatch")
            self.assertFalse((root / unity.TMP_FONT_BUNDLE_FILE).exists())

            forced = unity._install_experimental_tmp_font(
                detection, project, set(), True, force=True
            )
            self.assertEqual(forced["status"], "installed")
            self.assertTrue(forced["forced"])
            self.assertTrue((root / unity.TMP_FONT_BUNDLE_FILE).is_file())

            detection.details["unity_version"] = unity.TMP_FONT_BUNDLE_UNITY_VERSION
            installed = unity._install_experimental_tmp_font(
                detection, project, set(), True
            )
            self.assertEqual(installed["status"], "installed")
            self.assertGreater((root / unity.TMP_FONT_BUNDLE_FILE).stat().st_size, 1_000_000)

    def test_xunity_install_uses_provided_tmp_bundle_on_exact_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            data_dir = root / "Demo_Data"
            plugin = data_dir / "Managed" / "XUnity.AutoTranslator.Plugin.Core.dll"
            plugin.parent.mkdir(parents=True)
            plugin.write_bytes(b"existing")
            (data_dir / "globalgamemanagers").write_bytes(
                b"header\x00" + unity.TMP_FONT_BUNDLE_UNITY_VERSION.encode("ascii") + b"\x00"
            )
            (root / "AutoTranslator").mkdir()
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(data_dir), "exe": ""}
            )
            font_state = {
                "name": unity.NOTO_FONT_NAME,
                "path": "GameKO.Fonts/NotoSansCJKkr-Regular.otf",
                "license_path": "GameKO.Fonts/NotoSansKR-OFL.txt",
                "registry_value": "test",
                "font_added": False,
                "license_added": False,
            }

            with patch.object(unity, "_install_noto_font", return_value=font_state):
                self.assertEqual(unity.install(detection), "existing")

            state = json.loads(
                (project_path(root) / "unity_install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["tmp_font"]["status"], "installed")
            self.assertTrue((root / unity.TMP_FONT_BUNDLE_FILE).is_file())
            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read(root / "AutoTranslator" / "Config.ini", encoding="utf-8")
            self.assertEqual(
                config["Behaviour"]["FallbackFontTextMeshPro"],
                unity.TMP_FONT_BUNDLE_FILE,
            )

    def test_source_language_inference_is_conservative(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)

            english_root = base / "Sample Game [English]"
            english_data = english_root / "Sample Game_Data"
            (english_data / "StreamingAssets").mkdir(parents=True)
            self.assertEqual(
                unity.infer_unity_source_language(english_root, english_data), "en"
            )

            locale_root = base / "LocalizedBuild"
            locale_data = locale_root / "LocalizedBuild_Data"
            (locale_data / "StreamingAssets" / "Localization" / "en-US").mkdir(
                parents=True
            )
            self.assertEqual(
                unity.infer_unity_source_language(locale_root, locale_data), "en"
            )

            japanese_root = base / "MysteryBuild"
            japanese_data = japanese_root / "MysteryBuild_Data"
            japanese_data.mkdir(parents=True)
            (japanese_data / "app.info").write_text(
                "開発会社\n日本語ゲーム", encoding="utf-8"
            )
            self.assertEqual(
                unity.infer_unity_source_language(japanese_root, japanese_data), "ja"
            )

            ambiguous_root = base / "UnknownBuild"
            ambiguous_root.mkdir()
            self.assertEqual(
                unity.infer_unity_source_language(ambiguous_root), "ja"
            )
            self.assertEqual(
                unity.infer_unity_source_language(
                    ambiguous_root, metadata={"original_language": "English"}
                ),
                "en",
            )

            # Explicit original/default metadata wins even when both optional
            # language packs are installed.
            mixed_data = ambiguous_root / "UnknownBuild_Data"
            (mixed_data / "StreamingAssets" / "locales" / "en").mkdir(parents=True)
            (mixed_data / "StreamingAssets" / "locales" / "ja").mkdir(parents=True)
            self.assertEqual(
                unity.infer_unity_source_language(
                    ambiguous_root, mixed_data, {"default_language": "English"}
                ),
                "en",
            )

    def test_install_resolves_auto_language_and_records_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo [English]"
            data_dir = root / "Demo_Data"
            plugin = data_dir / "Managed" / "XUnity.AutoTranslator.Plugin.Core.dll"
            plugin.parent.mkdir(parents=True)
            plugin.write_bytes(b"existing")
            (root / "AutoTranslator").mkdir()
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(data_dir), "exe": ""}
            )
            font_state = {
                "name": unity.NOTO_FONT_NAME,
                "path": "GameKO.Fonts/NotoSansCJKkr-Regular.otf",
                "license_path": "GameKO.Fonts/NotoSansKR-OFL.txt",
                "registry_value": "test",
                "font_added": False,
                "license_added": False,
            }

            with patch.object(unity, "_install_noto_font", return_value=font_state):
                self.assertEqual(unity.install(detection), "existing")

            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read(root / "AutoTranslator" / "Config.ini", encoding="utf-8")
            self.assertEqual(config["General"]["FromLanguage"], "en")
            state = json.loads(
                (project_path(root) / "unity_install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["source_language"], "en")

    def test_repeated_install_preserves_first_config_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            data_dir = root / "Demo_Data"
            plugin = data_dir / "Managed" / "XUnity.AutoTranslator.Plugin.Core.dll"
            plugin.parent.mkdir(parents=True)
            plugin.write_bytes(b"existing")
            config_path = root / "AutoTranslator" / "Config.ini"
            config_path.parent.mkdir()
            original = "[General]\nLanguage=en\n"
            config_path.write_text(original, encoding="utf-8")
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(data_dir), "exe": ""}
            )
            font_state = {
                "name": unity.NOTO_FONT_NAME,
                "path": "GameKO.Fonts/NotoSansCJKkr-Regular.otf",
                "license_path": "GameKO.Fonts/NotoSansKR-OFL.txt",
                "registry_value": "test",
                "font_added": False,
                "license_added": False,
            }

            with patch.object(unity, "_install_noto_font", return_value=font_state):
                unity.install(detection, source_language="ja")
                unity.install(detection, source_language="en")

            backup = project_path(root) / "backup" / "unity_overwritten" / "AutoTranslator" / "Config.ini"
            self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_config_replaces_legacy_font_values(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "Config.ini"
            config_path.write_text(
                "[Behaviour]\nFallbackFontTextMeshPro=Malgun Gothic\n",
                encoding="utf-8",
            )

            unity._write_config(config_path)

            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read(config_path, encoding="utf-8")
            self.assertEqual(
                config["Behaviour"]["FallbackFontTextMeshPro"], unity.NOTO_FONT_NAME
            )

    def test_font_assets_are_installed_and_restore_unregisters_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "UnityGame"
            root.mkdir()
            project = project_path(root)
            project.mkdir()

            with patch.object(unity, "_register_font") as register:
                font_state = unity._install_noto_font(root, project, set())

            font_path = root / font_state["path"]
            license_path = root / font_state["license_path"]
            self.assertTrue(font_path.is_file())
            self.assertTrue(license_path.is_file())
            self.assertGreater(font_path.stat().st_size, 1_000_000)
            self.assertIn("SIL Open Font License", license_path.read_text(encoding="utf-8"))
            self.assertEqual(font_state["name"], "Noto Sans CJK KR")
            register.assert_called_once_with(font_path, font_state["registry_value"])

            state = {
                "added_files": [],
                "added_dirs": [],
                "backed_up_files": [],
                "font": font_state,
            }
            (project / "unity_install.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            with patch.object(unity, "_unregister_font") as unregister:
                self.assertEqual(unity.restore(root), 2)

            unregister.assert_called_once_with(
                font_path.resolve(), font_state["registry_value"]
            )
            self.assertFalse(font_path.exists())
            self.assertFalse(license_path.exists())

    def test_old_medium_ttf_install_migrates_to_provided_cjk_otf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "UnityGame"
            old_font = root / "GameKO.Fonts" / "NotoSansKR-Medium.ttf"
            old_font.parent.mkdir(parents=True)
            old_font.write_bytes(b"old-owned-font")
            project = project_path(root)
            project.mkdir()
            previous = {
                "path": "GameKO.Fonts/NotoSansKR-Medium.ttf",
                "registry_value": "GameKO Noto Sans KR old (TrueType)",
                "font_added": True,
                "license_added": False,
            }

            with (
                patch.object(unity, "_register_font"),
                patch.object(unity, "_unregister_font") as unregister,
            ):
                state = unity._install_noto_font(
                    root, project, set(), previous
                )

            self.assertFalse(old_font.exists())
            self.assertTrue((root / state["path"]).is_file())
            self.assertEqual(
                Path(state["path"]), Path("GameKO.Fonts/NotoSansCJKkr-Regular.otf")
            )
            unregister.assert_called_once_with(
                old_font, previous["registry_value"]
            )


if __name__ == "__main__":
    unittest.main()
