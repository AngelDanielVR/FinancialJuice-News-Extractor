"""
main.py

Orquestador del proyecto:
1. Extrae noticias recientes desde FinancialJuice.
2. Filtra solo las noticias no procesadas anteriormente.
3. Procesa el lote completo con Gemini usando rutas, fallback y reparto de carga.
4. Formatea y envia el resultado a Telegram.
5. Repite el ciclo cada X segundos (por defecto, cada 2 horas).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from Includes.Telegram_BotManager import (
    TelegramConfig,
    build_telegram_messages,
    send_messages_to_topic,
)
from Includes.financialjuice_extractor import ExtractorConfig, extract_to_repository
from Includes.news_ai_proccesing import (
    AIProcessingConfig,
    filter_unprocessed_news,
    load_processing_state,
    mark_news_as_processed,
    process_news_batch,
    save_processed_batch,
    save_processing_state,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "Config" / "settings.json"
REPOSITORY_DIR = BASE_DIR / "Repository"

LOGGER = logging.getLogger("financialjuice_pipeline")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def load_json_config(config_path: str | Path) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a JSON object: {path}")
    return data


def resolve_relative_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((BASE_DIR / path).resolve())


def parse_int_or_keep(value: str) -> int | str:
    stripped = value.strip()
    if stripped.startswith("@"):
        return stripped
    try:
        return int(stripped)
    except ValueError:
        return stripped


def apply_env_overrides(settings: Dict[str, Any]) -> Dict[str, Any]:
    settings.setdefault("runtime", {})
    settings.setdefault("financialjuice", {})
    settings.setdefault("gemini", {})
    settings.setdefault("telegram", {})

    env_map = {
        ("financialjuice", "email"): os.getenv("FJ_EMAIL"),
        ("financialjuice", "password"): os.getenv("FJ_PASSWORD"),
        ("financialjuice", "user_data_dir"): os.getenv("FJ_USER_DATA_DIR"),
        ("gemini", "api_key"): os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        ("telegram", "bot_token"): os.getenv("TELEGRAM_BOT_TOKEN"),
        ("telegram", "chat_id"): os.getenv("TELEGRAM_CHAT_ID"),
        ("telegram", "topic_thread_id"): os.getenv("TELEGRAM_TOPIC_THREAD_ID"),
    }

    for (section, key), value in env_map.items():
        if value is None or value == "":
            continue
        settings[section][key] = value

    return settings


def normalize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    settings.setdefault("runtime", {})
    settings.setdefault("financialjuice", {})
    settings.setdefault("gemini", {})
    settings.setdefault("telegram", {})

    runtime = settings["runtime"]
    runtime.setdefault("poll_interval_seconds", 7200)
    runtime.setdefault("failure_retry_seconds", 300)
    runtime.setdefault("extract_hours_window", 6)
    runtime.setdefault("run_once", False)
    runtime.setdefault("max_processed_ids", 10000)

    fj = settings["financialjuice"]
    fj.setdefault("email", "")
    fj.setdefault("password", "")
    fj.setdefault("headed", False)
    fj.setdefault("manual_login", False)
    fj.setdefault("user_data_dir", "Config/browser_profile")
    fj.setdefault("wait_ms", 45000)
    fj.setdefault("max_scroll_rounds", 40)
    fj.setdefault("scroll_pause_ms", 1200)
    fj.setdefault("debug", False)
    fj["user_data_dir"] = resolve_relative_path(fj.get("user_data_dir"))

    gemini = settings["gemini"]
    gemini.setdefault("api_key", "")
    gemini.setdefault("separate_market_commentary", True)

    if not gemini.get("models"):
        legacy_model = str(gemini.get("model", "gemini-3.1-flash-lite-preview"))
        legacy_temperature = float(gemini.get("temperature", 0.2))
        legacy_max_output = int(gemini.get("max_output_tokens", 8192))
        gemini["models"] = {
            legacy_model: {
                "enabled": True,
                "api_key": gemini.get("api_key", ""),
                "temperature": legacy_temperature,
                "max_output_tokens": legacy_max_output,
                "daily_limit": None,
                "per_minute_limit": None,
            }
        }

    for model_name, spec in gemini["models"].items():
        spec.setdefault("enabled", True)
        spec.setdefault("api_key", gemini.get("api_key", ""))
        spec.setdefault("temperature", 0.2)
        spec.setdefault("max_output_tokens", 8192)
        spec.setdefault("daily_limit", None)
        spec.setdefault("per_minute_limit", None)
        gemini["models"][model_name] = spec

    if not gemini.get("routes"):
        model_names = list(gemini["models"].keys())
        gemini["routes"] = {
            "batch_structuring": {
                "models": model_names,
                "temperature": 0.15,
                "max_output_tokens": 8192,
            },
            "market_commentary": {
                "models": model_names,
                "temperature": 0.2,
                "max_output_tokens": 4096,
            },
        }

    for route_name, route in gemini["routes"].items():
        route.setdefault("models", list(gemini["models"].keys()))
        route.setdefault("temperature", 0.2)
        route.setdefault("max_output_tokens", 8192)
        gemini["routes"][route_name] = route

    telegram = settings["telegram"]
    telegram.setdefault("bot_token", "")
    telegram.setdefault("chat_id", "")
    telegram.setdefault("topic_thread_id", None)
    telegram.setdefault("parse_mode", "HTML")
    telegram.setdefault("link_preview_disabled", True)
    telegram.setdefault("disable_notification", False)
    telegram.setdefault("protect_content", False)
    telegram.setdefault("timeout_seconds", 15)

    chat_id = telegram.get("chat_id")
    if isinstance(chat_id, str) and chat_id.strip():
        telegram["chat_id"] = parse_int_or_keep(chat_id)

    thread_id = telegram.get("topic_thread_id")
    if thread_id in ("", 0, "0", None):
        telegram["topic_thread_id"] = None
    else:
        telegram["topic_thread_id"] = int(thread_id)

    return settings


def validate_settings(settings: Dict[str, Any]) -> None:
    missing = []

    enabled_models = []
    for model_name, spec in settings.get("gemini", {}).get("models", {}).items():
        if not spec.get("enabled", True):
            continue
        api_key = str(spec.get("api_key", "") or settings.get("gemini", {}).get("api_key", "")).strip()
        if api_key:
            enabled_models.append(model_name)

    if not enabled_models:
        missing.append("gemini.models (al menos un modelo habilitado con api_key)")
    if not settings.get("telegram", {}).get("bot_token"):
        missing.append("telegram.bot_token")
    if settings.get("telegram", {}).get("chat_id") in (None, ""):
        missing.append("telegram.chat_id")

    if missing:
        raise ValueError("Missing required configuration values: " + ", ".join(missing))


def build_run_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_processing_state_path() -> Path:
    return REPOSITORY_DIR / "state" / "processing_state.json"


def get_model_usage_state_path() -> Path:
    return REPOSITORY_DIR / "state" / "model_usage.json"


def write_delivery_report(run_label: str, message_count: int, processed_batch: Dict[str, Any]) -> Path:
    out_dir = REPOSITORY_DIR / "deliveries" / run_label
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "delivery.json"
    payload = {
        "run_label": run_label,
        "message_count": message_count,
        "models_used": processed_batch.get("meta", {}).get("models_used", []),
        "delivered_at_iso": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_pipeline_cycle(settings: Dict[str, Any]) -> bool:
    REPOSITORY_DIR.mkdir(parents=True, exist_ok=True)
    state_path = get_processing_state_path()
    state = load_processing_state(state_path)
    run_label = build_run_label()

    runtime = settings["runtime"]
    fj = settings["financialjuice"]
    gemini = settings["gemini"]
    telegram = settings["telegram"]

    LOGGER.info("Iniciando ciclo %s", run_label)

    extraction_result = extract_to_repository(
        ExtractorConfig(
            repository_dir=REPOSITORY_DIR,
            hours=int(runtime["extract_hours_window"]),
            headed=bool(fj["headed"]),
            manual_login=bool(fj["manual_login"]),
            user_data_dir=fj.get("user_data_dir"),
            wait_ms=int(fj["wait_ms"]),
            max_scroll_rounds=int(fj["max_scroll_rounds"]),
            scroll_pause_ms=int(fj["scroll_pause_ms"]),
            debug=bool(fj["debug"]),
            email=(fj.get("email") or None),
            password=(fj.get("password") or None),
            run_label=run_label,
        )
    )

    LOGGER.info("Extraccion completada: %s noticias exportadas", extraction_result.exported_items)

    new_items = filter_unprocessed_news(extraction_result.items, state)
    if not new_items:
        LOGGER.info("No hay noticias nuevas pendientes de procesar. Ciclo finalizado.")
        return True

    LOGGER.info("Noticias nuevas detectadas: %s", len(new_items))

    processed_batch = process_news_batch(
        new_items,
        AIProcessingConfig(
            api_key=str(gemini.get("api_key", "")),
            separate_market_commentary=bool(gemini.get("separate_market_commentary", True)),
            models=gemini.get("models"),
            routes=gemini.get("routes"),
            usage_state_path=str(get_model_usage_state_path()),
        ),
    )

    batch_file = save_processed_batch(REPOSITORY_DIR, processed_batch, run_label=run_label)
    LOGGER.info("Salida IA guardada en %s", batch_file)

    messages = build_telegram_messages(processed_batch, run_label=run_label)
    LOGGER.info("Mensajes de Telegram generados: %s", len(messages))

    send_messages_to_topic(
        messages,
        TelegramConfig(
            bot_token=str(telegram["bot_token"]),
            chat_id=telegram["chat_id"],
            topic_thread_id=telegram.get("topic_thread_id"),
            parse_mode=telegram.get("parse_mode"),
            link_preview_disabled=bool(telegram.get("link_preview_disabled", True)),
            disable_notification=bool(telegram.get("disable_notification", False)),
            protect_content=bool(telegram.get("protect_content", False)),
            timeout_seconds=int(telegram.get("timeout_seconds", 15)),
        ),
    )

    mark_news_as_processed(
        state,
        new_items,
        max_stored_ids=int(runtime.get("max_processed_ids", 10000)),
    )
    save_processing_state(state_path, state)

    report_path = write_delivery_report(run_label, len(messages), processed_batch)
    LOGGER.info("Envio completado. Reporte guardado en %s", report_path)
    return True


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FinancialJuice + Gemini + Telegram pipeline")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the JSON config file.")
    parser.add_argument("--once", action="store_true", help="Run only one cycle and exit.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=args.verbose)

    try:
        settings = load_json_config(args.config)
        settings = apply_env_overrides(settings)
        settings = normalize_settings(settings)
        validate_settings(settings)
    except Exception as exc:
        LOGGER.error("Error cargando configuracion: %s", exc)
        return 1

    run_once = bool(args.once or settings.get("runtime", {}).get("run_once", False))
    poll_interval_seconds = int(settings["runtime"]["poll_interval_seconds"])
    failure_retry_seconds = int(settings["runtime"].get("failure_retry_seconds", 300))

    try:
        while True:
            try:
                success = run_pipeline_cycle(settings)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                LOGGER.exception("Fallo en el ciclo del pipeline: %s", exc)
                success = False

            if run_once:
                return 0 if success else 1

            sleep_seconds = poll_interval_seconds if success else failure_retry_seconds
            LOGGER.info("Esperando %s segundos hasta el siguiente ciclo...", sleep_seconds)
            time.sleep(max(1, sleep_seconds))
    except KeyboardInterrupt:
        LOGGER.info("Interrumpido por el usuario")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
