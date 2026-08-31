from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile


USER_AGENT = "GameKO/0.1 (+https://github.com/)"


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def safe_extract_zip(archive: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (destination / info.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"안전하지 않은 ZIP 경로: {info.filename}")
        zf.extractall(destination)
        extracted = [(destination / info.filename).resolve() for info in zf.infolist() if not info.is_dir()]
    return extracted


def download_release_asset(repo: str, name_contains: str, destination: Path) -> tuple[str, list[Path]]:
    release = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    matches = [a for a in release.get("assets", []) if name_contains.lower() in a["name"].lower()]
    if not matches:
        raise RuntimeError(f"{repo} 최신 릴리스에서 '{name_contains}' 파일을 찾지 못했습니다.")
    asset = sorted(matches, key=lambda a: len(a["name"]))[0]
    with tempfile.TemporaryDirectory(prefix="gameko-") as temp_dir:
        archive = Path(temp_dir) / asset["name"]
        download(asset["browser_download_url"], archive)
        files = safe_extract_zip(archive, destination)
    return str(release.get("tag_name", "unknown")), files


def download_first_compatible_asset(repo: str, name_contains: str, destination: Path) -> tuple[str, list[Path]]:
    """Include pre-releases and select the newest release containing a matching asset."""
    releases = fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page=20")
    if not isinstance(releases, list):
        raise RuntimeError(f"{repo} 릴리스 목록 형식이 올바르지 않습니다.")
    for release in releases:
        matches = [a for a in release.get("assets", []) if name_contains.lower() in a["name"].lower()]
        if not matches:
            continue
        asset = sorted(matches, key=lambda a: len(a["name"]))[0]
        with tempfile.TemporaryDirectory(prefix="gameko-") as temp_dir:
            archive = Path(temp_dir) / asset["name"]
            download(asset["browser_download_url"], archive)
            files = safe_extract_zip(archive, destination)
        return str(release.get("tag_name", "unknown")), files
    raise RuntimeError(f"{repo} 릴리스에서 '{name_contains}' 호환 파일을 찾지 못했습니다.")
