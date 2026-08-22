"""Whisper JSON conversion and caption pagination compatible with the old app."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


MAX_CHARACTERS_PER_PAGE = 30
MAX_PAGE_DURATION_MS = 6000
LONG_PAUSE_MS = 700
MARKDOWN_PARAGRAPH_PAUSE_MS = 1000
MARKDOWN_MIN_PARAGRAPH_CHARACTERS = 120


@dataclass(frozen=True, slots=True)
class Caption:
    text: str
    startMs: int
    endMs: int
    timestampMs: int | None
    confidence: float


def whisper_json_to_captions(raw: dict[str, Any]) -> list[Caption]:
    transcription = raw.get("transcription")
    if not isinstance(transcription, list):
        raise ValueError("Whisper JSON does not contain a transcription list")

    captions: list[Caption] = []
    for item in transcription:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        offsets = item.get("offsets")
        tokens = item.get("tokens")
        if not isinstance(text, str) or not text:
            continue
        if not isinstance(offsets, dict):
            continue
        start_ms = offsets.get("from")
        end_ms = offsets.get("to")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int):
            continue

        first_token = tokens[0] if isinstance(tokens, list) and tokens else {}
        probability = first_token.get("p") if isinstance(first_token, dict) else None
        t_dtw = first_token.get("t_dtw") if isinstance(first_token, dict) else None
        confidence = float(probability) if isinstance(probability, (int, float)) else 0.0
        timestamp_ms = (
            int(t_dtw * 10)
            if isinstance(t_dtw, (int, float)) and t_dtw != -1
            else None
        )
        captions.append(
            Caption(
                text=text.lstrip() if not captions else text,
                startMs=start_ms,
                endMs=end_ms,
                timestampMs=timestamp_ms,
                confidence=confidence,
            )
        )
    if not captions:
        raise ValueError("Whisper produced no usable caption segments")
    return captions


def captions_as_dicts(captions: list[Caption]) -> list[dict[str, object]]:
    return [asdict(caption) for caption in captions]


def _character_count(captions: list[Caption]) -> int:
    return sum(len(re.sub(r"\s", "", caption.text)) for caption in captions)


def _semantic_units(captions: list[Caption]) -> list[list[Caption]]:
    units: list[list[Caption]] = []
    for caption in captions:
        previous_unit = units[-1] if units else None
        previous_token = previous_unit[-1] if previous_unit else None
        continuation = (
            caption.text.startswith(" ")
            and previous_token is not None
            and caption.startMs - previous_token.endMs <= LONG_PAUSE_MS
        )
        if continuation:
            previous_unit.append(caption)
        else:
            units.append([caption])
    return units


def build_caption_pages(captions: list[Caption]) -> list[list[Caption]]:
    pages: list[list[Caption]] = []
    tokens: list[Caption] = []

    def flush() -> None:
        nonlocal tokens
        if tokens:
            pages.append(tokens)
            tokens = []

    for unit in _semantic_units(captions):
        first_token = unit[0]
        last_token = unit[-1]
        previous_token = tokens[-1] if tokens else None
        next_tokens = [*tokens, *unit]
        has_long_pause = (
            previous_token is not None
            and first_token.startMs - previous_token.endMs > LONG_PAUSE_MS
        )
        too_long = bool(tokens) and _character_count(next_tokens) > MAX_CHARACTERS_PER_PAGE
        too_slow = bool(tokens) and last_token.endMs - tokens[0].startMs > MAX_PAGE_DURATION_MS
        if has_long_pause or too_long or too_slow:
            flush()
        tokens.extend(unit)
        if re.search(r"[。！？!?；;]$", last_token.text.strip()):
            flush()
    flush()
    return pages


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def serialize_srt(captions: list[Caption]) -> str:
    blocks: list[str] = []
    for index, page in enumerate(build_caption_pages(captions), start=1):
        text = "".join(token.text for token in page).strip()
        blocks.append(
            f"{index}\n{_srt_timestamp(page[0].startMs)} --> "
            f"{_srt_timestamp(page[-1].endMs)}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def transcript_text(captions: list[Caption]) -> str:
    return "".join(caption.text for caption in captions).strip()


def transcript_paragraphs(
    captions: list[Caption],
    *,
    min_characters: int = MARKDOWN_MIN_PARAGRAPH_CHARACTERS,
    pause_ms: int = MARKDOWN_PARAGRAPH_PAUSE_MS,
) -> list[str]:
    """Group captions into readable paragraphs for Markdown output.

    whisper.cpp does not reliably emit sentence-ending punctuation for Chinese,
    so a speech pause also starts a new paragraph once the current one is long
    enough to stand on its own.
    """
    paragraphs: list[str] = []
    current: list[Caption] = []
    previous_end: int | None = None

    for caption in captions:
        pause = caption.startMs - previous_end if previous_end is not None else 0
        text_so_far = "".join(token.text for token in current)
        ends_sentence = bool(re.search(r"[。！？!?]\s*$", text_so_far))
        if (
            current
            and _character_count(current) >= min_characters
            and (pause >= pause_ms or ends_sentence)
        ):
            paragraphs.append(text_so_far.strip())
            current = []
        current.append(caption)
        previous_end = caption.endMs

    if current:
        paragraphs.append("".join(token.text for token in current).strip())
    return [paragraph for paragraph in paragraphs if paragraph]


def _duration_label(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    return f"{hours:02}:{minutes:02}:{remainder // 1000:02}"


def serialize_markdown(
    captions: list[Caption],
    *,
    title: str,
    audio_filename: str,
    model: str | None = None,
    language: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    model_label = model or "unknown"
    if language:
        model_label = f"{model_label}（{language}）"
    header = "\n".join(
        [
            f"# {title}",
            "",
            f"- **音频文件**：`{audio_filename}`",
            f"- **时长**：{_duration_label(captions[-1].endMs if captions else 0)}",
            f"- **识别模型**：{model_label}",
            f"- **生成时间**："
            f"{(generated_at or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')}",
        ]
    )
    body = "\n\n".join(transcript_paragraphs(captions))
    return f"{header}\n\n---\n\n{body}\n"

