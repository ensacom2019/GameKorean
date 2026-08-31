from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gameko import service
from gameko.model import Detection, project_path


class UnityWorkflowTests(unittest.TestCase):
    def _game(self, temp: str) -> tuple[Path, Detection]:
        root = Path(temp) / "Demo"
        data_dir = root / "Demo_Data"
        data_dir.mkdir(parents=True)
        detection = Detection(
            "unity", str(root), "Mono", {"data_dir": str(data_dir), "exe": ""}
        )
        project_path(root).mkdir()
        (project_path(root) / "unity_install.json").write_text(
            json.dumps({"source_language": "ja", "tmp_font": {"status": "disabled"}}),
            encoding="utf-8",
        )
        return root, detection

    def test_default_unity_mode_installs_only_xunity(self):
        with tempfile.TemporaryDirectory() as temp:
            _root, detection = self._game(temp)
            with (
                patch.object(service, "find_game", return_value=detection),
                patch.object(service.unity, "install", return_value="test") as install,
                patch.object(service, "extract_game") as extract,
            ):
                result = service.automatic(detection.root)
            self.assertIn("XUnity test 설치 완료", result)
            self.assertTrue(install.call_args.kwargs["experimental_tmp_font"])
            self.assertFalse(install.call_args.kwargs["force_tmp_font"])
            extract.assert_not_called()

    def test_experimental_unity_mode_removes_owned_xunity_and_runs_only_static_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            root, detection = self._game(temp)
            with (
                patch.object(service, "find_game", return_value=detection),
                patch.object(service.unity, "install", return_value="test") as install,
                patch.object(service.unity, "restore", return_value=4) as restore,
                patch.object(service, "extract_game", return_value=(project_path(root), 3)) as extract,
                patch.object(service, "translate_game", return_value=(2, 3)) as translate,
                patch.object(service, "apply_game", return_value=2) as apply,
            ):
                result = service.automatic(detection.root, unity_static_test=True)
            self.assertIn("정적 테스트 2/3개 번역, 2개 적용(XUnity 미설치)", result)
            install.assert_not_called()
            restore.assert_called_once_with(root)
            self.assertTrue(extract.call_args.kwargs["unity_static_test"])
            self.assertFalse(extract.call_args.kwargs["force_tmp_font"])
            translate.assert_called_once()
            apply.assert_called_once()

    def test_force_tmp_option_is_forwarded_to_both_unity_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root, detection = self._game(temp)
            with (
                patch.object(service, "find_game", return_value=detection),
                patch.object(service.unity, "install", return_value="test") as install,
            ):
                service.automatic(detection.root, force_tmp_font=True)
            self.assertTrue(install.call_args.kwargs["force_tmp_font"])

            with (
                patch.object(service, "find_game", return_value=detection),
                patch.object(service.unity, "restore", return_value=0),
                patch.object(service, "extract_game", return_value=(project_path(root), 1)) as extract,
                patch.object(service, "translate_game", return_value=(1, 1)),
                patch.object(service, "apply_game", return_value=1) as apply,
            ):
                service.automatic(
                    detection.root, unity_static_test=True, force_tmp_font=True
                )
            self.assertTrue(extract.call_args.kwargs["force_tmp_font"])
            self.assertTrue(apply.call_args.kwargs["force_tmp_font"])

    def test_static_failure_does_not_install_xunity_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            _root, detection = self._game(temp)
            logs: list[str] = []
            with (
                patch.object(service, "find_game", return_value=detection),
                patch.object(service.unity, "install", return_value="test") as install,
                patch.object(service.unity, "restore", return_value=0),
                patch.object(service, "extract_game", side_effect=RuntimeError("bad bundle")),
            ):
                with self.assertRaisesRegex(RuntimeError, "bad bundle"):
                    service.automatic(
                        detection.root, log=logs.append, unity_static_test=True
                    )
            install.assert_not_called()
            self.assertTrue(any("XUnity를 설치하지 않습니다" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
