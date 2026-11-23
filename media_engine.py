from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg
from mutagen import File as MutagenFile


class MediaEngineError(RuntimeError):
    pass


GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_DEFAULT_VOICE = "Kore"
GEMINI_TTS_VOICES = {
    "Puck": "Enérgica y juvenil",
    "Charon": "Profunda, para narración informativa",
    "Kore": "Clara y firme",
    "Fenrir": "Intensa, para relatos y documentales",
    "Aoede": "Ligera y natural",
    "Leda": "Juvenil y cálida",
}


def write_gemini_wav(data: bytes, mime: str, output: Path) -> float:
    """Wrap Gemini's signed little-endian PCM, or preserve a supplied PCM WAV."""
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data), "rb") as audio:
                frames = audio.getnframes()
                if not frames or len(audio.readframes(frames)) != frames * audio.getsampwidth() * audio.getnchannels():
                    raise wave.Error("WAV truncado")
                duration = frames / audio.getframerate()
        except (EOFError, ValueError, wave.Error) as exc:
            raise MediaEngineError("Gemini devolvió un WAV inválido.") from exc
        output.write_bytes(data)
        return duration
    parts = mime.lower().split(";")
    if parts[0].strip() != "audio/l16":
        raise MediaEngineError("Gemini devolvió un formato de audio no compatible.")
    params = dict(param.strip().split("=", 1) for param in parts[1:] if "=" in param)
    try:
        rate, channels = int(params.get("rate", 24000)), int(params.get("channels", 1))
    except ValueError as exc:
        raise MediaEngineError("Gemini devolvió parámetros PCM inválidos.") from exc
    if not data or not 8000 <= rate <= 192000 or channels not in {1, 2} or len(data) % (2 * channels):
        raise MediaEngineError("Gemini devolvió una pista PCM vacía o incompleta.")
    with wave.open(str(output), "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(data)
    return len(data) / (2 * channels * rate)


async def synthesize_gemini(text: str, output: Path, secret: str, voice: str, tier: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
        response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TTS_MODEL}:generateContent",
            headers={"x-goog-api-key": secret},
            json={
                "contents": [{"parts": [{"text": f"Lee en voz alta el siguiente texto, sin añadir ni traducir nada:\n\n{text}"}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
                },
            },
        )
        _raise_provider_error(response, "Google Gemini TTS")
    try:
        result = response.json()
        parts = result["candidates"][0]["content"]["parts"]
        audio_parts = [
            part["inlineData"] for part in parts
            if part.get("inlineData", {}).get("mimeType", "").lower().startswith("audio/")
        ]
        if not audio_parts:
            raise ValueError("No hay partes de audio")
        mime = audio_parts[0]["mimeType"]
        if any(part["mimeType"] != mime for part in audio_parts) or (len(audio_parts) > 1 and not mime.lower().startswith("audio/l16")):
            raise ValueError("Partes de audio incompatibles")
        data = b"".join(base64.b64decode(part["data"], validate=True) for part in audio_parts)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
        raise MediaEngineError("Gemini no devolvió audio utilizable. Revisa la cuota y el acceso al modelo en Google AI Studio.") from exc
    duration = await asyncio.to_thread(write_gemini_wav, data, mime, output)
    usage = result.get("usageMetadata") or {}
    input_tokens = usage.get("promptTokenCount", math.ceil(len(text) / 4))
    output_tokens = usage.get("candidatesTokenCount", math.ceil(duration * 25))
    cost = (input_tokens * .50 + output_tokens * 10) / 1_000_000 if tier == "paid" else 0.0
    return {"path": str(output), "duration": duration, "characters": len(text), "cost": round(cost, 8),
            "provider": "gemini", "model": GEMINI_TTS_MODEL, "voice": voice,
            "input_tokens": input_tokens, "output_tokens": output_tokens}


def ffmpeg_exe() -> str:
    """Return a usable FFmpeg binary, preferring PATH and falling back to imageio."""
    configured = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured and Path(configured).is_file():
        return configured
    on_path = shutil.which("ffmpeg")
    return on_path or imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg_run(args: list[str], timeout: int = 900, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [ffmpeg_exe(), "-hide_banner", "-y", *args]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
    if result.returncode:
        details = (result.stderr or result.stdout)[-3500:]
        raise MediaEngineError(f"FFmpeg terminó con error ({result.returncode}): {details}")
    return result


def audio_duration(path: str | Path) -> float:
    media = MutagenFile(str(path))
    if media is None or not getattr(media, "info", None) or not getattr(media.info, "length", None):
        raise MediaEngineError(f"No se pudo medir la duración del audio: {Path(path).name}")
    return round(float(media.info.length), 6)


async def synthesize_speech(
    provider: str,
    text: str,
    output_path: Path,
    *,
    secret: str | None = None,
    voice: str = "",
    model: str = "",
    cartesia_usd_per_1000_chars: float = 0,
    gemini_tier: str = "free",
) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise MediaEngineError("La escena no tiene narración.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "gemini":
        if not secret:
            raise MediaEngineError("Guarda la clave de Google Gemini en Ajustes → Modelos de lenguaje.")
        voice = voice or GEMINI_TTS_DEFAULT_VOICE
        if voice not in GEMINI_TTS_VOICES or model not in {"", GEMINI_TTS_MODEL}:
            raise MediaEngineError("Selecciona una voz y el modelo compatible con Google Gemini TTS.")
        return await synthesize_gemini(text, output_path, secret, voice, gemini_tier)
    if provider == "edge":
        try:
            import edge_tts
        except ImportError as exc:
            raise MediaEngineError("Edge-TTS no está instalado. Ejecuta run_web.bat para instalar los requisitos.") from exc
        voice = voice or "es-CO-SalomeNeural"
        communicate = edge_tts.Communicate(text, voice, rate="+0%", pitch="+0Hz")
        await communicate.save(str(output_path))
        rate = 0.0
    elif provider == "openai":
        if not secret:
            raise MediaEngineError("Guarda la API key de OpenAI en Ajustes → APIs.")
        voice = voice or "coral"
        model = model or "gpt-4o-mini-tts"
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {secret}"},
                json={"model": model, "voice": voice, "input": text, "response_format": "mp3"},
            )
            _raise_provider_error(response, "OpenAI TTS")
            output_path.write_bytes(response.content)
        rate = 0.015 / 60.0
    elif provider == "elevenlabs":
        if not secret:
            raise MediaEngineError("Guarda la API key de ElevenLabs en Ajustes → APIs.")
        if not voice:
            raise MediaEngineError("Indica el ID de una voz de ElevenLabs en el campo Voz.")
        model = model or "eleven_multilingual_v2"
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                headers={"xi-api-key": secret, "accept": "audio/mpeg", "content-type": "application/json"},
                params={"output_format": "mp3_44100_128"},
                json={"text": text, "model_id": model},
            )
            _raise_provider_error(response, "ElevenLabs")
            output_path.write_bytes(response.content)
        rate = 0.10 / 1000.0
    elif provider == "cartesia":
        if not secret:
            raise MediaEngineError("Guarda la API key de Cartesia en Ajustes → APIs.")
        if not voice:
            raise MediaEngineError("Indica el ID de una voz de Cartesia en el campo Voz.")
        model = model or "sonic-3"
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            response = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={"Authorization": f"Bearer {secret}", "Cartesia-Version": "2026-08-14", "Content-Type": "application/json"},
                json={
                    "model_id": model,
                    "transcript": text,
                    "voice": {"mode": "id", "id": voice},
                    "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
                },
            )
            _raise_provider_error(response, "Cartesia")
            output_path.write_bytes(response.content)
        # Cartesia credits are plan-dependent; the UI exposes the account's per-credit rate.
        rate = max(0.0, float(cartesia_usd_per_1000_chars)) / 1000.0
    else:
        raise MediaEngineError("Proveedor de voz no reconocido.")
    if not output_path.is_file() or output_path.stat().st_size < 100:
        raise MediaEngineError(f"{provider} no devolvió un archivo de audio válido.")
    duration = audio_duration(output_path)
    estimated_cost = len(text) * rate if provider in {"elevenlabs", "cartesia"} else duration * rate
    return {"path": str(output_path), "duration": duration, "characters": len(text), "cost": round(estimated_cost, 8), "provider": provider, "model": model, "voice": voice}


def _raise_provider_error(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    try:
        body = response.json()
        message = body.get("detail") or body.get("error", {}).get("message") or body.get("message") or response.text
    except Exception:
        message = response.text
    raise MediaEngineError(f"{provider} respondió HTTP {response.status_code}: {str(message)[:700]}")


async def generate_image(
    provider: str,
    prompt: str,
    output_path: Path,
    *,
    secret: str,
    model: str = "",
    aspect_ratio: str = "16:9",
    source_image: Path | None = None,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=15.0), follow_redirects=True) as client:
        if provider == "openai":
            model = model or "gpt-image-2.5-flare"
            if source_image and source_image.is_file():
                data = {"model": model, "prompt": prompt, "size": "1536x1024" if aspect_ratio == "16:9" else "1024x1536"}
                with source_image.open("rb") as image_file:
                    response = await client.post(
                        "https://api.openai.com/v1/images/edits",
                        headers={"Authorization": f"Bearer {secret}"},
                        data=data,
                        files={"image": (source_image.name, image_file, "application/octet-stream")},
                    )
            else:
                response = await client.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {secret}"},
                    json={"model": model, "prompt": prompt, "size": "1536x1024" if aspect_ratio == "16:9" else "1024x1536", "quality": "medium", "output_format": "png", "n": 1},
                )
            _raise_provider_error(response, "OpenAI Images")
            result = response.json()
            item = result.get("data", [{}])[0]
            image_url = item.get("url")
            if item.get("b64_json"):
                output_path.write_bytes(base64.b64decode(item["b64_json"]))
            elif image_url:
                downloaded = await client.get(image_url)
                _raise_provider_error(downloaded, "OpenAI Images download")
                output_path.write_bytes(downloaded.content)
            else:
                raise MediaEngineError("OpenAI no devolvió datos de imagen.")
            usage = result.get("usage", {})
            # Use metered tokens when present; otherwise estimate a standard 1536px generation.
            input_details = usage.get("input_tokens_details", {})
            output_details = usage.get("output_tokens_details", {})
            image_output_tokens = int(output_details.get("image_tokens", 0))
            text_input_tokens = int(input_details.get("text_tokens", usage.get("input_tokens", 0)))
            image_input_tokens = int(input_details.get("image_tokens", 0))
            if "gpt-image" in model and image_output_tokens:
                cost = (image_output_tokens * 30 + text_input_tokens * 5 + image_input_tokens * 8) / 1_000_000
            else:
                cost = 0.08 if "gpt-image" in model else 0.0
        elif provider == "replicate":
            model = model or "black-forest-labs/flux-schnell"
            owner, model_name = model.split("/", 1) if "/" in model else ("black-forest-labs", model)
            response = await client.post(
                f"https://api.replicate.com/v1/models/{owner}/{model_name}/predictions",
                headers={"Authorization": f"Bearer {secret}", "Prefer": "wait=60"},
                json={"input": {"prompt": prompt, "aspect_ratio": aspect_ratio, "output_format": "png", "num_outputs": 1}},
            )
            _raise_provider_error(response, "Replicate")
            result = response.json()
            prediction_id = result.get("id")
            deadline = time.monotonic() + 240
            while result.get("status") not in {"succeeded", "failed", "canceled"}:
                if time.monotonic() > deadline:
                    raise MediaEngineError("Replicate tardó demasiado en completar la imagen.")
                await asyncio.sleep(2)
                poll = await client.get(f"https://api.replicate.com/v1/predictions/{prediction_id}", headers={"Authorization": f"Bearer {secret}"})
                _raise_provider_error(poll, "Replicate")
                result = poll.json()
            if result.get("status") != "succeeded":
                raise MediaEngineError(f"Replicate terminó la predicción con estado {result.get('status')}: {result.get('error') or ''}")
            output = result.get("output")
            image_url = output[0] if isinstance(output, list) and output else output
            if not isinstance(image_url, str):
                raise MediaEngineError("Replicate no devolvió un enlace de imagen.")
            downloaded = await client.get(image_url)
            _raise_provider_error(downloaded, "Replicate image download")
            output_path.write_bytes(downloaded.content)
            metrics = result.get("metrics", {})
            predict_seconds = float(metrics.get("predict_time") or 0)
            # Flux Schnell is publicly listed as $0.003/output; other models are estimated from prediction time.
            cost = 0.003 if model_name == "flux-schnell" else max(0.0, predict_seconds * float(os.environ.get("AI_VIDEO_REPLICATE_USD_PER_SECOND", "0.0005")))
            usage = {"predict_time": predict_seconds}
        else:
            raise MediaEngineError("Proveedor de imagen no reconocido.")
    if not output_path.is_file() or output_path.stat().st_size < 256:
        raise MediaEngineError("El proveedor no devolvió un archivo de imagen válido.")
    return {"path": str(output_path), "provider": provider, "model": model, "cost": round(cost, 6), "usage": usage}


def join_audio(inputs: list[str | Path], output_path: Path) -> float:
    if not inputs:
        raise MediaEngineError("No hay locuciones de escena para unir.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command: list[str] = []
    for item in inputs:
        command += ["-i", str(item)]
    filters = "".join(f"[{index}:a]aresample=48000,asetpts=PTS-STARTPTS[a{index}];" for index in range(len(inputs)))
    filters += "".join(f"[a{index}]" for index in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[out]"
    ffmpeg_run([*command, "-filter_complex", filters, "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000", str(output_path)])
    return audio_duration(output_path)


def normalize_voice(source: Path, output: Path) -> float:
    ffmpeg_run(["-v", "error", "-i", str(source), "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                "-c:a", "pcm_s16le", str(output)])
    with wave.open(str(output), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def assemble_scene_audio(scenes: list[dict[str, Any]], output: Path) -> None:
    """Put each clip at its exact scene offset, with silence for pending clips."""
    output.parent.mkdir(parents=True, exist_ok=True)
    rate = 48000
    with tempfile.TemporaryDirectory() as scratch, wave.open(str(output), "wb") as track:
        track.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        converted = Path(scratch) / "clip.wav"
        for scene in scenes:
            samples = round(scene["end_seconds"] * rate) - round(scene["start_seconds"] * rate)
            path = Path(scene["tts_path"])
            if samples > 0 and path.is_file():
                if path.suffix.lower() != ".wav":
                    normalize_voice(path, converted)
                    path = converted
                with wave.open(str(path), "rb") as clip:
                    while samples > 0 and (data := clip.readframes(min(samples, rate))):
                        track.writeframesraw(data)
                        samples -= len(data) // 2
            while samples > 0:
                size = min(samples, rate)
                track.writeframesraw(bytes(size * 2))
                samples -= size


def write_scene_subtitles(scenes: list[dict[str, Any]], output: Path) -> None:
    def timestamp(seconds: float) -> str:
        whole, millis = divmod(max(0, round(seconds * 1000)), 1000)
        hours, remainder = divmod(whole, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    # Short cues within a long scene keep the captions readable in vertical video.
    from textwrap import wrap

    cues = []
    for scene in scenes:
        text = " ".join(scene["narration"].split()).replace("<", "‹").replace(">", "›").replace("{", "(").replace("}", ")")
        lines = wrap(text, width=37, break_long_words=False, break_on_hyphens=False)
        captions = ["\n".join(lines[index:index + 2]) for index in range(0, len(lines), 2)]
        total_words = len(text.split())
        start, end = scene["start_seconds"], scene["end_seconds"]
        words = 0
        for caption in captions:
            cue_start = start + (end - start) * words / total_words
            words += len(caption.split())
            cue_end = start + (end - start) * words / total_words
            cues.append(f"{len(cues) + 1}\n{timestamp(cue_start)} --> {timestamp(cue_end)}\n{caption}\n")
    output.write_text("\n".join(cues), encoding="utf-8")


def transcribe_word_times(audio_path: str | Path) -> list[dict[str, Any]]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    os.environ.setdefault("HF_HOME", str(local_app_data / "AI Video Studio Local" / "models"))
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MediaEngineError("faster-whisper no está instalado; reinicia con run_web.bat para instalarlo.") from exc
    try:
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path), word_timestamps=True, vad_filter=True)
        words: list[dict[str, Any]] = []
        for segment in segments:
            if segment.words:
                for word in segment.words:
                    words.append({"text": word.word.strip(), "start": float(word.start), "end": float(word.end)})
            else:
                parts = segment.text.split()
                if parts:
                    duration = max(0.01, float(segment.end) - float(segment.start))
                    for index, part in enumerate(parts):
                        start = float(segment.start) + duration * index / len(parts)
                        end = float(segment.start) + duration * (index + 1) / len(parts)
                        words.append({"text": part, "start": start, "end": end})
        if not words:
            raise MediaEngineError("El audio no contiene voz reconocible para sincronizar.")
        return words
    except MediaEngineError:
        raise
    except Exception as exc:
        raise MediaEngineError(f"No se pudo transcribir el audio maestro: {exc}") from exc


def align_scenes_to_words(scenes: list[dict[str, Any]], word_times: list[dict[str, Any]], total_duration: float) -> list[tuple[float, float]]:
    if not scenes or not word_times:
        raise MediaEngineError("Se necesitan escenas y palabras reconocidas para sincronizar.")
    total_scene_words = sum(max(1, len(str(scene["narration"]).split())) for scene in scenes)
    timings = []
    cursor = 0.0
    consumed = 0
    for index, scene in enumerate(scenes):
        count = max(1, len(str(scene["narration"]).split()))
        consumed += count
        if index == len(scenes) - 1:
            end = total_duration
        else:
            word_index = min(len(word_times) - 1, round(len(word_times) * consumed / total_scene_words) - 1)
            end = float(word_times[max(0, word_index)]["end"])
            end = max(cursor + 0.08, min(total_duration, end))
        timings.append((round(cursor, 4), round(end, 4)))
        cursor = end
    return timings


def apply_review_notes(
    notes: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    voice_gain_db: float,
    music_gain_db: float,
    sfx_gain_db: float,
) -> tuple[float, float, float, list[int]]:
    applied: list[int] = []
    updated_voice_gain = float(voice_gain_db)
    updated_music_gain = float(music_gain_db)
    updated_sfx_gain = float(sfx_gain_db)
    for note in notes:
        text = str(note.get("note", "")).lower()
        db_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:db|decibel(?:es)?)", text)
        is_lower = any(word in text for word in ("baja", "bajar", "reduce", "reducir"))
        is_raise = any(word in text for word in ("sube", "subir", "aumenta", "aumentar"))
        if db_match and (is_lower or is_raise):
            amount = float(db_match.group(1).replace(",", "."))
            delta = -amount if is_lower else amount
            if any(token in text for token in ("voz", "narrador", "narración", "narracion", "voice")):
                updated_voice_gain = max(-60.0, min(12.0, updated_voice_gain + delta))
                applied.append(int(note["id"]))
                continue
            if any(token in text for token in ("música", "musica", "fondo", "music")):
                updated_music_gain = max(-60.0, min(12.0, updated_music_gain + delta))
                applied.append(int(note["id"]))
                continue
            if any(token in text for token in ("efecto", "golpe", "máquina de escribir", "maquina de escribir", "sfx", "sonido")):
                updated_sfx_gain = max(-60.0, min(12.0, updated_sfx_gain + delta))
                applied.append(int(note["id"]))
                continue
        scene = next((item for item in scenes if item.get("id") == note.get("scene_id")), None)
        if scene and any(token in text for token in ("paneo", "panorámica", "panoramica", "zoom")):
            if "derecha" in text:
                scene["motion"] = "pan_right"
            elif "izquierda" in text:
                scene["motion"] = "pan_left"
            elif "alejar" in text or "zoom out" in text:
                scene["motion"] = "zoom_out"
            else:
                scene["motion"] = "zoom_in"
            applied.append(int(note["id"]))
    return updated_voice_gain, updated_music_gain, updated_sfx_gain, applied


def render_scene_segments(scenes, width, height, fps, staging):
    cursor_frames = 0
    encoding = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"]
    with (staging / "scenes.txt").open("w", encoding="ascii") as playlist:
        for index, scene in enumerate(scenes):
            end_frame = max(cursor_frames + 1, round(scene["end_seconds"] * fps))
            frames = end_frame - cursor_frames
            cursor_frames = end_frame
            progress = f"(1-cos(PI*on/{max(1, frames - 1)}))/2"
            motion = scene.get("motion") or ("zoom_in" if index % 2 == 0 else "zoom_out")
            if motion == "pan_left":
                zoom, x = "1.12", f"(iw-iw/zoom)*(1-({progress}))"
            elif motion == "pan_right":
                zoom, x = "1.12", f"(iw-iw/zoom)*({progress})"
            else:
                zoom = f"1.08-0.06*({progress})" if motion == "zoom_out" else f"1+0.06*({progress})"
                x = "iw/2-iw/zoom/2"
            filters = (
                f"scale={2 * width}:{2 * height}:force_original_aspect_ratio=increase,"
                f"crop={2 * width}:{2 * height},"
                f"zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d={frames}:s={width}x{height}:fps={fps},setsar=1"
            )
            segment = f"scene-{index:04}.mp4"
            ffmpeg_run(["-v", "error", "-i", str(Path(scene["image_path"]).resolve()), "-vf", filters, "-an",
                        "-frames:v", str(frames), *encoding, segment],
                       timeout=max(300, frames // fps * 25), cwd=staging)
            playlist.write(f"file '{segment}'\n")
    return cursor_frames / fps


def render_mp4(
    scenes: list[dict[str, Any]],
    output_path: Path,
    *,
    aspect_ratio: str = "16:9",
    voice_path: str | Path | None = None,
    music_path: str | Path | None = None,
    logo_path: str | Path | None = None,
    sound_effects: list[dict[str, Any]] | None = None,
    voice_gain_db: float = 0,
    music_gain_db: float = -15,
    sfx_gain_db: float = 0,
    music_ducking: bool = True,
    logo_position: str = "bottom_right",
    logo_scale: float = 0.14,
    logo_opacity: float = 0.85,
    size: tuple[int, int] | None = None,
    fps: int = 30,
    subtitle_mode: str = "none",
) -> dict[str, Any]:
    if not scenes:
        raise MediaEngineError("El proyecto no tiene escenas para exportar.")
    width, height = size or ((1920, 1080) if aspect_ratio == "16:9" else (1080, 1920))
    for scene in scenes:
        image_path = Path(str(scene.get("image_path") or ""))
        if not image_path.is_file():
            raise MediaEngineError(f"Falta la imagen local de la escena {scene.get('order_index')}. Genera o sube sus imágenes antes de exportar.")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="studio-render-", dir=output_path.parent) as scratch:
        staging = Path(scratch)
        total_duration = render_scene_segments(scenes, width, height, fps, staging)
        subtitle_path = staging / "captions.srt"
        if subtitle_mode != "none":
            write_scene_subtitles(scenes, subtitle_path)
        mix_video(staging, total_duration, width, voice_path, music_path, logo_path, sound_effects,
                  voice_gain_db, music_gain_db, sfx_gain_db, music_ducking, logo_position, logo_scale,
                  logo_opacity, subtitle_mode)
        rendered = staging / "output.mp4"
        if not rendered.is_file() or rendered.stat().st_size < 2048:
            raise MediaEngineError("FFmpeg no generó un MP4 utilizable.")
        ffmpeg_run(["-v", "error", "-i", str(rendered), "-f", "null", "-"], timeout=max(300, int(total_duration * 15)))
        suffix_path = output_path.with_suffix(".srt") if subtitle_mode == "srt" else None
        if suffix_path:
            subtitle_path.replace(suffix_path)
        rendered.replace(output_path)
    return {"path": str(output_path), "duration": round(total_duration, 3), "width": width, "height": height,
            "bytes": output_path.stat().st_size, "ffmpeg": ffmpeg_exe(),
            "subtitle_path": str(suffix_path) if suffix_path else ""}


def mix_video(staging, total_duration, width, voice_path, music_path, logo_path, sound_effects,
              voice_gain_db, music_gain_db, sfx_gain_db, music_ducking, logo_position, logo_scale,
              logo_opacity, subtitle_mode):
    args = ["-f", "concat", "-safe", "1", "-i", "scenes.txt"]
    voice_index = None
    if voice_path and Path(voice_path).is_file():
        voice_index = args.count("-i")
        args.extend(["-i", str(Path(voice_path).resolve())])
    music_index = None
    if music_path and Path(music_path).is_file():
        music_index = args.count("-i")
        args.extend(["-stream_loop", "-1", "-i", str(Path(music_path).resolve())])
    effects = [item for item in (sound_effects or []) if item.get("active", True) and Path(str(item.get("audio_path", ""))).is_file()]
    effect_indices = []
    for effect in effects:
        effect_indices.append(args.count("-i"))
        args.extend(["-i", str(Path(effect["audio_path"]).resolve())])
    logo_index = None
    if logo_path and Path(logo_path).is_file():
        logo_index = args.count("-i")
        args.extend(["-i", str(Path(logo_path).resolve())])
    filters: list[str] = []
    video_map = "0:v:0"
    if logo_index is not None:
        if logo_position == "top_left":
            position = "x=24:y=24"
        elif logo_position == "top_right":
            position = "x=W-w-24:y=24"
        elif logo_position == "bottom_left":
            position = "x=24:y=H-h-24"
        else:
            position = "x=W-w-24:y=H-h-24"
        filters.append(f"[{logo_index}:v]scale={round(width * logo_scale)}:-1,format=rgba,colorchannelmixer=aa={logo_opacity}[logo]")
        filters.append(f"[{video_map}][logo]overlay={position}:format=auto[branding]")
        video_map = "branding"
    if subtitle_mode == "burn":
        filters.append(f"[{video_map}]subtitles=filename=captions.srt:force_style='FontName=Arial,FontSize=18,Outline=2,MarginV=22'[captioned]")
        video_map = "captioned"
    audio_map = None
    audio_terms: list[str] = []
    voice_label = ""
    if voice_index is not None:
        filters.append(f"[{voice_index}:a]aresample=48000,apad,atrim=duration={total_duration:.6f},asetpts=PTS-STARTPTS,volume={math.pow(10, voice_gain_db / 20):.6f}[voicebase]")
        if music_index is not None and music_ducking:
            filters.append("[voicebase]asplit=2[voice][voicekey]")
            voice_label = "[voice]"
            sidechain_label = "[voicekey]"
        else:
            voice_label = "[voicebase]"
            sidechain_label = "[voicebase]"
        audio_terms.append(voice_label)
    if music_index is not None:
        filters.append(f"[{music_index}:a]aresample=48000,volume={math.pow(10, music_gain_db / 20):.6f}[musicraw]")
        if voice_index is not None and music_ducking:
            filters.append(f"[musicraw]{sidechain_label}sidechaincompress=threshold=0.035:ratio=8:attack=20:release=450[duckedmusic]")
            audio_terms.append("[duckedmusic]")
        else:
            audio_terms.append("[musicraw]")
    for index, effect in zip(effect_indices, effects):
        delay = max(0, int(float(effect.get("start_seconds", 0)) * 1000))
        effect_db = float(effect.get("gain_db", 0)) + sfx_gain_db
        label = f"sfx{index}"
        filters.append(f"[{index}:a]aresample=48000,volume={math.pow(10, effect_db / 20):.6f},adelay={delay}|{delay},atrim=duration={total_duration:.6f},asetpts=PTS-STARTPTS[{label}]")
        audio_terms.append(f"[{label}]")
    if audio_terms:
        if len(audio_terms) == 1:
            filters.append(f"{audio_terms[0]}atrim=duration={total_duration:.6f},asetpts=PTS-STARTPTS[aout]")
        else:
            filters.append("".join(audio_terms) + f"amix=inputs={len(audio_terms)}:normalize=0,alimiter=limit=0.95:level=0:latency=1,atrim=duration={total_duration:.6f},asetpts=PTS-STARTPTS[aout]")
        audio_map = "[aout]"
    command = list(args)
    if filters:
        command += ["-filter_complex", ";".join(filters)]
    command += ["-map", f"[{video_map}]" if video_map != "0:v:0" else video_map]
    if audio_map:
        command += ["-map", audio_map, "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-an"]
    command += ["-c:v", "copy"] if video_map == "0:v:0" else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"]
    command += ["-movflags", "+faststart", "-t", f"{total_duration:.6f}", "output.mp4"]
    ffmpeg_run(command, timeout=max(600, int(total_duration * 25)), cwd=staging)
