"""
news_ai_proccesing.py

Procesado por IA en lote para noticias nuevas de FinancialJuice.

Objetivos:
- Hacer una sola llamada por lote para estructurar noticias nuevas.
- Permitir una segunda llamada opcional para el comentario agregado de mercado,
  pudiendo usar un modelo distinto.
- Repartir carga entre varios modelos Gemini y aplicar fallback si alguno falla.
- Llevar un control local de uso por modelo para respetar cupos diarios y por minuto
  configurados por el usuario.
- Mantener estado de noticias ya procesadas para no repetir envíos.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google import genai
from google.genai import errors

MAX_INPUT_BODY_CHARS = 700
DEFAULT_MAX_STORED_IDS = 10000
_DIRECTION_VALUES = {"ALCISTA", "BAJISTA", "NEUTRAL", "MIXTO"}
_ASSET_TYPE_VALUES = {"INDEX", "STOCK", "ETF", "FOREX", "BOND", "COMMODITY", "CRYPTO", "UNKNOWN"}


@dataclass
class AIProcessingConfig:
    api_key: str = ""
    model: str = "gemini-2.5-flash"
    temperature: float = 0.2
    max_output_tokens: int = 8192
    separate_market_commentary: bool = True
    models: Optional[Dict[str, Dict[str, Any]]] = None
    routes: Optional[Dict[str, Dict[str, Any]]] = None
    usage_state_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def truncate_text(value: Optional[str], max_chars: int) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def build_news_id(item: Dict[str, Any]) -> str:
    payload = "|".join(
        [
            str(item.get("headline", "")).strip(),
            str(item.get("body", "") or "").strip(),
            str(item.get("timestamp_iso", "") or item.get("timestamp_text", "")).strip(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def attach_news_ids(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for item in items:
        cloned = dict(item)
        cloned["news_id"] = cloned.get("news_id") or build_news_id(cloned)
        enriched.append(cloned)
    return enriched


# ---------------------------------------------------------------------------
# Estado de noticias procesadas
# ---------------------------------------------------------------------------

def load_processing_state(state_path: str | Path) -> Dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        return {
            "processed_ids": [],
            "last_successful_run_iso": None,
            "updated_at_iso": None,
        }

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid processing state format in {path}")

    data.setdefault("processed_ids", [])
    data.setdefault("last_successful_run_iso", None)
    data.setdefault("updated_at_iso", None)
    return data


def save_processing_state(state_path: str | Path, state: Dict[str, Any]) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at_iso"] = now_utc_iso()
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def filter_unprocessed_news(items: Sequence[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    processed_ids = set(state.get("processed_ids", []))
    new_items: List[Dict[str, Any]] = []

    for item in attach_news_ids(items):
        if item["news_id"] in processed_ids:
            continue
        new_items.append(item)

    return new_items


def mark_news_as_processed(state: Dict[str, Any], items: Sequence[Dict[str, Any]], max_stored_ids: int = DEFAULT_MAX_STORED_IDS) -> Dict[str, Any]:
    processed_ids = list(state.get("processed_ids", []))
    known = set(processed_ids)

    for item in attach_news_ids(items):
        news_id = item["news_id"]
        if news_id in known:
            continue
        processed_ids.append(news_id)
        known.add(news_id)

    if len(processed_ids) > max_stored_ids:
        processed_ids = processed_ids[-max_stored_ids:]

    state["processed_ids"] = processed_ids
    state["last_successful_run_iso"] = now_utc_iso()
    return state


# ---------------------------------------------------------------------------
# Estado local de uso por modelo
# ---------------------------------------------------------------------------

def load_model_usage_state(state_path: Optional[str | Path]) -> Dict[str, Any]:
    if not state_path:
        return {"models": {}, "updated_at_iso": None}

    path = Path(state_path)
    if not path.exists():
        return {"models": {}, "updated_at_iso": None}

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        return {"models": {}, "updated_at_iso": None}

    data.setdefault("models", {})
    data.setdefault("updated_at_iso", None)
    return data


def save_model_usage_state(state_path: Optional[str | Path], state: Dict[str, Any]) -> None:
    if not state_path:
        return
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at_iso"] = now_utc_iso()
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _prepare_model_usage_bucket(model_name: str, usage_state: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    models = usage_state.setdefault("models", {})
    model_state = models.setdefault(model_name, {})

    current_day = now.strftime("%Y-%m-%d")
    current_minute = now.strftime("%Y-%m-%dT%H:%M")

    daily = model_state.get("daily", {})
    if daily.get("date") != current_day:
        daily = {"date": current_day, "count": 0}
    model_state["daily"] = daily

    minute = model_state.get("minute", {})
    if minute.get("bucket") != current_minute:
        minute = {"bucket": current_minute, "count": 0}
    model_state["minute"] = minute

    return model_state


def _limit_to_int(value: Any) -> Optional[int]:
    if value in (None, "", 0, "0"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def can_use_model(model_name: str, model_spec: Dict[str, Any], usage_state: Dict[str, Any], now: datetime) -> Tuple[bool, str]:
    if not model_spec.get("enabled", True):
        return False, "disabled"

    api_key = str(model_spec.get("api_key", "") or "").strip()
    if not api_key:
        return False, "missing api_key"

    model_state = _prepare_model_usage_bucket(model_name, usage_state, now)
    daily_limit = _limit_to_int(model_spec.get("daily_limit"))
    minute_limit = _limit_to_int(model_spec.get("per_minute_limit"))

    if daily_limit is not None and int(model_state["daily"].get("count", 0)) >= daily_limit:
        return False, f"daily limit reached ({daily_limit})"
    if minute_limit is not None and int(model_state["minute"].get("count", 0)) >= minute_limit:
        return False, f"per-minute limit reached ({minute_limit})"
    return True, "ok"


def reserve_model_call(model_name: str, usage_state: Dict[str, Any], now: datetime) -> None:
    model_state = _prepare_model_usage_bucket(model_name, usage_state, now)
    model_state["daily"]["count"] = int(model_state["daily"].get("count", 0)) + 1
    model_state["minute"]["count"] = int(model_state["minute"].get("count", 0)) + 1


def mark_model_limited(model_name: str, model_spec: Dict[str, Any], usage_state: Dict[str, Any], now: datetime, scope: str) -> None:
    model_state = _prepare_model_usage_bucket(model_name, usage_state, now)
    if scope == "daily":
        daily_limit = _limit_to_int(model_spec.get("daily_limit"))
        if daily_limit is not None:
            model_state["daily"]["count"] = daily_limit
    elif scope == "minute":
        minute_limit = _limit_to_int(model_spec.get("per_minute_limit"))
        if minute_limit is not None:
            model_state["minute"]["count"] = minute_limit


def classify_api_limit_scope(error_message: str) -> Optional[str]:
    text = (error_message or "").lower()
    if any(token in text for token in ("per day", "daily", "day limit", "quota exhausted")):
        return "daily"
    if any(token in text for token in ("per minute", "minute", "rate limit", "resource_exhausted", "too many requests")):
        return "minute"
    return None


# ---------------------------------------------------------------------------
# Configuracion del router de modelos
# ---------------------------------------------------------------------------

def normalize_processing_config(config: AIProcessingConfig) -> Dict[str, Any]:
    models = copy.deepcopy(config.models) if config.models else {}
    routes = copy.deepcopy(config.routes) if config.routes else {}

    if not models:
        models[config.model] = {
            "enabled": True,
            "api_key": config.api_key,
            "temperature": config.temperature,
            "max_output_tokens": config.max_output_tokens,
        }

    for model_name, spec in models.items():
        spec.setdefault("enabled", True)
        spec.setdefault("api_key", config.api_key)
        spec.setdefault("temperature", config.temperature)
        spec.setdefault("max_output_tokens", config.max_output_tokens)
        spec.setdefault("daily_limit", None)
        spec.setdefault("per_minute_limit", None)
        models[model_name] = spec

    if not routes:
        routes = {
            "batch_structuring": {
                "models": [config.model],
                "temperature": config.temperature,
                "max_output_tokens": config.max_output_tokens,
            },
            "market_commentary": {
                "models": [config.model],
                "temperature": config.temperature,
                "max_output_tokens": min(config.max_output_tokens, 4096),
            },
        }

    for route_name, route in routes.items():
        route.setdefault("models", list(models.keys()))
        route.setdefault("temperature", config.temperature)
        route.setdefault("max_output_tokens", config.max_output_tokens)
        routes[route_name] = route

    return {
        "models": models,
        "routes": routes,
        "separate_market_commentary": bool(config.separate_market_commentary),
        "usage_state_path": config.usage_state_path,
    }


# ---------------------------------------------------------------------------
# Prompts y esquemas
# ---------------------------------------------------------------------------

def build_structuring_prompt(news_items: Sequence[Dict[str, Any]]) -> str:
    compact_items = []
    for item in attach_news_ids(news_items):
        compact_items.append(
            {
                "source_id": item["news_id"],
                "headline": item.get("headline", ""),
                "body": truncate_text(item.get("body"), MAX_INPUT_BODY_CHARS),
                "tags": item.get("tags", []),
                "timestamp_text": item.get("timestamp_text", ""),
                "timestamp_iso": item.get("timestamp_iso"),
            }
        )

    payload = json.dumps(compact_items, ensure_ascii=False, indent=2)

    return f"""
Eres un editor financiero macro especializado en noticias de mercado.

Recibirás un lote de noticias en JSON. Devuelve solo JSON válido y estrictamente ajustado al esquema.
No escribas texto fuera del JSON.

Reglas obligatorias:
1. Trabaja exclusivamente con la información proporcionada.
2. Traduce cada titular al español y déjalo en MAYÚSCULAS.
3. Resume cada noticia en español con claridad y precisión, manteniendo tecnicismos.
4. Asigna importance_level entre 1 y 5.
5. Para cada noticia, indica market_bias usando solo: ALCISTA, BAJISTA, NEUTRAL o MIXTO.
6. Debes devolver exactamente {len(compact_items)} elementos en items, uno por cada source_id recibido, sin omitir ni duplicar.
7. impact_reason debe ser breve y operativo.
8. Mantén tono profesional y conciso.

Noticias del lote:
{payload}
""".strip()


def build_structuring_schema() -> Dict[str, Any]:
    direction_schema = {
        "type": "string",
        "enum": ["ALCISTA", "BAJISTA", "NEUTRAL", "MIXTO"],
    }
    item_schema = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string"},
            "translated_headline": {"type": "string"},
            "summary_es": {"type": "string"},
            "importance_level": {"type": "integer", "minimum": 1, "maximum": 5},
            "market_bias": direction_schema,
            "impact_reason": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
            },
            "timestamp_text": {"type": "string"},
        },
        "required": [
            "source_id",
            "translated_headline",
            "summary_es",
            "importance_level",
            "market_bias",
            "impact_reason",
            "tags",
            "timestamp_text",
        ],
    }
    return {
        "type": "object",
        "properties": {
            "batch_title": {"type": "string"},
            "items": {
                "type": "array",
                "items": item_schema,
            },
        },
        "required": ["batch_title", "items"],
    }


def build_market_commentary_prompt(structured_items: Sequence[Dict[str, Any]]) -> str:
    compact_items = []
    for item in structured_items:
        compact_items.append(
            {
                "source_id": item.get("source_id"),
                "translated_headline": item.get("translated_headline"),
                "summary_es": item.get("summary_es"),
                "importance_level": item.get("importance_level"),
                "market_bias": item.get("market_bias"),
                "impact_reason": item.get("impact_reason"),
                "tags": item.get("tags", []),
                "timestamp_text": item.get("timestamp_text"),
            }
        )

    payload = json.dumps(compact_items, ensure_ascii=False, indent=2)

    return f"""
Eres un estratega macro y editor de mercado para un canal de Telegram.

Recibirás noticias ya estructuradas y resumidas. Devuelve solo JSON válido y estrictamente ajustado al esquema.
No escribas texto fuera del JSON.

Tareas:
1. Redacta market_overview en español, claro y operativo.
2. Evalúa el impacto agregado estimado sobre IBEX35, EUROSTOXX50, S&P500 y NIKKEI225.
3. Para cada índice usa direction en {{ALCISTA, BAJISTA, NEUTRAL, MIXTO}}, confidence entre 0 y 100 y una reason breve.
4. impacted_assets solo debe incluir activos con una relación razonable con el lote; pueden ser índices, acciones, divisas, bonos, commodities o criptomonedas.
5. final_commentary debe actuar como comentario final útil para inversores, indicando sesgo agregado al alza o a la baja y dónde podría notarse más.
6. Si la información es insuficiente, usa NEUTRAL y explica la incertidumbre.
7. Mantén un tono profesional y no inventes datos concretos no contenidos o no inferibles razonablemente.

Noticias estructuradas del lote:
{payload}
""".strip()


def build_market_commentary_schema() -> Dict[str, Any]:
    direction_schema = {
        "type": "string",
        "enum": ["ALCISTA", "BAJISTA", "NEUTRAL", "MIXTO"],
    }
    index_impact_schema = {
        "type": "object",
        "properties": {
            "direction": direction_schema,
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "reason": {"type": "string"},
        },
        "required": ["direction", "confidence", "reason"],
    }
    asset_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "symbol": {"type": "string"},
            "asset_type": {
                "type": "string",
                "enum": ["INDEX", "STOCK", "ETF", "FOREX", "BOND", "COMMODITY", "CRYPTO", "UNKNOWN"],
            },
            "direction": direction_schema,
            "reason": {"type": "string"},
        },
        "required": ["name", "symbol", "asset_type", "direction", "reason"],
    }
    return {
        "type": "object",
        "properties": {
            "market_overview": {"type": "string"},
            "global_market_impact": {
                "type": "object",
                "properties": {
                    "ibex35": index_impact_schema,
                    "eurostoxx50": index_impact_schema,
                    "sp500": index_impact_schema,
                    "nikkei225": index_impact_schema,
                    "impacted_assets": {
                        "type": "array",
                        "items": asset_schema,
                    },
                    "final_commentary": {"type": "string"},
                },
                "required": [
                    "ibex35",
                    "eurostoxx50",
                    "sp500",
                    "nikkei225",
                    "impacted_assets",
                    "final_commentary",
                ],
            },
        },
        "required": ["market_overview", "global_market_impact"],
    }


# ---------------------------------------------------------------------------
# Parseo y normalizacion
# ---------------------------------------------------------------------------

def extract_first_json_block(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("The AI response is empty.")

    if text.startswith("{") and text.endswith("}"):
        return text

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Could not locate a JSON object in the AI response.")
    return match.group(0)


def clamp_importance(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))


def clamp_confidence(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 50
    return max(0, min(100, number))


def normalize_direction(value: Any) -> str:
    if not isinstance(value, str):
        return "NEUTRAL"
    value = value.strip().upper()
    return value if value in _DIRECTION_VALUES else "NEUTRAL"


def normalize_asset_type(value: Any) -> str:
    if not isinstance(value, str):
        return "UNKNOWN"
    value = value.strip().upper()
    return value if value in _ASSET_TYPE_VALUES else "UNKNOWN"


def fallback_item_from_source(item: Dict[str, Any]) -> Dict[str, Any]:
    headline = str(item.get("headline", "") or "").strip()
    translated_headline = headline.upper() if headline else "NOTICIA SIN TITULAR"
    return {
        "source_id": item["news_id"],
        "translated_headline": translated_headline,
        "summary_es": truncate_text(item.get("body") or item.get("headline") or "Sin resumen disponible.", 320),
        "importance_level": 3,
        "market_bias": "NEUTRAL",
        "impact_reason": "La IA no devolvió un análisis válido para esta noticia y se dejó en estado neutral por seguridad.",
        "tags": list(item.get("tags", [])),
        "timestamp_text": str(item.get("timestamp_text", "") or ""),
    }


def normalize_index_block(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    return {
        "direction": normalize_direction(value.get("direction")),
        "confidence": clamp_confidence(value.get("confidence")),
        "reason": truncate_text(str(value.get("reason", "") or "Sin comentario disponible."), 260),
    }


def normalize_impacted_assets(value: Any) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return assets

    for asset in value[:8]:
        if not isinstance(asset, dict):
            continue
        assets.append(
            {
                "name": truncate_text(str(asset.get("name", "") or "Activo no especificado"), 80),
                "symbol": truncate_text(str(asset.get("symbol", "") or "N/A"), 30),
                "asset_type": normalize_asset_type(asset.get("asset_type")),
                "direction": normalize_direction(asset.get("direction")),
                "reason": truncate_text(str(asset.get("reason", "") or "Sin motivo especificado."), 220),
            }
        )
    return assets


def normalize_structuring_output(raw_output: Dict[str, Any], source_news_items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    source_items = attach_news_ids(source_news_items)
    source_map = {item["news_id"]: item for item in source_items}

    raw_items = raw_output.get("items", []) if isinstance(raw_output, dict) else []
    normalized_items_map: Dict[str, Dict[str, Any]] = {}

    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id", "") or "").strip()
            if not source_id or source_id not in source_map:
                continue
            normalized_items_map[source_id] = {
                "source_id": source_id,
                "translated_headline": truncate_text(str(item.get("translated_headline", "") or "").strip() or source_map[source_id].get("headline", "").upper(), 180),
                "summary_es": truncate_text(str(item.get("summary_es", "") or "").strip() or "Sin resumen disponible.", 500),
                "importance_level": clamp_importance(item.get("importance_level")),
                "market_bias": normalize_direction(item.get("market_bias")),
                "impact_reason": truncate_text(str(item.get("impact_reason", "") or "Sin razonamiento disponible."), 260),
                "tags": [str(tag).strip() for tag in item.get("tags", []) if str(tag).strip()][:8],
                "timestamp_text": str(item.get("timestamp_text", "") or source_map[source_id].get("timestamp_text", "")),
            }

    normalized_items: List[Dict[str, Any]] = []
    for source in source_items:
        source_id = source["news_id"]
        normalized_items.append(normalized_items_map.get(source_id, fallback_item_from_source(source)))

    return {
        "batch_title": truncate_text(str(raw_output.get("batch_title", "") or "Resumen de noticias de mercado"), 120),
        "items": normalized_items,
        "source_news": source_items,
    }


def normalize_market_commentary_output(raw_output: Dict[str, Any]) -> Dict[str, Any]:
    global_impact = raw_output.get("global_market_impact", {}) if isinstance(raw_output, dict) else {}
    if not isinstance(global_impact, dict):
        global_impact = {}

    return {
        "market_overview": truncate_text(str(raw_output.get("market_overview", "") or "Sin overview disponible."), 800),
        "global_market_impact": {
            "ibex35": normalize_index_block(global_impact.get("ibex35")),
            "eurostoxx50": normalize_index_block(global_impact.get("eurostoxx50")),
            "sp500": normalize_index_block(global_impact.get("sp500")),
            "nikkei225": normalize_index_block(global_impact.get("nikkei225")),
            "impacted_assets": normalize_impacted_assets(global_impact.get("impacted_assets")),
            "final_commentary": truncate_text(str(global_impact.get("final_commentary", "") or "Sin comentario final disponible."), 1500),
        },
    }


# ---------------------------------------------------------------------------
# Llamadas a modelos y fallback por rutas
# ---------------------------------------------------------------------------

def _call_single_model(
    model_name: str,
    api_key: str,
    prompt: str,
    schema: Dict[str, Any],
    temperature: float,
    max_output_tokens: int,
) -> Dict[str, Any]:
    with genai.Client(api_key=api_key) as client:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "response_mime_type": "application/json",
                "response_json_schema": schema,
            },
        )

    response_text = getattr(response, "text", None)
    if not response_text:
        parsed = getattr(response, "parsed", None)
        if parsed is None:
            raise RuntimeError("Gemini returned an empty response.")
        response_text = json.dumps(parsed, ensure_ascii=False)

    return json.loads(extract_first_json_block(response_text))


def generate_json_with_routing(
    prompt: str,
    schema: Dict[str, Any],
    route_name: str,
    routing_config: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    models = routing_config["models"]
    routes = routing_config["routes"]
    if route_name not in routes:
        raise ValueError(f"Unknown AI route: {route_name}")

    route = routes[route_name]
    usage_state_path = routing_config.get("usage_state_path")
    usage_state = load_model_usage_state(usage_state_path)
    attempts: List[Dict[str, Any]] = []

    for model_name in route.get("models", []):
        model_spec = models.get(model_name)
        if not model_spec:
            attempts.append({"model_name": model_name, "route_name": route_name, "error": "model not configured"})
            continue

        now = now_utc()
        allowed, reason = can_use_model(model_name, model_spec, usage_state, now)
        if not allowed:
            attempts.append({"model_name": model_name, "route_name": route_name, "error": reason})
            continue

        reserve_model_call(model_name, usage_state, now)
        save_model_usage_state(usage_state_path, usage_state)

        api_key = str(model_spec.get("api_key", "") or "").strip()
        temperature = float(route.get("temperature", model_spec.get("temperature", 0.2)))
        max_output_tokens = int(route.get("max_output_tokens", model_spec.get("max_output_tokens", 8192)))

        try:
            raw_output = _call_single_model(
                model_name=model_name,
                api_key=api_key,
                prompt=prompt,
                schema=schema,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            attempts.append({"model_name": model_name, "route_name": route_name, "error": None})
            return raw_output, model_name, attempts
        except errors.APIError as exc:
            code = getattr(exc, "code", "unknown")
            message = getattr(exc, "message", str(exc))
            full_message = f"APIError {code}: {message}"
            scope = classify_api_limit_scope(full_message)
            if scope:
                mark_model_limited(model_name, model_spec, usage_state, now_utc(), scope=scope)
                save_model_usage_state(usage_state_path, usage_state)
            attempts.append({"model_name": model_name, "route_name": route_name, "error": full_message})
        except Exception as exc:
            attempts.append({"model_name": model_name, "route_name": route_name, "error": str(exc)})

    summary = "; ".join(
        f"{attempt['model_name']}: {attempt['error']}" for attempt in attempts if attempt.get("error")
    )
    raise RuntimeError(f"All candidate models failed for route '{route_name}'. {summary}")


# ---------------------------------------------------------------------------
# Fallback local del comentario agregado
# ---------------------------------------------------------------------------

def derive_dominant_bias(structured_items: Sequence[Dict[str, Any]]) -> Tuple[str, int]:
    score = 0
    for item in structured_items:
        importance = clamp_importance(item.get("importance_level"))
        bias = normalize_direction(item.get("market_bias"))
        if bias == "ALCISTA":
            score += importance
        elif bias == "BAJISTA":
            score -= importance

    if score >= 4:
        return "ALCISTA", min(60, 30 + score * 4)
    if score <= -4:
        return "BAJISTA", min(60, 30 + abs(score) * 4)
    return "NEUTRAL", 35


def build_local_commentary_fallback(structured_batch: Dict[str, Any]) -> Dict[str, Any]:
    items = structured_batch.get("items", [])
    direction, confidence = derive_dominant_bias(items)
    base_reason = (
        "Estimación local automática basada en el sesgo agregado de las noticias procesadas; "
        "no se obtuvo comentario IA válido y se aplica una lectura conservadora."
    )
    if direction == "ALCISTA":
        overview = "El lote mantiene un sesgo agregado ligeramente alcista, aunque con convicción moderada por tratarse de una inferencia automática de respaldo."
        final_commentary = "En conjunto, el flujo de noticias favorece una lectura ligeramente alcista. Aun así, conviene tratarlo como una señal de respaldo y no como un comentario macro definitivo, porque esta parte del análisis no pudo completarse con IA."
    elif direction == "BAJISTA":
        overview = "El lote mantiene un sesgo agregado ligeramente bajista, con convicción moderada por tratarse de una inferencia automática de respaldo."
        final_commentary = "En conjunto, el flujo de noticias favorece una lectura ligeramente bajista. Aun así, conviene tratarlo como una señal de respaldo y no como un comentario macro definitivo, porque esta parte del análisis no pudo completarse con IA."
    else:
        overview = "El lote no ofrece un sesgo agregado suficientemente claro y se clasifica como neutral por prudencia."
        final_commentary = "En conjunto, las noticias no permiten sostener un sesgo direccional claro. La lectura prudente es neutral hasta disponer de una interpretación de mercado más sólida."

    index_block = {
        "direction": direction,
        "confidence": confidence,
        "reason": base_reason,
    }

    return {
        "market_overview": overview,
        "global_market_impact": {
            "ibex35": dict(index_block),
            "eurostoxx50": dict(index_block),
            "sp500": dict(index_block),
            "nikkei225": dict(index_block),
            "impacted_assets": [],
            "final_commentary": final_commentary,
        },
    }


# ---------------------------------------------------------------------------
# Orquestacion principal
# ---------------------------------------------------------------------------

def process_news_batch(news_items: Sequence[Dict[str, Any]], config: AIProcessingConfig) -> Dict[str, Any]:
    if not news_items:
        raise ValueError("No news items were supplied for AI processing.")

    routing_config = normalize_processing_config(config)

    structuring_prompt = build_structuring_prompt(news_items)
    structuring_schema = build_structuring_schema()
    structuring_raw, structuring_model, structuring_attempts = generate_json_with_routing(
        structuring_prompt,
        structuring_schema,
        route_name="batch_structuring",
        routing_config=routing_config,
    )
    structured_batch = normalize_structuring_output(structuring_raw, news_items)

    commentary_prompt = build_market_commentary_prompt(structured_batch["items"])
    commentary_schema = build_market_commentary_schema()
    commentary_route_name = "market_commentary" if routing_config.get("separate_market_commentary", True) else "batch_structuring"

    commentary_model: Optional[str] = None
    commentary_attempts: List[Dict[str, Any]] = []
    try:
        commentary_raw, commentary_model, commentary_attempts = generate_json_with_routing(
            commentary_prompt,
            commentary_schema,
            route_name=commentary_route_name,
            routing_config=routing_config,
        )
        commentary = normalize_market_commentary_output(commentary_raw)
        commentary_raw_payload: Dict[str, Any] = commentary_raw
    except Exception as exc:
        commentary = build_local_commentary_fallback(structured_batch)
        commentary_model = "local-fallback"
        commentary_raw_payload = {
            "fallback_reason": str(exc),
            "generated_locally": True,
        }
        commentary_attempts.append(
            {
                "model_name": "local-fallback",
                "route_name": commentary_route_name,
                "error": None,
            }
        )

    models_used: List[str] = [structuring_model]
    if commentary_model and commentary_model not in models_used:
        models_used.append(commentary_model)

    processed_batch = {
        "meta": {
            "generated_at_iso": now_utc_iso(),
            "batch_size": len(structured_batch["items"]),
            "models_used": models_used,
            "batch_structuring_model": structuring_model,
            "market_commentary_model": commentary_model,
            "routes": {
                "batch_structuring": structuring_attempts,
                "market_commentary": commentary_attempts,
            },
        },
        "batch_title": structured_batch["batch_title"],
        "market_overview": commentary["market_overview"],
        "items": structured_batch["items"],
        "global_market_impact": commentary["global_market_impact"],
        "source_news": structured_batch["source_news"],
        "raw_model_response": {
            "batch_structuring": structuring_raw,
            "market_commentary": commentary_raw_payload,
        },
    }
    return processed_batch


def save_processed_batch(repository_dir: str | Path, processed_batch: Dict[str, Any], run_label: Optional[str] = None) -> Path:
    repository = Path(repository_dir)
    run_label = run_label or now_utc().strftime("%Y%m%dT%H%M%SZ")
    out_dir = repository / "processed_batches" / run_label
    out_dir.mkdir(parents=True, exist_ok=True)

    path = out_dir / "ai_batch.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(processed_batch, f, ensure_ascii=False, indent=2)
    return path


def load_jsonl_news(jsonl_path: str | Path) -> List[Dict[str, Any]]:
    path = Path(jsonl_path)
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


# ---------------------------------------------------------------------------
# CLI simple (modo legacy / pruebas manuales)
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process a JSONL batch of news with Gemini.")
    parser.add_argument("--jsonl", required=True, help="Path to the source JSONL file.")
    parser.add_argument("--api-key", required=True, help="Gemini API key.")
    parser.add_argument("--model", default="gemini-2.5-flash", help="Gemini model.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Model temperature.")
    parser.add_argument("--max-output-tokens", type=int, default=8192, help="Max output tokens.")
    parser.add_argument("--output", default=None, help="Optional output JSON path.")
    return parser.parse_args(argv)


def cli_main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        items = load_jsonl_news(args.jsonl)
        batch = process_news_batch(
            items,
            AIProcessingConfig(
                api_key=args.api_key,
                model=args.model,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
                separate_market_commentary=False,
            ),
        )

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(batch, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("[abort] interrupted by user")
        return 130
    except Exception as exc:
        print(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
