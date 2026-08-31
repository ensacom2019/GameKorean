from __future__ import annotations

import json
from pathlib import Path

from .model import Detection


def detect_game(path: str | Path) -> Detection:
    selected = Path(path).expanduser().resolve()
    root = selected.parent if selected.is_file() else selected

    candidates = [root / "www" / "data", root / "data", root / "Data"]
    for data_dir in candidates:
        system_path = data_dir / "System.json"
        if system_path.is_file() and any(data_dir.glob("Map*.json")):
            mz = (root / "js" / "rmmz_core.js").exists() or (root / "www" / "js" / "rmmz_core.js").exists()
            details = {"data_dir": str(data_dir)}
            # RPG Maker의 System.json locale은 배포본의 기본 언어라서,
            # 영어·일본어 문자열이 함께 있을 때 원작 쪽을 고르는 강한 근거가 됩니다.
            try:
                if system_path.stat().st_size <= 4 * 1024 * 1024:
                    system = json.loads(system_path.read_text(encoding="utf-8-sig"))
                    locale = system.get("locale") if isinstance(system, dict) else None
                    if isinstance(locale, str) and locale.strip():
                        details["default_language"] = locale.strip()
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            return Detection("rpgmaker", str(root), "MZ" if mz else "MV", details)

    data_files = []
    if selected.is_file() and selected.name.lower() in {"data.win", "game.win", "game.unx", "game.ios", "game.droid"}:
        data_files = [selected]
    else:
        for name in ("data.win", "game.win", "game.unx", "game.ios", "game.droid"):
            if (root / name).is_file():
                data_files.append(root / name)
    if data_files:
        return Detection("gamemaker", str(root), "data-file", {"data_file": str(data_files[0])})

    unity_data = next((p for p in root.glob("*_Data") if p.is_dir()), None)
    if unity_data:
        il2cpp = (root / "GameAssembly.dll").is_file()
        exe = next((p for p in root.glob("*.exe") if p.stem + "_Data" == unity_data.name), None)
        return Detection(
            "unity", str(root), "IL2CPP" if il2cpp else "Mono",
            {"data_dir": str(unity_data), "exe": str(exe) if exe else ""},
        )
    return Detection("unknown", str(root), "", {})
