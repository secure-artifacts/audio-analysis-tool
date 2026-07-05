from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Callable

try:
    from groq import APIStatusError, Groq, RateLimitError
except ModuleNotFoundError:
    class APIStatusError(Exception):
        status_code = 500
        body = "groq package is not installed"

    class RateLimitError(Exception):
        response = None

    Groq = None


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_PATH = ROOT / "server.log"
RUBRIC_PATH = ROOT / "rubric.json"
PID_PATH = ROOT / "server.pid"
LOG_LOCK = threading.Lock()
SEGMENT_SECONDS = 8 * 60
WHISPER_MODELS = {"whisper-large-v3-turbo", "whisper-large-v3"}
DEFAULT_CHAT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
LANGUAGE_NAMES = {
    "fr": "法语",
    "en": "英语",
    "pt": "葡萄牙语",
    "ar": "阿拉伯语",
    "mg": "马尔加什语",
}
PAIR_TITLES = {
    "fr": "法中对照",
    "en": "英中对照",
    "pt": "葡中对照",
    "ar": "阿中对照",
    "mg": "马中对照",
}


def redact_sensitive(text: object) -> str:
    value = str(text)
    value = re.sub(r"gsk_[A-Za-z0-9_-]+", "gsk_***", value)
    value = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", value)
    return value


def write_log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact_sensitive(message)}"
    sys.stderr.write(line + "\n")
    with LOG_LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as f:  # lgtm[py/clear-text-logging-sensitive-data]
            f.write(line + "\n")


def write_exception(message: str) -> None:
    write_log(f"{message}\n{redact_sensitive(traceback.format_exc())}")


class FormField:
    """Represents one multipart form field."""
    def __init__(self, value: str = "", filename: str = "", file_bytes: bytes = b""):
        self.value = value
        self.filename = filename
        self.file = BytesIO(file_bytes) if file_bytes else BytesIO()


def parse_multipart_form(headers: dict, body: bytes) -> dict[str, FormField]:
    """Parse multipart/form-data brut et retourne un dict {nom_champ: FormField}."""
    content_type = headers.get("Content-Type", "")
    match = re.search(r'boundary=([^;\s]+)', content_type)
    if not match:
        raise ValueError("Content-Type missing boundary")
    boundary = match.group(1).strip('"').encode()

    result: dict[str, FormField] = {}
    # Split by boundary.
    parts = body.split(b"--" + boundary)
    for part in parts:
        if not part or part.strip(b"\r\n") in (b"--", b""):
            continue
        # Split headers and body.
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        raw_headers = part[:header_end].decode("utf-8", errors="replace")
        raw_body = part[header_end + 4:]
        # Drop the optional trailing CRLF.
        if raw_body.endswith(b"\r\n"):
            raw_body = raw_body[:-2]

        # Extract Content-Disposition.
        disp_match = re.search(r'Content-Disposition:\s*form-data;\s*name="([^"]+)"', raw_headers)
        if not disp_match:
            continue
        name = disp_match.group(1)

        # Check whether this field is a file.
        filename_match = re.search(r'filename="([^"]*)"', raw_headers)
        if filename_match:
            filename = filename_match.group(1)
            result[name] = FormField(filename=filename, file_bytes=raw_body)
        else:
            result[name] = FormField(value=raw_body.decode("utf-8", errors="replace"))

    return result

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def now() -> float:
    return time.time()


def clean_keys(text: str) -> list[str]:
    keys = []
    seen = set()
    for line in re.split(r"[\r\n,; ]+", text):
        key = line.strip()
        if key and key not in seen:
            keys.append(key)
            seen.add(key)
    return keys


def safe_name(name: str) -> str:
    name = Path(name or "audio").name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120] or "audio"


def clean_job_id(job_id: str) -> str:
    job_id = str(job_id or "").lower()
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid job id")
    return f"{int(job_id, 16):012x}"


def inside_dir(base: Path, path: Path) -> Path:
    base_text = os.path.abspath(os.fspath(base))
    path_text = os.path.abspath(os.fspath(path))
    if path_text != base_text and not path_text.startswith(base_text + os.sep):
        raise ValueError("path escapes allowed directory")
    return Path(path_text)


def job_dir_for(job_id: str) -> Path:
    return inside_dir(DATA_DIR, DATA_DIR / clean_job_id(job_id))


def path_in_data(value: str | Path) -> Path | None:
    try:
        return inside_dir(DATA_DIR, Path(str(value)))
    except ValueError:
        return None


def checked_data_path(path: Path) -> Path:
    safe_path = path_in_data(path)
    if safe_path is None:
        raise ValueError("invalid data path")
    return safe_path


def job_result_path(job_id: str) -> Path:
    return checked_data_path(job_dir_for(job_id) / "result.json")


def write_json_file(path: Path, data: object) -> None:
    checked_data_path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")  # lgtm[py/path-injection]


def read_json_file(path: Path, encoding: str = "utf-8-sig") -> object:
    return json.loads(checked_data_path(path).read_text(encoding=encoding))  # lgtm[py/path-injection]


def remove_data_tree(path: Path) -> None:
    shutil.rmtree(str(checked_data_path(path)), ignore_errors=True)  # lgtm[py/path-injection]


def checked_read_path(path: Path) -> Path:
    path_text = os.path.abspath(os.fspath(path))
    if path_text == os.path.abspath(os.fspath(ROOT / "index.html")):
        return Path(path_text)
    return inside_dir(DATA_DIR, Path(path_text))


def safe_header_value(value: str, fallback: str = "application/octet-stream") -> str:
    value = str(value or fallback)
    if "\r" in value or "\n" in value:
        return fallback
    return value


def format_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def update_job(job_id: str, **patch: object) -> None:
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(patch)


def get_job(job_id: str) -> dict | None:
    with LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def saved_annotations(job_id: str) -> dict:
    result_path = job_result_path(job_id)
    if not result_path.is_file():
        return {}
    try:
        data = read_json_file(result_path)
        return data.get("annotations") or {} if isinstance(data, dict) else {}
    except Exception:
        return {}


class KeyPool:
    def __init__(self, keys: list[str], job_id: str):
        self.keys = keys
        self.job_id = job_id
        self.index = 0
        self.lock = threading.Lock()

    def next(self) -> str:
        with self.lock:
            key = self.keys[self.index % len(self.keys)]
            self.index += 1
            return key

    def label(self, key: str) -> str:
        return "API key"


def run_ffmpeg(job_id: str, audio_path: Path, chunks_dir: Path) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg。请先安装 ffmpeg，并确认命令行可以运行 ffmpeg。")

    chunks_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = chunks_dir / "chunk_%04d.flac"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "flac",
        "-f",
        "segment",
        "-segment_time",
        str(SEGMENT_SECONDS),
        "-reset_timestamps",
        "1",
        str(output_pattern),
    ]
    update_job(job_id, message="正在用 ffmpeg 切片音频...")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError("ffmpeg 切片失败：\n" + proc.stderr[-3000:])
    chunks = sorted(chunks_dir.glob("chunk_*.flac"))
    if not chunks:
        raise RuntimeError("ffmpeg 没有生成音频切片。")
    return chunks


def _call_groq(
    job_id: str,
    key_pool: KeyPool,
    label: str,
    call_fn: Callable[[Groq], dict],
) -> dict:
    """Call Groq with key rotation and basic retry handling."""
    failed_keys: set[str] = set()
    total_keys = len(key_pool.keys)
    log = lambda msg: write_log(f"[_call_groq] {msg}")

    while len(failed_keys) < total_keys:
        if Groq is None:
            raise RuntimeError("缺少 groq Python 包。请先运行：pip install groq")
        key = key_pool.next()
        if key in failed_keys:
            continue
        masked = key_pool.label(key)
        log(f"{label} - trying key {masked}")
        update_job(job_id, message=f"{label}: using key {masked}")

        try:
            client = Groq(api_key=key)
            result = call_fn(client)
            log(f"{label} - key {masked} ok")
            return result
        except RateLimitError as exc:
            wait = 30
            try:
                raw = exc.response.headers.get("retry-after", "30")
                wait = max(1, min(600, int(float(raw))))
            except (ValueError, TypeError, AttributeError):
                pass
            log(f"{label} - key {masked} rate limited, waiting {wait}s")
            update_job(
                job_id,
                message=f"Groq rate limited. Waiting {wait}s before switching key...",
                waiting_until=now() + wait,
            )
            time.sleep(wait)
            continue
        except APIStatusError as exc:
            status = exc.status_code
            resp_body = str(exc.body)[:2000]
            log(f"{label} - {masked} HTTP {status}: {resp_body}")
            if status in (401, 403):
                failed_keys.add(key)
                log(f"{label} - {masked} rejected ({status}), marked failed ({len(failed_keys)}/{total_keys})")
                update_job(
                    job_id,
                    message=f"{masked} rejected ({status}); switching to next key...",
                )
                continue
            log(f"{label} - {masked} unrecoverable error {status}")
            raise RuntimeError(f"Groq {label} HTTP {status}: {resp_body}") from exc
        except Exception:
            write_exception(f"Groq {label} unexpected error")
            raise

    log(f"{label} - all {total_keys} key(s) rejected")
    raise RuntimeError(f"Groq {label} failed: all {total_keys} key(s) returned 403")


def groq_json_request(job_id: str, key_pool: KeyPool, payload: dict) -> dict:
    def call(client: Groq) -> dict:
        return client.chat.completions.create(**payload).model_dump()
    return _call_groq(job_id, key_pool, "chat request", call)


def rows_missing_zh(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if (row.get("fr") or row.get("fr_raw") or "").strip()
        and not (row.get("zh") or "").strip()
    ]


def transcribe_chunk(job_id: str, key_pool: KeyPool, path: Path, model: str, language: str, index: int, total: int) -> dict:
    prompts = {
        "fr": "Transcription audio en français. Conserver les noms propres et le style oral.",
        "en": "Transcription of an English audio recording. Keep proper names and oral style.",
        "pt": "Transcrição de áudio em português. Manter nomes próprios e estilo oral.",
        "ar": "تفريغ تسجيل صوتي باللغة العربية. حافظ على الأسماء والأسلوب الشفهي.",
        "mg": "Fandikana lahateny am-peo amin'ny teny malagasy. Tazomy ny anarana manokana sy ny fomba fiteny am-bava.",
    }
    prompt = prompts.get(language, prompts["en"])

    def call(client: Groq) -> dict:
        with checked_data_path(path).open("rb") as f:
            return client.audio.transcriptions.create(
                file=f,
                model=model,
                language=language if language in LANGUAGE_NAMES else "en",
                response_format="verbose_json",
                temperature=0,
                timestamp_granularities=["segment"],
                prompt=prompt,
            ).model_dump()

    return _call_groq(job_id, key_pool, f"transcribe chunk {index}/{total}", call)


def group_segments(segments: list[dict]) -> list[dict]:
    paragraphs: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(s["text"].strip() for s in current if s.get("text", "").strip())
        if text:
            start = current[0]["start"]
            end = current[-1]["end"]
            paragraphs.append(
                {
                    "id": len(paragraphs) + 1,
                    "timestamp": format_ts(start),
                    "start": start,
                    "end": end,
                    "speaker": "发言人",
                    "fr_raw": text,
                }
            )
        current.clear()

    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue
        if current:
            gap = float(seg["start"]) - float(current[-1]["end"])
            chars = sum(len(s.get("text", "")) for s in current)
            if gap > 2.2 or chars > 520 or re.search(r"[.!?…]$", current[-1].get("text", "").strip()):
                flush()
        current.append(seg)
    flush()
    return paragraphs


def extract_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response is not JSON")
    return json.loads(text[start : end + 1])


def chat_content(resp: dict) -> str:
    return resp["choices"][0]["message"]["content"]


def parse_rubric_json(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    rubric = []
    for item in data:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("text") or item.get("name") or item.get("label") or item.get("criterion") or "").strip()
            detail = str(item.get("detail") or "").strip()
        else:
            title = str(item).strip()
            detail = ""
        if title.lower() in {"none", "null", "undefined"}:
            title = ""
        if title or detail:
            rubric.append({"title": (title or "未命名")[:80], "detail": detail[:1000]})
    return rubric


def review_item_markdown(item: dict) -> str:
    time_part = f"（{item.get('time')}）" if item.get("time") else ""
    criterion = f"{item.get('criterion')}：" if item.get("criterion") else ""
    title = criterion + (item.get("title") or item.get("summary") or "观察")
    lines = [f"- {time_part}{title}".strip()]
    labels = [
        ("detail", "位置/说明"),
        ("reason", "原因"),
        ("impact", "影响"),
        ("suggestion", "改法"),
        ("practice", "下次练习目标"),
    ]
    for key, label in labels:
        if item.get(key):
            lines.append(f"  - {label}：{item[key]}")
    return "\n".join(lines)


def structured_review_markdown(review: dict) -> str:
    if not review:
        return ""
    sections = [
        ("总评", review.get("overall")),
        ("主要优点", review.get("strengths")),
        ("主要问题", review.get("issues")),
        ("针对评价标准", review.get("focus")),
    ]
    lines: list[str] = []
    for title, value in sections:
        if not value:
            continue
        lines.extend([f"## {title}", ""])
        if isinstance(value, list):
            lines.extend(review_item_markdown(item) for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            if value.get("summary"):
                lines.extend([str(value["summary"]), ""])
            lines.extend(review_item_markdown(item) for item in value.get("items", []) if isinstance(item, dict))
        else:
            lines.append(str(value))
        lines.append("")
    return "\n".join(lines).strip()


def is_review_object(value: dict) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("overall", "strengths", "issues", "focus"))


def patch_paragraph(result: dict, para_id: str, patch: dict) -> dict | None:
    paragraph = next(
        (
            p for p in result.get("paragraphs", [])
            if str(p.get("id") or p.get("timestamp") or "") == str(para_id)
        ),
        None,
    )
    if not paragraph:
        return None
    for field in ("speaker", "fr", "zh"):
        if field in patch:
            paragraph[field] = str(patch[field])[:20000]
    return paragraph


def polish_and_translate(job_id: str, key_pool: KeyPool, paragraphs: list[dict], chat_model: str, language: str = "fr") -> list[dict]:
    if not paragraphs:
        return []
    out: list[dict] = []
    batch_size = 12
    batches = [paragraphs[i : i + batch_size] for i in range(0, len(paragraphs), batch_size)]
    lang_name = LANGUAGE_NAMES.get(language, "法语")
    system = (
        f"你是{lang_name}录音转写整理助手。只修正明显口误、错词和不通顺处，不能扩写、删减或改变原意。"
        f"把{lang_name}整理成自然段，并翻译成自然中文。必须保留每段 id、timestamp、speaker。"
        "每个输入段落都必须返回中文 zh；即使原文不完整，也要按能理解的内容直译，不得留空。"
        "只返回 JSON：{\"paragraphs\":[{\"id\":1,\"timestamp\":\"00:00:00\",\"speaker\":\"发言人\",\"fr\":\"...\",\"zh\":\"...\"}]}"
    )
    for i, batch in enumerate(batches, 1):
        update_job(job_id, progress=65 + int(i / len(batches) * 20), message=f"正在整理和翻译第 {i}/{len(batches)} 批...")
        payload = {
            "model": chat_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "paragraphs": [
                                {
                                    "id": p["id"],
                                    "timestamp": p["timestamp"],
                                    "speaker": p["speaker"],
                                    "fr_raw": p["fr_raw"],
                                }
                                for p in batch
                            ]
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        try:
            data = extract_json_object(chat_content(groq_json_request(job_id, key_pool, payload)))
            returned = data.get("paragraphs", [])
        except Exception:
            write_exception(f"polish/translate batch {i}/{len(batches)} failed to parse or request")
            returned = []

        by_id = {int(p.get("id", 0)): p for p in returned if str(p.get("id", "")).isdigit()}
        translated_batch: list[dict] = []
        for p in batch:
            got = by_id.get(p["id"], {})
            translated_batch.append(
                {
                    **p,
                    "fr": got.get("fr") or p["fr_raw"],
                    "zh": got.get("zh") or "",
                    "speaker": got.get("speaker") or p["speaker"],
                }
            )
        missing = rows_missing_zh(translated_batch)
        if missing:
            update_job(job_id, message=f"补齐漏翻段落 {len(missing)} 段...")
            retry_payload = {
                "model": chat_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "把输入段落翻译成自然中文。只返回 JSON：{\"paragraphs\":[{\"id\":1,\"zh\":\"...\"}]}。每个 id 都必须返回非空 zh。"},
                    {"role": "user", "content": json.dumps({"paragraphs": [{"id": p["id"], "fr": p.get("fr") or p.get("fr_raw", "")} for p in missing]}, ensure_ascii=False)},
                ],
            }
            try:
                retry_data = extract_json_object(chat_content(groq_json_request(job_id, key_pool, retry_payload)))
                zh_by_id = {
                    int(p.get("id", 0)): str(p.get("zh") or "").strip()
                    for p in retry_data.get("paragraphs", [])
                    if str(p.get("id", "")).isdigit()
                }
                for p in missing:
                    p["zh"] = zh_by_id.get(p["id"], p.get("zh", ""))
            except Exception:
                write_exception(f"retry missing zh failed for {len(missing)} paragraph(s)")
                pass
        out.extend(translated_batch)
    return out


def evaluate_recording(
    job_id: str,
    key_pool: KeyPool,
    paragraphs: list[dict],
    chat_model: str,
    original_script: str,
    rubric: list[dict] | None = None,
) -> tuple[str, dict]:
    if not paragraphs:
        return "", {}
    duration = max((p.get("end", 0) for p in paragraphs), default=0)
    words = sum(len((p.get("fr") or p.get("fr_raw") or "").split()) for p in paragraphs)
    wpm = round(words / max(duration / 60, 1))
    transcript = "\n".join(
        f"[{p['timestamp']}] {p.get('speaker', '发言人')} FR: {p.get('fr') or p.get('fr_raw', '')} ZH: {p.get('zh', '')}"
        for p in paragraphs
    )
    if len(transcript) > 28000:
        head = transcript[:14000]
        tail = transcript[-14000:]
        transcript = head + "\n\n[中间内容因长度折叠，评价时重点使用可见时间点]\n\n" + tail

    selected_rubric = parse_rubric_json(json.dumps(rubric or [], ensure_ascii=False))
    update_job(job_id, progress=92, message="正在生成录音评价...")
    system = (
        "你是录音内容分析助手。请用中文评价这段录音，必须具体、温和、准确。"
        "指出优点和问题时尽量带 00:00:00 格式时间点。不要编造音频中没有的内容。"
        "不要复述输入内容，不要返回统计、参考稿、实际录音转录或评价要求。"
        "只返回 JSON，不要 Markdown，不要代码块。"
    )
    review_format = {
        "overall": "3-5句总评",
        "strengths": [{"time": "00:00:00", "title": "优点标题", "detail": "具体说明"}],
        "issues": [{"time": "00:00:00", "title": "问题标题", "detail": "问题位置/具体表现", "reason": "原因", "impact": "影响", "suggestion": "具体改法", "practice": "下次练习目标"}],
        "focus": [] if not selected_rubric else [{"criterion": "所选评价标准", "time": "00:00:00", "title": "针对性观察", "detail": "问题位置/具体表现", "reason": "原因", "impact": "影响", "suggestion": "具体改法", "practice": "下次练习目标"}],
    }
    user = {
        "统计": {"大约词每分钟": wpm, "总时长": format_ts(duration)},
        "参考稿": original_script[:10000],
        "实际录音转录": transcript,
        "返回格式": review_format,
        "本次评价标准": selected_rubric,
        "评价要求": [
            "每个评价标准都包含 title 和 detail；title 是评价方向，detail 是判断细则。",
            "如果本次评价标准为空，focus 必须返回空数组 []，不要自行添加固定评价方向。",
            "如果本次评价标准不为空，focus 数组要按本次评价标准逐项返回，criterion 必须等于对应标准的 title。",
            "问题和专项观察尽量写清楚：位置、原因、影响、改法、下次练习目标。",
        ],
    }
    payload = {
        "model": chat_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    }
    content = chat_content(groq_json_request(job_id, key_pool, payload)).strip()
    try:
        structured = extract_json_object(content)
        if not is_review_object(structured):
            raise ValueError("model returned non-review JSON")
        markdown = structured_review_markdown(structured)
        return markdown or "暂无评价", structured
    except Exception as exc:
        write_log(f"review response invalid: {exc}")
        return f"评价生成失败：模型没有返回有效评价。请换一个整理/翻译模型后再点“分析”。", {}


def run_job(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    try:
        job_dir = job_dir_for(job_id)
        result_path = job_result_path(job_id)
        audio_path = checked_data_path(job_dir / "audio")
        sys.stderr.write(f"[run_job] {job_id} keys={len(job['keys'])}\n")
        sys.stderr.write(f"[run_job] {job_id} whisper={job['whisper_model']}, chat={job['chat_model']}\n")
        update_job(job_id, status="running", progress=2, message="任务开始...")
        chunks = run_ffmpeg(job_id, audio_path, checked_data_path(job_dir / "chunks"))
        key_pool = KeyPool(job["keys"], job_id)

        # Register result_path early so the frontend can read partial results.
        update_job(job_id, result_path=str(result_path))

        all_segments: list[dict] = []
        all_paragraphs: list[dict] = []
        total_chunks = len(chunks)

        # 根据时间范围过滤需要处理的切片
        range_start = job.get("range_start")
        range_end = job.get("range_end")
        chunk_indices: list[int] = []
        for idx in range(total_chunks):
            cstart = idx * SEGMENT_SECONDS
            cend = (idx + 1) * SEGMENT_SECONDS
            if range_start is not None and cend <= range_start:
                continue
            if range_end is not None and cstart >= range_end:
                continue
            chunk_indices.append(idx)

        if not chunk_indices:
            raise RuntimeError("时间范围内没有音频切片，请调整起止时间。")

        total_active = len(chunk_indices)
        sys.stderr.write(f"[run_job] {job_id} active chunks {total_active}/{total_chunks} range {range_start or 0}~{range_end or 'end'}s\n")

        for pos, idx in enumerate(chunk_indices, 1):
            index = idx + 1
            chunk = chunks[idx]
            chunk_start = idx * SEGMENT_SECONDS

            # 1. Transcribe this chunk
            progress_base = 8 + int((pos - 1) / total_active * 40)  # 8 鈫?48
            update_job(job_id, progress=progress_base, waiting_until=None, message=f"转录第 {pos}/{total_active} 段...")
            data = transcribe_chunk(job_id, key_pool, chunk, job["whisper_model"], job.get("language", "fr"), index, total_active)
            raw_segments = data.get("segments") or []
            if not raw_segments and data.get("text"):
                raw_segments = [{"start": 0, "end": SEGMENT_SECONDS, "text": data["text"]}]

            for seg in raw_segments:
                text = str(seg.get("text", "")).strip()
                if text:
                    all_segments.append({
                        "start": chunk_start + float(seg.get("start", 0)),
                        "end": chunk_start + float(seg.get("end", 0)),
                        "text": text,
                    })

            # Save raw segments incrementally.
            write_json_file(job_dir / "raw_segments.json", all_segments)

            # 2. Polish and translate this chunk.
            translate_progress = 50 + int((pos - 1) / total_active * 35)  # 50 鈫?85
            update_job(job_id, progress=translate_progress, message=f"整理翻译第 {pos}/{total_active} 段...")
            chunk_paragraphs = group_segments([
                s for s in all_segments
                if chunk_start <= s["start"] < chunk_start + SEGMENT_SECONDS
            ])
            if chunk_paragraphs:
                id_offset = len(all_paragraphs)
                for p in chunk_paragraphs:
                    p["id"] += id_offset
                translated = polish_and_translate(job_id, key_pool, chunk_paragraphs, job["chat_model"], job.get("language", "fr"))
                all_paragraphs.extend(translated)

            # Save partial result incrementally.
            partial_result = {
                "id": job_id,
                "audio_name": job["audio_name"],
                "whisper_model": job["whisper_model"],
                "chat_model": job["chat_model"],
                "created_at": job["created_at"],
                "language": job.get("language", "fr"),
                "script": job.get("script", ""),
                "paragraphs": all_paragraphs,
                "review": "",
                "structured_review": {},
                "rubric": job.get("rubric", []),
                "annotations": saved_annotations(job_id),
            }
            write_json_file(result_path, partial_result)

            # Pause checkpoint.
            if pos < total_active:
                for _ in range(600):
                    current = get_job(job_id)
                    if current is None:
                        return
                    if not current.get("paused"):
                        break
                    update_job(job_id, status="paused", message="已暂停，点击继续恢复")
                    time.sleep(1)
                else:
                    raise RuntimeError("暂停超时，任务取消。")
                # Refresh job state after resume.
                update_job(job_id, status="running", waiting_until=None)

        # 3. Evaluate after the transcript is complete.
        review = ""
        structured_review = {}
        if job.get("make_review"):
            update_job(job_id, progress=90, message="正在生成录音评价...")
            try:
                review, structured_review = evaluate_recording(job_id, key_pool, all_paragraphs, job["chat_model"], job.get("script", ""), job.get("rubric"))
            except Exception as exc:
                review = f"评价生成失败：{exc}\n\n转录和翻译已完成。可以稍后更换模型或 Key 后重新转录并生成评价。"
                structured_review = {}
                write_exception(f"review failed for {job_id}: {exc}")

        result = {
            "id": job_id,
            "audio_name": job["audio_name"],
            "whisper_model": job["whisper_model"],
            "chat_model": job["chat_model"],
            "created_at": job["created_at"],
            "language": job.get("language", "fr"),
            "script": job.get("script", ""),
            "paragraphs": all_paragraphs,
            "review": review,
            "structured_review": structured_review,
            "rubric": job.get("rubric", []),
            "annotations": saved_annotations(job_id),
        }
        write_json_file(result_path, result)
        update_job(job_id, status="done", progress=100, message="完成", result_path=str(result_path), result=result)
    except Exception as exc:
        write_exception(f"job {job_id} failed: {exc}")
        update_job(job_id, status="error", message=str(exc), error=str(exc), progress=100)


def markdown_result(result: dict) -> str:
    annotations = result.get("annotations") or {}
    lines = [
        f"# {result.get('audio_name', '录音转录')}",
        "",
        f"- Groq 转录模型：{result.get('whisper_model', '')}",
        f"- 文本整理模型：{result.get('chat_model', '')}",
        "",
    ]
    if result.get("rubric"):
        lines.extend(["## 评价标准", ""])
        for item in result["rubric"]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('title', '未命名')}")
                if item.get("detail"):
                    lines.append(f"  - {item['detail']}")
            else:
                lines.append(f"- {item}")
        lines.append("")
    lines.extend([f"## {PAIR_TITLES.get(result.get('language'), '法中对照')}", ""])
    for p in result.get("paragraphs", []):
        para_id = str(p.get("id") or p.get("timestamp") or "")
        annotation = annotations.get(para_id) or {}
        lines.extend(
            [
                f"### [{p.get('timestamp', '')}] {p.get('speaker', '')}",
                "",
                p.get("fr", p.get("fr_raw", "")),
                "",
                p.get("zh", ""),
                "",
            ]
        )
        if annotation.get("color") or annotation.get("note"):
            lines.extend(
                [
                    "**标注**",
                    "",
                    f"- 状态：{annotation.get('color') or '普通'}",
                    f"- 备注：{annotation.get('note') or ''}",
                    "",
                ]
            )
    if result.get("review"):
        lines.extend(["## 录音评价", "", result["review"], ""])
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    def send_header(self, keyword: str, value: object) -> None:
        super().send_header(keyword, safe_header_value(str(value), ""))

    def load_job_result(self, job_id: str, job: dict | None = None) -> tuple[dict | None, Path]:
        job_id = clean_job_id(job_id)
        result_path = job_result_path(job_id)
        result = (job or {}).get("result")
        if not result and result_path.is_file():
            loaded = read_json_file(result_path)
            result = loaded if isinstance(loaded, dict) else None
        return result, result_path

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self.send_file(ROOT / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/api/rubric":
            rubric = []
            if RUBRIC_PATH.is_file():
                rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8-sig"))
            self.send_json({"rubric": rubric if isinstance(rubric, list) else []})
            return
        if self.path == "/api/jobs":
            # Return in-memory jobs plus completed jobs saved under data/.
            items = []
            seen_ids = set()
            with LOCK:
                stale_ids = []
                for j in JOBS.values():
                    finished = j.get("status") in ("done", "error")
                    result_path = job_result_path(j["id"]) if j.get("id") else None
                    if finished and result_path and not result_path.is_file():
                        stale_ids.append(j["id"])
                        continue
                    items.append({
                        "id": j["id"],
                        "audio_name": j.get("audio_name", ""),
                        "display_name": (j.get("result") or {}).get("display_name") or j.get("display_name", ""),
                        "note": (j.get("result") or {}).get("note") or j.get("note", ""),
                        "status": j.get("status", ""),
                        "progress": j.get("progress", 0),
                        "created_at": j.get("created_at", 0),
                        "message": j.get("message", ""),
                    })
                    seen_ids.add(j["id"])
                for job_id in stale_ids:
                    JOBS.pop(job_id, None)
            # Scan data/ to restore completed jobs after restart.
            if DATA_DIR.is_dir():
                for d in sorted(DATA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                    if d.is_dir() and d.name not in seen_ids:
                        try:
                            clean_job_id(d.name)
                        except ValueError:
                            continue
                        result_file = d / "result.json"
                        if result_file.is_file():
                            try:
                                loaded = read_json_file(result_file, encoding="utf-8")
                                r = loaded if isinstance(loaded, dict) else {}
                                items.append({
                                    "id": d.name,
                                    "audio_name": r.get("audio_name", d.name),
                                    "display_name": r.get("display_name", ""),
                                    "note": r.get("note", ""),
                                    "status": "done",
                                    "progress": 100,
                                    "created_at": r.get("created_at", d.stat().st_mtime),
                                    "message": "",
                                })
                            except Exception:
                                pass
            items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            self.send_json(items)
            return
        match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})(?:/(result|audio|download\.md|pause|resume))?$", self.path)
        if match:
            job_id, action = match.groups()
            job_dir = job_dir_for(job_id)
            job = get_job(job_id)
            if not job:
                # Try restoring a completed job from data/.
                result_file = job_dir / "result.json"
                if result_file.is_file():
                    try:
                        loaded = read_json_file(result_file, encoding="utf-8")
                        r = loaded if isinstance(loaded, dict) else {}
                        job = {
                            "id": job_id,
                            "status": "done",
                            "progress": 100,
                            "message": "完成",
                            "audio_name": r.get("audio_name", job_id),
                            "result_path": str(result_file),
                            "result": r,
                        }
                        # Cache it for later requests.
                        with LOCK:
                            JOBS[job_id] = job
                    except Exception:
                        pass
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            if action == "audio":
                audio_path = checked_data_path(job_dir / "audio")
                if not audio_path.is_file():
                    self.send_json({"error": "audio not found"}, 404)
                    return
                self.send_file(audio_path, "application/octet-stream")
                return
            if action == "result":
                result, _ = self.load_job_result(job_id, job)
                self.send_json(result or {"error": "result not ready"})
                return
            if action == "download.md":
                result, _ = self.load_job_result(job_id, job)
                if not result:
                    self.send_json({"error": "result not ready"}, 409)
                    return
                body = markdown_result(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=\"recording.md\"")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_json(
                {
                    "id": job_id,
                    "status": job.get("status"),
                    "progress": job.get("progress", 0),
                    "message": job.get("message", ""),
                    "error": job.get("error"),
                    "waiting_until": job.get("waiting_until"),
                }
            )
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/api/rubric":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                rubric = []
                for item in payload.get("rubric") or []:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    detail = str(item.get("detail") or "").strip()
                    if title or detail:
                        rubric.append({"title": title[:80], "detail": detail[:1000], "enabled": item.get("enabled") is not False})
                RUBRIC_PATH.write_text(json.dumps(rubric, ensure_ascii=False, indent=2), encoding="utf-8")
                self.send_json({"ok": True})
            except Exception as exc:
                write_exception(f"save rubric failed: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return

        review_match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})/review$", self.path)
        if review_match:
            try:
                job_id = clean_job_id(review_match.group(1))
                job = get_job(job_id)
                result, result_path = self.load_job_result(job_id, job)
                if not result:
                    self.send_json({"error": "result not ready"}, 409)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                keys = clean_keys(str(payload.get("api_keys") or ""))
                if not keys:
                    self.send_json({"error": "请输入至少一个 Groq API Key"}, 400)
                    return
                chat_model = str(payload.get("chat_model") or result.get("chat_model") or DEFAULT_CHAT_MODEL).strip() or DEFAULT_CHAT_MODEL
                rubric = parse_rubric_json(json.dumps(payload.get("rubric") or [], ensure_ascii=False))
                review, structured = evaluate_recording(job_id, KeyPool(keys, job_id), result.get("paragraphs", []), chat_model, result.get("script", ""), rubric)
                result["review"] = review
                result["structured_review"] = structured
                result["chat_model"] = chat_model
                write_json_file(result_path, result)
                if job:
                    update_job(job_id, result=result, result_path=str(result_path))
                self.send_json({"ok": True, "result": result})
            except Exception as exc:
                write_exception(f"generate review failed: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return

        meta_match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})/meta$", self.path)
        if meta_match:
            try:
                job_id = clean_job_id(meta_match.group(1))
                job = get_job(job_id)
                result, result_path = self.load_job_result(job_id, job)
                if not result:
                    self.send_json({"error": "result not ready"}, 409)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                result["display_name"] = str(payload.get("display_name") or "")[:200]
                result["note"] = str(payload.get("note") or "")[:1000]
                write_json_file(result_path, result)
                if job:
                    update_job(job_id, result=result, result_path=str(result_path), display_name=result["display_name"], note=result["note"])
                self.send_json({"ok": True, "display_name": result["display_name"], "note": result["note"]})
            except Exception as exc:
                write_exception(f"save meta failed: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return

        annot_match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})/annotations$", self.path)
        if annot_match:
            try:
                job_id = clean_job_id(annot_match.group(1))
                job = get_job(job_id)
                result, result_path = self.load_job_result(job_id, job)
                if not result:
                    self.send_json({"error": "result not ready"}, 409)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                para_id = str(payload.get("para_id") or "")
                annotation = payload.get("annotation") or {}
                if not para_id:
                    self.send_json({"error": "para_id required"}, 400)
                    return
                result.setdefault("annotations", {})[para_id] = {
                    "color": str(annotation.get("color") or ""),
                    "note": str(annotation.get("note") or ""),
                }
                write_json_file(result_path, result)
                if job:
                    update_job(job_id, result=result, result_path=str(result_path))
                self.send_json({"ok": True})
            except Exception as exc:
                write_exception(f"save annotation failed: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return

        para_match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})/paragraphs$", self.path)
        if para_match:
            try:
                job_id = clean_job_id(para_match.group(1))
                job = get_job(job_id)
                result, result_path = self.load_job_result(job_id, job)
                if not result:
                    self.send_json({"error": "result not ready"}, 409)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                para_id = str(payload.get("para_id") or "")
                patch = payload.get("patch") or {}
                paragraph = patch_paragraph(result, para_id, patch)
                if not paragraph:
                    self.send_json({"error": "paragraph not found"}, 404)
                    return
                write_json_file(result_path, result)
                if job:
                    update_job(job_id, result=result, result_path=str(result_path))
                self.send_json({"ok": True, "paragraph": paragraph})
            except Exception as exc:
                write_exception(f"patch paragraph failed: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return

        # pause / resume
        pause_match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})/(pause|resume)$", self.path)
        if pause_match:
            job_id, action = pause_match.groups()
            job_id = clean_job_id(job_id)
            job = get_job(job_id)
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            if action == "pause":
                update_job(job_id, paused=True, status="paused", message="已暂停")
                write_log(f"[do_POST] {job_id} paused")
                self.send_json({"status": "paused"})
            else:  # resume
                update_job(job_id, paused=False, status="running", message="继续中...")
                write_log(f"[do_POST] {job_id} resumed")
                self.send_json({"status": "running"})
            return

        # create job
        if self.path != "/api/jobs":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            form = parse_multipart_form(self.headers, raw_body)
            audio = form.get("audio")
            if audio is None or not audio.filename:
                self.send_json({"error": "请选择音频文件"}, 400)
                return
            keys = clean_keys(form.get("api_keys", FormField()).value)
            if not keys:
                self.send_json({"error": "请输入至少一个 Groq API Key"}, 400)
                return
            whisper_model = form.get("whisper_model", FormField("whisper-large-v3-turbo")).value
            if whisper_model not in WHISPER_MODELS:
                self.send_json({"error": "不支持的 Whisper 模型"}, 400)
                return
            chat_model = form.get("chat_model", FormField(DEFAULT_CHAT_MODEL)).value.strip() or DEFAULT_CHAT_MODEL
            rubric = parse_rubric_json(form.get("rubric_json", FormField()).value)

            # Parse time range in seconds.
            def parse_range(val: str) -> float | None:
                try:
                    v = float(val.strip())
                    return v if v >= 0 else None
                except (ValueError, TypeError, AttributeError):
                    return None
            range_start = parse_range(form.get("range_start", FormField("0")).value)
            range_end = parse_range(form.get("range_end", FormField("")).value)

            write_log(f"[do_POST] parsed {len(keys)} key(s)")
            msg = f"[do_POST] whisper={whisper_model}, chat={chat_model}"
            if range_start is not None or range_end is not None:
                msg += f", range=[{range_start or 0}..{range_end or 'end'}]s"
            write_log(msg)

            job_id = clean_job_id(uuid.uuid4().hex[:12])
            job_dir = job_dir_for(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            audio_name = safe_name(audio.filename)
            audio_path = checked_data_path(job_dir / "audio")
            with audio_path.open("wb") as f:
                shutil.copyfileobj(audio.file, f)

            job = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "message": "已加入队列",
                "created_at": now(),
                "job_dir": str(job_dir),
                "audio_path": str(audio_path),
                "audio_name": audio_name,
                "keys": keys,
                "whisper_model": whisper_model,
                "chat_model": chat_model,
                "script": form.get("script", FormField()).value,
                "rubric": rubric,
                "make_review": form.get("make_review", FormField()).value == "on",
                "language": form.get("language", FormField("fr")).value.strip() or "fr",
                "range_start": range_start,  # secondes ou None
                "range_end": range_end,
                "paused": False,
            }
            with LOCK:
                JOBS[job_id] = job
            threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
            self.send_json({"id": job_id})
        except Exception as exc:
            write_exception(f"create job failed: {exc}")
            self.send_json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        if self.path == "/api/jobs":
            with LOCK:
                JOBS.clear()
            if DATA_DIR.is_dir():
                remove_data_tree(DATA_DIR)
            self.send_json({"ok": True})
            return

        review_match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})/review$", self.path)
        if review_match:
            try:
                job_id = clean_job_id(review_match.group(1))
                job = get_job(job_id)
                result, result_path = self.load_job_result(job_id, job)
                if not result:
                    self.send_json({"error": "result not ready"}, 409)
                    return
                result["review"] = ""
                result["structured_review"] = {}
                write_json_file(result_path, result)
                if job:
                    update_job(job_id, result=result, result_path=str(result_path))
                self.send_json({"ok": True})
            except Exception as exc:
                write_exception(f"delete review failed: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return

        match = re.match(r"^/api/jobs/([0-9a-fA-F]{12})$", self.path)
        if not match:
            self.send_json({"error": "not found"}, 404)
            return
        job_id = clean_job_id(match.group(1))
        with LOCK:
            JOBS.pop(job_id, None)
        job_dir = job_dir_for(job_id)
        remove_data_tree(job_dir)
        sys.stderr.write(f"[do_DELETE] {job_id} deleted\n")
        self.send_json({"ok": True})

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            path = checked_read_path(path)
            content_type = safe_header_value(content_type)
            size = path.stat().st_size
            start, end = 0, size - 1
            status = 200
            range_header = self.headers.get("Range", "")
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = int(right) if right else end
                elif right:
                    start = max(0, size - int(right))
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except ConnectionError:
            write_log(f"client disconnected while sending {safe_name(path.name)}")

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        write_log(fmt % args)


def self_test() -> None:
    assert format_ts(83) == "00:01:23"
    assert clean_job_id("ABCDEF123456") == "abcdef123456"
    try:
        clean_job_id("../bad")
        raise AssertionError("bad job id accepted")
    except ValueError:
        pass
    assert job_dir_for("abcdef123456").name == "abcdef123456"
    try:
        inside_dir(DATA_DIR, DATA_DIR / ".." / "server.py")
        raise AssertionError("escaped path accepted")
    except ValueError:
        pass
    assert safe_header_value("x\r\nInjected: y", "fallback") == "fallback"
    assert parse_rubric_json('["表达清晰"]')[0]["title"] == "表达清晰"
    assert parse_rubric_json('[{"title":"表达清晰","detail":"是否清楚"}]')[0]["detail"] == "是否清楚"
    assert parse_rubric_json('["None"]') == []
    edited = {"paragraphs": [{"id": 1, "speaker": "发言人", "fr": "Bonjour", "zh": "你好"}]}
    assert patch_paragraph(edited, "1", {"speaker": "听众", "fr": "Salut"})["speaker"] == "听众"
    assert edited["paragraphs"][0]["fr"] == "Salut"
    assert rows_missing_zh([{"fr": "Bonjour", "zh": ""}, {"fr": "Merci", "zh": "谢谢"}]) == [{"fr": "Bonjour", "zh": ""}]
    paras = group_segments(
        [
            {"start": 0, "end": 1, "text": "Bonjour."},
            {"start": 1.5, "end": 3, "text": "Nous lisons Jean."},
            {"start": 8, "end": 9, "text": "Merci."},
        ]
    )
    assert len(paras) == 3
    assert extract_json_object('```json\n{"ok": true}\n```')["ok"] is True
    assert not is_review_object({"统计": {}, "实际录音转录": "x"})
    assert is_review_object({"overall": "ok"})
    rendered = markdown_result({
        "audio_name": "demo.mp3",
        "paragraphs": [{"id": 1, "timestamp": "00:00:01", "speaker": "发言人", "fr": "Bonjour", "zh": "你好"}],
        "annotations": {"1": {"note": "test note"}},
    })
    assert "test note" in rendered
    assert "下次练习目标" in review_item_markdown({"title": "表达", "practice": "每段后停顿两秒"})
    assert "主要优点" in structured_review_markdown({"strengths": [{"time": "00:00:01", "title": "清晰", "detail": "表达清楚"}]})
    print("self-test ok")


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", "8765"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    def stop_server(signum: int, _frame: object) -> None:
        write_log(f"stopping server by signal {signum}")
        raise KeyboardInterrupt

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, stop_server)

    url = f"http://127.0.0.1:{port}"
    print(f"打开浏览器访问：{url}")
    print("按 Ctrl+C 停止服务。")
    try:
        webbrowser.open(url)
    except Exception as exc:
        write_log(f"open browser failed: {exc}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        write_log("server stopped")
    finally:
        httpd.server_close()
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
