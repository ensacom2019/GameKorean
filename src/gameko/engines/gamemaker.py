from __future__ import annotations

import json
from hashlib import sha256
from importlib.resources import as_file, files
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ..downloads import download_release_asset
from ..model import Detection, Entry, project_path
from ..text import JA_RE, guess_language, is_translatable


EXPORT_SCRIPT = r'''using System.Linq;
using System.Text.Json;
EnsureDataLoaded();
string output = Environment.GetEnvironmentVariable("GAMEKO_STRINGS");
File.WriteAllText(output, JsonSerializer.Serialize(Data.Strings.Select(x => x.Content).ToArray()));
'''

IMPORT_SCRIPT = r'''using System.Text.Json;
EnsureDataLoaded();
string input = Environment.GetEnvironmentVariable("GAMEKO_STRINGS");
string[] values = JsonSerializer.Deserialize<string[]>(File.ReadAllText(input));
if (values.Length != Data.Strings.Count) throw new Exception("String table length mismatch");
for (int i = 0; i < values.Length; i++) Data.Strings[i].Content = values[i];
'''


# UndertaleModTool stores GameMaker fonts as bitmap glyph metadata plus a texture
# page.  A TTF copied beside data.win is not used automatically.  This script
# preserves every existing glyph and its coordinates, appends only the Korean
# characters needed by the translated strings, and repoints the same font
# resource to the merged atlas.  Compatibility gates intentionally reject SDF
# and unusually large/ambiguous font collections instead of risking data loss.
FONT_PATCH_SCRIPT = r'''using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using UndertaleModLib;
using UndertaleModLib.Models;
using UndertaleModLib.Util;
using ImageMagick;
using ImageMagick.Drawing;

EnsureDataLoaded();
string fontPath = Environment.GetEnvironmentVariable("GAMEKO_FONT");
string charsPath = Environment.GetEnvironmentVariable("GAMEKO_FONT_CHARS");
string reportPath = Environment.GetEnvironmentVariable("GAMEKO_FONT_REPORT");
List<string> patched = new();
List<string> covered = new();
List<string> skipped = new();
List<string> errors = new();

try
{
    if (String.IsNullOrWhiteSpace(fontPath) || !File.Exists(fontPath))
        throw new Exception("Noto Sans CJK KR font file is missing");
    if (String.IsNullOrWhiteSpace(charsPath) || !File.Exists(charsPath))
        throw new Exception("required glyph list is missing");

    char[] required = File.ReadAllText(charsPath)
        .Where(c => !Char.IsControl(c) && !Char.IsSurrogate(c))
        .Distinct().OrderBy(c => c).ToArray();
    if (required.Length == 0)
    {
        skipped.Add("번역문에 추가할 한글 글리프가 없습니다.");
    }
    else if (Data.Fonts == null || Data.Fonts.Count == 0)
    {
        skipped.Add("data.win에 bitmap font 리소스가 없습니다. 런타임 font_add 또는 sprite font를 사용하는 게임일 수 있습니다.");
    }
    else if (Data.Fonts.Count > 8)
    {
        skipped.Add($"font 리소스가 {Data.Fonts.Count}개라 실제 사용 폰트를 안전하게 특정할 수 없습니다(안전 한도 8개).");
    }
    else
    {
        foreach (UndertaleFont font in Data.Fonts)
        {
            string fontName = font?.Name?.Content ?? "<unnamed>";
            try
            {
                if (font == null || font.Texture == null || font.Glyphs == null)
                {
                    skipped.Add($"{fontName}: texture 또는 glyph 테이블이 없습니다.");
                    continue;
                }
                HashSet<ushort> existing = font.Glyphs.Select(g => g.Character).ToHashSet();
                char[] missing = required.Where(c => !existing.Contains(c)).ToArray();
                if (missing.Length == 0)
                {
                    covered.Add($"{fontName}: 필요한 글리프를 이미 포함합니다.");
                    continue;
                }
                if (font.SDFSpread != 0)
                {
                    skipped.Add($"{fontName}: SDF font에는 일반 bitmap 글리프를 혼합하지 않습니다.");
                    continue;
                }
                double pointSize = Convert.ToDouble(font.EmSize);
                if (pointSize < 6 || pointSize > 96)
                {
                    skipped.Add($"{fontName}: 지원하지 않는 font 크기입니다({pointSize:0.##}px).");
                    continue;
                }
                if (missing.Length > 1536)
                {
                    skipped.Add($"{fontName}: 누락 글리프가 {missing.Length}개로 안전 한도(1536)를 넘습니다.");
                    continue;
                }

                string tempDir = Path.Combine(Path.GetTempPath(), "gameko-font-" + Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(tempDir);
                try
                {
                    string originalPath = Path.Combine(tempDir, "original.png");
                    using (TextureWorker worker = new())
                        worker.ExportAsPNG(font.Texture, originalPath);
                    using MagickImage original = new(originalPath);
                    if (original.Width > 4096 || original.Height > 4096)
                    {
                        skipped.Add($"{fontName}: 기존 font atlas가 4096px 한도를 넘습니다.");
                        continue;
                    }

                    Drawables metricSettings = new();
                    metricSettings.Font(fontPath).FontPointSize(pointSize);
                    List<Tuple<char, int, int, int, int>> glyphPlan = new();
                    int atlasWidth = NextPowerOfTwo(Math.Max(1024, (int)original.Width));
                    int x = 2;
                    int y = (int)original.Height + 2;
                    int rowHeight = 0;
                    foreach (char ch in missing)
                    {
                        ITypeMetric metric = metricSettings.FontTypeMetrics(ch.ToString());
                        if (metric == null)
                            throw new Exception($"U+{(int)ch:X4} 글리프 크기를 계산하지 못했습니다.");
                        int width = Math.Max(2, (int)Math.Ceiling(metric.TextWidth) + 4);
                        int height = Math.Max(2, (int)Math.Ceiling(metric.Ascent - metric.Descent) + 4);
                        int baseline = Math.Max(1, (int)Math.Ceiling(metric.Ascent) + 2);
                        int shift = Math.Max(1, (int)Math.Ceiling(metric.TextWidth));
                        if (width > atlasWidth - 4 || height > 512)
                            throw new Exception($"U+{(int)ch:X4} 글리프 크기가 비정상적입니다({width}x{height}).");
                        if (x + width + 2 > atlasWidth)
                        {
                            x = 2;
                            y += rowHeight + 2;
                            rowHeight = 0;
                        }
                        glyphPlan.Add(Tuple.Create(ch, x, y, width, Math.Min(32767, shift)));
                        x += width + 2;
                        rowHeight = Math.Max(rowHeight, height);
                    }
                    int requiredHeight = y + rowHeight + 2;
                    int atlasHeight = NextPowerOfTwo(requiredHeight);
                    if (atlasHeight > 4096)
                    {
                        skipped.Add($"{fontName}: 병합 font atlas가 4096px 한도를 넘습니다.");
                        continue;
                    }

                    using MagickImage atlas = new(MagickColors.Transparent, (uint)atlasWidth, (uint)atlasHeight);
                    atlas.Composite(original, 0, 0, CompositeOperator.Over);
                    List<UndertaleFont.Glyph> newGlyphs = font.Glyphs.ToList();
                    foreach (Tuple<char, int, int, int, int> plan in glyphPlan)
                    {
                        char ch = plan.Item1;
                        int gx = plan.Item2;
                        int gy = plan.Item3;
                        int width = plan.Item4;
                        ITypeMetric metric = metricSettings.FontTypeMetrics(ch.ToString());
                        int height = Math.Max(2, (int)Math.Ceiling(metric.Ascent - metric.Descent) + 4);
                        int baseline = Math.Max(1, (int)Math.Ceiling(metric.Ascent) + 2);
                        new Drawables()
                            .Font(fontPath).FontPointSize(pointSize)
                            .FillColor(MagickColors.White)
                            .Text(gx + 2, gy + baseline, ch.ToString())
                            .Draw(atlas);
                        newGlyphs.Add(new UndertaleFont.Glyph()
                        {
                            Character = ch,
                            SourceX = (ushort)gx,
                            SourceY = (ushort)gy,
                            SourceWidth = (ushort)width,
                            SourceHeight = (ushort)height,
                            Shift = (short)plan.Item5,
                            Offset = 0,
                        });
                    }

                    string atlasPath = Path.Combine(tempDir, "atlas.png");
                    atlas.Format = MagickFormat.Png;
                    atlas.Write(atlasPath);
                    UndertaleEmbeddedTexture texture = new()
                    {
                        Name = new UndertaleString($"Texture {Data.EmbeddedTextures.Count}")
                    };
                    texture.TextureData.Image = GMImage.FromPng(File.ReadAllBytes(atlasPath));
                    Data.EmbeddedTextures.Add(texture);
                    UndertaleTexturePageItem page = new()
                    {
                        Name = new UndertaleString($"PageItem {Data.TexturePageItems.Count}"),
                        TexturePage = texture,
                        SourceX = 0,
                        SourceY = 0,
                        SourceWidth = (ushort)atlasWidth,
                        SourceHeight = (ushort)atlasHeight,
                        TargetX = 0,
                        TargetY = 0,
                        TargetWidth = (ushort)atlasWidth,
                        TargetHeight = (ushort)atlasHeight,
                        BoundingWidth = (ushort)atlasWidth,
                        BoundingHeight = (ushort)atlasHeight,
                    };
                    Data.TexturePageItems.Add(page);

                    if (Data.TextureGroupInfo is not null)
                    {
                        UndertaleTextureGroupInfo group = Data.TextureGroupInfo
                            .FirstOrDefault(t => t.Fonts.Any(f => f.Resource == font));
                        if (group != null && !group.TexturePages.Any(t => t.Resource == texture))
                            group.TexturePages.Add(new UndertaleResourceById<UndertaleEmbeddedTexture, UndertaleChunkTXTR>() { Resource = texture });
                    }
                    font.Texture = page;
                    font.Glyphs.Clear();
                    foreach (UndertaleFont.Glyph glyph in newGlyphs.OrderBy(g => g.Character))
                        font.Glyphs.Add(glyph);
                    font.RangeStart = Math.Min(font.RangeStart, (ushort)missing.Min(c => c));
                    font.RangeEnd = Math.Max(font.RangeEnd, (uint)missing.Max(c => c));
                    patched.Add($"{fontName}: Noto Sans CJK KR 글리프 {missing.Length}개 추가");
                }
                finally
                {
                    try { Directory.Delete(tempDir, true); } catch { }
                }
            }
            catch (Exception exc)
            {
                errors.Add($"{fontName}: {exc.Message}");
            }
        }
    }
}
catch (Exception exc)
{
    errors.Add(exc.Message);
}

Dictionary<string, object> report = new()
{
    ["format"] = 1,
    ["required_glyphs"] = File.Exists(charsPath ?? "") ? File.ReadAllText(charsPath).Distinct().Count() : 0,
    ["patched"] = patched,
    ["covered"] = covered,
    ["skipped"] = skipped,
    ["errors"] = errors,
};
File.WriteAllText(reportPath, JsonSerializer.Serialize(report));

int NextPowerOfTwo(int value)
{
    int result = 1;
    while (result < value && result < 8192) result <<= 1;
    return result;
}
'''


FONT_FILENAME = "NotoSansCJKkr-Regular.otf"
FONT_LICENSE_FILENAME = "NotoSansKR-OFL.txt"
FONT_DIRNAME = "GameKO.Fonts"
FONT_REPORT_FILENAME = "gamemaker_font_report.json"
FONT_STATE_FILENAME = "gamemaker_font_state.json"
FONT_WARNING_FILENAME = "GameKO-폰트-확인필요.txt"


def _required_font_characters(entries: list[Entry]) -> str:
    """Return printable BMP glyph candidates used by translated strings.

    The UndertaleModTool script removes glyphs already present in each font.
    Keeping punctuation, currency signs and remaining non-Korean characters here
    avoids tofu when a translator introduces typographic quotes or ellipses.
    """
    characters = {
        char
        for entry in entries
        if entry.target.strip() and entry.target != entry.source
        for char in entry.target
        if ord(char) <= 0xFFFF and char.isprintable() and not char.isspace()
    }
    return "".join(sorted(characters))


def _font_state_path(root: Path) -> Path:
    return project_path(root) / FONT_STATE_FILENAME


def _copy_resource(resource_name: str, destination: Path) -> bool:
    resource = files("gameko").joinpath("assets", resource_name)
    if not resource.is_file():
        raise RuntimeError(f"GameKO에 포함된 파일을 찾지 못했습니다: {resource_name}")
    with as_file(resource) as source:
        if destination.exists():
            if not destination.is_file():
                raise RuntimeError(f"GameMaker 폰트 경로가 파일이 아닙니다: {destination}")
            bundled_hash = sha256(source.read_bytes()).digest()
            existing_hash = sha256(destination.read_bytes()).digest()
            if existing_hash != bundled_hash:
                raise RuntimeError(f"기존 파일과 충돌해 덮어쓰지 않았습니다: {destination}")
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return True


def _prepare_font_bundle(root: Path) -> tuple[Path, dict[str, Any]]:
    font_dir = root / FONT_DIRNAME
    font_path = font_dir / FONT_FILENAME
    license_path = font_dir / FONT_LICENSE_FILENAME
    created: list[str] = []
    try:
        if _copy_resource(FONT_FILENAME, font_path):
            created.append(font_path.relative_to(root).as_posix())
        if _copy_resource(FONT_LICENSE_FILENAME, license_path):
            created.append(license_path.relative_to(root).as_posix())
    except Exception:
        for rel in created:
            created_path = root / rel
            if created_path.is_file():
                created_path.unlink()
        raise
    state_path = _font_state_path(root)
    previous: dict[str, Any] = {}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    all_created = sorted(set(previous.get("created", [])) | set(created))
    state = {"format": 1, "created": all_created, "font": font_path.relative_to(root).as_posix()}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return font_path, state


def _write_font_warning(root: Path, report: dict[str, Any]) -> None:
    patched = report.get("patched", [])
    covered = report.get("covered", [])
    skipped = report.get("skipped", [])
    errors = report.get("errors", [])
    warning_path = root / FONT_DIRNAME / FONT_WARNING_FILENAME
    if (patched or covered) and not skipped and not errors:
        if warning_path.is_file():
            warning_path.unlink()
        return
    lines = [
        "GameKO GameMaker 한글 폰트 적용 보고서",
        "",
        "번역 문자열은 data.win에 적용되었지만 일부 또는 모든 폰트에 한글 글리프를 자동 주입하지 못했습니다.",
        "GameMaker 게임은 폰트 사용 방식이 게임마다 달라 실제 화면에서 한글 표시를 확인해야 합니다.",
        f"번들 폰트: {FONT_FILENAME}",
    ]
    for label, values in (("적용", patched), ("기존 글리프 확인", covered), ("제외", skipped), ("오류", errors)):
        if values:
            lines.extend(("", f"[{label}]", *(f"- {value}" for value in values)))
    warning_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state_path = _font_state_path(root)
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8-sig"))
            state = loaded if isinstance(loaded, dict) else {"format": 1, "created": []}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            state = {"format": 1, "created": []}
    else:
        state = {"format": 1, "created": []}
    rel = warning_path.relative_to(root).as_posix()
    state["created"] = sorted(set(state.get("created", [])) | {rel})
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_tool(root: Path) -> Path:
    tools = project_path(root) / "tools" / "undertale_mod_tool"
    executable = next(tools.rglob("UndertaleModCli.exe"), None) if tools.exists() else None
    if executable:
        return executable
    download_release_asset("UnderminersTeam/UndertaleModTool", "-Windows.zip", tools)
    executable = next(tools.rglob("UndertaleModCli.exe"), None)
    if not executable:
        raise RuntimeError("UndertaleModCli.exe를 다운로드했지만 찾지 못했습니다.")
    return executable


def extract(detection: Detection) -> list[Entry]:
    root = Path(detection.root)
    project = project_path(root)
    project.mkdir(parents=True, exist_ok=True)
    tool = _ensure_tool(root)
    script = project / "gm_export.csx"
    script.write_text(EXPORT_SCRIPT, encoding="utf-8")
    raw = project / "gamemaker_strings.json"
    env = os.environ.copy()
    env["GAMEKO_STRINGS"] = str(raw)
    result = subprocess.run([str(tool), "load", detection.details["data_file"], "-s", str(script)], cwd=root, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode != 0 or not raw.exists():
        raise RuntimeError("GameMaker 문자열 추출 실패:\n" + (result.stderr or result.stdout)[-2000:])
    strings: list[str] = json.loads(raw.read_text(encoding="utf-8-sig"))
    rel = Path(detection.details["data_file"]).relative_to(root).as_posix()
    entries: list[Entry] = []
    common_ui = {"start", "continue", "options", "settings", "quit", "exit", "save", "load", "back", "cancel", "confirm", "yes", "no", "new game", "game over", "retry"}
    for index, value in enumerate(strings):
        english_ui = isinstance(value, str) and value.strip().lower() in common_ui
        english_phrase = isinstance(value, str) and (" " in value.strip() or any(ch in value for ch in "!?.,:'\""))
        safe_candidate = isinstance(value, str) and (JA_RE.search(value) or english_ui or english_phrase)
        if safe_candidate and is_translatable(value):
            entry = Entry.create(value, rel, f"/Strings/{index}", f"GameMaker 문자열 #{index}")
            entry.source_lang = guess_language(value)
            entries.append(entry)
    return entries


def apply(root: Path, detection: Detection, entries: list[Entry]) -> int:
    project = project_path(root)
    raw_path = project / "gamemaker_strings.json"
    strings: list[str] = json.loads(raw_path.read_text(encoding="utf-8-sig"))
    count = 0
    for entry in entries:
        if not entry.target.strip() or entry.target == entry.source:
            continue
        index = int(entry.pointer.rsplit("/", 1)[1])
        if strings[index] not in {entry.source, entry.target}:
            raise RuntimeError(f"GameMaker 문자열 #{index}이 추출 후 바뀌었습니다.")
        strings[index] = entry.target
        count += 1
    translated = project / "gamemaker_strings_translated.json"
    translated.write_text(json.dumps(strings, ensure_ascii=False), encoding="utf-8")
    script = project / "gm_import.csx"
    script.write_text(IMPORT_SCRIPT, encoding="utf-8")
    tool = _ensure_tool(root)
    data_file = Path(detection.details["data_file"])
    strings_output = project / (data_file.name + ".strings.patched")
    output = project / (data_file.name + ".patched")
    strings_output.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    env = os.environ.copy()
    env["GAMEKO_STRINGS"] = str(translated)
    result = subprocess.run([str(tool), "load", str(data_file), "-s", str(script), "-o", str(strings_output), "-f"], cwd=root, env=env, capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not strings_output.exists():
        raise RuntimeError("GameMaker 패치 생성 실패:\n" + (result.stderr or result.stdout)[-2000:])
    verify = subprocess.run([str(tool), "info", str(strings_output)], cwd=root, capture_output=True, text=True, timeout=180)
    if verify.returncode != 0:
        raise RuntimeError("생성된 GameMaker 데이터 검증 실패:\n" + (verify.stderr or verify.stdout)[-2000:])

    final_output = strings_output
    required_chars = _required_font_characters(entries)
    if required_chars:
        report_path = project / FONT_REPORT_FILENAME
        chars_path = project / "gamemaker_required_glyphs.txt"
        font_script = project / "gm_font_patch.csx"
        chars_path.write_text(required_chars, encoding="utf-8")
        font_script.write_text(FONT_PATCH_SCRIPT, encoding="utf-8")
        report: dict[str, Any]
        try:
            font_path, _ = _prepare_font_bundle(root)
            report_path.unlink(missing_ok=True)
            font_env = os.environ.copy()
            font_env.update({
                "GAMEKO_FONT": str(font_path),
                "GAMEKO_FONT_CHARS": str(chars_path),
                "GAMEKO_FONT_REPORT": str(report_path),
            })
            font_result = subprocess.run(
                [str(tool), "load", str(strings_output), "-s", str(font_script), "-o", str(output), "-f"],
                cwd=root, env=font_env, capture_output=True, text=True, timeout=300,
            )
            if font_result.returncode != 0 or not output.exists():
                raise RuntimeError("UndertaleModTool 글리프 주입 실패: " + (font_result.stderr or font_result.stdout)[-1200:])
            font_verify = subprocess.run([str(tool), "info", str(output)], cwd=root, capture_output=True, text=True, timeout=180)
            if font_verify.returncode != 0:
                raise RuntimeError("글리프 주입 후 data.win 검증 실패: " + (font_verify.stderr or font_verify.stdout)[-1200:])
            final_output = output
            if report_path.is_file():
                loaded = json.loads(report_path.read_text(encoding="utf-8-sig"))
                report = loaded if isinstance(loaded, dict) else {}
            else:
                report = {"patched": [], "covered": [], "skipped": [], "errors": ["폰트 패치 보고서가 생성되지 않았습니다."]}
        except Exception as exc:
            output.unlink(missing_ok=True)
            report = {
                "format": 1,
                "required_glyphs": len(required_chars),
                "patched": [],
                "covered": [],
                "skipped": ["안전한 자동 글리프 주입을 완료하지 못해 문자열 패치만 사용합니다."],
                "errors": [str(exc)],
            }
        report["font_file"] = f"{FONT_DIRNAME}/{FONT_FILENAME}"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if (root / FONT_DIRNAME).is_dir():
            _write_font_warning(root, report)

    backup = project / "backup" / data_file.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(data_file, backup)
    shutil.copy2(final_output, data_file)
    return count


def restore(root: Path, detection: Detection) -> int:
    data_file = Path(detection.details["data_file"])
    backup = project_path(root) / "backup" / data_file.name
    restored = 0
    if backup.exists():
        shutil.copy2(backup, data_file)
        restored += 1
    state_path = _font_state_path(root)
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            state = {}
        resolved_root = root.resolve()
        for rel in state.get("created", []) if isinstance(state, dict) else []:
            if not isinstance(rel, str):
                continue
            target = (root / rel).resolve()
            if resolved_root in target.parents and target.is_file():
                target.unlink()
                restored += 1
        try:
            (root / FONT_DIRNAME).rmdir()
        except OSError:
            pass
        state_path.unlink(missing_ok=True)
    return restored
