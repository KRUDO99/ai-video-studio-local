# AI Video Studio Local

Editor local de guion, escenas, recursos, locución, notas y exportación MP4 para Windows. El servidor escucha únicamente en `127.0.0.1`; los medios y la base SQLite se guardan en `%LOCALAPPDATA%\AI Video Studio Local`.

## Iniciar en Windows

1. Instala Python 3.11 y asegúrate de que `py -3.11` funcione.
2. Abre `run_desktop.bat` para la ventana nativa o `run_web.bat` para usar el servidor desde el navegador en `http://127.0.0.1:8765`.
3. El primer inicio crea un entorno virtual de la app e instala `requirements.txt`.
4. Si vas a usar un proveedor de pago, guarda su clave en **Ajustes → APIs**.

Los archivos de proyecto, escenas, voces generadas, imágenes, música, efectos y MP4 quedan en el almacenamiento local de Windows. Las claves se guardan con el almacén de credenciales de Windows (`keyring`).

## Flujo multimedia

- **Guion:** en **Pegar guion listo (Sin API)**, pega tus bloques y pulsa **Convertir en escenas del proyecto**. Cada bloque lleva el número de escena y los campos de locución y prompt.

  ```text
  ESCENA 1
  LOCUCIÓN: Todo empezó junto al río.
  PROMPT VISUAL: Puente de piedra al amanecer, plano general.

  ESCENA 2
  LOCUCIÓN: Aquella mañana nadie cruzó el puente.
  PROMPT VISUAL: Puente vacío cubierto por la niebla.
  ```

- **Prompts y estilos:** edita los prompts en su lista y copia uno o todos. El estilo activo se añade al copiar. Crea y selecciona presets en **Estilos**.
- **Recursos:** arrastra o selecciona las imágenes que generaste externamente. **Auto-asignar imágenes por orden** usa primero nombres como `escena_1.png` y `escena_2.png`. Ordena el resto por nombre y llena las escenas libres. Desde la galería puedes elegir manualmente la escena para cualquier imagen.
- **Escenas:** revisa las miniaturas, escucha o genera la voz individual y sube imágenes directamente a cada escena.
- **Audio:** Edge-TTS (`es-CO-SalomeNeural`) es la opción gratuita inicial. OpenAI TTS, ElevenLabs y Cartesia usan sus claves guardadas. La app mide cada archivo de voz, actualiza los tiempos por escena y concatena la narración. Un audio maestro subido se transcribe localmente con `faster-whisper` para sincronizar sus marcas de tiempo con el guion.
- **Voces pendientes:** usa el botón de la línea de tiempo para completar escenas que aún no tienen locución. Para pasar de un borrador a ElevenLabs, elige el proveedor y su ID de voz en **Audio** y pulsa **Generar o reemplazar todas las voces**.
- **Google Gemini TTS:** elige este proveedor en **Audio**, selecciona una voz y pulsa **Escuchar muestra**. La misma clave de Google Gemini en **Ajustes → Modelos de lenguaje** sirve para el guion y la voz. Elige la capa gratuita o de pago para calcular el costo estimado de tu proyecto. Google aplica la cuota y la tarifa de tu cuenta. En **Escenas** también puedes elegir Gemini y su voz para una sola escena.
- **Imágenes:** genera escenas individuales con una corrección, o genera las faltantes / todas con OpenAI Images o Replicate Flux Schnell. El prompt base del estilo se combina con el prompt visual y la narración. También se pueden seguir subiendo imágenes locales desde el inspector.
- **Audio de proyecto:** puedes cargar música, efectos de sonido y un logotipo PNG. El mezclador puede reducir música cuando habla la narración y aplica volumen, posición, opacidad y tamaño del logotipo al exportar.
- **El Repaso:** las notas se asocian al instante del MP4. “Aplicar notas” las procesa durante una nueva exportación. Se aplican instrucciones de paneo/zoom por escena y cambios reconocidos de volumen para narración, música y efectos de sonido en dB.
- **Exportación:** FFmpeg produce MP4 H.264/AAC a 1920×1080 (16:9) o 1080×1920 (9:16), con movimiento Ken Burns y cortes limpios entre escenas. Elige un archivo SRT aparte o subtítulos integrados. Los subtítulos siguen los tiempos de las escenas y los segmentos largos se reparten dentro de cada escena. `imageio-ffmpeg` provee un binario de respaldo si no existe `ffmpeg` en PATH.

La primera sincronización de audio maestro descarga el modelo `tiny` de faster-whisper en `%LOCALAPPDATA%\AI Video Studio Local\models`. Edge-TTS necesita conexión de red para solicitar audio a su servicio gratuito. Las opciones OpenAI, ElevenLabs, Cartesia, Replicate y los modelos LLM pueden consumir saldo.

Los ajustes de voz, mezcla y formato se guardan al cambiarlos. Los MP4 y archivos SRT quedan en `%LOCALAPPDATA%\AI Video Studio Local\media\projects\1\exports`, desde donde puedes abrirlos o copiarlos.

## Costos estimados

El contador suma tokens LLM por modelo, caracteres TTS, imágenes generadas y total estimado en USD. ElevenLabs se calcula por caracteres. Cartesia permite introducir la tarifa por 1000 caracteres de acuerdo con tu plan. Replicate Flux Schnell usa su tarifa pública por imagen; modelos alternos permiten estimación por tiempo de predicción. Las tarifas públicas, planes, región, calidad y descuentos pueden cambiar; el contador sirve para estimación, no sustituye el registro de facturación del proveedor.

## Verificación E2E

Con las dependencias instaladas, ejecuta:

```bat
"%LOCALAPPDATA%\AI Video Studio Local\venv\Scripts\python.exe" scripts\test_media_e2e.py
```

La comprobación usa un proyecto temporal para importar el guion, editar prompts, subir y asignar imágenes, generar voz con Edge-TTS y exportar MP4 horizontal con SRT y vertical con subtítulos integrados. También comprueba la persistencia de SQLite al volver a abrir el proyecto. Añade `--offline` al comando para sustituir Edge-TTS por audio local de prueba.

La prueba rápida de proveedores comprueba el WAV, el uso de la clave de Gemini, el cálculo de costo y la generación por escena y en lote. Usa respuestas de prueba de Gemini y ElevenLabs y solicita una locución real de Edge-TTS:

```bat
"%LOCALAPPDATA%\AI Video Studio Local\venv\Scripts\python.exe" scripts\test_voice_providers.py --edge-live
```

Google indica que `gemini-2.5-flash-preview-tts` se retira el 18 de diciembre de 2026. [Calendario de retirada de modelos](https://aistudio.google.com/docs/deprecations#audio-preview-models).

## APIs externas

- [OpenAI](https://developers.openai.com/api/docs/guides/text-to-speech) para guion, imágenes y voz.
- [ElevenLabs](https://elevenlabs.io/docs/api-reference/text-to-speech) y [Cartesia](https://docs.cartesia.ai/api-reference/tts/bytes) para voz.
- [Replicate](https://replicate.com/docs/reference/http) para imágenes Flux.
- [Anthropic](https://platform.claude.com/docs/en/api/messages) y [Gemini](https://ai.google.dev/api/generate-content) para guion.

## Empaquetado

`build_exe.bat` permanece disponible, pero no se ejecutó para esta verificación. Requiere WebView2 Runtime para la ventana nativa.
