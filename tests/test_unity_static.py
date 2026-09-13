from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import UnityPy

from gameko.engines import unity_static
from gameko.model import Detection, project_path


FIXTURE = Path(__file__).parent / "fixtures" / "unity_dialogue.bundle"
ASSETS_FIXTURE = Path(__file__).parent / "fixtures" / "unity_dialogue.assets"


def _bundle_script(path: Path) -> str:
    with path.open("rb") as source:
        env = UnityPy.load(source)
        return next(
            str(obj.parse_as_object().m_Script)
            for obj in env.objects
            if obj.type.name == "TextAsset"
        )


class UnityStaticTests(unittest.TestCase):
    def test_addressables_catalog_bin_crc_is_disabled_for_modified_bundle(self):
        bundle_name = "localization-string-tables-japanese.bundle"
        prefix = bundle_name.encode("utf-8") + b"\0" * 20
        hash_offset = len(prefix)
        raw = prefix + b"0123456789abcdef0123456789abcdef" + struct.pack(
            "<IIII", hash_offset - 20, hash_offset, 0x5BFCBB54, 5925
        )
        patched = unity_static._patch_binary_catalog_crc(raw, bundle_name, 5925)
        records = unity_static._binary_catalog_crc_records(patched)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][2], 0)
        self.assertEqual(records[0][3], 5925)

    def test_length_prefixed_catalog_record_matches_by_size_across_separate_tables(self):
        bundle_name = "localization-string-tables-japanese_assets_all.bundle"
        prefix = bundle_name.encode("utf-8") + b"\0" * 5000
        hash_value = b"abcdef0123456789abcdef0123456789"
        raw = prefix + struct.pack("<I", 32) + hash_value + struct.pack(
            "<IIII", 17, 29, 0x5BFCBB54, 123456
        )

        patched = unity_static._patch_binary_catalog_crc(raw, bundle_name, 123456)
        records = unity_static._binary_catalog_crc_records(patched)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][2], 0)
        self.assertEqual(records[0][3], 123456)

    def test_catalog_crc_fallback_disables_only_structurally_verified_records(self):
        bundle_name = "localization-string-tables-japanese_assets_all.bundle"
        prefix = bundle_name.encode("utf-8") + b"\0" * 5000
        first = struct.pack("<I", 32) + b"0123456789abcdef0123456789abcdef" + struct.pack(
            "<IIII", 11, 22, 0x11111111, 3000
        )
        unrelated = b"not-a-record-0123456789abcdef0123456789abcdef" + struct.pack(
            "<IIII", 0, 0, 0x33333333, 5000
        )
        second = struct.pack("<I", 32) + b"fedcba9876543210fedcba9876543210" + struct.pack(
            "<IIII", 33, 44, 0x22222222, 4000
        )
        raw = prefix + first + unrelated + second

        patched = unity_static._patch_binary_catalog_crc(raw, bundle_name, 9999)
        records = unity_static._binary_catalog_crc_records(patched)

        self.assertEqual([record[2] for record in records], [0, 0])
        self.assertIn(struct.pack("<I", 0x33333333), patched)

    def test_catalog_without_verified_crc_record_is_rejected(self):
        bundle_name = "localization-string-tables-japanese_assets_all.bundle"
        raw = bundle_name.encode("utf-8") + b"-0123456789abcdef0123456789abcdef"

        with self.assertRaisesRegex(ValueError, "지원되는 CRC 레코드"):
            unity_static._patch_binary_catalog_crc(raw, bundle_name, 123456)

    def test_unity_localization_string_table_rows_are_extracted(self):
        data = SimpleNamespace(
            m_LocaleId=SimpleNamespace(m_Code="ja-JP"),
            m_TableData=[
                SimpleNamespace(m_Localized="前髪"),
                SimpleNamespace(m_Localized=""),
                SimpleNamespace(m_Localized="1920 × 1080"),
            ],
        )
        locale, records = unity_static._localization_table_records(data)
        self.assertEqual(locale, "ja-JP")
        self.assertEqual(records, [([0], "前髪")])

    def test_stripped_tmp_font_uses_bundled_matching_schema(self):
        marker = object()
        expected = SimpleNamespace(m_Name="Stripped Font")

        class StrippedObject:
            serialized_type = SimpleNamespace(
                script_id=b"tmp-script", old_type_hash=b"tmp-schema"
            )

            @staticmethod
            def parse_as_object():
                raise ValueError("typetree stripped")

            @staticmethod
            def read_typetree(*, nodes, wrap):
                self.assertIs(nodes, marker)
                self.assertTrue(wrap)
                return expected

        data, schema = unity_static._read_tmp_font(
            StrippedObject(),
            {
                "node": marker,
                "script_id": b"tmp-script",
                "old_type_hash": b"tmp-schema",
            },
        )
        self.assertIs(data, expected)
        self.assertIs(schema, marker)

    def test_content_records_only_select_structured_text(self):
        content_format, records = unity_static._content_records(
            '{"line":"Hello","nested":["開始します。",42]}', "dialogue.json"
        )
        self.assertEqual(content_format, "json")
        self.assertEqual(records, [(["line"], "Hello"), (["nested", 0], "開始します。")])
        self.assertEqual(unity_static._content_records("Hello", "texture.bin"), ("", []))

    def test_assetbundle_extract_apply_verify_and_restore(self):
        original_hash = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            streaming = root / "Demo_Data" / "StreamingAssets"
            streaming.mkdir(parents=True)
            bundle = streaming / "dialogue.bundle"
            shutil.copy2(FIXTURE, bundle)
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(root / "Demo_Data")}
            )

            entries = unity_static.extract(detection)
            by_source = {entry.source: entry for entry in entries}
            self.assertIn("Hello world.", by_source)
            self.assertIn("ゲームを開始します。", by_source)
            by_source["Hello world."].target = "안녕하세요."
            by_source["ゲームを開始します。"].target = "게임을 시작합니다."

            applied = unity_static.apply(
                root,
                detection,
                [by_source["Hello world."], by_source["ゲームを開始します。"]],
            )
            self.assertEqual(applied, 2)
            patched = json.loads(_bundle_script(bundle))
            self.assertEqual(patched["lines"], ["안녕하세요.", "게임을 시작합니다."])
            self.assertTrue((project_path(root) / "backup" / "unity_static" / "Demo_Data" / "StreamingAssets" / "dialogue.bundle").is_file())

            # 재추출할 때 현재 번역본이 아니라 최초 백업을 기준으로 삼아야
            # 번역을 다시 실행하거나 수정한 CSV를 재적용할 수 있습니다.
            extracted_again = {entry.source for entry in unity_static.extract(detection)}
            self.assertIn("Hello world.", extracted_again)
            self.assertIn("ゲームを開始します。", extracted_again)

            self.assertEqual(unity_static.restore(root), 1)
            self.assertEqual(hashlib.sha256(bundle.read_bytes()).hexdigest(), original_hash)
            self.assertFalse((project_path(root) / unity_static.STATE_FILE).exists())

    def test_restore_refuses_missing_backup_before_changing_any_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            target = root / "Demo_Data" / "StreamingAssets" / "dialogue.bundle"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"patched")
            project = project_path(root)
            project.mkdir()
            (project / unity_static.STATE_FILE).write_text(
                json.dumps({
                    "format": 1,
                    "engine": "unity",
                    "patched_files": ["Demo_Data/StreamingAssets/dialogue.bundle"],
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "원본 백업이 없습니다"):
                unity_static.restore(root)
            self.assertEqual(target.read_bytes(), b"patched")
            self.assertTrue((project / unity_static.STATE_FILE).is_file())

    def test_streamingassets_json_is_patched_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            streaming = root / "Demo_Data" / "StreamingAssets"
            streaming.mkdir(parents=True)
            dialogue = streaming / "dialogue.json"
            original = '{"line":"Hello player!","speaker":"Alice"}'
            dialogue.write_text(original, encoding="utf-8")
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(root / "Demo_Data")}
            )

            entries = unity_static.extract(detection)
            entry = next(item for item in entries if item.source == "Hello player!")
            entry.target = "안녕하세요!"
            self.assertEqual(unity_static.apply(root, detection, [entry]), 1)
            self.assertEqual(json.loads(dialogue.read_text(encoding="utf-8"))["line"], "안녕하세요!")
            self.assertEqual(unity_static.restore(root), 1)
            self.assertEqual(dialogue.read_text(encoding="utf-8"), original)

    def test_raw_resources_assets_keeps_logical_name_when_using_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            data_dir = root / "Demo_Data"
            data_dir.mkdir(parents=True)
            resources = data_dir / "resources.assets"
            shutil.copy2(ASSETS_FIXTURE, resources)
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(data_dir)}
            )

            entry = next(
                item for item in unity_static.extract(detection)
                if item.source == "ゲームを開始します。"
            )
            entry.target = "게임을 시작합니다."
            self.assertEqual(unity_static.apply(root, detection, [entry]), 1)
            self.assertEqual(
                json.loads(_bundle_script(resources))["lines"][1],
                "게임을 시작합니다.",
            )
            # 최초 백업을 baseline으로 사용한 두 번째 적용도 같은 객체를 찾습니다.
            self.assertEqual(unity_static.apply(root, detection, [entry]), 1)

    def test_forced_tmp_font_static_patch_is_verified_and_restorable(self):
        source_bundle = (
            Path(unity_static.__file__).parent.parent
            / "assets"
            / unity_static.TMP_FONT_BUNDLE_FILE
        )
        original_hash = hashlib.sha256(source_bundle.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Demo"
            streaming = root / "Demo_Data" / "StreamingAssets"
            streaming.mkdir(parents=True)
            target = streaming / "font.bundle"
            shutil.copy2(source_bundle, target)
            detection = Detection(
                "unity", str(root), "Mono", {"data_dir": str(root / "Demo_Data")}
            )

            real_copy = shutil.copy2

            def copy_with_state_check(source, destination, *args, **kwargs):
                if Path(destination).resolve() == target.resolve():
                    state = json.loads(
                        (project_path(root) / unity_static.STATE_FILE).read_text(encoding="utf-8")
                    )
                    self.assertIn(
                        "Demo_Data/StreamingAssets/font.bundle", state["patched_files"]
                    )
                return real_copy(source, destination, *args, **kwargs)

            with patch.object(unity_static.shutil, "copy2", side_effect=copy_with_state_check):
                self.assertEqual(
                    unity_static.apply(root, detection, [], force_tmp_font=True), 0
                )
            report = json.loads(
                (project_path(root) / unity_static.FONT_REPORT_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(report["font_assets"], 1)
            self.assertEqual(report["patched_files"], ["Demo_Data/StreamingAssets/font.bundle"])
            self.assertEqual(unity_static.restore(root), 1)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
