from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


PROJECT_DIR = "gameko_project"


@dataclass
class Detection:
    engine: str
    root: str
    variant: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Entry:
    id: str
    source: str
    target: str = ""
    file: str = ""
    pointer: str = ""
    context: str = ""
    source_lang: str = "auto"
    status: str = "new"

    @classmethod
    def create(cls, source: str, file: str, pointer: str, context: str = "") -> "Entry":
        raw = f"{file}\0{pointer}\0{source}".encode("utf-8")
        return cls(sha256(raw).hexdigest()[:20], source, file=file, pointer=pointer, context=context)


def project_path(game_root: Path) -> Path:
    return game_root / PROJECT_DIR


def save_project(
    game_root: Path,
    detection: Detection,
    entries: list[Entry],
    metadata: dict[str, Any] | None = None,
) -> Path:
    out = project_path(game_root)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": 1,
        "engine": detection.engine,
        "variant": detection.variant,
        "game_root": str(game_root.resolve()),
        "details": detection.details,
        "entries": len(entries),
    }
    if metadata:
        manifest.update(metadata)
    _write_manifest(out / "manifest.json", manifest)
    save_entries(game_root, entries)
    return out


def update_manifest(game_root: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """번역 프로젝트 설정을 기존 감지 정보는 보존하면서 원자적으로 갱신합니다."""
    path = project_path(game_root) / "manifest.json"
    manifest = load_manifest(game_root)
    manifest.update(changes)
    _write_manifest(path, manifest)
    return manifest


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def save_entries(game_root: Path, entries: list[Entry]) -> Path:
    """번역표를 중간에 잘린 파일이 남지 않도록 원자적으로 저장합니다."""
    out = project_path(game_root)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "translations.jsonl"
    temp = out / "translations.jsonl.tmp"
    with temp.open("w", encoding="utf-8", newline="\n") as fh:
        for entry in entries:
            fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    temp.replace(path)
    return path


def load_entries(game_root: Path) -> list[Entry]:
    path = project_path(game_root) / "translations.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"번역 프로젝트가 없습니다: {path}")
    entries: list[Entry] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(Entry(**json.loads(line)))
        except Exception as exc:
            raise ValueError(f"translations.jsonl {line_no}행 오류: {exc}") from exc
    return entries


def load_manifest(game_root: Path) -> dict[str, Any]:
    return json.loads((project_path(game_root) / "manifest.json").read_text(encoding="utf-8"))
