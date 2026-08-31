from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .model import Entry, project_path, save_entries
from .text import guess_language, is_translatable, protect_tokens, restore_tokens, split_tokens


Progress = Callable[[int, int, str], None]


class Translator:
    max_workers = 1

    def translate(self, text: str, source_lang: str) -> str:
        raise NotImplementedError


class GoogleWebTranslator(Translator):
    """API 키 없는 웹 번역 체인: Google Chrome → Google Web → MyMemory."""

    max_workers = 4

    def __init__(self):
        self._blocked_until: dict[str, float] = {}
        self._condition = threading.Condition()
        self._active = 0
        self._concurrency_limit = self.max_workers
        self._successes_since_throttle = 0
        self._last_throttle = 0.0

    @property
    def current_concurrency(self) -> int:
        with self._condition:
            return self._concurrency_limit

    def _enter_gate(self) -> None:
        with self._condition:
            while self._active >= self._concurrency_limit:
                self._condition.wait()
            self._active += 1

    def _leave_gate(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def _rate_limited(self, backend: str, code: int) -> None:
        now = time.monotonic()
        with self._condition:
            self._concurrency_limit = max(1, self._concurrency_limit // 2)
            self._successes_since_throttle = 0
            self._last_throttle = now
            self._blocked_until[backend] = now + (30 if code == 429 else 300)
            self._condition.notify_all()

    def _translation_succeeded(self) -> None:
        with self._condition:
            self._successes_since_throttle += 1
            stable = time.monotonic() - self._last_throttle >= 15
            if stable and self._successes_since_throttle >= 20 and self._concurrency_limit < self.max_workers:
                self._concurrency_limit += 1
                self._successes_since_throttle = 0
                self._condition.notify_all()

    @staticmethod
    def _read_json(request: urllib.request.Request) -> object:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _google_chrome(self, text: str, source_lang: str) -> str:
        query = urllib.parse.urlencode({
            "client": "dict-chrome-ex",
            "sl": source_lang if source_lang in {"ja", "en"} else "auto",
            "tl": "ko",
            "q": text,
        })
        request = urllib.request.Request(
            "https://clients5.google.com/translate_a/t?" + query,
            headers={"User-Agent": "Mozilla/5.0 GameKO/0.2"},
        )
        payload = self._read_json(request)
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], str):
            raise RuntimeError("Google Chrome 웹 번역 응답 형식이 올바르지 않습니다.")
        return payload[0]

    def _google_web(self, text: str, source_lang: str) -> str:
        query = urllib.parse.urlencode({
            "client": "gtx", "sl": source_lang if source_lang in {"ja", "en"} else "auto",
            "tl": "ko", "dt": "t", "q": text,
        })
        request = urllib.request.Request(
            "https://translate.googleapis.com/translate_a/single?" + query,
            headers={"User-Agent": "Mozilla/5.0 GameKO/0.2"},
        )
        payload = self._read_json(request)
        return "".join(part[0] for part in payload[0] if part and part[0])

    def _mymemory(self, text: str, source_lang: str) -> str:
        language = source_lang if source_lang in {"ja", "en"} else guess_language(text)
        if language not in {"ja", "en"}:
            raise RuntimeError("MyMemory가 원문 언어를 판별하지 못했습니다.")
        if len(text.encode("utf-8")) > 480:
            raise RuntimeError("MyMemory의 문장당 500바이트 제한을 초과했습니다.")
        query = urllib.parse.urlencode({"q": text, "langpair": f"{language}|ko", "mt": "1"})
        request = urllib.request.Request(
            "https://api.mymemory.translated.net/get?" + query,
            headers={"User-Agent": "Mozilla/5.0 GameKO/0.2"},
        )
        payload = self._read_json(request)
        status = int(payload.get("responseStatus", 0))
        translated = payload.get("responseData", {}).get("translatedText", "")
        if status != 200 or not isinstance(translated, str) or not translated.strip():
            raise RuntimeError(str(payload.get("responseDetails") or f"MyMemory HTTP 상태 {status}"))
        # 일부 MyMemory 응답은 UTF-8 한글을 Latin-1로 잘못 표시하므로 복구합니다.
        if not any("가" <= char <= "힣" for char in translated):
            try:
                repaired = translated.encode("latin1").decode("utf-8")
                if any("가" <= char <= "힣" for char in repaired):
                    translated = repaired
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        return translated

    def translate(self, text: str, source_lang: str) -> str:
        self._enter_gate()
        try:
            backends = (
                ("Google Chrome", self._google_chrome),
                ("Google Web", self._google_web),
                ("MyMemory", self._mymemory),
            )
            errors: list[str] = []
            for cycle in range(2):
                now = time.monotonic()
                attempted = False
                for name, backend in backends:
                    if self._blocked_until.get(name, 0) > now:
                        continue
                    attempted = True
                    try:
                        translated = backend(text, source_lang)
                        if translated.strip():
                            self._translation_succeeded()
                            return translated
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")
                        if isinstance(exc, urllib.error.HTTPError) and exc.code in {403, 429}:
                            self._rate_limited(name, exc.code)
                if attempted or cycle:
                    break
                # 모든 서비스가 잠시 제한된 경우 가장 먼저 풀릴 때까지만 기다렸다 재시도합니다.
                unblock_at = min(self._blocked_until.values(), default=now)
                time.sleep(max(0.1, min(30, unblock_at - now)))
            raise RuntimeError("키 없는 웹 번역 서비스가 모두 실패했습니다. " + " / ".join(errors))
        finally:
            self._leave_gate()


class OpenAICompatibleTranslator(Translator):
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key

    def translate(self, text: str, source_lang: str) -> str:
        language = "일본어" if source_lang == "ja" else "영어" if source_lang == "en" else "영어 또는 일본어"
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "게임 현지화 번역가입니다. 결과에는 번역문만 출력합니다. ⟦GKO숫자⟧ 토큰, 줄바꿈, 변수와 서식 코드는 한 글자도 바꾸지 않습니다. 자연스럽고 짧은 한국어 UI/대사로 번역합니다."},
                {"role": "user", "content": f"다음 {language} 게임 문자열을 한국어로 번역하세요:\n{text}"},
            ],
        }
        headers = {"Content-Type": "application/json", "User-Agent": "GameKO/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
        return result["choices"][0]["message"]["content"].strip()


class ArgosTranslator(Translator):
    def __init__(self, install_models: bool = False):
        try:
            import argostranslate.package as package
            import argostranslate.translate as translate
        except ImportError as exc:
            raise RuntimeError("오프라인 번역 기능이 없습니다. 'pip install -e .[offline]'로 설치하세요.") from exc
        self.package = package
        self.module = translate
        self.install_models = install_models
        if install_models:
            package.update_package_index()
            self._ensure("en", "ko")
            self._ensure("ja", "en")

    def _ensure(self, source: str, target: str) -> None:
        installed = self.module.get_installed_languages()
        src = next((x for x in installed if x.code == source), None)
        dst = next((x for x in installed if x.code == target), None)
        if src and dst:
            try:
                src.get_translation(dst)
                return
            except Exception:
                pass
        available = self.package.get_available_packages()
        model = next((x for x in available if x.from_code == source and x.to_code == target), None)
        if not model:
            raise RuntimeError(f"Argos {source}→{target} 모델을 찾지 못했습니다.")
        self.package.install_from_path(model.download())

    def translate(self, text: str, source_lang: str) -> str:
        source = source_lang if source_lang in {"ja", "en"} else guess_language(text)
        if source == "auto":
            return text
        try:
            return self.module.translate(text, source, "ko")
        except Exception as exc:
            hint = " --install-models 옵션으로 모델을 설치하세요." if not self.install_models else ""
            raise RuntimeError(f"Argos {source}→ko 번역 실패.{hint} ({exc})") from exc


def make_translator(provider: str, *, base_url: str = "http://localhost:11434/v1", model: str = "", api_key: str = "", install_models: bool = False) -> Translator:
    # "google"은 기존 CLI/프로젝트와의 하위 호환용 별칭입니다.
    if provider in {"web", "google"}:
        return GoogleWebTranslator()
    if provider == "argos":
        return ArgosTranslator(install_models)
    if provider == "openai":
        if not model:
            raise ValueError("OpenAI 호환 번역에는 모델 이름이 필요합니다.")
        return OpenAICompatibleTranslator(base_url, model, api_key or os.environ.get("OPENAI_API_KEY", ""))
    raise ValueError(f"알 수 없는 번역 제공자: {provider}")


def translate_preserving_tokens(
    translator: Translator, source: str, source_language: str
) -> str:
    """Translate once with markers, then safely retry around tokens if a site drops one."""
    protected, tokens = protect_tokens(source)
    translated = translator.translate(protected, source_language)
    if not tokens:
        return translated
    try:
        return restore_tokens(translated, tokens)
    except ValueError:
        # 일부 키 없는 웹 번역기는 특수 보호 표식까지 삭제하거나 고칩니다.
        # 이 경우 자연어 구간만 따로 번역하고 원본 제어 태그를 그대로 결합합니다.
        rebuilt: list[str] = []
        translated_spans = 0
        for is_token, part in split_tokens(source):
            if is_token or not is_translatable(part):
                rebuilt.append(part)
                continue
            rebuilt.append(translator.translate(part, source_language))
            translated_spans += 1
        if not translated_spans:
            raise
        return "".join(rebuilt)


def translate_project(
    root: Path,
    translator: Translator,
    progress: Progress | None = None,
    source_language: str | None = None,
) -> tuple[int, int]:
    from .model import load_entries

    entries = load_entries(root)
    memory_path = project_path(root) / "translation_memory.json"
    memory: dict[str, str] = {}
    if memory_path.exists():
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    unique: dict[str, list[Entry]] = {}
    for entry in entries:
        entry_language = (
            entry.source_lang if entry.source_lang in {"ja", "en"} else guess_language(entry.source)
        )
        if source_language in {"ja", "en"} and entry_language != source_language:
            continue
        if not entry.target.strip():
            unique.setdefault(entry.source, []).append(entry)
    total = len(unique)
    done = 0
    completed_since_checkpoint = 0
    last_checkpoint = time.monotonic()

    def checkpoint(force: bool = False) -> None:
        nonlocal completed_since_checkpoint, last_checkpoint
        due = completed_since_checkpoint >= 50 or time.monotonic() - last_checkpoint >= 2
        if not force and not due:
            return
        memory_temp = memory_path.with_suffix(".json.tmp")
        memory_temp.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
        memory_temp.replace(memory_path)
        save_entries(root, entries)
        completed_since_checkpoint = 0
        last_checkpoint = time.monotonic()

    def use_translation(source: str, linked: list[Entry], translated: str, is_new: bool) -> None:
        nonlocal done, completed_since_checkpoint
        if is_new:
            memory[source] = translated
            completed_since_checkpoint += 1
        for entry in linked:
            entry.target = translated
            entry.status = "machine"
        done += 1
        if progress:
            progress(done, total, source[:80])

    pending: list[tuple[str, list[Entry]]] = []
    for source, linked in unique.items():
        if source in memory:
            use_translation(source, linked, memory[source], False)
        else:
            pending.append((source, linked))

    def translate_one(source: str, linked: list[Entry]) -> str:
        language = source_language if source_language in {"ja", "en"} else linked[0].source_lang
        if language not in {"ja", "en"}:
            language = guess_language(source)
        return translate_preserving_tokens(translator, source, language)

    errors: list[tuple[str, Exception]] = []
    workers = min(max(1, getattr(translator, "max_workers", 1)), max(1, len(pending)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gameko-translate") as executor:
        future_sources = {
            executor.submit(translate_one, source, linked): (source, linked)
            for source, linked in pending
        }
        for future in as_completed(future_sources):
            source, linked = future_sources[future]
            try:
                use_translation(source, linked, future.result(), True)
            except Exception as exc:
                errors.append((source, exc))
            checkpoint()

    checkpoint(force=True)
    if errors:
        source, exc = errors[0]
        raise RuntimeError(f"{len(errors)}개 문장 번역에 실패했습니다. 첫 오류: {source[:80]} ({exc})") from exc
    return done, total
