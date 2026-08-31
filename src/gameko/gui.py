from __future__ import annotations

import os
from datetime import datetime
from importlib.resources import as_file, files
import json
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from collections import deque
from tkinter import filedialog, messagebox, ttk

from .service import apply_game, automatic, export_csv, extract_game, find_game, import_csv, restore_game, translate_game


WEB_PROVIDER = "웹 자동 번역 (키 없음)"
OPENAI_PROVIDER = "OpenAI 호환 API (고급)"
ARGOS_PROVIDER = "Argos 오프라인 (고급)"
SOURCE_LANGUAGE_LABELS = {
    "자동(원작 감지·일본어 우선)": "auto",
    "일본어만": "ja",
    "영어만": "en",
}
DEFAULT_SOURCE_LANGUAGE_LABEL = "자동(원작 감지·일본어 우선)"


def source_language_key(label: str) -> str:
    return SOURCE_LANGUAGE_LABELS.get(label, "auto")


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def completion_dialog(action: str, result: str) -> tuple[str, str]:
    lead = {
        "자동 번역": "자동 번역이 완료되었습니다.",
        "원본 복원": "원본 복원이 완료되었습니다.",
    }.get(action, f"{action} 작업이 완료되었습니다.")
    detail = result.strip()
    return f"{action} 완료", lead + (f"\n\n{detail}" if detail else "")


def _error_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def error_reason(exc: BaseException) -> str:
    """Return a short Korean explanation without hiding the original exception."""
    chain = _error_chain(exc)
    text = " ".join(str(item) for item in chain).lower()
    http_code = next((getattr(item, "code", None) for item in chain if getattr(item, "code", None)), None)
    if http_code == 429 or "429" in text:
        return "번역 사이트의 요청 제한(429)에 걸렸습니다. 잠시 후 자동 감속·재시도되며, 계속 실패하면 몇 분 뒤 다시 실행하세요."
    if http_code == 403 or "403" in text:
        return "번역 사이트가 요청을 거부했습니다(403). 일시 차단, 접속 지역 또는 서비스 정책이 원인일 수 있습니다."
    if "보호 토큰" in text:
        return "웹 번역기가 게임 제어 태그 보호 표식을 삭제했습니다. 최신 버전은 태그 앞뒤 문장만 나눠 번역하고 원본 태그를 그대로 복원해 자동 재시도합니다."
    if "복원할 활성 백업 기록" in text or "백업 정보가 손상" in text or "원본 백업이 없습니다" in text:
        return "GameKO의 복원 상태나 원본 백업이 없거나 손상됐습니다. 백업이 없는데 게임 파일이 변경된 경우 Steam 등의 파일 무결성 확인으로 원본을 다시 받으세요."
    if any(isinstance(item, PermissionError) for item in chain):
        return "파일 쓰기 권한이 없거나 게임/백신이 파일을 사용 중입니다. 게임을 종료하고 폴더 권한을 확인하세요."
    if any(isinstance(item, FileNotFoundError) for item in chain):
        return "필요한 게임 파일이나 도구 파일을 찾지 못했습니다. 선택한 게임 폴더와 삭제·격리된 파일을 확인하세요."
    if "timed out" in text or "timeout" in text or any(isinstance(item, TimeoutError) for item in chain):
        return "서버 또는 설치 프로그램이 제한 시간 안에 응답하지 않았습니다. 네트워크를 확인한 뒤 다시 시도하세요."
    if "connection" in text or "name resolution" in text or "getaddrinfo" in text:
        return "번역 서버에 연결하지 못했습니다. 인터넷 연결, 방화벽 또는 번역 서비스 상태를 확인하세요."
    if "json" in text or "manifest" in text or "translations.jsonl" in text:
        return "GameKO 작업 파일이나 게임 데이터의 형식이 손상됐거나 예상한 구조와 다릅니다."
    if "tmp" in text or "unity" in text or "assetbundle" in text:
        return "Unity 리소스 구조 또는 버전이 예상 형식과 다릅니다. 강제 TMP 적용 중이었다면 원본 복원 후 옵션을 끄고 다시 시도하세요."
    return "아래의 실제 예외 메시지와 상세 오류 보고서에서 실패 지점을 확인할 수 있습니다."


def format_error_message(action: str, exc: BaseException, report_path: Path | None = None) -> str:
    chain = _error_chain(exc)
    messages = [str(item).strip() or "메시지 없음" for item in chain]
    lines = [
        f"실패 단계: {action}",
        f"오류 종류: {type(exc).__name__}",
        f"오류 내용: {messages[0]}",
    ]
    if len(messages) > 1:
        lines.append(f"직접 원인: {messages[-1]}")
    lines.extend(("", "가능한 이유:", error_reason(exc)))
    if report_path is not None:
        lines.extend(("", f"상세 오류 보고서: {report_path}"))
    if action in {"자동 번역", "번역 적용", "CSV 가져오기·적용"}:
        lines.extend(("", "게임 파일이 변경된 상태라면 GUI의 [원본 복원]으로 되돌릴 수 있습니다."))
    return "\n".join(lines)


def write_error_report(
    selected_path: str,
    action: str,
    exc: BaseException,
    traceback_text: str,
    context: dict[str, object] | None = None,
) -> Path | None:
    """Persist full diagnostics beside the game; failure to log must not hide the real error."""
    try:
        selected = Path(selected_path).expanduser().resolve()
        root = selected if selected.is_dir() else selected.parent
        try:
            detection = find_game(root)
            if detection.engine != "unknown":
                root = Path(detection.root)
        except Exception:
            pass
        report_dir = root / "gameko_project" / "error_logs"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        report_path = report_dir / f"error-{stamp}.txt"
        details = [
            "GameKO by AINFORGE 오류 보고서",
            f"발생 시각: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"실패 단계: {action}",
            f"선택 경로: {selected_path}",
            f"오류 종류: {type(exc).__module__}.{type(exc).__name__}",
            f"오류 내용: {str(exc) or '메시지 없음'}",
            f"가능한 이유: {error_reason(exc)}",
        ]
        for index, cause in enumerate(_error_chain(exc)[1:], start=1):
            details.append(f"원인 {index}: {type(cause).__module__}.{type(cause).__name__}: {cause}")
        if context:
            details.extend(("", "실행 설정:"))
            details.extend(f"- {key}: {value}" for key, value in context.items())
        details.extend(("", "전체 추적:", traceback_text.rstrip(), ""))
        report_path.write_text("\n".join(details), encoding="utf-8")
        return report_path
    except Exception:
        return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GameKO by AINFORGE - 게임 자동 한국어 번역")
        self.geometry("890x740")
        self.minsize(780, 650)
        self._brand_logo_source: tk.PhotoImage | None = None
        self._brand_logo: tk.PhotoImage | None = None
        try:
            logo_resource = files("gameko").joinpath("assets", "AINFORGE.png")
            with as_file(logo_resource) as logo_path:
                self._brand_logo_source = tk.PhotoImage(file=str(logo_path))
            self._brand_logo = self._brand_logo_source.subsample(7, 7)
            self.iconphoto(True, self._brand_logo_source)
        except (FileNotFoundError, tk.TclError):
            # 로고 파일 문제로 번역 기능 전체가 시작되지 않는 상황은 피합니다.
            self._brand_logo_source = None
            self._brand_logo = None
        self.events: queue.Queue = queue.Queue()
        initial = sys.argv[1] if len(sys.argv) > 1 and Path(sys.argv[1]).exists() else str(Path.cwd())
        self.path = tk.StringVar(value=str(Path(initial).resolve()))
        self.engine = tk.StringVar(value="아직 검사하지 않음")
        self.provider = tk.StringVar(value=WEB_PROVIDER)
        self.source_language = tk.StringVar(value=DEFAULT_SOURCE_LANGUAGE_LABEL)
        self.base_url = tk.StringVar(value="http://localhost:11434/v1")
        self.model = tk.StringVar(value="")
        self.install_models = tk.BooleanVar(value=False)
        self.unity_static_test = tk.BooleanVar(value=False)
        self.force_tmp_font = tk.BooleanVar(value=False)
        self.time_text = tk.StringVar(value="진행 0.0% · 경과 00:00:00 · 남은 --:--:--")
        self._job_started: float | None = None
        self._job_running = False
        self._progress_value = 0
        self._progress_total = 0
        self._progress_samples: deque[tuple[float, int]] = deque(maxlen=40)
        self._build()
        self.after(100, self._poll)
        self.after(300, self.scan)

    def _build(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        brand = ttk.Frame(outer)
        brand.pack(fill="x", pady=(0, 4))
        if self._brand_logo is not None:
            ttk.Label(brand, image=self._brand_logo).pack(side="left", padx=(0, 12))
        brand_text = ttk.Frame(brand)
        brand_text.pack(side="left", anchor="center")
        ttk.Label(brand_text, text="GameKO", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(brand_text, text="CREATED BY AINFORGE", foreground="#666").pack(anchor="w")
        ttk.Label(outer, text="게임 폴더만 지정하면 엔진을 판별하고 영어·일본어를 한국어로 번역합니다.").pack(anchor="w", pady=(0, 14))

        path_row = ttk.Frame(outer)
        path_row.pack(fill="x")
        ttk.Entry(path_row, textvariable=self.path).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="폴더 선택", command=self.browse).pack(side="left", padx=(8, 0))
        ttk.Button(path_row, text="감지", command=self.scan).pack(side="left", padx=(8, 0))
        ttk.Label(outer, textvariable=self.engine, foreground="#2457a6").pack(anchor="w", pady=(8, 14))

        options = ttk.LabelFrame(outer, text="번역 방식 — 기본값은 API 키 없는 웹 자동 번역", padding=10)
        options.pack(fill="x")
        ttk.Label(options, text="방식").grid(row=0, column=0, sticky="w")
        provider_values = (WEB_PROVIDER, OPENAI_PROVIDER) if getattr(sys, "frozen", False) else (WEB_PROVIDER, ARGOS_PROVIDER, OPENAI_PROVIDER)
        providers = ttk.Combobox(options, textvariable=self.provider, values=provider_values, state="readonly", width=28)
        providers.grid(row=0, column=1, padx=(8, 18), sticky="w")
        if not getattr(sys, "frozen", False):
            ttk.Checkbutton(options, text="Argos 모델 자동 설치", variable=self.install_models).grid(row=0, column=2, sticky="w")
        else:
            ttk.Label(options, text="GameMaker·RPG Maker도 자동 버튼만 누르면 웹 번역됩니다.", foreground="#2457a6").grid(row=0, column=2, columnspan=3, sticky="w")
        ttk.Label(options, text="원문 언어").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            options,
            textvariable=self.source_language,
            values=tuple(SOURCE_LANGUAGE_LABELS),
            state="readonly",
            width=28,
        ).grid(row=1, column=1, padx=(8, 18), pady=(8, 0), sticky="w")
        ttk.Label(options, text="자동은 게임 전체를 보고 원작 언어를 정합니다.", foreground="#666").grid(
            row=1, column=2, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Label(options, text="OpenAI 호환 URL").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.base_url, width=38).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 18), pady=(8, 0))
        ttk.Label(options, text="모델").grid(row=2, column=3, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.model, width=20).grid(row=2, column=4, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(
            options,
            text="Unity 정적 패치(테스트) — 대본·CSV만 재패킹하며 XUnity는 설치하지 않음",
            variable=self.unity_static_test,
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(9, 0))
        ttk.Checkbutton(
            options,
            text="버전 무시하고 TMP 강제 적용 — 실행 오류 가능, 원본 복원 지원",
            variable=self.force_tmp_font,
        ).grid(row=4, column=0, columnspan=5, sticky="w", pady=(6, 0))
        options.columnconfigure(2, weight=1)
        options.columnconfigure(4, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=14)
        self.auto_button = ttk.Button(buttons, text="자동 번역 시작", command=self.auto)
        self.auto_button.pack(side="left")
        self.extract_button = ttk.Button(buttons, text="1. 추출만", command=self.extract)
        self.extract_button.pack(side="left", padx=(8, 0))
        self.translate_button = ttk.Button(buttons, text="2. 번역만", command=self.translate)
        self.translate_button.pack(side="left", padx=(8, 0))
        self.apply_button = ttk.Button(buttons, text="3. 적용", command=self.apply)
        self.apply_button.pack(side="left", padx=(8, 0))
        self.restore_button = ttk.Button(buttons, text="원본 복원", command=self.restore)
        self.restore_button.pack(side="right")

        dialogue = ttk.LabelFrame(outer, text="대사 직접 편집", padding=10)
        dialogue.pack(fill="x", pady=(0, 14))
        self.export_button = ttk.Button(dialogue, text="대사 추출·내보내기...", command=self.export_dialogue)
        self.export_button.pack(side="left")
        self.import_button = ttk.Button(dialogue, text="수정한 대사 가져와 적용...", command=self.import_and_apply)
        self.import_button.pack(side="left", padx=(8, 0))
        self.open_csv_button = ttk.Button(dialogue, text="작업용 CSV 열기", command=self.open_csv)
        self.open_csv_button.pack(side="left", padx=(8, 0))
        ttk.Label(dialogue, text="CSV의 target 열을 수정한 뒤 다시 가져오세요.", foreground="#666").pack(side="left", padx=(14, 0))
        self.action_buttons = (
            self.auto_button, self.extract_button, self.translate_button, self.apply_button,
            self.restore_button, self.export_button, self.import_button, self.open_csv_button,
        )

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x")
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.time_text, anchor="e", width=43).pack(side="right", padx=(12, 0))
        self.status = ttk.Label(outer, text="대기 중")
        self.status.pack(anchor="w", pady=(4, 8))
        self.log = tk.Text(outer, height=18, wrap="word", state="disabled", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True)
        ttk.Label(outer, text="주의: 정식 번역 도구가 허용된 게임/개인 백업에만 사용하세요. DRM 해제는 지원하지 않습니다.", foreground="#777").pack(anchor="w", pady=(8, 0))

    def browse(self):
        selected = filedialog.askdirectory(initialdir=self.path.get() or str(Path.cwd()))
        if selected:
            self.path.set(selected)
            self.scan()

    def _put(self, kind, *values):
        self.events.put((kind, values))

    def _log(self, message):
        self._put("log", str(message))

    def _progress(self, value, total, text):
        self._put("progress", value, total, text)

    def _error_context(self) -> dict[str, object]:
        return {
            "감지 표시": self.engine.get(),
            "번역 방식": self.provider.get(),
            "원문 언어": self.source_language.get(),
            "Unity 정적 패치": self.unity_static_test.get(),
            "TMP 버전 무시 강제 적용": self.force_tmp_font.get(),
        }

    def _show_error(self, action: str, exc: BaseException, traceback_text: str | None = None) -> None:
        report_path = write_error_report(
            self.path.get(), action, exc, traceback_text or traceback.format_exc(), self._error_context()
        )
        message = format_error_message(action, exc, report_path)
        self._log(message)
        messagebox.showerror("GameKO 오류 원인", message)

    def _run(self, function, action: str = "작업"):
        for button in self.action_buttons:
            button.configure(state="disabled")
        now = time.monotonic()
        self._job_started = now
        self._job_running = True
        self._progress_value = 0
        self._progress_total = 0
        self._progress_samples.clear()
        self.progress.configure(maximum=1, value=0)
        self.time_text.set("진행 0.0% · 경과 00:00:00 · 남은 --:--:--")
        self.status.configure(text=f"{action} 중...")
        selected_path = self.path.get()
        error_context = self._error_context()
        def worker():
            try:
                result = function()
                self._put("done", action, str(result) if result is not None else "완료")
            except Exception as exc:
                full_traceback = traceback.format_exc()
                report_path = write_error_report(
                    selected_path, action, exc, full_traceback, error_context
                )
                self._put("error", format_error_message(action, exc, report_path))
        threading.Thread(target=worker, daemon=True).start()

    def _record_progress(self, value: int, total: int) -> None:
        now = time.monotonic()
        if self._progress_total <= 0 and total > 0:
            self._progress_samples.clear()
        self._progress_value = value
        self._progress_total = total
        if not self._progress_samples or self._progress_samples[-1][1] != value:
            self._progress_samples.append((now, value))
        while len(self._progress_samples) > 2 and now - self._progress_samples[1][0] > 30:
            self._progress_samples.popleft()

    def _estimated_remaining(self) -> float | None:
        if self._progress_total <= 0 or self._progress_value >= self._progress_total:
            return 0.0 if self._progress_total > 0 else None
        if len(self._progress_samples) < 2:
            return None
        started_at, started_value = self._progress_samples[0]
        ended_at, ended_value = self._progress_samples[-1]
        elapsed = ended_at - started_at
        completed = ended_value - started_value
        if elapsed < 0.25 or completed <= 0:
            return None
        return (self._progress_total - self._progress_value) / (completed / elapsed)

    def _refresh_time_text(self) -> None:
        if not self._job_running or self._job_started is None:
            return
        elapsed = format_duration(time.monotonic() - self._job_started)
        percent = (self._progress_value / self._progress_total * 100) if self._progress_total else 0.0
        remaining = self._estimated_remaining()
        remaining_text = format_duration(remaining) if remaining is not None else "--:--:--"
        self.time_text.set(f"진행 {percent:5.1f}% · 경과 {elapsed} · 남은 {remaining_text}")

    def _finish_clock(self, label: str) -> None:
        elapsed = format_duration(time.monotonic() - self._job_started) if self._job_started is not None else "00:00:00"
        self._job_running = False
        if self._progress_total:
            percent = self._progress_value / self._progress_total * 100
            self.time_text.set(f"{label} · 진행 {percent:5.1f}% · 총 {elapsed}")
        else:
            self.time_text.set(f"{label} · 총 {elapsed}")

    def _poll(self):
        try:
            while True:
                kind, values = self.events.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", values[0] + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "progress":
                    value, total, text = values
                    self._record_progress(value, total)
                    self.progress.configure(maximum=max(total, 1), value=value)
                    self.status.configure(text=f"번역 {value:,}/{total:,}: {text}")
                elif kind == "done":
                    for button in self.action_buttons:
                        button.configure(state="normal")
                    action, result = values
                    self.status.configure(text=result)
                    self._finish_clock("완료")
                    self._log(f"완료({action}): {result}")
                    title, message = completion_dialog(action, result)
                    self.lift()
                    messagebox.showinfo(title, message, parent=self)
                elif kind == "error":
                    for button in self.action_buttons:
                        button.configure(state="normal")
                    first_line = values[0].splitlines()[0] if values[0] else "오류 발생"
                    self.status.configure(text=first_line)
                    self._finish_clock("중단")
                    self._log("오류 원인:\n" + values[0])
                    messagebox.showerror("GameKO 오류 원인", values[0])
        except queue.Empty:
            pass
        self._refresh_time_text()
        self.after(100, self._poll)

    def _detection(self):
        detection = find_game(self.path.get())
        if detection.engine == "unknown":
            raise RuntimeError("지원되는 게임 엔진을 찾지 못했습니다.")
        return detection

    def scan(self):
        try:
            d = find_game(self.path.get())
            label = "지원되는 게임을 찾지 못함" if d.engine == "unknown" else f"감지됨: {d.engine} {d.variant} — {d.root}"
            self.engine.set(label)
        except Exception as exc:
            self.engine.set(str(exc))

    def _options(self):
        return {"base_url": self.base_url.get(), "model": self.model.get(), "api_key": "", "install_models": self.install_models.get()}

    def _provider_key(self):
        return {
            WEB_PROVIDER: "web",
            OPENAI_PROVIDER: "openai",
            ARGOS_PROVIDER: "argos",
        }.get(self.provider.get(), "web")

    def _source_language_key(self):
        return source_language_key(self.source_language.get())

    def auto(self):
        source_language = self._source_language_key()
        self._run(lambda: automatic(
            self.path.get(), self._provider_key(), self._options(), self._log, self._progress,
            source_language=source_language,
            unity_static_test=self.unity_static_test.get(),
            force_tmp_font=self.force_tmp_font.get(),
        ), action="자동 번역")

    def extract(self):
        source_language = self._source_language_key()
        self._run(lambda: extract_game(
            self._detection(), self._log, source_language=source_language,
            unity_static_test=self.unity_static_test.get(),
            force_tmp_font=self.force_tmp_font.get(),
        )[0], action="대사 추출")

    def translate(self):
        source_language = self._source_language_key()
        self._run(lambda: translate_game(
            Path(self._detection().root), self._provider_key(), self._options(), self._log, self._progress,
            source_language=source_language,
        ), action="문장 번역")

    def apply(self):
        source_language = self._source_language_key()

        def task():
            root = Path(self._detection().root)
            csv_path = root / "gameko_project" / "translations.csv"
            if csv_path.exists():
                import_csv(root)
            return f"{apply_game(root, self._log, source_language=source_language, force_tmp_font=self.force_tmp_font.get()):,}개 적용 완료"
        self._run(task, action="번역 적용")

    def restore(self):
        if not messagebox.askyesno(
            "원본 복원", "GameKO가 만든 백업으로 원본 파일을 복원할까요?",
            parent=self,
        ):
            return
        def task():
            count = restore_game(self._detection(), self._log)
            return f"복원한 파일: {count:,}개"
        self._run(task, action="원본 복원")

    def export_dialogue(self):
        try:
            detection = self._detection()
            root = Path(detection.root)
            selected = filedialog.asksaveasfilename(
                title="대사 CSV 내보내기",
                initialdir=str(root),
                initialfile=f"{root.name}_대사.csv",
                defaultextension=".csv",
                filetypes=(("CSV 파일", "*.csv"), ("모든 파일", "*.*")),
            )
            if not selected:
                return
            source_language = self._source_language_key()

            def task():
                if not (root / "gameko_project" / "translations.jsonl").exists():
                    self._log("번역 프로젝트가 없어 먼저 대사를 추출합니다.")
                    extract_game(
                        detection, self._log, source_language=source_language,
                        unity_static_test=self.unity_static_test.get(),
                        force_tmp_font=self.force_tmp_font.get(),
                    )
                path = export_csv(root, selected)
                self._log(f"대사 CSV 내보내기: {path}")
                return f"대사 파일을 내보냈습니다: {path}"

            self._run(task, action="대사 CSV 내보내기")
        except Exception as exc:
            self._show_error("대사 CSV 내보내기", exc)

    def import_and_apply(self):
        try:
            root = Path(self._detection().root)
            selected = filedialog.askopenfilename(
                title="수정한 대사 CSV 가져오기",
                initialdir=str(root),
                filetypes=(("CSV 파일", "*.csv"), ("모든 파일", "*.*")),
            )
            if not selected:
                return
            if not messagebox.askyesno(
                "수정한 대사 적용",
                "선택한 CSV의 target 열을 게임에 적용할까요?\n최초 적용 시 원본 파일은 자동으로 백업됩니다.",
            ):
                return
            source_language = self._source_language_key()

            def task():
                imported = import_csv(root, selected)
                applied = apply_game(
                    root, self._log, source_language=source_language,
                    force_tmp_font=self.force_tmp_font.get(),
                )
                self._log(f"수정한 CSV {imported:,}행을 가져왔습니다.")
                return f"대사 {imported:,}행을 가져와 번역 {applied:,}개를 적용했습니다."

            self._run(task, action="CSV 가져오기·적용")
        except Exception as exc:
            self._show_error("CSV 가져오기·적용", exc)

    def open_csv(self):
        try:
            root = Path(self._detection().root)
            path = export_csv(root)
            os.startfile(path)
        except Exception as exc:
            self._show_error("작업용 CSV 열기", exc)


def main():
    if "--unity-static-smoke-test" in sys.argv:
        try:
            index = sys.argv.index("--unity-static-smoke-test")
            smoke_path = sys.argv[index + 1]
            detection = find_game(smoke_path)
            _project, count = extract_game(
                detection, unity_static_test=True, source_language="ja",
                force_tmp_font=True,
            )
            if detection.engine != "unity" or count < 1:
                raise RuntimeError("Unity 정적 패치 배포본 검사에서 대사를 찾지 못했습니다.")
            apply_game(
                Path(detection.root), source_language="ja", force_tmp_font=True
            )
            font_report = json.loads(
                (
                    Path(detection.root)
                    / "gameko_project"
                    / "unity_static_font_report.json"
                ).read_text(encoding="utf-8")
            )
            if int(font_report.get("font_assets", 0)) < 1:
                raise RuntimeError("Unity 정적 패치 배포본 검사에서 TMP 폰트를 교체하지 못했습니다.")
            restore_game(detection)
        except Exception:
            try:
                error_root = Path(smoke_path) / "gameko_project"
                error_root.mkdir(parents=True, exist_ok=True)
                (error_root / "frozen_smoke_error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
            except Exception:
                pass
            raise SystemExit(1)
        raise SystemExit(0)
    app = App()
    if "--smoke-test" in sys.argv:
        app.withdraw()
        app.after(800, app.destroy)
    app.mainloop()


if __name__ == "__main__":
    main()
