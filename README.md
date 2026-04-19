# FinancialJuice News Extractor & Analyzer

Pipeline en Python para extraer noticias de FinancialJuice, procesarlas en lote con Gemini, distribuir carga entre varios modelos con *fallback* automático y publicar el resultado en un *topic* de Telegram con control de estado local.

---

## Características

- Extracción dinámica de FinancialJuice mediante navegador real con **Playwright**.
- Procesado en lote de noticias nuevas para minimizar llamadas a la API.
- Soporte **multi-modelo** con estrategias separadas para:
  - estructuración y clasificación del lote,
  - comentario final de impacto en mercado.
- *Fallback* automático entre modelos según disponibilidad y límites configurados.
- Publicación formateada en Telegram con partición automática de mensajes largos.
- Persistencia de estado para evitar duplicados, reprocesados y reenvíos innecesarios.
- Arquitectura modular, separando extracción, IA, mensajería y configuración.

---

## Estructura del proyecto

```text
Dev/
├─ main.py
├─ requirements.txt
├─ README.md
├─ Config/
│  ├─ settings.json
│  ├─ settings.example.json
│  └─ browser_profile/
├─ Includes/
│  ├─ __init__.py
│  ├─ financialjuice_extractor.py
│  ├─ news_ai_proccesing.py
│  └─ Telegram_BotManager.py
└─ Repository/
````

> **Nota:** El archivo `news_ai_proccesing.py` mantiene ese nombre por compatibilidad con la estructura actual del proyecto. La grafía correcta en inglés sería `processing`.

---

## Arquitectura general

El pipeline está compuesto por tres módulos principales:

### 1. `financialjuice_extractor.py`

Responsable de abrir FinancialJuice, autenticarse si es necesario, recorrer el feed dinámico y exportar las noticias a formatos estructurados (`.jsonl`, `.json`, `.md`).

### 2. `news_ai_proccesing.py`

Responsable de:

* filtrar noticias nuevas,
* agruparlas en un único lote,
* enviarlas a Gemini,
* traducirlas y clasificarlas,
* redactar un comentario agregado de impacto en mercado,
* seleccionar modelos en función de prioridad, cuota disponible y *fallback*.

### 3. `Telegram_BotManager.py`

Responsable de:

* formatear el resultado final,
* trocearlo si supera el límite de Telegram,
* enviarlo al *topic* configurado del supergrupo.

### 4. `main.py`

Orquestador principal del sistema:

* ejecuta la extracción,
* carga el estado,
* determina qué noticias son nuevas,
* dispara el procesamiento IA,
* publica el resultado,
* actualiza estado y contadores.

---

## Requisitos

* Python 3.10 o superior
* Chromium instalado a través de Playwright
* Cuenta funcional en FinancialJuice
* Al menos una API key válida de Gemini
* Bot de Telegram con permisos de escritura en el grupo/topic de destino

---

## Instalación

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## Configuración

Toda la configuración operativa se centraliza en:

```text
Config/settings.json
```

Puedes partir de `Config/settings.example.json` y crear tu copia local.

---

## Configuración por bloques

### FinancialJuice

Configura los parámetros del bloque `financialjuice`:

* `email`: correo de acceso
* `password`: contraseña
* `user_data_dir`: ruta del perfil persistente del navegador
* `headed`: abre el navegador visible
* `manual_login`: pausa el script para autenticar manualmente
* `wait_ms`: tiempo máximo de espera del feed
* `max_scroll_rounds`: número máximo de rondas de *scroll*
* `scroll_pause_ms`: pausa entre *scrolls*

#### Recomendación operativa

Para la primera ejecución:

* usa `headed = true`
* usa `manual_login = true`
* guarda la sesión en `Config/browser_profile`

Después, una vez consolidada la sesión, puedes pasar a modo silencioso.

> **Importante:** `Config/browser_profile/` puede contener cookies, tokens y otros datos sensibles. No debe subirse al repositorio.

---

### Gemini

El bloque `gemini` define:

* una API key global opcional,
* claves específicas por modelo,
* límites locales por día y por minuto,
* rutas de ejecución por tarea.

#### Tareas separadas

* `batch_structuring`

  * traducción al español,
  * resumen del lote,
  * clasificación por importancia,
  * estructuración del contenido final.

* `market_commentary`

  * comentario final agregado,
  * interpretación del impacto esperado en:

    * IBEX 35
    * EURO STOXX 50
    * S&P 500
    * NIKKEI 225
    * acciones, divisas o criptomonedas relevantes si aplica.

#### Estrategia recomendada

**Para `batch_structuring`:**

* priorizar modelos de alto volumen y menor coste operativo.

**Para `market_commentary`:**

* priorizar modelos más sólidos para síntesis narrativa y juicio agregado.

#### Sobre los límites

El proyecto mantiene un contador local en:

```text
Repository/state/model_usage.json
```

Este contador sirve para autocontrol interno del pipeline, pero **no representa necesariamente el consumo real del proveedor** si utilizas la misma API key desde otros scripts, equipos o servicios.

---

### Telegram

Configura en el bloque `telegram`:

* `bot_token`
* `chat_id`
* `topic_thread_id`
* `parse_mode`

> **Nota:** En supergrupos, el `chat_id` normalmente empieza por `-100`.

El proyecto utiliza `message_thread_id` para publicar directamente dentro de un *topic* concreto.

---

## Variables de entorno opcionales

Puedes evitar guardar credenciales directamente en `settings.json` usando variables de entorno:

Linux/macOS:

```bash
export FJ_EMAIL="tu_email"
export FJ_PASSWORD="tu_password"
export GEMINI_API_KEY="tu_api_key"
export TELEGRAM_BOT_TOKEN="tu_bot_token"
export TELEGRAM_CHAT_ID="-1001234567890"
export TELEGRAM_TOPIC_THREAD_ID="12345"
```

Windows PowerShell:

```powershell
$env:FJ_EMAIL="tu_email"
$env:FJ_PASSWORD="tu_password"
$env:GEMINI_API_KEY="tu_api_key"
$env:TELEGRAM_BOT_TOKEN="tu_bot_token"
$env:TELEGRAM_CHAT_ID="-1001234567890"
$env:TELEGRAM_TOPIC_THREAD_ID="12345"
```

Estas variables pueden complementar o sobrescribir la configuración local según la implementación concreta del proyecto.

---

## Flujo de ejecución

Cada ciclo de `main.py` sigue esta secuencia:

1. Abre FinancialJuice y extrae las noticias del intervalo configurado.
2. Guarda la extracción en `Repository/financialjuice/<run_label>/`.
3. Carga el estado local del proyecto.
4. Filtra únicamente noticias nuevas no procesadas.
5. Agrupa las noticias nuevas en una sola consulta IA.
6. Ejecuta la ruta `batch_structuring`.
7. Ejecuta la ruta `market_commentary`.
8. Si el comentario de mercado falla, activa un *fallback* local conservador.
9. Construye mensajes HTML seguros para Telegram.
10. Divide mensajes largos si superan el límite de Telegram.
11. Publica en el *topic* configurado.
12. Marca el lote como procesado y actualiza contadores de uso por modelo.

---

## Estrategia de extracción

El extractor está diseñado para entornos donde el contenido se renderiza dinámicamente tras la carga inicial de la página.

### Principios de diseño

* No depende únicamente de selectores CSS frágiles.
* Busca contenedores candidatos del feed.
* Espera a que exista texto realmente renderizado.
* Hace *scroll* incremental hasta que el contenido deja de crecer.
* Identifica noticias usando heurísticas basadas en marca temporal.
* Filtra ruido visual y bloques no informativos.
* Exporta formatos adecuados para análisis posterior.

### Formatos de salida

Por cada ejecución, el extractor puede generar:

* `news.jsonl`
* `news.json`
* `news.md`
* `summary.json`

Además, si el modo `debug` está activado, puede generar:

* capturas de pantalla,
* volcados HTML,
* volcados de texto,
* informes de selectores.

---

## Modos de ejecución

### Ejecución única

```bash
python main.py --once
```

Adecuado para:

* pruebas manuales,
* depuración,
* ejecución controlada desde cron, Task Scheduler o systemd.

### Ejecución continua

```bash
python main.py --loop
```

Mantiene el proceso activo y ejecuta un ciclo periódico según la frecuencia configurada.

### Scheduler externo

También puedes ejecutar el proyecto mediante un planificador del sistema usando `--once`, por ejemplo:

* `cron` en Linux
* Task Scheduler en Windows
* `systemd timers`
* scheduler de contenedores o CI/CD

En entornos productivos, este enfoque suele ser más robusto que un bucle interno permanente.

---

## Estado local y persistencia

El proyecto guarda estado local para evitar reprocesados y duplicados.

Ejemplo de estructura:

```text
Repository/
├─ financialjuice/
│  └─ <run_label>/
│     ├─ news.jsonl
│     ├─ news.json
│     ├─ news.md
│     ├─ summary.json
│     └─ debug/
└─ state/
   ├─ processed_news.json
   ├─ sent_batches.json
   └─ model_usage.json
```

### Objetivos del estado local

* no volver a procesar noticias ya tratadas,
* no reenviar lotes ya publicados,
* controlar cuotas locales por modelo,
* permitir reintentos seguros si Telegram falla.

> **Importante:** Si Telegram falla, el sistema puede reintentar el envío en el siguiente ciclo sin necesidad de volver a consumir procesamiento IA, siempre que el resultado del lote siga disponible y la lógica de estado lo contemple.

---

## Estrategia multi-modelo

La lógica IA está pensada para operar con varias rutas y prioridades.

### Reparto recomendado de carga

#### Ruta `batch_structuring`

Asignar preferencia a modelos:

* rápidos,
* económicos,
* escalables,
* adecuados para salida estructurada.

#### Ruta `market_commentary`

Asignar preferencia a modelos:

* más consistentes en síntesis,
* más adecuados para comentario agregado y juicio contextual.

### Fallback automático

Si un modelo:

* falla,
* supera su límite local,
* no tiene API key disponible,
* devuelve respuesta inválida,

el sistema prueba automáticamente el siguiente modelo de la ruta configurada.

---

## Salida esperada hacia Telegram

El mensaje final publicado en Telegram puede incluir:

* importancia global del lote,
* titulares traducidos,
* resumen breve por noticia,
* etiquetas,
* hora,
* comentario final agregado,
* impacto esperado sobre índices o activos relevantes,
* referencia opcional al modelo utilizado.

El contenido se genera en HTML seguro para mejorar compatibilidad con `sendMessage`.

---

## Pruebas manuales

### 1. Probar extractor de FinancialJuice

Primera autenticación con navegador visible:

```bash
python Includes/financialjuice_extractor.py \
  --out-dir Repository/test_extract \
  --hours 6 \
  --headed \
  --manual-login
```

Extracción más profunda con mayor tolerancia de renderizado:

```bash
python Includes/financialjuice_extractor.py \
  --out-dir Repository/test_extract \
  --hours 24 \
  --max-scroll-rounds 60 \
  --scroll-pause-ms 2000
```

---

### 2. Probar procesado IA en lote

```bash
python Includes/news_ai_proccesing.py \
  --jsonl Repository/test_extract/news.jsonl \
  --api-key TU_API_KEY \
  --model gemini-2.5-flash \
  --output Repository/test_ai_batch.json
```

---

### 3. Probar envío a Telegram

```bash
python Includes/Telegram_BotManager.py \
  --bot-token TU_BOT_TOKEN \
  --chat-id -1001234567890 \
  --thread-id 12345 \
  --text "<b>Prueba de conexión exitosa</b>" \
  --parse-mode HTML
```

---

## Resolución de problemas

## `No news items extracted from rendered feed text`

Suele indicar que:

* el feed no terminó de renderizar,
* la sesión no está autenticada,
* apareció un *popup* bloqueante,
* el DOM cambió y las heurísticas actuales no bastan.

### Solución recomendada

Ejecuta en modo visible con depuración:

```bash
python Includes/financialjuice_extractor.py \
  --headed \
  --manual-login \
  --debug \
  --hours 6 \
  --out-dir out
```

Después inspecciona:

```text
out/debug/
```

Busca especialmente:

* capturas tras login,
* capturas tras *scroll*,
* `debug_selector_report.txt`,
* volcados de texto de contenedores.

---

## El feed se ve, pero faltan noticias

Puede ocurrir si el *scroll* avanza demasiado rápido.

### Acción recomendada

Aumenta la pausa entre *scrolls*:

```bash
--scroll-pause-ms 2000
```

o más si la página responde lentamente.

---

## Un modelo IA agota su cuota local

El pipeline saltará al siguiente modelo disponible dentro de la ruta configurada.

### Comportamiento esperado

* si falla `batch_structuring`, el lote no podrá procesarse correctamente;
* si falla `market_commentary`, el sistema puede generar un comentario local conservador.

---

## Telegram falla pero la IA ya procesó el lote

El lote no debería marcarse como enviado hasta que Telegram confirme el envío correctamente.
Esto permite reintentos en el siguiente ciclo sin perder información.

---

## Buenas prácticas

* No subas `Config/settings.json` con credenciales reales.
* No subas `Config/browser_profile/`.
* No subas `Repository/` salvo que quieras versionar estado o muestras de salida.
* Añade secretos y artefactos operativos al `.gitignore`.
* Usa `settings.example.json` como plantilla compartible.
* Valida la sesión de FinancialJuice manualmente antes de automatizar en segundo plano.
* Supervisa periódicamente el estado local de cuotas si trabajas con varias claves o varios entornos.

---

## Prompt sugerido para análisis externo

Si deseas analizar `news.jsonl` o `news.md` fuera del pipeline principal, este prompt suele producir buenos resultados:

```text
Analiza de forma exhaustiva el dataset de noticias de FinancialJuice adjunto.

Tareas:
1. Agrupa las noticias por temáticas.
2. Construye una línea de tiempo narrativa del mercado.
3. Identifica impactos probables de primer y segundo orden por clase de activo.
4. Señala contradicciones, continuaciones y evolución de historias.
5. Separa hechos del titular frente a interpretación.
6. Produce un resumen orientado a traders y otro orientado a gestores de riesgo.
7. Extrae todas las referencias a macro, tipos, FX, renta variable, materias primas, cripto, geopolítica y shipping/energía.
8. Destaca qué cambió durante la ventana temporal analizada.

Devuelve:
- Resumen ejecutivo
- Línea de tiempo cronológica
- Clusters temáticos
- Matriz de impacto por activo
- Preguntas abiertas
- Riesgos clave
```


## Enfoque recomendado de despliegue

### Fase 1 — Validación manual

* `headed = true`
* `manual_login = true`
* pruebas de extracción y envío aisladas

### Fase 2 — Automatización controlada

* perfil persistente consolidado
* ejecución con `main.py --once`
* scheduler externo cada 2 horas

### Fase 3 — Operación estable

* monitoreo de errores
* revisión periódica de cuota local de modelos
* revisión de cambios en el DOM de FinancialJuice
* ajuste de prioridades de modelos según coste, calidad y disponibilidad


## Aviso

Este proyecto depende de servicios externos y de estructuras web que pueden cambiar con el tiempo:

* DOM y comportamiento de FinancialJuice
* disponibilidad y límites de modelos Gemini
* API de Telegram Bot

Por tanto, conviene revisar periódicamente:

* la estrategia de extracción,
* los modelos configurados,
* las cuotas locales,
* y el comportamiento de publicación.


