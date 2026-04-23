"""
Telegram_BotManager.py

Gestor de envio a Telegram para topics (forum supergroups).

Incluye:
- Envio de uno o varios mensajes a un topic concreto.
- Formateo HTML seguro para resultados del procesamiento IA.
- Empaquetado de secciones para respetar el limite practico de longitud.
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import requests

MAX_TELEGRAM_MESSAGE_LENGTH = 4096
SAFE_TELEGRAM_MESSAGE_LENGTH = 3800
_DIRECTION_EMOJI = {
    "ALCISTA": "📈",
    "BAJISTA": "📉",
    "NEUTRAL": "⚖️",
    "MIXTO": "↔️",
}


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: int | str
    topic_thread_id: Optional[int] = None
    parse_mode: Optional[str] = "HTML"
    link_preview_disabled: bool = True
    disable_notification: bool = False
    protect_content: bool = False
    timeout_seconds: int = 15


def truncate_text(value: str, max_chars: int) -> str:
    value = (value or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def html_escape(value: Any, max_chars: Optional[int] = None) -> str:
    text = "" if value is None else str(value)
    if max_chars is not None:
        text = truncate_text(text, max_chars)
    return html.escape(text)


def direction_emoji(direction: str) -> str:
    return _DIRECTION_EMOJI.get((direction or "").upper(), "⚖️")


def sanitize_hashtag(tag: str) -> str:
    cleaned = re.sub(r"[^\w]+", "", str(tag or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("_")
    return cleaned[:30]


def safe_int(value: Any, default: int = 0, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def join_model_names(value: Any, max_chars: int = 180) -> str:
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        text = ", ".join(parts)
    else:
        text = str(value or "").strip()
    return truncate_text(text, max_chars)


def build_news_card(item: Dict[str, Any]) -> str:
    importance = safe_int(item.get("importance_level", 3), default=3, minimum=1, maximum=5)
    headline = html_escape(item.get("translated_headline", "SIN TITULAR"), 180)
    summary = html_escape(item.get("summary_es", "Sin resumen disponible."), 650)
    timestamp = html_escape(item.get("timestamp_text", ""), 40)
    market_bias = str(item.get("market_bias", "NEUTRAL") or "NEUTRAL").upper()
    impact_reason = html_escape(item.get("impact_reason", "Sin comentario adicional."), 260)

    tag_tokens = []
    for raw_tag in item.get("tags", [])[:8]:
        clean = sanitize_hashtag(str(raw_tag))
        if clean:
            tag_tokens.append(f"#{clean}")
    tag_line = " ".join(tag_tokens)

    parts = [
        "━━━━━━━━━━━━",
        f"🚨 <b>IMPORTANCIA {importance}/5</b>",
        f"📢 <b>{headline}</b>",
        f"📝 {summary}",
    ]
    if tag_line:
        parts.append(f"🏷 <code>{html_escape(tag_line, 220)}</code>")
    if timestamp:
        parts.append(f"🕒 <code>{timestamp}</code>")
    parts.append(f"📊 <b>Sesgo:</b> {direction_emoji(market_bias)} <b>{html_escape(market_bias, 12)}</b>")
    parts.append(f"💬 <i>{impact_reason}</i>")
    return "\n".join(parts)


def build_index_lines(label: str, payload: Dict[str, Any]) -> str:
    payload = payload or {}
    direction = str(payload.get("direction", "NEUTRAL") or "NEUTRAL").upper()
    confidence = safe_int(payload.get("confidence", 50), default=50, minimum=0, maximum=100)
    reason = html_escape(payload.get("reason", "Sin comentario disponible."), 260)
    emoji = direction_emoji(direction)
    return (
        f"<b>{html_escape(label)}</b>: {emoji} <b>{html_escape(direction, 12)}</b> "
        f"<code>{confidence}%</code>\n{reason}"
    )


def build_market_overview_section(processed_batch: Dict[str, Any]) -> str:
    market_overview = html_escape(
        processed_batch.get("market_overview", "Sin resumen agregado disponible."),
        1400,
    )
    return "\n".join(
        [
            "🌍 <b>VISIÓN GENERAL DEL MERCADO</b>",
            market_overview,
        ]
    )


def build_market_impact_section(processed_batch: Dict[str, Any]) -> str:
    impact = processed_batch.get("global_market_impact", {}) or {}
    lines = ["📈 <b>IMPACTO AGREGADO DE MERCADO</b>"]
    lines.append("")
    lines.append(build_index_lines("IBEX35", impact.get("ibex35", {})))
    lines.append("")
    lines.append(build_index_lines("EUROSTOXX50", impact.get("eurostoxx50", {})))
    lines.append("")
    lines.append(build_index_lines("S&P500", impact.get("sp500", {})))
    lines.append("")
    lines.append(build_index_lines("NIKKEI225", impact.get("nikkei225", {})))

    assets = impact.get("impacted_assets", []) or []
    if assets:
        lines.append("")
        lines.append("🎯 <b>ACTIVOS DESTACADOS</b>")
        for asset in assets[:8]:
            direction = str(asset.get("direction", "NEUTRAL") or "NEUTRAL").upper()
            name = html_escape(asset.get("name", "Activo"), 80)
            symbol = html_escape(asset.get("symbol", "N/A"), 30)
            asset_type = html_escape(asset.get("asset_type", "UNKNOWN"), 20)
            reason = html_escape(asset.get("reason", "Sin motivo especificado."), 220)
            lines.append(
                f"• <b>{name}</b> (<code>{symbol}</code>, {asset_type}) - "
                f"{direction_emoji(direction)} <b>{html_escape(direction, 12)}</b>\n"
                f"  {reason}"
            )

    return "\n".join(lines)


def build_final_commentary_section(processed_batch: Dict[str, Any]) -> str:
    impact = processed_batch.get("global_market_impact", {}) or {}
    final_commentary = html_escape(
        impact.get("final_commentary", "Sin comentario final disponible."),
        1800,
    )
    return "\n".join(
        [
            "🧭 <b>COMENTARIO FINAL</b>",
            final_commentary,
        ]
    )


def build_header_section(processed_batch: Dict[str, Any], run_label: Optional[str] = None) -> str:
    meta = processed_batch.get("meta", {}) or {}
    batch_title = html_escape(processed_batch.get("batch_title", "Resumen de noticias"), 120)
    batch_size = len(processed_batch.get("items", []) or [])
    generated_at = html_escape(meta.get("generated_at_iso", ""), 40)

    batch_models = join_model_names(
        meta.get("batch_structuring_models") or meta.get("batch_structuring_model") or "",
        max_chars=180,
    )
    commentary_model = join_model_names(meta.get("market_commentary_model") or "", max_chars=80)

    parts = [
        f"📰 <b>{batch_title}</b>",
        f"<b>Noticias nuevas:</b> <code>{batch_size}</code>",
    ]
    if run_label:
        parts.append(f"<b>Lote:</b> <code>{html_escape(run_label, 50)}</code>")
    if generated_at:
        parts.append(f"<b>Generado:</b> <code>{generated_at}</code>")

    if batch_models and commentary_model:
        if batch_models == commentary_model:
            parts.append(f"<b>Modelo:</b> <code>{html_escape(batch_models, 180)}</code>")
        else:
            parts.append(
                f"<b>Modelos:</b> <code>{html_escape(batch_models, 180)}</code> + "
                f"<code>{html_escape(commentary_model, 80)}</code>"
            )
    elif batch_models:
        parts.append(f"<b>Modelo:</b> <code>{html_escape(batch_models, 180)}</code>")
    elif commentary_model:
        parts.append(f"<b>Modelo comentario:</b> <code>{html_escape(commentary_model, 80)}</code>")

    return "\n".join(parts)


def build_telegram_sections(processed_batch: Dict[str, Any], run_label: Optional[str] = None) -> List[str]:
    sections: List[str] = [build_header_section(processed_batch, run_label=run_label)]

    # Recupera explícitamente el overview, que ahora viene fuera del header lógico del lote.
    sections.append(build_market_overview_section(processed_batch))

    items = processed_batch.get("items", []) or []
    if items:
        sections.append("🗞 <b>NOTICIAS DESTACADAS</b>")
        for item in items:
            sections.append(build_news_card(item))

    # Recupera explícitamente impacto agregado y comentario final.
    sections.append(build_market_impact_section(processed_batch))
    sections.append(build_final_commentary_section(processed_batch))
    return sections


def _force_split_section(section: str, max_length: int) -> List[str]:
    if len(section) <= max_length:
        return [section]

    lines = section.split("\n")
    chunks: List[str] = []
    current = ""

    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = line
        else:
            raw = line
            while len(raw) > max_length:
                chunks.append(raw[:max_length])
                raw = raw[max_length:]
            current = raw

    if current:
        chunks.append(current)
    return chunks


def pack_sections_into_messages(sections: Sequence[str], max_length: int = SAFE_TELEGRAM_MESSAGE_LENGTH) -> List[str]:
    messages: List[str] = []
    current = ""

    for section in sections:
        if not section or not str(section).strip():
            continue
        for piece in _force_split_section(section, max_length=max_length):
            candidate = piece if not current else f"{current}\n\n{piece}"
            if len(candidate) <= max_length:
                current = candidate
                continue
            if current:
                messages.append(current)
            current = piece

    if current:
        messages.append(current)

    return messages


def build_telegram_messages(processed_batch: Dict[str, Any], run_label: Optional[str] = None) -> List[str]:
    sections = build_telegram_sections(processed_batch, run_label=run_label)
    return pack_sections_into_messages(sections)


def send_telegram_message_to_topic(
    bot_token: str,
    chat_id: int | str,
    thread_id: Optional[int],
    text: str,
    parse_mode: Optional[str] = "HTML",
    link_preview_disabled: bool = True,
    disable_notification: bool = False,
    protect_content: bool = False,
    timeout_seconds: int = 15,
) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }

    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if link_preview_disabled:
        payload["link_preview_options"] = {"is_disabled": True}
    if disable_notification:
        payload["disable_notification"] = True
    if protect_content:
        payload["protect_content"] = True

    response = requests.post(url, json=payload, timeout=timeout_seconds)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"Telegram HTTP error: {response.status_code} - {response.text}") from exc
    data = response.json()
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram API returned ok=false: {data}")
    return data


def send_messages_to_topic(messages: Sequence[str], config: TelegramConfig) -> List[Dict[str, Any]]:
    if not config.bot_token:
        raise ValueError("Telegram bot token is missing.")
    if not messages:
        return []

    results: List[Dict[str, Any]] = []
    for text in messages:
        if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
            raise ValueError(
                f"A message is too long for Telegram ({len(text)} > {MAX_TELEGRAM_MESSAGE_LENGTH})."
            )
        results.append(
            send_telegram_message_to_topic(
                bot_token=config.bot_token,
                chat_id=config.chat_id,
                thread_id=config.topic_thread_id,
                text=text,
                parse_mode=config.parse_mode,
                link_preview_disabled=config.link_preview_disabled,
                disable_notification=config.disable_notification,
                protect_content=config.protect_content,
                timeout_seconds=config.timeout_seconds,
            )
        )
    return results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a message to a Telegram topic.")
    parser.add_argument("--bot-token", required=True, help="Telegram bot token.")
    parser.add_argument("--chat-id", required=True, help="Target supergroup chat id.")
    parser.add_argument("--thread-id", type=int, default=None, help="Topic thread id.")
    parser.add_argument("--text", required=True, help="Text to send.")
    parser.add_argument("--parse-mode", default="HTML", help="Telegram parse mode.")
    return parser.parse_args(argv)


def cli_main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        send_telegram_message_to_topic(
            bot_token=args.bot_token,
            chat_id=args.chat_id,
            thread_id=args.thread_id,
            text=args.text,
            parse_mode=args.parse_mode,
        )
        print("[ok] message sent")
        return 0
    except KeyboardInterrupt:
        print("[abort] interrupted by user")
        return 130
    except Exception as exc:
        print(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())