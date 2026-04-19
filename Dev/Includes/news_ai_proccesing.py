from google import genai
import json

# client = genai.Client(api_key="TU_API_KEY_AQUI")
client = genai.Client(api_key="AIzaSyDn4PACnKXiMXRLEtVwh4l4tl-N9UaE31A")

def procesar_noticia(json_data):
    # Extraemos los datos del JSON que proporcionaste
    # headline = json_data.get("headline", "")
    # tags = ", ".join(json_data.get("tags", []))
    # timestamp = json_data.get("timestamp_text", "")
    headline = "Iran's Parliament Speaker Ghalibaf: Any current traffic through Strait is under our control. If US blockade continues, passage through the Strait of Hormuz will be restricted"
    tags = "Energy, US, Bonds US, Indexes, USD"
    timestamp = "00:21 Apr 19"

    prompt = f"""
    Eres un editor de noticias internacionales experto. Tu tarea es procesar esta noticia para un canal de Telegram.
    
    NOTICIA ORIGINAL:
    Titular: {headline}
    Etiquetas: {tags}
    Hora: {timestamp}

    INSTRUCCIONES:
    1. Traduce fielmente al español sin perder tecnicismos.
    2. Determina el "Nivel de Importancia" del 1 al 5 (donde 5 es crítico/breaking news) basándote en el impacto global.
    3. Usa este formato exacto de salida en Markdown:
    
    🚨 *IMPORTANCIA: [Nivel]/5*
    
    📢 **[TITULAR TRADUCIDO EN MAYÚSCULAS]**
    
    [Traducción breve y clara del cuerpo o explicación del titular]
    
    🌐 **Etiquetas:** #[Tag1] #[Tag2]
    🕒 **Hora:** {timestamp}
    """

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt
    )
    
    return response.text

# --- Ejemplo de ejecución ---
noticia_json = {
    "headline": "Iran's Parliament Speaker Ghalibaf: Any current traffic through Strait is under our control. If US blockade continues, passage through the Strait of Hormuz will be restricted",
    "tags": ["Energy", "US", "Bonds", "US", "Indexes", "USD"],
    "timestamp_text": "00:21 Apr 19"
}

mensaje_para_telegram = procesar_noticia(noticia_json)
print(mensaje_para_telegram)