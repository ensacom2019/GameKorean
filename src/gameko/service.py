from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Callable

from .detect import detect_game
from .language import LanguageSelection, select_original_language
from .model import (
    Detection, Entry, load_entries, load_manifest, project_path, save_entries,
    save_project, update_manifest,
)
from .text import guess_language
from .translators import make_translator, translate_project
from .engines import gamemaker, rpgmaker, unity, unity_static


Log = Callable[[str], None]
SOURCE_LANGUAGES = {"auto", "ja", "en"}
LANGUAGE_NAMES = {"ja": "일본어", "en": "영어"}


def _validate_source_language(value: str) -> str:
    if value not in SOURCE_LANGUAGES:
        raise ValueError(f"지원하지 않는 원문 언어 선택입니다: {value}")
    return value


def _selection_metadata(selection: LanguageSelection, preference: str) -> dict:
    supported = [language for language, count in selection.counts.items() if count]
    return {
        "source_language": selection.selected,
        "source_language_preference": preference,
        "supported_languages": supported,
        "language_counts": selection.counts,
        "language_reason": selection.reason,
    }


def _selection_reason(reason: str) -> str:
    if reason.startswith("override:"):
        return "사용자 지정"
    if reason.startswith("single_language:"):
        return "한 언어만 발견"
    if reason.startswith("metadata:"):
        return "게임의 원작/기본 언어 설정"
    if reason.startswith("file_layout:") or reason.startswith("path:"):
        return "기본 데이터와 언어팩 배치"
    return "원작 근거가 불충분해 일본어 우선"


def _log_language_selection(selection: LanguageSelection, log: Log | None) -> None:
    if not log:
        return
    found = []
    if selection.ja_count:
        found.append(f"일본어 {selection.ja_count:,}개")
    if selection.en_count:
        found.append(f"영어 {selection.en_count:,}개")
    summary = ", ".join(found) if found else "영어·일본어 문자열 없음"
    selected = LANGUAGE_NAMES[selection.selected]
    log(f"언어 분석: {summary} → {selected}만 번역 ({_selection_reason(selection.reason)})")


def _entry_language(entry: Entry) -> str:
    return entry.source_lang if entry.source_lang in {"ja", "en"} else guess_language(entry.source)


def _project_selection(root: Path, preference: str, log: Log | None = None) -> LanguageSelection:
    preference = _validate_source_language(preference)
    manifest = load_manifest(root)
    entries = load_entries(root)
    detection = Detection(
        manifest["engine"], str(root), manifest.get("variant", ""), manifest.get("details", {})
    )
    selection = select_original_language(entries, detection, preference=preference)
    update_manifest(root, _selection_metadata(selection, preference))
    _log_language_selection(selection, log)
    return selection


def find_game(path: str | Path) -> Detection:
    selected = Path(path).expanduser().resolve()
    direct = detect_game(selected)
    if direct.engine != "unknown":
        return direct
    if not selected.is_dir():
        return direct
    found: list[Detection] = []
    for child in sorted(selected.iterdir()):
        if child.is_dir() and child.name != "gameko_project":
            detection = detect_game(child)
            if detection.engine != "unknown":
                found.append(detection)
    unique = {(item.engine, item.root): item for item in found}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        names = ", ".join(Path(item.root).name for item in unique.values())
        raise RuntimeError(f"게임이 여러 개 발견되었습니다. 하나의 게임 폴더를 직접 선택하세요: {names}")
    parent = selected.parent if selected.is_dir() else selected.parent.parent
    if parent != selected and parent.exists():
        parent_detection = detect_game(parent)
        if parent_detection.engine != "unknown":
            return parent_detection
    return direct


def extract_game(
    detection: Detection,
    log: Log | None = None,
    source_language: str = "auto",
    unity_static_test: bool = False,
    force_tmp_font: bool = False,
) -> tuple[Path, int]:
    root = Path(detection.root)
    if log:
        log(f"엔진 감지: {detection.engine} {detection.variant}")
    if detection.engine == "rpgmaker":
        entries = rpgmaker.extract(detection)
    elif detection.engine == "gamemaker":
        if log:
            log("UndertaleModTool CLI를 확인/다운로드하고 있습니다...")
        entries = gamemaker.extract(detection)
    elif detection.engine == "unity":
        if not unity_static_test:
            raise RuntimeError(
                "Unity 정적 추출은 테스트 기능입니다. 'Unity 정적 패치(테스트)'를 켜거나 "
                "CLI에서 --unity-static-test를 지정하세요."
            )
        if log:
            log("Unity TextAsset·Localization StringTable·AssetBundle·StreamingAssets를 안전 모드로 검사합니다...")
        entries = unity_static.extract(detection)
    else:
        raise RuntimeError("지원 엔진을 찾지 못했습니다.")
    preference = _validate_source_language(source_language)
    selection = select_original_language(entries, detection, preference=preference)
    metadata = _selection_metadata(selection, preference)
    if detection.engine == "unity":
        metadata["unity_static_test"] = True
        metadata["force_tmp_font"] = bool(force_tmp_font)
    out = save_project(root, detection, entries, metadata)
    export_csv(root)
    _log_language_selection(selection, log)
    selected_count = sum(1 for entry in entries if _entry_language(entry) == selection.selected)
    if log:
        log(f"전체 {len(entries):,}개 중 {selected_count:,}개를 번역 대상으로 선택했습니다.")
        if detection.engine == "unity":
            report_path = project_path(root) / unity_static.SCAN_REPORT_FILE
            if report_path.is_file():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if report.get("errors"):
                        log(
                            f"읽지 못한 Unity 리소스 {len(report['errors']):,}개는 건너뛰었습니다. "
                            f"상세: {report_path}"
                        )
                        for reason in report["errors"][:5]:
                            log(f"Unity 읽기 실패 원인: {reason}")
                        if len(report["errors"]) > 5:
                            log(f"나머지 {len(report['errors']) - 5:,}개 오류는 보고서에 기록했습니다.")
                except (OSError, json.JSONDecodeError):
                    pass
    return out, selected_count


def translate_game(
    root: Path,
    provider: str,
    options: dict,
    log: Log | None = None,
    progress=None,
    source_language: str = "auto",
) -> tuple[int, int]:
    selection = _project_selection(root, source_language, log)
    translator = make_translator(provider, **options)
    if log:
        name = "웹 자동 번역(키 없음, 최대 4개 동시·제한 시 자동 감속)" if provider in {"web", "google"} else provider
        log(f"{name}을 시작합니다.")
    result = translate_project(root, translator, progress, source_language=selection.selected)
    export_csv(root)
    return result


def apply_game(
    root: Path,
    log: Log | None = None,
    source_language: str = "auto",
    force_tmp_font: bool | None = None,
) -> int:
    manifest = load_manifest(root)
    if force_tmp_font is None:
        force_tmp_font = bool(manifest.get("force_tmp_font", False))
    elif manifest.get("force_tmp_font") != bool(force_tmp_font):
        manifest = update_manifest(root, {"force_tmp_font": bool(force_tmp_font)})
    detection = Detection(manifest["engine"], str(root), manifest.get("variant", ""), manifest.get("details", {}))
    selection = _project_selection(root, source_language, log)
    entries = [entry for entry in load_entries(root) if _entry_language(entry) == selection.selected]
    if detection.engine == "rpgmaker":
        count = rpgmaker.apply(root, entries)
    elif detection.engine == "gamemaker":
        count = gamemaker.apply(root, detection, entries)
    elif detection.engine == "unity":
        if not manifest.get("unity_static_test"):
            raise RuntimeError("이 Unity 번역 프로젝트는 정적 패치 테스트로 추출되지 않았습니다.")
        count = unity_static.apply(
            root, detection, entries, force_tmp_font=bool(force_tmp_font)
        )
    else:
        raise RuntimeError("이 엔진은 파일 적용 대상이 아닙니다.")
    if log:
        log(f"번역 {count:,}개를 적용했습니다. 원본 백업도 보관했습니다.")
        if detection.engine == "rpgmaker":
            log("Noto Sans CJK KR Regular OTF 폰트도 게임 설정에 자동 적용했습니다.")
        elif detection.engine == "gamemaker":
            log("GameMaker 비트맵 폰트에 필요한 Noto Sans CJK KR 글리프를 안전 범위에서 자동 추가했습니다. 제외된 폰트는 GameKO.Fonts의 보고서를 확인하세요.")
        elif detection.engine == "unity":
            log("검증된 Unity TextAsset·Localization StringTable을 재패킹했습니다. 추출되지 않은 동적 문장은 정적 모드에서 제외됩니다.")
            if force_tmp_font:
                report_path = project_path(root) / unity_static.FONT_REPORT_FILE
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    log(
                        f"TMP 강제 교체: 폰트 {int(report.get('font_assets', 0)):,}개 / "
                        f"파일 {len(report.get('patched_files', [])):,}개. 상세: {report_path}"
                    )
                    if report.get("errors"):
                        log(f"교체하지 못한 Unity 폰트 리소스 {len(report['errors']):,}개가 있습니다.")
                        for reason in report["errors"][:5]:
                            log(f"TMP 교체 실패 원인: {reason}")
                        if len(report["errors"]) > 5:
                            log(f"나머지 {len(report['errors']) - 5:,}개 오류는 보고서에 기록했습니다.")
                    elif int(report.get("font_assets", 0)) == 0:
                        log(
                            "TMP 강제 교체 대상이 없습니다. 게임 내부에서 읽을 수 있는 기존 TMP FontAsset과 "
                            "같은 파일의 아틀라스를 찾지 못했습니다."
                        )
                except (OSError, json.JSONDecodeError, ValueError, TypeError):
                    log("TMP 강제 교체 결과 보고서를 읽지 못했습니다.")
    return count


def restore_game(detection: Detection, log: Log | None = None) -> int:
    root = Path(detection.root)
    if detection.engine == "rpgmaker":
        count = rpgmaker.restore(root)
    elif detection.engine == "gamemaker":
        count = gamemaker.restore(root, detection)
    elif detection.engine == "unity":
        count = unity_static.restore(root) + unity.restore(root)
    else:
        raise RuntimeError("지원 엔진을 찾지 못했습니다.")
    if log:
        log(f"원본 상태로 복원했습니다 ({count:,}개 파일).")
    return count


def automatic(
    path: str | Path,
    provider: str = "web",
    options: dict | None = None,
    log: Log | None = None,
    progress=None,
    source_language: str = "auto",
    unity_static_test: bool = False,
    force_tmp_font: bool = False,
) -> str:
    source_language = _validate_source_language(source_language)
    detection = find_game(path)
    if detection.engine == "unknown":
        raise RuntimeError("Unity, GameMaker, RPG Maker MV/MZ 게임을 찾지 못했습니다.")
    root = Path(detection.root)
    if log:
        log(f"게임 폴더: {root}")
        log(f"자동 판별 결과: {detection.engine} {detection.variant}")
    if detection.engine == "unity":
        if unity_static_test:
            removed = unity.restore(root)
            if log:
                if removed:
                    log(f"기존에 GameKO가 설치한 XUnity 관련 파일 {removed:,}개를 제거했습니다.")
                log("Unity 정적 패치 모드: XUnity를 설치하지 않습니다.")
            extract_game(
                detection, log, source_language=source_language, unity_static_test=True,
                force_tmp_font=force_tmp_font,
            )
            done, total = translate_game(
                root, provider, options or {}, log, progress, source_language=source_language
            )
            applied = apply_game(
                root, log, source_language=source_language,
                force_tmp_font=force_tmp_font,
            )
            return (
                f"Unity {detection.variant}: 정적 테스트 {done:,}/{total:,}개 번역, "
                f"{applied:,}개 적용(XUnity 미설치)"
            )
        if log:
            log("XUnity.AutoTranslator 최신 릴리스를 설치하고 한국어 설정을 구성합니다...")
        version = unity.install(
            detection, source_language=source_language, experimental_tmp_font=True,
            force_tmp_font=force_tmp_font,
        )
        state = json.loads(
            (project_path(root) / "unity_install.json").read_text(encoding="utf-8")
        )
        resolved_language = state.get("source_language", "ja")
        if log:
            log(f"Unity 원문 언어: {LANGUAGE_NAMES.get(resolved_language, resolved_language)}")
            tmp_font = state.get("tmp_font", {})
            if tmp_font.get("status") == "installed":
                qualifier = "(Unity 버전 무시)" if tmp_font.get("forced") else ""
                log(f"AINFORGE Noto Sans CJK KR TMP 폰트를 XUnity에 연결했습니다{qualifier}.")
            else:
                detected = tmp_font.get("detected_unity_version") or "감지 실패"
                required = tmp_font.get("required_unity_version") or "알 수 없음"
                log(
                    "제공된 TMP 번들의 Unity 버전이 맞지 않아 Noto TTF 폴백을 사용합니다 "
                    f"(게임 {detected}, TMP {required})."
                )
            log(f"Unity 런타임 번역기 {version} 및 Noto Sans CJK KR Regular 설치 완료. 게임을 실행하면 자동 번역됩니다.")
        runtime_result = (
            f"Unity {detection.variant}: {LANGUAGE_NAMES.get(resolved_language, resolved_language)}→한국어, "
            f"XUnity {version} 설치 완료"
        )
        return runtime_result
    extract_game(detection, log, source_language=source_language)
    done, total = translate_game(
        root, provider, options or {}, log, progress, source_language=source_language
    )
    apply_game(root, log, source_language=source_language)
    return f"{detection.engine} {detection.variant}: {done:,}/{total:,}개 번역 및 적용 완료"


CSV_FIELDS = ["id", "context", "source_lang", "source", "target", "status"]


def export_csv(root: Path, destination: str | Path | None = None) -> Path:
    """사람이 편집할 수 있는 UTF-8(BOM) CSV 번역표를 내보냅니다."""
    entries = load_entries(root)
    path = Path(destination).expanduser().resolve() if destination else project_path(root) / "translations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow({key: getattr(entry, key) for key in writer.fieldnames})
    return path


def import_csv(root: Path, source: str | Path | None = None) -> int:
    """수정한 CSV의 target/status만 기존 번역 프로젝트에 안전하게 병합합니다."""
    entries = load_entries(root)
    by_id = {entry.id: entry for entry in entries}
    path = Path(source).expanduser().resolve() if source else project_path(root) / "translations.csv"
    if not path.exists():
        raise FileNotFoundError(f"가져올 대사 파일이 없습니다: {path}")
    count = 0
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        missing = {"id", "target"} - fields
        if missing:
            raise ValueError(f"대사 CSV에 필수 열이 없습니다: {', '.join(sorted(missing))}")
        for line_no, row in enumerate(reader, 2):
            entry_id = (row.get("id") or "").strip()
            if not entry_id:
                raise ValueError(f"대사 CSV {line_no}행의 id가 비어 있습니다.")
            if entry_id in seen:
                raise ValueError(f"대사 CSV {line_no}행에 중복 id가 있습니다: {entry_id}")
            seen.add(entry_id)
            entry = by_id.get(entry_id)
            if entry is None:
                raise ValueError(f"현재 게임에서 찾을 수 없는 대사 id입니다 ({line_no}행): {entry_id}")
            csv_source = row.get("source")
            if csv_source is not None and csv_source != entry.source:
                raise ValueError(f"원문이 현재 추출본과 다릅니다 ({line_no}행). 다른 게임/이전 추출본인지 확인하세요.")
            target = row.get("target") or ""
            entry.target = target
            requested_status = (row.get("status") or "").strip()
            entry.status = requested_status or ("edited" if target.strip() else "new")
            count += 1
    if not count:
        raise ValueError("가져올 대사가 한 행도 없습니다.")
    save_entries(root, entries)
    # 외부 CSV를 가져온 경우 내부 작업용 사본도 같은 내용으로 갱신합니다.
    internal = project_path(root) / "translations.csv"
    if path != internal.resolve():
        export_csv(root, internal)
    return count
