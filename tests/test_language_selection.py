from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from gameko import cli, gui
from gameko.model import Detection


class DummyVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class SourceLanguageSelectionTests(unittest.TestCase):
    def test_app_does_not_override_tkinter_internal_options_method(self):
        self.assertNotIn("_options", gui.App.__dict__)

    def test_gui_labels_map_to_service_values(self):
        self.assertEqual(
            gui.SOURCE_LANGUAGE_LABELS,
            {
                "자동(원작 감지·일본어 우선)": "auto",
                "일본어만": "ja",
                "영어만": "en",
            },
        )
        self.assertEqual(gui.source_language_key(gui.DEFAULT_SOURCE_LANGUAGE_LABEL), "auto")
        self.assertEqual(gui.source_language_key("일본어만"), "ja")

    def test_done_event_opens_action_specific_modal_popup(self):
        app = object.__new__(gui.App)
        app.events = gui.queue.Queue()
        app.events.put(("done", ("자동 번역", "Unity 번역 및 적용 완료")))
        app.action_buttons = []
        app.status = Mock()
        app._finish_clock = Mock()
        app._log = Mock()
        app.lift = Mock()
        app._refresh_time_text = Mock()
        app.after = Mock()

        with patch.object(gui.messagebox, "showinfo") as showinfo:
            gui.App._poll(app)

        showinfo.assert_called_once_with(
            "자동 번역 완료",
            "자동 번역이 완료되었습니다.\n\nUnity 번역 및 적용 완료",
            parent=app,
        )
        self.assertEqual(gui.source_language_key("영어만"), "en")
        self.assertEqual(gui.source_language_key("알 수 없는 값"), "auto")

    def test_folder_dialog_uses_parent_and_exe_parent_as_initial_directory(self):
        root = Path.cwd().resolve()
        app = object.__new__(gui.App)
        app.path = DummyVar(str(root / "Sample Game.exe"))
        app.lift = Mock()
        app.scan = Mock()
        app._show_error = Mock()

        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(gui.filedialog, "askdirectory", return_value=str(root)) as choose,
        ):
            gui.App.browse(app)

        choose.assert_called_once_with(
            parent=app,
            title="번역할 게임 폴더 선택",
            initialdir=str(root),
            mustexist=True,
        )
        self.assertEqual(app.path.get(), str(root))
        app.scan.assert_called_once_with()

    def test_folder_dialog_error_is_reported(self):
        app = object.__new__(gui.App)
        app.path = DummyVar(str(Path.cwd()))
        app.lift = Mock()
        app.scan = Mock()
        app._show_error = Mock()
        failure = RuntimeError("dialog failed")

        with patch.object(gui.filedialog, "askdirectory", side_effect=failure):
            gui.App.browse(app)

        app._show_error.assert_called_once_with("게임 폴더 선택", failure)

    def test_cli_parser_accepts_source_language_for_workflow_commands(self):
        parser = cli._parser()
        for command in ("extract", "translate", "apply", "auto"):
            with self.subTest(command=command):
                args = parser.parse_args([command, "game", "--source-lang", "ja"])
                self.assertEqual(args.source_language, "ja")
                args = parser.parse_args([command, "game", "--source-language", "en"])
                self.assertEqual(args.source_language, "en")
                args = parser.parse_args([command, "game"])
                self.assertEqual(args.source_language, "auto")

    def test_cli_forwards_source_language_separately_from_provider_options(self):
        detection = Detection("rpgmaker", str(Path("game").resolve()), "MV", {})
        with (
            patch.object(cli, "find_game", return_value=detection),
            patch.object(cli, "extract_game", return_value=(Path("project"), 0)) as extract,
            patch.object(cli, "translate_game", return_value=(0, 0)) as translate,
            patch.object(cli, "import_csv", return_value=0),
            patch.object(cli, "apply_game", return_value=0) as apply,
            patch.object(cli, "automatic", return_value="완료") as automatic,
        ):
            self.assertEqual(cli.main(["extract", "game", "--source-lang", "ja"]), 0)
            self.assertEqual(extract.call_args.kwargs["source_language"], "ja")

            self.assertEqual(cli.main(["translate", "game", "--source-lang", "en"]), 0)
            self.assertEqual(translate.call_args.kwargs["source_language"], "en")
            self.assertNotIn("source_language", translate.call_args.args[2])

            self.assertEqual(cli.main(["apply", "game", "--source-lang", "ja"]), 0)
            self.assertEqual(apply.call_args.kwargs["source_language"], "ja")

            self.assertEqual(cli.main(["auto", "game", "--source-lang", "en"]), 0)
            self.assertEqual(automatic.call_args.kwargs["source_language"], "en")
            self.assertNotIn("source_language", automatic.call_args.args[2])

    def test_cli_forwards_unity_static_test_only_for_extract_and_auto(self):
        parser = cli._parser()
        self.assertTrue(parser.parse_args(["extract", "game", "--unity-static-test"]).unity_static_test)
        self.assertTrue(parser.parse_args(["auto", "game", "--unity-static-test"]).unity_static_test)

        detection = Detection("unity", str(Path("game").resolve()), "Mono", {})
        with (
            patch.object(cli, "find_game", return_value=detection),
            patch.object(cli, "extract_game", return_value=(Path("project"), 0)) as extract,
            patch.object(cli, "automatic", return_value="완료") as automatic,
        ):
            self.assertEqual(cli.main(["extract", "game", "--unity-static-test"]), 0)
            self.assertTrue(extract.call_args.kwargs["unity_static_test"])
            self.assertEqual(cli.main(["auto", "game", "--unity-static-test"]), 0)
            self.assertTrue(automatic.call_args.kwargs["unity_static_test"])

    def test_gui_workflow_forwards_dropdown_value(self):
        root = Path("game").resolve()
        detection = Detection("rpgmaker", str(root), "MV", {})
        app = object.__new__(gui.App)
        app.path = DummyVar(str(root))
        app.provider = DummyVar(gui.WEB_PROVIDER)
        app.source_language = DummyVar("일본어만")
        app.base_url = DummyVar("http://localhost:11434/v1")
        app.model = DummyVar("")
        app.install_models = DummyVar(False)
        app.unity_static_test = DummyVar(True)
        app.force_tmp_font = DummyVar(True)
        app._detection = lambda: detection
        app._log = lambda _message: None
        app._progress = lambda _value, _total, _text: None
        app._run = lambda function, action="작업": function()

        with (
            patch.object(gui, "automatic", return_value="완료") as automatic,
            patch.object(gui, "extract_game", return_value=(Path("project"), 0)) as extract,
            patch.object(gui, "translate_game", return_value=(0, 0)) as translate,
            patch.object(gui, "apply_game", return_value=0) as apply,
        ):
            app.auto()
            app.extract()
            app.translate()
            app.apply()

        self.assertEqual(automatic.call_args.kwargs["source_language"], "ja")
        self.assertEqual(extract.call_args.kwargs["source_language"], "ja")
        self.assertEqual(translate.call_args.kwargs["source_language"], "ja")
        self.assertEqual(apply.call_args.kwargs["source_language"], "ja")
        self.assertTrue(automatic.call_args.kwargs["unity_static_test"])
        self.assertTrue(extract.call_args.kwargs["unity_static_test"])
        self.assertTrue(automatic.call_args.kwargs["force_tmp_font"])
        self.assertTrue(extract.call_args.kwargs["force_tmp_font"])
        self.assertTrue(apply.call_args.kwargs["force_tmp_font"])
        self.assertNotIn("source_language", automatic.call_args.args[2])
        self.assertNotIn("source_language", translate.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
