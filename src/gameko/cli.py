from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .service import apply_game, automatic, export_csv, extract_game, find_game, import_csv, restore_game, translate_game

SOURCE_LANGUAGE_CHOICES = ("auto", "ja", "en")


def _add_source_language_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-language", "--source-lang",
        dest="source_language",
        choices=SOURCE_LANGUAGE_CHOICES,
        default="auto",
        help="원문 언어: auto=원작 감지(일본어 우선), ja=일본어만, en=영어만",
    )


def _options(args) -> dict:
    return {
        "base_url": getattr(args, "base_url", "http://localhost:11434/v1"),
        "model": getattr(args, "model", ""),
        "api_key": getattr(args, "api_key", ""),
        "install_models": getattr(args, "install_models", False),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gameko", description="게임 엔진 자동 감지 한국어 번역 도구")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "extract", "apply", "restore"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path", nargs="?", default=".")
        if name in {"extract", "apply"}:
            _add_source_language_argument(cmd)
        if name == "extract":
            cmd.add_argument(
                "--unity-static-test", action="store_true",
                help="Unity TextAsset/AssetBundle 정적 대본 추출 테스트를 켭니다.",
            )
            cmd.add_argument(
                "--force-tmp-font", action="store_true",
                help="Unity 버전을 무시하고 제공된 TMP 폰트 교체를 시도합니다.",
            )
        if name == "apply":
            cmd.add_argument("--force-tmp-font", action="store_true")
    export_cmd = sub.add_parser("export-csv", help="대사를 CSV로 내보냅니다.")
    export_cmd.add_argument("path", nargs="?", default=".")
    export_cmd.add_argument("--output", "-o", help="내보낼 CSV 경로")
    import_cmd = sub.add_parser("import-csv", help="수정한 CSV를 번역 프로젝트로 가져옵니다.")
    import_cmd.add_argument("path", nargs="?", default=".")
    import_cmd.add_argument("--input", "-i", help="가져올 CSV 경로")
    for name in ("translate", "auto"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path", nargs="?", default=".")
        cmd.add_argument(
            "--provider", choices=["web", "google", "argos", "openai"], default="web",
            help="기본값 web은 API 키 없는 웹 자동 번역입니다. google은 호환용 별칭입니다.",
        )
        cmd.add_argument("--base-url", default="http://localhost:11434/v1")
        cmd.add_argument("--model", default="")
        cmd.add_argument("--api-key", default="")
        cmd.add_argument("--install-models", action="store_true")
        if name == "auto":
            cmd.add_argument(
                "--unity-static-test", action="store_true",
                help="XUnity 없이 Unity 정적 번역·재패킹 테스트를 켭니다.",
            )
            cmd.add_argument(
                "--force-tmp-font", action="store_true",
                help="Unity 버전을 무시하고 제공된 TMP 폰트 교체를 시도합니다.",
            )
        _add_source_language_argument(cmd)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log = print
    try:
        detection = find_game(args.path)
        root = Path(detection.root)
        if args.command == "scan":
            print(f"{detection.engine}\t{detection.variant}\t{detection.root}")
        elif args.command == "extract":
            extract_game(
                detection, log, source_language=args.source_language,
                unity_static_test=args.unity_static_test,
                force_tmp_font=args.force_tmp_font,
            )
        elif args.command == "translate":
            translate_game(
                root, args.provider, _options(args), log,
                lambda n, total, text: print(f"[{n}/{total}] {text}"),
                source_language=args.source_language,
            )
        elif args.command == "apply":
            import_csv(root)
            apply_game(
                root, log, source_language=args.source_language,
                force_tmp_font=args.force_tmp_font,
            )
        elif args.command == "restore":
            restore_game(detection, log)
        elif args.command == "export-csv":
            print(export_csv(root, args.output))
        elif args.command == "import-csv":
            print(f"{import_csv(root, args.input)}개 항목을 가져왔습니다.")
        elif args.command == "auto":
            print(automatic(
                args.path, args.provider, _options(args), log,
                lambda n, total, text: print(f"[{n}/{total}] {text}"),
                source_language=args.source_language,
                unity_static_test=args.unity_static_test,
                force_tmp_font=args.force_tmp_font,
            ))
        return 0
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
