from __future__ import annotations

import json
from importlib.resources import as_file, files
from pathlib import Path
import re
import shutil
from typing import Any, Iterator

from ..model import Detection, Entry, project_path
from ..text import guess_language, is_translatable


FIELD_MAP: dict[str, set[str]] = {
    "Actors.json": {"name", "nickname", "profile"},
    "Classes.json": {"name"},
    "Skills.json": {"name", "description", "message1", "message2"},
    "Items.json": {"name", "description"},
    "Weapons.json": {"name", "description"},
    "Armors.json": {"name", "description"},
    "Enemies.json": {"name"},
    "States.json": {"name", "message1", "message2", "message3", "message4"},
    "MapInfos.json": {"name"},
}
EVENT_SCALARS = {401: [0], 405: [0], 320: [1], 324: [1], 325: [1], 402: [1]}

FONT_FILENAME = "NotoSansCJKkr-Regular.otf"
FONT_STATE_FILENAME = "rpgmaker_font_state.json"
FONT_BACKUP_DIRNAME = "rpgmaker_font_backup"
MV_FONT_FACE = "GameFont"


def _pointer(parts: list[str | int]) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _walk_fields(node: Any, fields: set[str], parts: list[str | int]) -> Iterator[tuple[list[str | int], str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            at = parts + [key]
            if key in fields and isinstance(value, str) and is_translatable(value):
                yield at, value
            yield from _walk_fields(value, fields, at)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_fields(value, fields, parts + [index])


def _walk_all_strings(node: Any, parts: list[str | int]) -> Iterator[tuple[list[str | int], str]]:
    if isinstance(node, str):
        if is_translatable(node):
            yield parts, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_all_strings(value, parts + [key])
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_all_strings(value, parts + [index])


def _walk_events(node: Any, parts: list[str | int]) -> Iterator[tuple[list[str | int], str, str]]:
    if isinstance(node, dict):
        if isinstance(node.get("code"), int) and isinstance(node.get("parameters"), list):
            code, params = node["code"], node["parameters"]
            if code == 102 and params and isinstance(params[0], list):
                for index, value in enumerate(params[0]):
                    if isinstance(value, str) and is_translatable(value):
                        yield parts + ["parameters", 0, index], value, "선택지"
            elif code == 101 and len(params) > 4 and isinstance(params[4], str) and is_translatable(params[4]):
                yield parts + ["parameters", 4], params[4], "화자 이름"
            elif code in EVENT_SCALARS:
                for index in EVENT_SCALARS[code]:
                    if len(params) > index and isinstance(params[index], str) and is_translatable(params[index]):
                        yield parts + ["parameters", index], params[index], f"이벤트 명령 {code}"
        for key, value in node.items():
            yield from _walk_events(value, parts + [key])
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_events(value, parts + [index])


def extract(detection: Detection) -> list[Entry]:
    root = Path(detection.root)
    data_dir = Path(detection.details["data_dir"])
    entries: list[Entry] = []
    for path in sorted(data_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        fields = FIELD_MAP.get(path.name, set())
        if path.name == "System.json":
            fields = {"gameTitle", "currencyUnit"}
        if path.name.startswith("Map") and path.name != "MapInfos.json":
            fields = {"displayName"}
        for parts, value in _walk_fields(data, fields, []):
            entry = Entry.create(value, rel, _pointer(parts), path.name)
            entry.source_lang = guess_language(value)
            entries.append(entry)
        if path.name == "System.json" and isinstance(data, dict):
            for system_key in ("terms", "armorTypes", "elements", "equipTypes", "skillTypes", "weaponTypes"):
                if system_key in data:
                    for parts, value in _walk_all_strings(data[system_key], [system_key]):
                        entry = Entry.create(value, rel, _pointer(parts), f"System.json: {system_key}")
                        entry.source_lang = guess_language(value)
                        entries.append(entry)
        if path.name.startswith("Map") or path.name in {"CommonEvents.json", "Troops.json"}:
            for parts, value, context in _walk_events(data, []):
                entry = Entry.create(value, rel, _pointer(parts), f"{path.name}: {context}")
                entry.source_lang = guess_language(value)
                entries.append(entry)
    return entries


def _resolve_pointer(data: Any, pointer: str) -> tuple[Any, str | int]:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    current = data
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    last: str | int = int(parts[-1]) if isinstance(current, list) else parts[-1]
    return current, last


def _data_dir(root: Path) -> Path:
    for candidate in (root / "www" / "data", root / "data", root / "Data"):
        if (candidate / "System.json").is_file():
            return candidate
    raise RuntimeError("RPG Maker System.json을 찾지 못해 한글 폰트를 적용할 수 없습니다.")


def _variant(root: Path) -> str:
    manifest = project_path(root) / "manifest.json"
    if manifest.is_file():
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if saved.get("engine") == "rpgmaker" and saved.get("variant") in {"MV", "MZ"}:
                return saved["variant"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    if (root / "js" / "rmmz_core.js").is_file() or (root / "www" / "js" / "rmmz_core.js").is_file():
        return "MZ"
    return "MV"


def _relative_game_path(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RuntimeError(f"게임 폴더 밖의 폰트 경로는 수정하지 않습니다: {path}")
    return resolved.relative_to(resolved_root).as_posix()


def _font_state_path(root: Path) -> Path:
    return project_path(root) / FONT_STATE_FILENAME


def _load_font_state(root: Path) -> dict[str, Any]:
    path = _font_state_path(root)
    if not path.is_file():
        return {"format": 1, "created": [], "backed_up": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"RPG Maker 폰트 백업 정보가 손상되었습니다: {path}") from exc
    if not isinstance(state, dict) or state.get("format") != 1:
        raise RuntimeError(f"지원하지 않는 RPG Maker 폰트 백업 형식입니다: {path}")
    for key in ("created", "backed_up"):
        if not isinstance(state.get(key), list) or not all(isinstance(item, str) for item in state[key]):
            raise RuntimeError(f"RPG Maker 폰트 백업 정보의 {key} 목록이 올바르지 않습니다: {path}")
    return state


def _save_font_state(root: Path, state: dict[str, Any]) -> None:
    path = _font_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _remember_font_target(root: Path, target: Path, state: dict[str, Any], backup_source: Path | None = None) -> None:
    rel = _relative_game_path(root, target)
    if rel in state["created"] or rel in state["backed_up"]:
        return
    if target.is_file():
        backup = project_path(root) / FONT_BACKUP_DIRNAME / Path(rel)
        backup.parent.mkdir(parents=True, exist_ok=True)
        source = backup_source if backup_source is not None and backup_source.is_file() else target
        shutil.copy2(source, backup)
        state["backed_up"].append(rel)
    elif target.exists():
        raise RuntimeError(f"폰트를 적용할 경로가 파일이 아닙니다: {target}")
    else:
        state["created"].append(rel)


def _bundled_font_resource():
    resource = files("gameko").joinpath("assets", FONT_FILENAME)
    if not resource.is_file():
        raise RuntimeError(f"GameKO에 포함된 한글 폰트를 찾지 못했습니다: {FONT_FILENAME}")
    return resource


def _copy_bundled_font(destination: Path) -> None:
    resource = _bundled_font_resource()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with as_file(resource) as source:
        shutil.copy2(source, destination)


def _patch_mv_font_css(css: str) -> str:
    source = f'src: url("{FONT_FILENAME}") format("opentype");'
    face_pattern = re.compile(r"@font-face\s*\{[^}]*\}", re.IGNORECASE | re.DOTALL)
    family_pattern = re.compile(
        rf"font-family\s*:\s*(?:['\"])?{re.escape(MV_FONT_FACE)}(?:['\"])?\s*;",
        re.IGNORECASE,
    )
    src_pattern = re.compile(r"src\s*:[^;}]*;?", re.IGNORECASE)
    replaced = False

    def replace_face(match: re.Match[str]) -> str:
        nonlocal replaced
        block = match.group(0)
        if not family_pattern.search(block):
            return block
        replaced = True
        if src_pattern.search(block):
            return src_pattern.sub(source, block, count=1)
        return block[:-1].rstrip() + "\n    " + source + "\n}"

    patched = face_pattern.sub(replace_face, css)
    if replaced:
        return patched
    separator = "" if not patched or patched.endswith(("\n", "\r")) else "\n"
    return (
        patched
        + separator
        + "\n/* GameKO Korean font */\n"
        + "@font-face {\n"
        + f"    font-family: {MV_FONT_FACE};\n"
        + f"    {source}\n"
        + "    font-weight: normal;\n"
        + "    font-style: normal;\n"
        + "}\n"
    )


def _prepare_font_install(root: Path, data_dir: Path, variant: str, planned: dict[str, tuple[Path, Any]]) -> tuple[Path, Path | None, dict[str, Any]]:
    content_root = data_dir.parent
    font_dir = content_root / "fonts"
    font_path = font_dir / FONT_FILENAME
    css_path = font_dir / "gamefont.css" if variant == "MV" else None
    system_path = data_dir / "System.json"
    state = _load_font_state(root)

    _remember_font_target(root, font_path, state)
    if css_path is not None:
        _remember_font_target(root, css_path, state)
    else:
        rel = system_path.relative_to(root).as_posix()
        original_system = project_path(root) / "backup" / Path(rel)
        _remember_font_target(root, system_path, state, original_system)
        if rel not in planned:
            base = original_system if original_system.is_file() else system_path
            planned[rel] = (system_path, json.loads(base.read_text(encoding="utf-8-sig")))
        system = planned[rel][1]
        if not isinstance(system, dict):
            raise RuntimeError(f"RPG Maker MZ System.json 형식이 올바르지 않습니다: {system_path}")
        advanced = system.get("advanced")
        if not isinstance(advanced, dict):
            raise RuntimeError(f"RPG Maker MZ System.json에 advanced 설정이 없습니다: {system_path}")
        advanced["mainFontFilename"] = FONT_FILENAME

    _save_font_state(root, state)
    return font_path, css_path, state


def _finish_font_install(root: Path, font_path: Path, css_path: Path | None, state: dict[str, Any]) -> None:
    _copy_bundled_font(font_path)
    if css_path is None:
        return
    rel = _relative_game_path(root, css_path)
    if rel in state["backed_up"]:
        base = project_path(root) / FONT_BACKUP_DIRNAME / Path(rel)
        css = base.read_text(encoding="utf-8-sig")
    elif css_path.is_file():
        css = css_path.read_text(encoding="utf-8-sig")
    else:
        css = ""
    css_path.parent.mkdir(parents=True, exist_ok=True)
    temp = css_path.with_suffix(css_path.suffix + ".gameko.tmp")
    temp.write_text(_patch_mv_font_css(css), encoding="utf-8")
    temp.replace(css_path)


def apply(root: Path, entries: list[Entry]) -> int:
    # 폰트가 누락된 잘못된 배포본이라면 게임 파일을 하나도 바꾸기 전에 중단합니다.
    _bundled_font_resource()
    data_dir = _data_dir(root)
    variant = _variant(root)
    changed = [e for e in entries if e.target.strip() and e.target != e.source]
    grouped: dict[str, list[Entry]] = {}
    for entry in changed:
        grouped.setdefault(entry.file, []).append(entry)
    backup = project_path(root) / "backup"
    count = 0
    previous = {
        path.relative_to(backup).as_posix()
        for path in backup.rglob("*")
        if path.is_file()
    } if backup.exists() else set()
    planned: dict[str, tuple[Path, Any]] = {}

    # 수정본은 이전 번역 결과가 아닌 최초 백업에서 다시 만듭니다. 모든 포인터를
    # 먼저 검증한 뒤 파일을 바꾸므로 잘못된 CSV가 일부만 적용되는 일도 막습니다.
    for rel in sorted(previous | set(grouped)):
        path = root / Path(rel)
        backup_path = backup / Path(rel)
        base_path = backup_path if backup_path.exists() else path
        data = json.loads(base_path.read_text(encoding="utf-8-sig"))
        for entry in grouped.get(rel, []):
            parent, key = _resolve_pointer(data, entry.pointer)
            if parent[key] != entry.source:
                raise RuntimeError(f"원본이 추출 후 바뀌었습니다: {entry.file}{entry.pointer}")
            parent[key] = entry.target
            count += 1
        planned[rel] = (path, data)

    for rel in grouped:
        path = root / Path(rel)
        backup_path = backup / Path(rel)
        if not backup_path.exists():
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)

    font_path, css_path, font_state = _prepare_font_install(root, data_dir, variant, planned)

    for path, data in planned.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".gameko.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)
    _finish_font_install(root, font_path, css_path, font_state)
    return count


def restore(root: Path) -> int:
    backup = project_path(root) / "backup"
    count = 0
    if backup.exists():
        for path in backup.rglob("*"):
            if path.is_file():
                target = root / path.relative_to(backup)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                count += 1
    state_path = _font_state_path(root)
    if state_path.is_file():
        state = _load_font_state(root)
        font_backup = project_path(root) / FONT_BACKUP_DIRNAME
        for rel in state["backed_up"]:
            target = root / Path(rel)
            _relative_game_path(root, target)
            source = font_backup / Path(rel)
            if not source.is_file():
                raise RuntimeError(f"RPG Maker 폰트 원본 백업이 없습니다: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
        created_parents: set[Path] = set()
        for rel in state["created"]:
            target = root / Path(rel)
            _relative_game_path(root, target)
            if target.is_file():
                target.unlink()
                count += 1
            created_parents.add(target.parent)
        for directory in sorted(created_parents, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()  # GameKO가 만든 뒤 비어 있는 폴더만 제거합니다.
            except OSError:
                pass
        state_path.unlink()
        if font_backup.exists():
            shutil.rmtree(font_backup)
    return count
