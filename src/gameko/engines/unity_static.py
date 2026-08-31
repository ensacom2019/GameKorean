from __future__ import annotations

import base64
import csv
import hashlib
from importlib.resources import as_file, files
import io
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
from typing import Any, Iterable, Iterator

from ..model import Detection, Entry, project_path
from ..text import guess_language, is_translatable


POINTER_PREFIX = "gameko-unity-static:"
STATE_FILE = "unity_static_state.json"
SCAN_REPORT_FILE = "unity_static_scan.json"
FONT_REPORT_FILE = "unity_static_font_report.json"
BACKUP_DIR = "backup/unity_static"
TMP_FONT_BUNDLE_FILE = "gameko_notosanscjkkr_sdf_u6000_3_23f1"
MAX_CANDIDATES = 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
TEXT_SUFFIXES = {".json", ".csv", ".tsv", ".txt", ".ink", ".yarn"}
PLAIN_TEXT_HINT = re.compile(
    r"dialog|locali[sz]|lang|story|script|scenario|message|subtitle|caption|conversation|string|"
    r"(?:^|[_ .-])text(?:$|[_ .-])",
    re.I,
)


def _unitypy():
    try:
        import UnityPy
        from UnityPy.helpers import TypeTreeHelper
    except ImportError as exc:
        raise RuntimeError(
            "Unity 정적 패치 테스트에는 UnityPy 1.25가 필요합니다. GameKO를 다시 설치하거나 최신 배포본을 사용하세요."
        ) from exc
    major_minor = ".".join(str(getattr(UnityPy, "__version__", "0")).split(".")[:2])
    if major_minor != "1.25":
        raise RuntimeError(
            f"검증되지 않은 UnityPy 버전입니다: {getattr(UnityPy, '__version__', 'unknown')} (필요: 1.25.x)"
        )
    # UnityPy 문서가 경고하는 네이티브 typetree 충돌을 피하고 안전성을 우선합니다.
    TypeTreeHelper.read_typetree_boost = False
    return UnityPy


def _load_environment(UnityPy: Any, stream: Any, logical_name: str, source_dir: Path) -> Any:
    """Load one file with a stable logical name while retaining dependency lookup."""
    env = UnityPy.Environment(path=str(source_dir))
    loaded = env.load_file(stream, name=logical_name)
    env.file = loaded
    return env


def _clone_serialized(value: Any) -> Any:
    """Clone Unity typetree values without copying non-pickleable TypeTreeNode objects."""
    if isinstance(value, list):
        return [_clone_serialized(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_serialized(item) for item in value)
    if isinstance(value, dict):
        return {key: _clone_serialized(item) for key, item in value.items()}
    if type(value).__name__ == "UnknownObject" and hasattr(value, "__dict__"):
        node = value.__dict__.get("__node__")
        fields = {
            key: _clone_serialized(item)
            for key, item in value.__dict__.items()
            if key != "__node__"
        }
        return type(value)(__node__=node, **fields)
    return value


TMP_FONT_FIELDS = (
    "m_Version", "m_FaceInfo", "m_AtlasPopulationMode", "InternalDynamicOS",
    "m_GlyphTable", "m_CharacterTable", "m_AtlasTextureIndex",
    "m_IsMultiAtlasTexturesEnabled", "m_GetFontFeatures", "m_ClearDynamicDataOnBuild",
    "m_AtlasWidth", "m_AtlasHeight", "m_AtlasPadding", "m_AtlasRenderMode",
    "m_UsedGlyphRects", "m_FreeGlyphRects", "m_FontFeatureTable",
    "m_ShouldReimportFontFeatures", "normalStyle", "normalSpacingOffset",
    "boldStyle", "boldSpacing", "italicStyle", "tabSize",
)


def _load_tmp_font_payload() -> dict[str, Any]:
    UnityPy = _unitypy()
    resource = files("gameko").joinpath("assets", TMP_FONT_BUNDLE_FILE)
    with as_file(resource) as source_path, source_path.open("rb") as source:
        env = _load_environment(UnityPy, source, TMP_FONT_BUNDLE_FILE, source_path.parent)
        font_obj = next(
            obj
            for obj in env.objects
            if getattr(getattr(obj, "type", None), "name", "") == "MonoBehaviour"
            and hasattr(obj.parse_as_object(), "m_CharacterTable")
            and hasattr(obj.parse_as_object(), "m_GlyphTable")
        )
        font = font_obj.parse_as_object()
        texture = next(
            obj.parse_as_object()
            for obj in env.objects
            if getattr(getattr(obj, "type", None), "name", "") == "Texture2D"
        )
        return {
            # Unity 릴리스 빌드는 MonoBehaviour typetree를 제거하는 경우가
            # 많습니다. 번들에 포함된 같은 TMP_FontAsset 스키마를 보관해
            # script/type hash가 일치하는 자산을 안전하게 역직렬화합니다.
            "node": font_obj.serialized_type.node,
            "script_id": bytes(font_obj.serialized_type.script_id),
            "old_type_hash": bytes(font_obj.serialized_type.old_type_hash),
            "fields": {
                name: _clone_serialized(getattr(font, name))
                for name in TMP_FONT_FIELDS if hasattr(font, name)
            },
            # UnityPy의 image 편의 속성은 오디오 변환기와 FMOD DLL까지 함께
            # 불러옵니다. 폰트 아틀라스는 검증된 비압축 RGBA32이므로 원시
            # 데이터를 그대로 옮겨 동결 EXE에도 불필요한 오디오 의존성을
            # 만들지 않습니다.
            "texture": {
                "image_data": bytes(texture.get_image_data()),
                "m_Width": int(texture.m_Width),
                "m_Height": int(texture.m_Height),
                "m_TextureFormat": int(texture.m_TextureFormat),
                "m_CompleteImageSize": int(texture.m_CompleteImageSize),
                "m_MipCount": int(texture.m_MipCount) if texture.m_MipCount is not None else None,
                "m_MipMap": bool(texture.m_MipMap) if texture.m_MipMap is not None else None,
            },
            "characters": len(font.m_CharacterTable),
        }


def _looks_like_tmp_font(data: Any) -> bool:
    return all(
        hasattr(data, name)
        for name in ("m_CharacterTable", "m_GlyphTable", "m_AtlasTextures", "m_Material")
    )


def _read_tmp_font(obj: Any, payload: dict[str, Any]) -> tuple[Any, Any | None]:
    """Read a TMP font, supplying the bundled schema for stripped player assets."""
    try:
        return obj.parse_as_object(), None
    except Exception:
        serialized_type = getattr(obj, "serialized_type", None)
        if (
            serialized_type is None
            or bytes(getattr(serialized_type, "script_id", b"")) != payload["script_id"]
            or bytes(getattr(serialized_type, "old_type_hash", b"")) != payload["old_type_hash"]
        ):
            raise
        return obj.read_typetree(nodes=payload["node"], wrap=True), payload["node"]


def _patch_tmp_fonts_in_container(
    source: Path, destination: Path, logical_name: str, payload: dict[str, Any]
) -> int:
    UnityPy = _unitypy()
    with source.open("rb") as input_stream:
        env = _load_environment(UnityPy, input_stream, logical_name, source.parent)
        fonts: list[tuple[Any, Any, Any | None]] = []
        for obj in env.objects:
            if getattr(getattr(obj, "type", None), "name", "") != "MonoBehaviour":
                continue
            try:
                data, schema_node = _read_tmp_font(obj, payload)
            except Exception:
                continue
            if not _looks_like_tmp_font(data) or not data.m_AtlasTextures:
                continue
            fonts.append((obj, data, schema_node))
        if not fonts:
            return 0

        # 기존 일본어/중국어 문자표를 모두 한글 문자표로 덮어쓰면 언어 선택
        # 화면의 日本語/简体中文 같은 라벨이 네모가 됩니다. 컨테이너마다
        # 대표 fallback 하나만 한글 SDF로 바꾸고 나머지 폰트는 원본 글리프를
        # 유지한 채 그 fallback을 참조하게 합니다.
        def host_score(record: tuple[Any, Any, Any | None]) -> tuple[int, int]:
            obj, data, _schema_node = record
            name = str(getattr(data, "m_Name", "")).lower()
            if "fallback" in name:
                rank = 0
            elif "liberation" in name or "arial" in name:
                rank = 1
            elif not any(token in name for token in ("jp", "sc", "tc", "japan", "china")):
                rank = 2
            else:
                rank = 3
            return rank, int(getattr(obj, "path_id", 0))

        local_hosts = [
            record for record in fonts
            if int(getattr(record[1].m_AtlasTextures[0], "m_FileID", 0)) == 0
            and record[1].m_AtlasTextures[0].deref() is not None
        ]
        if not local_hosts:
            raise ValueError("같은 파일 안에서 교체 가능한 TMP fallback 아틀라스를 찾지 못했습니다.")
        host_obj, host_data, host_schema = min(local_hosts, key=host_score)
        atlas_ptr = host_data.m_AtlasTextures[0]
        texture_obj = atlas_ptr.deref()
        if texture_obj is None or getattr(getattr(texture_obj, "type", None), "name", "") != "Texture2D":
            raise ValueError(f"TMP 아틀라스 Texture2D를 찾지 못했습니다: {host_data.m_Name}")

        for field, value in payload["fields"].items():
            if hasattr(host_data, field):
                setattr(host_data, field, _clone_serialized(value))
        # 기존 게임의 material/script/path ID는 보존해 UI 참조를 끊지 않습니다.
        host_data.m_AtlasTextures = [atlas_ptr]
        host_data.m_AtlasTextureIndex = 0
        if hasattr(host_data, "m_IsMultiAtlasTexturesEnabled"):
            host_data.m_IsMultiAtlasTexturesEnabled = False

        for obj, data, schema_node in fonts:
            if obj is not host_obj:
                fallbacks = list(getattr(data, "m_FallbackFontAssetTable", []))
                host_path_id = int(getattr(host_obj, "path_id", 0))
                if not any(
                    int(getattr(pointer, "m_FileID", -1)) == 0
                    and int(getattr(pointer, "m_PathID", -1)) == host_path_id
                    for pointer in fallbacks
                ):
                    pointer_type = type(data.m_Material)
                    fallbacks.append(
                        pointer_type(
                            m_FileID=0,
                            m_PathID=host_path_id,
                            assetsfile=obj.assets_file,
                        )
                    )
                    data.m_FallbackFontAssetTable = fallbacks
            if schema_node is None:
                obj.patch(data)
            else:
                obj.save_typetree(data, nodes=schema_node)

        texture = texture_obj.parse_as_object()
        texture_name = getattr(texture, "m_Name", "")
        donor = payload["texture"]
        texture.image_data = donor["image_data"]
        texture.m_Width = donor["m_Width"]
        texture.m_Height = donor["m_Height"]
        texture.m_TextureFormat = donor["m_TextureFormat"]
        texture.m_CompleteImageSize = len(donor["image_data"])
        if texture.m_MipCount is not None:
            texture.m_MipCount = donor["m_MipCount"] or 1
        if texture.m_MipMap is not None:
            texture.m_MipMap = bool(donor["m_MipMap"])
        if texture.m_StreamData is not None:
            texture.m_StreamData.path = ""
            texture.m_StreamData.offset = 0
            texture.m_StreamData.size = 0
        texture.m_Name = texture_name
        texture_obj.patch(texture)

        packer = "original" if type(env.file).__name__ == "BundleFile" else None
        raw = env.file.save(packer=packer)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return len(fonts)


def _count_korean_tmp_fonts(
    path: Path, logical_name: str, payload: dict[str, Any] | None = None
) -> int:
    UnityPy = _unitypy()
    payload = payload or _load_tmp_font_payload()
    fonts: list[tuple[int, bool, list[tuple[int, int]]]] = []
    with path.open("rb") as source:
        env = _load_environment(UnityPy, source, logical_name, path.parent)
        for obj in env.objects:
            if getattr(getattr(obj, "type", None), "name", "") != "MonoBehaviour":
                continue
            try:
                data, _schema_node = _read_tmp_font(obj, payload)
                unicodes = {
                    int(getattr(character, "m_Unicode"))
                    for character in data.m_CharacterTable
                }
                fallbacks = [
                    (int(pointer.m_FileID), int(pointer.m_PathID))
                    for pointer in getattr(data, "m_FallbackFontAssetTable", [])
                ]
            except Exception:
                continue
            fonts.append(
                (
                    int(getattr(obj, "path_id", 0)),
                    ord("가") in unicodes and ord("힣") in unicodes,
                    fallbacks,
                )
            )
    direct_korean = {path_id for path_id, direct, _fallbacks in fonts if direct}
    return sum(
        1
        for path_id, direct, fallbacks in fonts
        if direct or any(file_id == 0 and target in direct_korean for file_id, target in fallbacks)
    )


def _apply_forced_tmp_font(root: Path, detection: Detection, backup_root: Path) -> tuple[list[str], int]:
    project_path(root).mkdir(parents=True, exist_ok=True)
    containers, _loose = _candidate_files(detection)
    payload = _load_tmp_font_payload()
    report: dict[str, Any] = {
        "format": 1, "forced": True, "candidates": len(containers),
        "font_assets": 0, "patched_files": [], "errors": [],
    }
    staged: dict[str, Path] = {}
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="unity-font-", dir=project_path(root)) as temp:
        staging = Path(temp)
        for target in containers:
            relative = _safe_relative(root, target)
            output = staging / Path(relative)
            try:
                patched = _patch_tmp_fonts_in_container(target, output, relative, payload)
                if not patched:
                    continue
                if _count_korean_tmp_fonts(output, relative, payload) < patched:
                    raise RuntimeError("한글 TMP 문자표 재검증에 실패했습니다.")
                staged[relative] = output
                counts[relative] = patched
            except Exception as exc:
                report["errors"].append(f"{relative}: {exc}")

        for relative, output in staged.items():
            target = _resolve_game_file(root, relative)
            backup = backup_root / Path(relative)
            if not backup.is_file():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            shutil.copy2(output, target)

    report["patched_files"] = sorted(staged)
    report["font_assets"] = sum(counts.values())
    report_path = project_path(root) / FONT_REPORT_FILE
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return sorted(staged), int(report["font_assets"])


def _safe_relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RuntimeError(f"게임 폴더 밖의 Unity 파일은 처리하지 않습니다: {path}")
    return resolved.relative_to(resolved_root).as_posix()


def _resolve_game_file(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RuntimeError(f"올바르지 않은 Unity 파일 경로입니다: {relative}")
    target = (root / Path(relative)).resolve()
    _safe_relative(root, target)
    return target


def _encode_pointer(data: dict[str, Any]) -> str:
    return POINTER_PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _decode_pointer(value: str) -> dict[str, Any]:
    if not value.startswith(POINTER_PREFIX):
        raise ValueError("Unity 정적 패치 포인터가 아닙니다.")
    data = json.loads(value[len(POINTER_PREFIX):])
    if not isinstance(data, dict) or data.get("kind") not in {"container", "loose"}:
        raise ValueError("Unity 정적 패치 포인터가 손상되었습니다.")
    return data


def _walk_json_strings(node: Any, parts: list[str | int]) -> Iterator[tuple[list[str | int], str]]:
    if isinstance(node, str):
        if is_translatable(node):
            yield parts, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_json_strings(value, parts + [str(key)])
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_json_strings(value, parts + [index])


def _resolve_json_path(node: Any, parts: list[Any]) -> tuple[Any, str | int]:
    if not parts:
        raise ValueError("JSON 루트 문자열은 안전 패치 대상에서 제외합니다.")
    current = node
    for raw in parts[:-1]:
        key: str | int = int(raw) if isinstance(current, list) else str(raw)
        current = current[key]
    last: str | int = int(parts[-1]) if isinstance(current, list) else str(parts[-1])
    return current, last


def _csv_rows(text: str, suffix: str) -> tuple[list[list[str]], str]:
    delimiter = "\t" if suffix.lower() == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return rows, delimiter


def _line_records(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _line_value(line: str) -> str:
    return line.rstrip("\r\n")


def _content_records(text: str, name: str) -> tuple[str, list[tuple[list[Any], str]]]:
    if not text or len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        return "", []
    if "\x00" in text or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        return "", []
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(text.lstrip("\ufeff"))
        except json.JSONDecodeError:
            pass
        else:
            return "json", [(parts, value) for parts, value in _walk_json_strings(data, []) if parts]

    suffix = Path(name).suffix.lower()
    if suffix in {".csv", ".tsv"}:
        try:
            rows, _ = _csv_rows(text, suffix)
        except csv.Error:
            return "", []
        records: list[tuple[list[Any], str]] = []
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                if is_translatable(value):
                    records.append(([row_index, column_index], value))
        return "csv", records

    if suffix in {".txt", ".ink", ".yarn"} or PLAIN_TEXT_HINT.search(Path(name).stem):
        records = []
        for index, line in enumerate(_line_records(text)):
            value = _line_value(line)
            if is_translatable(value):
                records.append(([index], value))
        return "lines", records
    return "", []


def _localization_table_records(data: Any) -> tuple[str, list[tuple[list[Any], str]]]:
    """Return Unity Localization StringTable rows from a serialized asset."""
    table = getattr(data, "m_TableData", None)
    locale_id = getattr(data, "m_LocaleId", None)
    locale = str(getattr(locale_id, "m_Code", "") or "")
    if not isinstance(table, list) or not locale:
        return "", []
    records: list[tuple[list[Any], str]] = []
    for index, item in enumerate(table):
        value = getattr(item, "m_Localized", None)
        if isinstance(value, str) and is_translatable(value):
            records.append(([index], value))
    return locale, records


def _looks_like_unity_container(path: Path) -> bool:
    lower = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in {".assets", ".bundle", ".unity3d"}:
        return True
    if lower in {"globalgamemanagers", "data.unity3d"} or lower.startswith(("level", "resources.assets", "sharedassets")):
        return True
    try:
        with path.open("rb") as fh:
            signature = fh.read(16)
    except OSError:
        return False
    return signature.startswith((b"UnityFS\0", b"UnityWeb\0", b"UnityRaw\0"))


def _candidate_files(detection: Detection) -> tuple[list[Path], list[Path]]:
    root = Path(detection.root).resolve()
    data_dir = Path(detection.details.get("data_dir", "")).resolve()
    if not data_dir.is_dir() or root not in data_dir.parents:
        raise RuntimeError("Unity 데이터 폴더를 찾지 못했습니다.")

    containers: list[Path] = []
    loose: list[Path] = []
    seen: set[Path] = set()
    streaming = data_dir / "StreamingAssets"

    def consider(path: Path, allow_loose: bool) -> None:
        if len(seen) >= MAX_CANDIDATES or path in seen or not path.is_file() or path.is_symlink():
            return
        seen.add(path)
        if path.suffix.lower() in {".ress", ".resource", ".dll", ".pdb", ".exe"}:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= 0:
            return
        if _looks_like_unity_container(path):
            containers.append(path)
        elif allow_loose and path.suffix.lower() in TEXT_SUFFIXES and size <= MAX_TEXT_BYTES:
            loose.append(path)

    # Unity의 기본 SerializedFile은 데이터 폴더 바로 아래에 있습니다.
    try:
        for path in data_dir.iterdir():
            consider(path, False)
    except OSError:
        pass
    # AssetBundle, Addressables 및 느슨한 로컬라이징 파일은 StreamingAssets 아래를 검사합니다.
    if streaming.is_dir():
        try:
            for path in streaming.rglob("*"):
                consider(path, True)
                if len(seen) >= MAX_CANDIDATES:
                    break
        except OSError:
            pass
    return sorted(containers), sorted(loose)


def _object_key(obj: Any) -> tuple[str, int]:
    assets_file = getattr(obj, "assets_file", None)
    internal = str(getattr(assets_file, "name", ""))
    return internal, int(getattr(obj, "path_id"))


def _extract_container(
    root: Path, path: Path, report: dict[str, Any], source_path: Path | None = None
) -> list[Entry]:
    UnityPy = _unitypy()
    entries: list[Entry] = []
    rel = _safe_relative(root, path)
    try:
        # UnityPy 1.25는 경로 문자열로 열면 Windows에서 입력 파일 핸들을 늦게
        # 해제할 수 있으므로 우리가 직접 연 스트림의 수명을 명확하게 제한합니다.
        with (source_path or path).open("rb") as source:
            env = _load_environment(UnityPy, source, rel, (source_path or path).parent)
            report["versions"].update(
                str(getattr(getattr(obj, "assets_file", None), "unity_version", ""))
                for obj in env.objects
                if getattr(getattr(obj, "assets_file", None), "unity_version", "")
            )
            for obj in env.objects:
                object_type = getattr(getattr(obj, "type", None), "name", "")
                if object_type == "TextAsset":
                    try:
                        data = obj.parse_as_object()
                        name = str(getattr(data, "m_Name", "TextAsset"))
                        script = str(getattr(data, "m_Script", ""))
                        content_format, records = _content_records(script, name)
                    except Exception as exc:
                        report["errors"].append(f"{rel}: TextAsset {getattr(obj, 'path_id', '?')} 읽기 실패: {exc}")
                        continue
                    locale = ""
                elif object_type == "MonoBehaviour":
                    try:
                        data = obj.parse_as_object()
                        locale, records = _localization_table_records(data)
                        name = str(getattr(data, "m_Name", "StringTable"))
                    except Exception:
                        continue
                    if not locale:
                        continue
                    content_format = "string_table"
                else:
                    continue
                if not content_format:
                    continue
                internal, path_id = _object_key(obj)
                for parts, value in records:
                    pointer = _encode_pointer({
                        "kind": "container", "internal": internal, "path_id": path_id,
                        "container": rel, "format": content_format, "path": parts, "name": name,
                        "locale": locale,
                    })
                    context = (
                        f"Unity StringTable: {name} ({locale})"
                        if content_format == "string_table"
                        else f"Unity TextAsset: {name} ({content_format})"
                    )
                    entry = Entry.create(value, rel, pointer, context)
                    language = locale.split("-", 1)[0].lower() if locale else ""
                    # Locale가 명시된 StringTable은 한자만 있는 중국어 문구가
                    # 일본어로 오인되지 않도록 실제 locale을 우선합니다.
                    entry.source_lang = language or guess_language(value)
                    entries.append(entry)
        report["scanned_containers"] += 1
    except Exception as exc:
        report["errors"].append(f"{rel}: Unity 리소스 읽기 실패: {exc}")
    return entries


def _decode_loose(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("텍스트 파일이 안전 제한(8 MiB)을 넘습니다.")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeError:
            continue
    raise UnicodeError("지원 인코딩(UTF-8/UTF-16/CP932)이 아닙니다.")


def _extract_loose(
    root: Path, path: Path, report: dict[str, Any], source_path: Path | None = None
) -> list[Entry]:
    rel = _safe_relative(root, path)
    try:
        text, encoding = _decode_loose(source_path or path)
        content_format, records = _content_records(text, path.name)
    except Exception as exc:
        report["errors"].append(f"{rel}: 느슨한 텍스트 읽기 실패: {exc}")
        return []
    entries = []
    for parts, value in records:
        pointer = _encode_pointer({
            "kind": "loose", "format": content_format, "path": parts,
            "name": path.name, "encoding": encoding,
        })
        entry = Entry.create(value, rel, pointer, f"Unity StreamingAssets: {path.name} ({content_format})")
        entry.source_lang = guess_language(value)
        entries.append(entry)
    report["scanned_loose"] += 1
    return entries


def extract(detection: Detection) -> list[Entry]:
    root = Path(detection.root).resolve()
    containers, loose = _candidate_files(detection)
    report: dict[str, Any] = {
        "format": 1,
        "unitypy_version": str(getattr(_unitypy(), "__version__", "unknown")),
        "container_candidates": len(containers),
        "loose_candidates": len(loose),
        "scanned_containers": 0,
        "scanned_loose": 0,
        "versions": set(),
        "errors": [],
    }
    entries: list[Entry] = []
    backup_root = project_path(root) / BACKUP_DIR
    for path in containers:
        relative = _safe_relative(root, path)
        baseline = backup_root / Path(relative)
        entries.extend(
            _extract_container(root, path, report, baseline if baseline.is_file() else None)
        )
    for path in loose:
        relative = _safe_relative(root, path)
        baseline = backup_root / Path(relative)
        entries.extend(
            _extract_loose(root, path, report, baseline if baseline.is_file() else None)
        )
    report["versions"] = sorted(report["versions"])
    report["entries"] = len(entries)
    output = project_path(root)
    output.mkdir(parents=True, exist_ok=True)
    (output / SCAN_REPORT_FILE).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entries


def _patch_content(text: str, content_format: str, items: list[tuple[dict[str, Any], Entry]]) -> str:
    if content_format == "json":
        bom = text.startswith("\ufeff")
        data = json.loads(text.lstrip("\ufeff"))
        for pointer, entry in items:
            parent, key = _resolve_json_path(data, pointer["path"])
            if parent[key] != entry.source:
                raise ValueError(f"원문이 바뀌었습니다: {entry.source[:80]}")
            parent[key] = entry.target
        compact = "\n" not in text
        result = json.dumps(
            data, ensure_ascii=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        )
        return ("\ufeff" if bom else "") + result

    if content_format == "csv":
        suffix = Path(str(items[0][0].get("name", ""))).suffix.lower()
        rows, delimiter = _csv_rows(text, suffix)
        for pointer, entry in items:
            row, column = map(int, pointer["path"])
            if rows[row][column] != entry.source:
                raise ValueError(f"원문이 바뀌었습니다: {entry.source[:80]}")
            rows[row][column] = entry.target
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=delimiter, lineterminator="\r\n" if "\r\n" in text else "\n")
        writer.writerows(rows)
        return output.getvalue()

    if content_format == "lines":
        lines = _line_records(text)
        for pointer, entry in items:
            index = int(pointer["path"][0])
            ending = lines[index][len(_line_value(lines[index])):]
            if _line_value(lines[index]) != entry.source:
                raise ValueError(f"원문이 바뀌었습니다: {entry.source[:80]}")
            lines[index] = entry.target + ending
        return "".join(lines)
    raise ValueError(f"지원하지 않는 Unity 텍스트 형식입니다: {content_format}")


def _group_entries(entries: Iterable[Entry]) -> dict[str, list[tuple[dict[str, Any], Entry]]]:
    grouped: dict[str, list[tuple[dict[str, Any], Entry]]] = {}
    for entry in entries:
        if not entry.target.strip():
            continue
        pointer = _decode_pointer(entry.pointer)
        grouped.setdefault(entry.file, []).append((pointer, entry))
    return grouped


def _patch_container(source: Path, destination: Path, items: list[tuple[dict[str, Any], Entry]]) -> None:
    UnityPy = _unitypy()
    with source.open("rb") as input_stream:
        logical_name = str(items[0][0].get("container") or source.name)
        env = _load_environment(UnityPy, input_stream, logical_name, source.parent)
        wanted: dict[tuple[str, int], list[tuple[dict[str, Any], Entry]]] = {}
        for pointer, entry in items:
            wanted.setdefault((str(pointer["internal"]), int(pointer["path_id"])), []).append((pointer, entry))
        found: set[tuple[str, int]] = set()
        for obj in env.objects:
            key = _object_key(obj)
            linked = wanted.get(key)
            if not linked:
                continue
            data = obj.parse_as_object()
            formats = {str(pointer["format"]) for pointer, _ in linked}
            if len(formats) != 1:
                raise ValueError(f"동일 Unity 객체에 서로 다른 형식 포인터가 있습니다: {key}")
            content_format = formats.pop()
            object_type = getattr(getattr(obj, "type", None), "name", "")
            if content_format == "string_table":
                if object_type != "MonoBehaviour":
                    raise ValueError(f"대상 Unity 객체가 StringTable이 아닙니다: {key}")
                table = getattr(data, "m_TableData", None)
                if not isinstance(table, list):
                    raise ValueError(f"Unity StringTable 데이터가 없습니다: {key}")
                for pointer, entry in linked:
                    index = int(pointer["path"][0])
                    current = str(getattr(table[index], "m_Localized"))
                    if current != entry.source:
                        raise ValueError(f"원문이 바뀌었습니다: {entry.source[:80]}")
                    table[index].m_Localized = entry.target
            else:
                if object_type != "TextAsset":
                    raise ValueError(f"대상 Unity 객체가 TextAsset이 아닙니다: {key}")
                data.m_Script = _patch_content(str(data.m_Script), content_format, linked)
            obj.patch(data)
            found.add(key)
        missing = set(wanted) - found
        if missing:
            raise ValueError(f"Unity 텍스트 객체를 다시 찾지 못했습니다: {sorted(missing)[:3]}")
        packer = "original" if type(env.file).__name__ == "BundleFile" else None
        raw = env.file.save(packer=packer)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)


def _patch_loose(source: Path, destination: Path, items: list[tuple[dict[str, Any], Entry]]) -> None:
    text, detected_encoding = _decode_loose(source)
    formats = {str(pointer["format"]) for pointer, _ in items}
    if len(formats) != 1:
        raise ValueError("동일 파일에 서로 다른 텍스트 형식 포인터가 있습니다.")
    patched = _patch_content(text, formats.pop(), items)
    encoding = str(items[0][0].get("encoding") or detected_encoding)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patched.encode(encoding))


def _binary_catalog_crc_records(raw: bytes) -> list[tuple[int, int, int, int]]:
    """Return (hash offset, CRC offset, CRC, bundle size) records from catalog.bin."""
    records: list[tuple[int, int, int, int]] = []
    for match in re.finditer(rb"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", raw):
        end = match.end()
        if end + 16 > len(raw):
            continue
        first_ref, second_ref, crc, size = struct.unpack_from("<IIII", raw, end)
        # Addressables BinaryStorageBuffer bundle options point back to the
        # nearby hash record. This rejects unrelated GUID/hash strings.
        if (
            second_ref != match.start()
            or first_ref > match.start()
            or match.start() - first_ref > 128
            or size <= 0
        ):
            continue
        records.append((match.start(), end + 8, crc, size))
    return records


def _patch_binary_catalog_crc(raw: bytes, bundle_name: str, original_size: int) -> bytes:
    records = _binary_catalog_crc_records(raw)
    name = Path(bundle_name).name.encode("utf-8").lower()
    lowered = raw.lower()
    name_offsets: list[int] = []
    start = 0
    while True:
        offset = lowered.find(name, start)
        if offset < 0:
            break
        name_offsets.append(offset)
        start = offset + 1
    if not name_offsets:
        raise ValueError(f"Addressables catalog.bin에서 번들 이름을 찾지 못했습니다: {bundle_name}")

    exact_size = [record for record in records if record[3] == original_size]
    candidates = exact_size or records
    ranked: list[tuple[int, tuple[int, int, int, int]]] = []
    for record in candidates:
        preceding = [offset for offset in name_offsets if offset <= record[0]]
        if not preceding:
            continue
        distance = record[0] - max(preceding)
        if distance <= 4096:
            ranked.append((distance, record))
    if not ranked:
        raise ValueError(f"Addressables CRC 레코드를 찾지 못했습니다: {bundle_name}")
    ranked.sort(key=lambda item: item[0])
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise ValueError(f"Addressables CRC 레코드가 모호합니다: {bundle_name}")
    _distance, (_hash_offset, crc_offset, _crc, _size) = ranked[0]
    patched = bytearray(raw)
    struct.pack_into("<I", patched, crc_offset, 0)
    return bytes(patched)


def _patch_json_catalog_crc(raw: bytes) -> bytes:
    text = raw.decode("utf-8-sig")
    data = json.loads(text)
    changed = 0

    def visit(value: Any) -> None:
        nonlocal changed
        if isinstance(value, dict):
            if "m_Crc" in value and isinstance(value["m_Crc"], (int, float)):
                value["m_Crc"] = 0
                changed += 1
            for key, child in list(value.items()):
                if key == "m_ExtraDataString" and isinstance(child, str):
                    try:
                        decoded = base64.b64decode(child, validate=True)
                    except (ValueError, TypeError):
                        pass
                    else:
                        patched = bytearray(decoded)
                        records = _binary_catalog_crc_records(decoded)
                        for _hash_offset, crc_offset, _crc, _size in records:
                            struct.pack_into("<I", patched, crc_offset, 0)
                        if records:
                            value[key] = base64.b64encode(patched).decode("ascii")
                            changed += len(records)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    if not changed:
        raise ValueError("Addressables catalog.json에서 CRC 필드를 찾지 못했습니다.")
    compact = "\n" not in text
    result = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )
    return result.encode("utf-8")


def _find_addressables_catalog(root: Path, bundle: Path) -> Path:
    for parent in (bundle.parent, *bundle.parents):
        if parent == root.parent:
            break
        for name in ("catalog.bin", "catalog.json"):
            candidate = parent / name
            if candidate.is_file():
                _safe_relative(root, candidate)
                return candidate
        if parent == root:
            break
    raise FileNotFoundError(f"수정할 Addressables catalog를 찾지 못했습니다: {bundle}")


def _stage_addressables_catalogs(
    root: Path,
    staging: Path,
    backup_root: Path,
    bundles: list[tuple[Path, int]],
) -> dict[str, Path]:
    grouped: dict[Path, list[tuple[Path, int]]] = {}
    for bundle, original_size in bundles:
        grouped.setdefault(_find_addressables_catalog(root, bundle), []).append(
            (bundle, original_size)
        )

    staged: dict[str, Path] = {}
    for catalog, linked in grouped.items():
        relative = _safe_relative(root, catalog)
        baseline = backup_root / Path(relative)
        source = baseline if baseline.is_file() else catalog
        raw = source.read_bytes()
        if catalog.suffix.lower() == ".bin":
            for bundle, original_size in linked:
                raw = _patch_binary_catalog_crc(raw, bundle.name, original_size)
        else:
            raw = _patch_json_catalog_crc(raw)
        output = staging / Path(relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        staged[relative] = output

        hash_file = catalog.with_suffix(".hash")
        if hash_file.is_file():
            hash_relative = _safe_relative(root, hash_file)
            hash_output = staging / Path(hash_relative)
            hash_output.parent.mkdir(parents=True, exist_ok=True)
            hash_output.write_text(hashlib.md5(raw).hexdigest(), encoding="ascii")
            staged[hash_relative] = hash_output
    return staged


def _read_pointer_value(root_file: Path, pointer: dict[str, Any]) -> str:
    if pointer["kind"] == "loose":
        text, _ = _decode_loose(root_file)
    else:
        UnityPy = _unitypy()
        with root_file.open("rb") as input_stream:
            logical_name = str(pointer.get("container") or root_file.name)
            env = _load_environment(UnityPy, input_stream, logical_name, root_file.parent)
            wanted = (str(pointer["internal"]), int(pointer["path_id"]))
            for obj in env.objects:
                if _object_key(obj) == wanted:
                    data = obj.parse_as_object()
                    if str(pointer["format"]) == "string_table":
                        return str(data.m_TableData[int(pointer["path"][0])].m_Localized)
                    text = str(data.m_Script)
                    break
            else:
                raise ValueError(f"검증할 Unity 텍스트 객체를 찾지 못했습니다: {wanted}")
    content_format = str(pointer["format"])
    if content_format == "json":
        node = json.loads(text.lstrip("\ufeff"))
        parent, key = _resolve_json_path(node, pointer["path"])
        return str(parent[key])
    if content_format == "csv":
        suffix = Path(str(pointer.get("name", ""))).suffix.lower()
        rows, _ = _csv_rows(text, suffix)
        row, column = map(int, pointer["path"])
        return rows[row][column]
    if content_format == "lines":
        return _line_value(_line_records(text)[int(pointer["path"][0])])
    raise ValueError(f"검증할 수 없는 형식입니다: {content_format}")


def _state_path(root: Path) -> Path:
    return project_path(root) / STATE_FILE


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def apply(
    root: Path,
    detection: Detection,
    entries: list[Entry],
    force_tmp_font: bool = False,
) -> int:
    root = root.resolve()
    grouped = _group_entries(entries)
    if not grouped and not force_tmp_font:
        return 0
    backup_root = project_path(root) / BACKUP_DIR
    staged: dict[str, Path] = {}
    count = 0
    if grouped:
        with tempfile.TemporaryDirectory(prefix="unity-static-", dir=project_path(root)) as temp:
            staging = Path(temp)
            addressable_bundles: list[tuple[Path, int]] = []
            for relative, items in grouped.items():
                target = _resolve_game_file(root, relative)
                if not target.is_file():
                    raise FileNotFoundError(f"Unity 원본 파일이 없습니다: {target}")
                backup = backup_root / Path(relative)
                baseline = backup if backup.is_file() else target
                output = staging / Path(relative)
                kinds = {str(pointer["kind"]) for pointer, _ in items}
                if len(kinds) != 1:
                    raise ValueError(f"동일 Unity 파일에 서로 다른 패치 종류가 섞였습니다: {relative}")
                if kinds == {"container"}:
                    _patch_container(baseline, output, items)
                else:
                    _patch_loose(baseline, output, items)
                if any(str(pointer.get("format")) == "string_table" for pointer, _entry in items):
                    addressable_bundles.append((target, int(baseline.stat().st_size)))
                for pointer, entry in items:
                    if _read_pointer_value(output, pointer) != entry.target:
                        raise RuntimeError(f"Unity 재패킹 검증 실패: {relative} / {entry.context}")
                staged[relative] = output
                count += len(items)

            if addressable_bundles:
                staged.update(
                    _stage_addressables_catalogs(
                        root, staging, backup_root, addressable_bundles
                    )
                )

            replaced: list[str] = []
            try:
                for relative, output in staged.items():
                    target = _resolve_game_file(root, relative)
                    backup = backup_root / Path(relative)
                    if not backup.is_file():
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup)
                    shutil.copy2(output, target)
                    replaced.append(relative)
            except Exception:
                for relative in replaced:
                    backup = backup_root / Path(relative)
                    if backup.is_file():
                        shutil.copy2(backup, _resolve_game_file(root, relative))
                raise

    font_files: list[str] = []
    font_assets = 0
    if force_tmp_font:
        font_files, font_assets = _apply_forced_tmp_font(root, detection, backup_root)

    previous: dict[str, Any] = {}
    if _state_path(root).is_file():
        try:
            previous = json.loads(_state_path(root).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    previous_files = previous.get("patched_files", [])
    if not isinstance(previous_files, list) or not all(
        isinstance(value, str) for value in previous_files
    ):
        previous_files = []
    patched_files = sorted(set(previous_files) | set(staged) | set(font_files))
    _save_state(root, {
        "format": 1,
        "engine": detection.engine,
        "patched_files": patched_files,
        "translations": count,
        "forced_tmp_font_assets": font_assets,
    })
    return count


def restore(root: Path) -> int:
    root = root.resolve()
    state_path = _state_path(root)
    if not state_path.is_file():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unity 정적 패치 백업 정보가 손상되었습니다: {state_path}") from exc
    if state.get("format") != 1 or not isinstance(state.get("patched_files"), list):
        raise RuntimeError(f"지원하지 않는 Unity 정적 패치 백업 형식입니다: {state_path}")
    backup_root = project_path(root) / BACKUP_DIR
    restored = 0
    for relative in state["patched_files"]:
        if not isinstance(relative, str):
            raise RuntimeError("Unity 정적 패치 백업 경로가 손상되었습니다.")
        source = backup_root / Path(relative)
        target = _resolve_game_file(root, relative)
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored += 1
    return restored
