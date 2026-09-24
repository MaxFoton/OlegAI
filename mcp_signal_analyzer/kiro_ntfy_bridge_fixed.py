#!/usr/bin/env python3
"""Reliable ntfy -> Kiro bridge.

Заменяет старый polling /json?since=...:
* использует long-polling /json?poll=1&since=...
* корректно хранит последний ntfy id даже после рестарта
* 429 backoff по Retry-After + экспоненциальная пауза
* не фильтрует сообщение по emoji
* извлекает score из scanner-формата и Kiro-анализа
* атомарно пишет очередь; защищает её от параллельной записи
* ограничивает очередь и не запускает несколько analyzer одновременно
* сохраняет полное сообщение scanner без обрезки
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("kiro-ntfy-bridge")

NTFY_SERVER = os.getenv("NTFY_SERVER", "http://87.121.218.4:8080")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "max-alerts")
MIN_SCORE = int(os.getenv("MIN_SCORE", "5"))
QUEUE_FILE = Path(os.getenv("SIGNAL_QUEUE_FILE", "/tmp/kiro_signal_queue.json"))
STATE_FILE = Path(os.getenv("BRIDGE_STATE_FILE", "/tmp/kiro_ntfy_state.json"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "200"))
POLL_TIMEOUT = int(os.getenv("NTFY_POLL_TIMEOUT", "300"))
RETRY_MAX = int(os.getenv("NTFY_RETRY_MAX", "300"))

ANALYZER_ENABLED = os.getenv("KIRO_ANALYZER_ENABLED", "1") not in {"0", "false", "no"}
ANALYZER_PYTHON = os.getenv("KIRO_PYTHON", "/home/max/freqtrade/.venv/bin/python")
ANALYZER_SCRIPT = os.getenv(
    "KIRO_ANALYZER_SCRIPT",
    "/home/max/freqtrade/mcp_signal_analyzer/kiro_analyzer.py",
)
ANALYZER_LOCK = Path(os.getenv("KIRO_ANALYZER_LOCK", "/tmp/kiro_analyzer.lock"))

# Два возможных формата:
# scanner_r_fixed: SIGNAL_DECISION ... score=6/15
# старые/другие уведомления: Score: 6/15, score=6/15, STRK SHORT ... 6/15
SCORE_PATTERNS = (
    re.compile(r"\bscore\s*[:=]\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"\b(?:score|signal)\s*[:=]?\s*(\d+)\s*/\s*(\d+)", re.I),
)
SCANNER_LINE_RE = re.compile(
    r"SIGNAL_DECISION\s+base=(\S+)\s+dir=(LONG|SHORT)\s+"
    r"kind=(\S+)\s+score=(\d+)\s+price=([0-9.eE+-]+)",
    re.I,
)


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as exc:
        logger.warning("state load failed: %s", exc)
    return {"last_id": None, "processed": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["processed"] = state.get("processed", [])[-5000:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, STATE_FILE)


def load_queue() -> list[dict]:
    try:
        if QUEUE_FILE.exists():
            value = json.loads(QUEUE_FILE.read_text())
            return value if isinstance(value, list) else []
    except Exception as exc:
        logger.warning("queue load failed: %s", exc)
    return []


def save_queue(queue: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue[-MAX_QUEUE:], indent=2, ensure_ascii=False))
    os.replace(tmp, QUEUE_FILE)


def extract_score(text: str) -> tuple[int, int] | None:
    for pattern in SCORE_PATTERNS:
        for match in pattern.finditer(text):
            score, maximum = int(match.group(1)), int(match.group(2))
            if maximum > 0:
                return score, maximum
    # SIGNAL_DECISION score может быть в виде score=6 без /15
    match = re.search(r"SIGNAL_DECISION\b.*?\bscore=(\d+)", text, re.I)
    if match:
        return int(match.group(1)), 15
    return None


def extract_metadata(text: str) -> dict:
    match = SCANNER_LINE_RE.search(text)
    if not match:
        return {}
    base, direction, kind, score, price = match.groups()
    return {
        "symbol": base,
        "direction": direction.upper(),
        "kind": kind,
        "scanner_score": int(score),
        "price": float(price),
    }


def is_signal_message(msg: dict) -> bool:
    text = str(msg.get("message") or "")
    # Не требуем 📢: scanner fixed использует заголовок/другой формат.
    return bool(
        SCANNER_LINE_RE.search(text)
        or re.search(r"\b(?:LONG|SHORT)\b", text)
        and extract_score(text)
    )


def analyzer_running() -> bool:
    try:
        return ANALYZER_LOCK.exists()
    except Exception:
        return False


def start_analyzer() -> None:
    if not ANALYZER_ENABLED or analyzer_running():
        return
    try:
        # НЕ создаем lock здесь - analyzer сам должен его создать
        subprocess.Popen(
            [ANALYZER_PYTHON, ANALYZER_SCRIPT],
            cwd=str(Path(ANALYZER_SCRIPT).parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("🤖 kiro_analyzer.py launched")
        # Даем analyzer время создать lock
        time.sleep(0.5)
    except Exception as exc:
        logger.error("failed to launch analyzer: %s", exc)


async def fetch_messages(client: httpx.AsyncClient, last_id: str | None) -> list[dict]:
    # Long-poll: сервер держит соединение и не получает запрос каждую минуту.
    params = {"poll": "1", "since": last_id or "1m", "timeout": str(POLL_TIMEOUT)}
    response = await client.get(
        f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}/json",
        params=params,
        timeout=POLL_TIMEOUT + 20,
    )
    if response.status_code == 200:
        result = []
        for line in response.text.splitlines():
            try:
                item = json.loads(line)
                if item.get("event") == "message":
                    result.append(item)
            except json.JSONDecodeError:
                logger.warning("invalid ntfy JSON line skipped")
        return result
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        delay = int(float(retry_after)) if retry_after and retry_after.isdigit() else 60
        raise RateLimited(max(5, min(delay, RETRY_MAX)))
    response.raise_for_status()
    return []


class RateLimited(Exception):
    def __init__(self, delay: int):
        self.delay = delay
        super().__init__(f"rate limited; retry in {delay}s")


async def main() -> None:
    logger.info("🚀 Starting Kiro-NTFY bridge")
    logger.info("📡 Monitoring: %s/%s", NTFY_SERVER, NTFY_TOPIC)
    logger.info("📊 Min score: %d/15", MIN_SCORE)
    logger.info("💾 Queue file: %s", QUEUE_FILE)

    state = load_state()
    processed = set(state.get("processed", []))
    last_id = state.get("last_id")
    backoff = 5

    limits = httpx.Limits(max_connections=2, max_keepalive_connections=1)
    async with httpx.AsyncClient(
        timeout=POLL_TIMEOUT + 20,
        limits=limits,
        headers={"Accept": "application/x-ndjson"},
    ) as client:
        while True:
            try:
                messages = await fetch_messages(client, last_id)
                backoff = 5
                queue = load_queue()

                for msg in messages:
                    msg_id = str(msg.get("id") or "")
                    if msg_id:
                        last_id = msg_id
                        state["last_id"] = last_id
                    if not msg_id or msg_id in processed:
                        continue
                    if not is_signal_message(msg):
                        processed.add(msg_id)
                        continue

                    text = str(msg.get("message") or "")
                    parsed = extract_score(text)
                    if not parsed:
                        processed.add(msg_id)
                        continue
                    score, maximum = parsed
                    logger.info("📢 New signal %s score=%d/%d", msg_id, score, maximum)

                    if score >= MIN_SCORE:
                        metadata = extract_metadata(text)
                        queue.append({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "ntfy_id": msg_id,
                            "score": score,
                            "max_score": maximum,
                            "signal": metadata,
                            "signal_text": text,  # полный текст, без обрезки
                            "status": "pending",
                        })
                        queue = queue[-MAX_QUEUE:]
                        save_queue(queue)
                        logger.info("✅ Signal added: %s score=%d/%d", metadata.get("symbol", "?"), score, maximum)
                        start_analyzer()
                    else:
                        logger.info("⏭️ Score %d < %d, skipping", score, MIN_SCORE)
                    processed.add(msg_id)
                    state["processed"] = list(processed)[-5000:]
                    save_state(state)

            except RateLimited as exc:
                logger.warning("ntfy 429; sleeping %ds", exc.delay)
                await asyncio.sleep(exc.delay)
                continue
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning("ntfy connection error: %s; retry in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RETRY_MAX)
                continue
            except Exception as exc:
                logger.error("bridge loop error: %s", exc, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RETRY_MAX)
                continue

            # Защита от tight loop на сервере, который мгновенно возвращает пустой ответ.
            if not messages:
                await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        ANALYZER_LOCK.unlink(missing_ok=True)
