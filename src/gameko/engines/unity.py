from __future__ import annotations

import configparser
import ctypes
import filecmp
import filecmp
import hashlib
from importlib.resources import as_file, files
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from ..downloads import download_first_compatible_asset, download_release_asset
from ..model import Detection, project_path


NOTO_FONT_NAME = "Noto Sans CJK KR"
NOTO_FONT_FILE = "NotoSansCJKkr-Regular.otf"
NOTO_FONT_LICENSE_FILE = "NotoSansKR-OFL.txt"
TMP_FONT_BUNDLE_FILE = "gameko_notosanscjkkr_sdf_u6000_3_23f1"
TMP_FONT_BUNDLE_UNITY_VERSION = "6000.3.23f1"
FONT_REGISTRY_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
SOURCE_LANGUAGES = {"auto", "ja", "en"}
JAPANESE_LANGUAGE_MARKERS = {"ja", "ja-jp", "jp", "japanese"}
ENGLISH_LANGUAGE_MARKERS = {"en", "en-us", "en-gb", "eng", "english"}
LOCALE_CONTAINER_NAMES = {
    "i18n", "l10n", "lang", "language", "languages", "locale", "locales",
    "localization", "localizations", "translation", "translations",
}


def _backup_file(root: Path, project: Path, path: Path, backed_up: set[str]) -> None:
    rel = str(path.relative_to(root))
    if rel in backed_up or not path.is_file():
        return
    destination = project / "backup" / "unity_overwritten" / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    backed_up.add(rel)


def _install_release(root: Path, project: Path, repo: str, matcher: str, backed_up: set[str], include_prerelease: bool = False) -> str:
    with tempfile.TemporaryDirectory(prefix="gameko-unity-") as temp:
        staging = Path(temp)
        if include_prerelease:
            version, files = download_first_compatible_asset(repo, matcher, staging)
        else:
            version, files = download_release_asset(repo, matcher, staging)
        for source in files:
            rel = source.relative_to(staging)
            target = root / rel
            if target.exists():
                _backup_file(root, project, target, backed_up)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return version


def _normalize_source_language(source_language: str) -> str:
    value = (source_language or "auto").strip().lower().replace("_", "-")
    if value not in SOURCE_LANGUAGES:
        raise ValueError("Unity 원문 언어는 auto, ja, en 중 하나여야 합니다.")
    return value


def _contains_japanese(text: str) -> bool:
    return any(
        "\u3040" <= char <= "\u30ff" or "\uff66" <= char <= "\uff9f"
        for char in text
    )


def _language_markers(text: str) -> tuple[bool, bool]:
    normalized = text.casefold().replace("_", "-")

    def contains(marker: str) -> bool:
        return re.search(
            rf"(?<![a-z]){re.escape(marker)}(?![a-z])", normalized
        ) is not None

    return (
        any(contains(marker) for marker in JAPANESE_LANGUAGE_MARKERS),
        any(contains(marker) for marker in ENGLISH_LANGUAGE_MARKERS),
    )


def _read_small_metadata(path: Path, limit: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as fh:
            raw = fh.read(limit)
    except OSError:
        return ""
    for encoding in ("utf-8-sig", "utf-16", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    return ""


def _locale_path_markers(base: Path, max_entries: int = 256) -> tuple[bool, bool]:
    """Inspect only shallow names under known localization/StreamingAssets roots."""
    if not base.is_dir():
        return False, False
    found_ja = False
    found_en = False
    pending: list[tuple[Path, int, bool]] = [(base, 0, False)]
    inspected = 0
    while pending and inspected < max_entries:
        current, depth, in_locale_container = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            inspected += 1
            name = child.stem.casefold().replace("_", "-")
            locale_context = in_locale_container or current.name.casefold() in LOCALE_CONTAINER_NAMES
            if locale_context or depth == 0:
                ja, en = _language_markers(name)
                found_ja = found_ja or ja
                found_en = found_en or en
            if child.is_dir() and depth < 2 and inspected < max_entries:
                pending.append((child, depth + 1, locale_context or name in LOCALE_CONTAINER_NAMES))
        if found_ja and found_en:
            break
    return found_ja, found_en


def infer_unity_source_language(
    root: Path, data_dir: Path | None = None, metadata: dict | None = None
) -> str:
    """Best-effort source language inference; ambiguous games default to Japanese."""
    root = root.resolve()
    data_dir = data_dir.resolve() if data_dir and data_dir.exists() else None
    names = [root.name]
    try:
        names.extend(path.stem for path in root.glob("*.exe"))
    except OSError:
        pass
    if data_dir:
        names.append(data_dir.stem.removesuffix("_Data"))

    metadata_text = "\n".join(names)
    metadata = metadata or {}
    explicit_ja = False
    explicit_en = False
    for key in ("original_language", "source_language", "default_language", "base_language"):
        value = metadata.get(key)
        if isinstance(value, str):
            metadata_text += "\n" + value
            ja, en = _language_markers(value)
            explicit_ja = explicit_ja or ja or _contains_japanese(value)
            explicit_en = explicit_en or en
    # 명시적인 원작/기본 언어는 두 로캘 폴더가 함께 있는 것보다 강한 근거입니다.
    if explicit_ja != explicit_en:
        return "ja" if explicit_ja else "en"
    for candidate in filter(None, (root / "app.info", data_dir / "app.info" if data_dir else None)):
        metadata_text += "\n" + _read_small_metadata(candidate)
    if _contains_japanese(metadata_text):
        return "ja"
    found_ja, found_en = _language_markers(metadata_text)

    locale_roots = [root / "StreamingAssets"]
    if data_dir:
        locale_roots.append(data_dir / "StreamingAssets")
    for locale_root in locale_roots:
        ja, en = _locale_path_markers(locale_root)
        found_ja = found_ja or ja
        found_en = found_en or en
    return "en" if found_en and not found_ja else "ja"


def _resolve_source_language(
    root: Path,
    source_language: str,
    data_dir: Path | None = None,
    metadata: dict | None = None,
) -> str:
    selected = _normalize_source_language(source_language)
    if selected == "auto":
        return infer_unity_source_language(root, data_dir, metadata)
    return selected


def detect_unity_version(detection: Detection) -> str:
    """Read the embedded player version without modifying or fully loading game assets."""
    explicit = detection.details.get("unity_version")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    root = Path(detection.root)
    data_dir_value = detection.details.get("data_dir")
    data_dir = Path(data_dir_value) if data_dir_value else None
    candidates: list[Path] = []
    if data_dir and data_dir.is_dir():
        candidates.extend((
            data_dir / "globalgamemanagers",
            data_dir / "data.unity3d",
            data_dir / "resources.assets",
        ))
    candidates.extend(sorted(root.glob("*.unity3d"))[:4])
    version_pattern = re.compile(rb"(?<![0-9])([0-9]{1,4}\.[0-9]+\.[0-9]+[abcfp][0-9]+)(?![0-9])")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                raw = fh.read(1024 * 1024)
        except OSError:
            continue
        match = version_pattern.search(raw)
        if match:
            return match.group(1).decode("ascii")
    return ""


def _copy_bundled_asset(
    root: Path,
    project: Path,
    resource_name: str,
    destination: Path,
    backed_up: set[str],
) -> None:
    resource = files("gameko").joinpath("assets", resource_name)
    if not resource.is_file():
        raise RuntimeError(f"GameKO에 포함된 파일을 찾지 못했습니다: {resource_name}")
    with as_file(resource) as source:
        if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            return
        if destination.is_file():
            _backup_file(root, project, destination, backed_up)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _install_experimental_tmp_font(
    detection: Detection,
    project: Path,
    backed_up: set[str],
    enabled: bool,
    force: bool = False,
) -> dict:
    version = detect_unity_version(detection)
    state = {
        "enabled": bool(enabled),
        "required_unity_version": TMP_FONT_BUNDLE_UNITY_VERSION,
        "detected_unity_version": version,
        "bundle": "",
        "forced": bool(force),
        "status": "disabled",
    }
    if not enabled:
        return state
    if version != TMP_FONT_BUNDLE_UNITY_VERSION and not force:
        state["status"] = "version_unknown" if not version else "version_mismatch"
        return state
    root = Path(detection.root)
    destination = root / TMP_FONT_BUNDLE_FILE
    _copy_bundled_asset(root, project, TMP_FONT_BUNDLE_FILE, destination, backed_up)
    state["bundle"] = TMP_FONT_BUNDLE_FILE
    state["status"] = "installed"
    return state


def _font_registry_value(root: Path) -> str:
    identity = str(root.resolve()).casefold().encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    return f"GameKO {NOTO_FONT_NAME} {suffix} (OpenType)"


def _broadcast_font_change() -> None:
    # SendMessageTimeout prevents a hung window from blocking installation.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(
        ctypes.c_void_p(0xFFFF),  # HWND_BROADCAST
        0x001D,  # WM_FONTCHANGE
        0,
        0,
        0x0002,  # SMTO_ABORTIFHUNG
        1000,
        ctypes.byref(result),
    )


def _register_font(path: Path, registry_value: str) -> None:
    """Register a game-local font for the current Windows user and session."""
    if os.name != "nt":
        raise RuntimeError("Unity 한글 폰트 자동 등록은 Windows에서만 지원합니다.")
    import winreg

    path = path.resolve()
    previous: str | None = None
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, FONT_REGISTRY_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
    ) as key:
        try:
            value, value_type = winreg.QueryValueEx(key, registry_value)
            if value_type == winreg.REG_SZ:
                previous = value
        except FileNotFoundError:
            pass
        if previous and Path(previous).resolve() != path:
            raise RuntimeError(f"동일한 GameKO 폰트 등록 이름이 이미 사용 중입니다: {previous}")
        winreg.SetValueEx(key, registry_value, 0, winreg.REG_SZ, str(path))

    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.AddFontResourceExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    gdi32.AddFontResourceExW.restype = ctypes.c_int
    if gdi32.AddFontResourceExW(str(path), 0, None) == 0:
        if previous is None:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, FONT_REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, registry_value)
        raise RuntimeError("Noto Sans CJK KR 폰트를 Windows 글꼴 테이블에 등록하지 못했습니다.")
    _broadcast_font_change()


def _unregister_font(path: Path, registry_value: str) -> None:
    if os.name != "nt":
        return
    import winreg

    path = path.resolve()
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.RemoveFontResourceExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    gdi32.RemoveFontResourceExW.restype = ctypes.c_int
    gdi32.RemoveFontResourceExW(str(path), 0, None)

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            FONT_REGISTRY_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            registered, value_type = winreg.QueryValueEx(key, registry_value)
            if value_type == winreg.REG_SZ and Path(registered).resolve() == path:
                winreg.DeleteValue(key, registry_value)
    except FileNotFoundError:
        pass
    _broadcast_font_change()


def _install_noto_font(
    root: Path, project: Path, backed_up: set[str], previous: dict | None = None
) -> dict:
    fonts_dir = root / "GameKO.Fonts"
    font_path = fonts_dir / NOTO_FONT_FILE
    license_path = fonts_dir / NOTO_FONT_LICENSE_FILE
    font_added = not font_path.exists()
    license_added = not license_path.exists()

    assets = (
        (font_path, NOTO_FONT_FILE),
        (license_path, NOTO_FONT_LICENSE_FILE),
    )
    for path, resource_name in assets:
        _copy_bundled_asset(root, project, resource_name, path, backed_up)

    registry_value = _font_registry_value(root)
    _register_font(font_path, registry_value)
    previous = previous or {}
    previous_path = str(previous.get("path", ""))
    if previous_path and Path(previous_path) != font_path.relative_to(root):
        previous_registry = str(previous.get("registry_value", ""))
        if previous_registry:
            _unregister_font(root / previous_path, previous_registry)
        old_font = root / previous_path
        if previous.get("font_added") and old_font.is_file():
            old_font.unlink()
    return {
        "name": NOTO_FONT_NAME,
        "path": str(font_path.relative_to(root)),
        "license_path": str(license_path.relative_to(root)),
        "registry_value": registry_value,
        # Preserve ownership when install is run repeatedly before restore.
        "font_added": font_added or bool(previous.get("font_added")),
        "license_added": license_added or bool(previous.get("license_added")),
    }


def _write_config(
    path: Path,
    source_language: str = "auto",
    tmp_font_bundle: str = "",
) -> None:
    source_language = _normalize_source_language(source_language)
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    if path.exists():
        config.read(path, encoding="utf-8-sig")
    values = {
        "Service": {"Endpoint": "GoogleTranslateV2", "FallbackEndpoint": "GoogleTranslate"},
        "General": {"Language": "ko", "FromLanguage": source_language},
        "TextFrameworks": {
            "EnableUGUI": "True", "EnableUIElements": "True", "EnableNGUI": "True",
            "EnableTextMeshPro": "True", "EnableTextMesh": "True", "EnableIMGUI": "True",
        },
        "Behaviour": {
            "MaxCharactersPerTranslation": "400", "EnableUIResizing": "True",
            "ForceUIResizing": "True", "EnableBatching": "True",
            "OverrideFont": NOTO_FONT_NAME,
            "OverrideFontTextMeshPro": "" if tmp_font_bundle else NOTO_FONT_NAME,
            "FallbackFontTextMeshPro": tmp_font_bundle or NOTO_FONT_NAME,
        },
    }
    for section, options in values.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in options.items():
            config.set(section, key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        config.write(fh, space_around_delimiters=False)


def install(
    detection: Detection,
    source_language: str = "auto",
    experimental_tmp_font: bool = True,
    force_tmp_font: bool = False,
) -> str:
    root = Path(detection.root)
    configured_data_dir = detection.details.get("data_dir")
    data_dir = Path(configured_data_dir) if configured_data_dir else None
    if data_dir and not data_dir.is_dir():
        data_dir = None
    resolved_source_language = _resolve_source_language(
        root, source_language, data_dir, detection.details
    )
    project = project_path(root)
    project.mkdir(parents=True, exist_ok=True)
    state_path = project / "unity_install.json"
    previous_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    before = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and project not in p.parents}
    before_dirs = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir() and project not in p.parents and p != project}
    previous_backups = previous_state.get("backed_up_files", [])
    if not isinstance(previous_backups, list):
        previous_backups = []
    backed_up: set[str] = {
        value for value in previous_backups if isinstance(value, str)
    }
    if detection.variant == "Mono":
        already_installed = (root / "AutoTranslator").is_dir() and any(Path(detection.details["data_dir"]).rglob("XUnity.AutoTranslator.Plugin.Core.dll"))
        if already_installed:
            version = "existing"
        else:
            managed = Path(detection.details["data_dir"]) / "Managed"
            for dll in managed.rglob("*.dll") if managed.exists() else []:
                _backup_file(root, project, dll, backed_up)
            version = _install_release(root, project, "bbepis/XUnity.AutoTranslator", "XUnity.AutoTranslator-ReiPatcher-", backed_up)
            setup = root / "SetupReiPatcherAndAutoTranslator.exe"
            if not setup.exists():
                raise RuntimeError("XUnity ReiPatcher 설치 프로그램을 찾지 못했습니다.")
            result = subprocess.run([str(setup)], cwd=root, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                raise RuntimeError("XUnity ReiPatcher 설정 실패:\n" + (result.stderr or result.stdout)[-2000:])
        config_path = root / "AutoTranslator" / "Config.ini"
    else:
        if not (root / "BepInEx").is_dir():
            exe = Path(detection.details.get("exe", ""))
            machine = "x64"
            if exe.is_file():
                with exe.open("rb") as fh:
                    fh.seek(0x3C)
                    pe_offset = int.from_bytes(fh.read(4), "little")
                    fh.seek(pe_offset + 4)
                    machine = "x86" if int.from_bytes(fh.read(2), "little") == 0x14C else "x64"
            _install_release(root, project, "BepInEx/BepInEx", f"Unity.IL2CPP-win-{machine}-", backed_up, include_prerelease=True)
        plugin = root / "BepInEx" / "plugins" / "XUnity.AutoTranslator" / "XUnity.AutoTranslator.Plugin.Core.dll"
        version = "existing" if plugin.exists() else _install_release(root, project, "bbepis/XUnity.AutoTranslator", "XUnity.AutoTranslator-BepInEx-IL2CPP-", backed_up)
        config_path = root / "BepInEx" / "config" / "AutoTranslatorConfig.ini"
    font_state = _install_noto_font(root, project, backed_up, previous_state.get("font"))
    tmp_font_state = _install_experimental_tmp_font(
        detection, project, backed_up, experimental_tmp_font, force_tmp_font
    )
    if config_path.exists():
        _backup_file(root, project, config_path, backed_up)
    _write_config(
        config_path, resolved_source_language, str(tmp_font_state.get("bundle", ""))
    )
    after = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and project not in p.parents}
    after_dirs = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir() and project not in p.parents and p != project}
    state = {
        "version": version,
        "variant": detection.variant,
        "source_language": resolved_source_language,
        "added_files": sorted((after - before) | set(previous_state.get("added_files", []))),
        "added_dirs": sorted((after_dirs - before_dirs) | set(previous_state.get("added_dirs", []))),
        "backed_up_files": sorted(backed_up),
        "config": str(config_path.relative_to(root)),
        "font": font_state,
        "tmp_font": tmp_font_state,
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return version


def restore(root: Path) -> int:
    state_path = project_path(root) / "unity_install.json"
    if not state_path.exists():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unity XUnity 백업 정보가 손상되었습니다: {state_path}") from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"Unity XUnity 백업 정보가 손상되었습니다: {state_path}")
    backed_up_files = state.get("backed_up_files", [])
    if not isinstance(backed_up_files, list) or not all(
        isinstance(value, str) for value in backed_up_files
    ):
        raise RuntimeError(f"Unity XUnity 백업 경로가 손상되었습니다: {state_path}")
    backup_root = project_path(root) / "backup" / "unity_overwritten"
    invalid_backups = [
        rel for rel in backed_up_files
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts
    ]
    if invalid_backups:
        raise RuntimeError(f"Unity XUnity 백업 경로가 손상되었습니다: {invalid_backups[0]}")
    missing_backups = [rel for rel in backed_up_files if not (backup_root / rel).is_file()]
    if missing_backups:
        sample = ", ".join(missing_backups[:3])
        raise RuntimeError(
            f"Unity XUnity 원본 백업이 없습니다 ({len(missing_backups):,}개): {sample}. "
            "복원을 시작하지 않았습니다."
        )
    removed = 0
    font_state = state.get("font", {})
    font_rel = font_state.get("path", "")
    if font_rel:
        font_path = (root / font_rel).resolve()
        if root.resolve() in font_path.parents:
            _unregister_font(font_path, str(font_state.get("registry_value", "")))
    added_files = set(state.get("added_files", []))
    if font_state.get("font_added") and font_rel:
        added_files.add(font_rel)
    if font_state.get("license_added") and font_state.get("license_path"):
        added_files.add(str(font_state["license_path"]))
    for rel in sorted(added_files):
        path = (root / rel).resolve()
        if root.resolve() in path.parents and path.is_file():
            path.unlink()
            removed += 1
    for rel in backed_up_files:
        source = backup_root / rel
        target = (root / rel).resolve()
        if root.resolve() not in target.parents:
            raise RuntimeError(f"게임 폴더 밖의 Unity 백업 경로입니다: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if not filecmp.cmp(source, target, shallow=False):
            raise RuntimeError(f"Unity XUnity 원본 복원 검증에 실패했습니다: {rel}")
        removed += 1
    for rel in sorted(state.get("added_dirs", []), key=lambda value: len(Path(value).parts), reverse=True):
        directory = (root / rel).resolve()
        if root.resolve() not in directory.parents:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    if font_rel:
        try:
            (root / font_rel).parent.rmdir()
        except OSError:
            pass
    state_path.unlink(missing_ok=True)
    return removed
