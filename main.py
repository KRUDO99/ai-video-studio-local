from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import keyring
import keyring.errors
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

from media_engine import (
    GEMINI_TTS_DEFAULT_VOICE,
    GEMINI_TTS_MODEL,
    GEMINI_TTS_VOICES,
    MediaEngineError,
    align_scenes_to_words,
    apply_review_notes,
    audio_duration,
    generate_image,
    assemble_scene_audio,
    normalize_voice,
    render_mp4,
    synthesize_speech,
    transcribe_word_times,
    write_scene_subtitles,
)


APP_NAME = "AI Video Studio Local"
ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
DATA_DIR = LOCAL_APP_DATA / APP_NAME
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "studio.sqlite3"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

APP_TOKEN = secrets.token_urlsafe(32)
KEYRING_SERVICE = "ai-video-studio-local"
HTTP_TIMEOUT = httpx.Timeout(18.0, connect=7.0)

PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "category": "Guion · imágenes · voz",
        "secret_label": "API key",
        "testable": True,
        "note": "Modelos, generación de guion, imágenes y voz.",
        "link": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic · Claude",
        "category": "Guion · prompts",
        "secret_label": "API key",
        "testable": True,
        "note": "Consulta modelos y genera guiones desde la pestaña Guion.",
        "link": "https://console.anthropic.com/settings/keys",
    },
    "gemini": {
        "label": "Google · Gemini",
        "category": "Guion · voz",
        "secret_label": "API key",
        "testable": True,
        "note": "Usa esta misma clave para generar guiones y voz con Google Gemini TTS.",
        "link": "https://aistudio.google.com/app/apikey",
        "speech": {"label": "Google Gemini TTS", "model": GEMINI_TTS_MODEL,
                   "default_voice": GEMINI_TTS_DEFAULT_VOICE, "voices": GEMINI_TTS_VOICES},
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "category": "Voz en off · SFX",
        "secret_label": "API key",
        "testable": True,
        "note": "La prueba consulta el catálogo de modelos sin generar audio.",
        "link": "https://elevenlabs.io/app/settings/api-keys",
    },
    "cartesia": {
        "label": "Cartesia",
        "category": "Voz en off",
        "secret_label": "API key",
        "testable": True,
        "note": "La prueba consulta voces disponibles; no crea audio.",
        "link": "https://play.cartesia.ai/",
    },
    "stability": {
        "label": "Stability AI",
        "category": "Imágenes",
        "secret_label": "API key",
        "testable": False,
        "note": "Clave guardada. Esta versión no envía pruebas que puedan consumir créditos y aún no activa la generación de imágenes.",
        "link": "https://platform.stability.ai/account/keys",
    },
    "replicate": {
        "label": "Replicate · Flux",
        "category": "Imágenes",
        "secret_label": "API token",
        "testable": True,
        "note": "La prueba consulta la cuenta sin crear una predicción.",
        "link": "https://replicate.com/account/api-tokens",
    },
    "jamendo": {
        "label": "Jamendo",
        "category": "Música de fondo",
        "secret_label": "Client ID",
        "testable": True,
        "note": "La prueba consulta una pista de muestra del catálogo.",
        "link": "https://developer.jamendo.com/v3.0",
    },
    "freesound": {
        "label": "Freesound",
        "category": "Efectos de sonido",
        "secret_label": "API key",
        "testable": True,
        "note": "La prueba busca un sonido de muestra sin descargar archivos.",
        "link": "https://freesound.org/apiv2/apply",
    },
}

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def local_only_and_app_token(request: Request, call_next):
    peer = request.client.host if request.client else ""
    if peer not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return HTMLResponse("Solo se permiten conexiones desde este equipo.", status_code=403)
    if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("x-local-app-token") != APP_TOKEN:
            return HTMLResponse("Token de aplicación inválido.", status_code=403)
        if any(job["project_job"] and job["status"] == "running" for job in JOBS.values()):
            return JSONResponse({"detail": "Espera a que termine la tarea del proyecto en curso."}, status_code=409)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def media_url(path_value: str | None) -> str:
    if not path_value:
        return ""
    try:
        relative = Path(path_value).resolve().relative_to(MEDIA_DIR.resolve()).as_posix()
    except (OSError, ValueError):
        return ""
    return "/media/" + quote(relative)


def project_media_dir(project_id: int = 1) -> Path:
    result = MEDIA_DIR / "projects" / str(project_id)
    result.mkdir(parents=True, exist_ok=True)
    return result


def add_usage(provider: str, model: str, kind: str, units: float, cost: float, *, input_tokens: int = 0, output_tokens: int = 0, project_id: int = 1) -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO usage_events(project_id,provider,model,kind,units,input_tokens,output_tokens,estimated_cost,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, provider, model or "", kind, units, input_tokens, output_tokens, cost, utc_now()),
        )


def llm_cost_estimate(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    name = model.lower()
    if provider == "openai":
        if "gpt-6-astra" in name:
            in_rate, out_rate = 10.00, 50.00
        elif "gpt-6.1-sol" in name:
            in_rate, out_rate = 2.00, 10.00
        elif "gpt-6.1-terra" in name:
            in_rate, out_rate = 1.00, 5.00
        elif "gpt-6-luna" in name:
            in_rate, out_rate = 0.10, 0.50
        elif "gpt-5.6-luna" in name:
            in_rate, out_rate = 0.20, 1.20
        elif "gpt-5.6" in name or name == "gpt-5.6":
            in_rate, out_rate = 4.00, 20.00
        else:
            in_rate, out_rate = 0.50, 2.50
    elif provider == "anthropic":
        if "haiku 5.5" in name or "haiku-5-5" in name:
            in_rate, out_rate = 0.05, 0.25
        elif "sonnet 5.5" in name or "sonnet-5-5" in name:
            in_rate, out_rate = 2.00, 10.00
        elif "sonnet 5" in name or "sonnet-5" in name:
            in_rate, out_rate = 1.00, 5.00
        elif "haiku" in name:
            in_rate, out_rate = 0.50, 2.50
        elif "fable 5.2" in name or "fable-5-2" in name:
            in_rate, out_rate = 8.00, 40.00
        elif "opus 5.5" in name or "opus-5-5" in name:
            in_rate, out_rate = 4.00, 20.00
        elif "opus" in name:
            in_rate, out_rate = 2.50, 12.50
        else:
            in_rate, out_rate = 1.50, 7.50
    else:
        if "flash-lite" in name or "flash_lite" in name:
            in_rate, out_rate = 0.25, 1.50
        elif "3.8-flash" in name or "3.8_flash" in name:
            in_rate, out_rate = 0.75, 3.75
        elif "3.1-pro" in name or "3.1_pro" in name:
            in_rate, out_rate = 2.00, 12.00
        elif "flash" in name:
            in_rate, out_rate = 0.50, 3.00
        elif "pro" in name:
            in_rate, out_rate = 1.25, 10.00
        else:
            in_rate, out_rate = 0.75, 3.75
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 8)


def initialize_database() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                aspect_ratio TEXT NOT NULL DEFAULT '16:9',
                duration_seconds REAL NOT NULL DEFAULT 79,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                audio_path TEXT NOT NULL DEFAULT '',
                music_path TEXT NOT NULL DEFAULT '',
                logo_path TEXT NOT NULL DEFAULT '',
                voice_provider TEXT NOT NULL DEFAULT 'edge',
                voice_model TEXT NOT NULL DEFAULT '',
                voice_name TEXT NOT NULL DEFAULT 'es-CO-SalomeNeural',
                image_provider TEXT NOT NULL DEFAULT 'openai',
                image_model TEXT NOT NULL DEFAULT 'gpt-image-2.5-flare',
                voice_gain_db REAL NOT NULL DEFAULT 0,
                music_gain_db REAL NOT NULL DEFAULT -15,
                sfx_gain_db REAL NOT NULL DEFAULT 0,
                music_ducking INTEGER NOT NULL DEFAULT 1,
                logo_position TEXT NOT NULL DEFAULT 'bottom_right',
                logo_scale REAL NOT NULL DEFAULT 0.14,
                logo_opacity REAL NOT NULL DEFAULT 0.85,
                cartesia_usd_per_1000_chars REAL NOT NULL DEFAULT 0,
                last_export_path TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS styles (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                style_prompt TEXT NOT NULL DEFAULT '',
                tone TEXT NOT NULL DEFAULT '',
                default_voice TEXT NOT NULL DEFAULT '',
                images_per_minute INTEGER NOT NULL DEFAULT 24,
                reference_images_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS image_assets (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                order_index INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                narration TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                place TEXT NOT NULL DEFAULT '',
                era TEXT NOT NULL DEFAULT '',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                match_score INTEGER NOT NULL DEFAULT 0,
                visual_prompt TEXT NOT NULL DEFAULT '',
                image_path TEXT NOT NULL DEFAULT '',
                tts_path TEXT NOT NULL DEFAULT '',
                motion TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS review_notes (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                scene_id INTEGER REFERENCES scenes(id) ON DELETE SET NULL,
                time_seconds REAL NOT NULL,
                note TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendiente',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_costs (
                project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                image_cost REAL NOT NULL DEFAULT 0,
                voice_cost REAL NOT NULL DEFAULT 0,
                llm_tokens INTEGER NOT NULL DEFAULT 0,
                llm_cost REAL NOT NULL DEFAULT 0,
                llm_input_tokens INTEGER NOT NULL DEFAULT 0,
                llm_output_tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sound_effects (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                audio_path TEXT NOT NULL,
                start_seconds REAL NOT NULL DEFAULT 0,
                gain_db REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                units REAL NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        migrations = {
            "projects": {
                "audio_path": "TEXT NOT NULL DEFAULT ''", "music_path": "TEXT NOT NULL DEFAULT ''", "logo_path": "TEXT NOT NULL DEFAULT ''",
                "voice_provider": "TEXT NOT NULL DEFAULT 'edge'", "voice_model": "TEXT NOT NULL DEFAULT ''", "voice_name": "TEXT NOT NULL DEFAULT 'es-CO-SalomeNeural'",
                "image_provider": "TEXT NOT NULL DEFAULT 'openai'", "image_model": "TEXT NOT NULL DEFAULT 'gpt-image-2.5-flare'",
                "voice_gain_db": "REAL NOT NULL DEFAULT 0", "music_gain_db": "REAL NOT NULL DEFAULT -15", "sfx_gain_db": "REAL NOT NULL DEFAULT 0",
                "music_ducking": "INTEGER NOT NULL DEFAULT 1", "logo_position": "TEXT NOT NULL DEFAULT 'bottom_right'", "logo_scale": "REAL NOT NULL DEFAULT 0.14",
                "logo_opacity": "REAL NOT NULL DEFAULT 0.85", "cartesia_usd_per_1000_chars": "REAL NOT NULL DEFAULT 0", "last_export_path": "TEXT NOT NULL DEFAULT ''",
                "style_id": "INTEGER REFERENCES styles(id)", "last_subtitle_path": "TEXT NOT NULL DEFAULT ''",
                "subtitle_mode": "TEXT NOT NULL DEFAULT 'none'",
                "gemini_tts_tier": "TEXT NOT NULL DEFAULT 'free'",
            },
            "scenes": {"tts_path": "TEXT NOT NULL DEFAULT ''", "motion": "TEXT NOT NULL DEFAULT ''",
                       "tts_duration": "REAL NOT NULL DEFAULT 0", "tts_provider": "TEXT NOT NULL DEFAULT ''"},
            "project_costs": {"llm_cost": "REAL NOT NULL DEFAULT 0", "llm_input_tokens": "INTEGER NOT NULL DEFAULT 0", "llm_output_tokens": "INTEGER NOT NULL DEFAULT 0"},
        }
        for table, columns in migrations.items():
            known = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in known:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        for scene in connection.execute("SELECT project_id, order_index, image_path FROM scenes WHERE image_path<>''").fetchall():
            connection.execute("INSERT OR IGNORE INTO image_assets(project_id,name,path) VALUES(?,?,?)",
                               (scene["project_id"], f"Escena {scene['order_index']} · {Path(scene['image_path']).name}", scene["image_path"]))
        exists = connection.execute("SELECT id FROM projects LIMIT 1").fetchone()
        if exists:
            return
        now = utc_now()
        cursor = connection.execute(
            "INSERT INTO projects(title,description,aspect_ratio,duration_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("youtube stickman", "Historia ilustrada · borrador local", "16:9", 79.07, now, now),
        )
        project_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO styles(name,style_prompt,tone,default_voice,images_per_minute) VALUES(?,?,?,?,?)",
            ("Cómic web · Stickman", "Ilustración 2D de cómic web, líneas marcadas, color cálido y continuidad del personaje", "Narración clara y dinámica", "Edge-TTS · es-CO-SalomeNeural", 23),
        )
        connection.execute("INSERT INTO project_costs(project_id) VALUES(?)", (project_id,))
        narration = [
            "Hoy vamos a descubrir por qué una idea pequeña puede cambiarlo todo.",
            "La historia comienza en una ciudad que crecía junto al río.",
            "Los primeros comerciantes llevaban sus productos en carretas.",
            "Al caer la tarde, la plaza se llenaba de voces y movimiento.",
            "Cada viaje traía noticias, herramientas y nuevas oportunidades.",
            "Pero el camino también tenía riesgos que nadie podía ignorar.",
            "La lluvia transformaba la ruta en barro y ralentizaba el viaje.",
            "Entonces apareció una solución que parecía demasiado sencilla.",
            "Un puente conectó los dos lados y acortó horas de recorrido.",
            "La gente volvió a encontrarse y el mercado empezó a crecer.",
            "Los pequeños talleres se convirtieron en negocios familiares.",
            "Con el tiempo, llegaron más viajeros desde lugares lejanos.",
            "Cada persona aportaba una habilidad distinta a la comunidad.",
            "Así nació una red de intercambio que sostuvo a la ciudad.",
            "El cambio no ocurrió de un día para otro, sino paso a paso.",
            "Una decisión valiente abrió la puerta a nuevas posibilidades.",
            "Y la historia siguió avanzando con cada generación.",
            "A veces, una sola conexión puede cambiar el rumbo de todos.",
            "Lo importante es mirar el problema desde otro ángulo.",
            "Porque las mejores ideas suelen empezar con una pregunta.",
            "¿Qué pasaría si hacemos las cosas de una forma diferente?",
            "La respuesta puede estar más cerca de lo que pensamos.",
            "Las personas se unieron para construir algo duradero.",
            "El trabajo compartido hizo posible llegar mucho más lejos.",
            "Y las nuevas rutas trajeron historias inesperadas.",
            "Cada generación dejó algo mejor para la siguiente.",
            "La ciudad cambió, pero conservó el espíritu de sus comienzos.",
            "Hoy podemos ver las huellas de ese esfuerzo colectivo.",
            "Una prueba de que el progreso nace cuando colaboramos.",
            "Gracias por acompañarme; nos vemos en la próxima historia.",
        ]
        durations = [2.6, 2.9, 2.2, 3.1, 2.5, 2.7, 3.0, 2.4, 3.2, 2.5, 2.8, 2.4, 3.1, 2.3, 2.6, 2.7, 2.4, 3.0, 2.3, 2.8, 2.7, 2.4, 3.0, 2.6, 2.4, 2.6, 2.8, 2.5, 3.0, 2.7]
        total = sum(durations)
        scale = 79.07 / total
        cursor_time = 0.0
        for index, line in enumerate(narration):
            length = durations[index] * scale
            start, end = cursor_time, cursor_time + length
            cursor_time = end
            subjects = ["comerciantes", "carretas", "mercado", "puente", "viajeros", "comunidad"]
            places = ["ciudad junto al río", "plaza central", "camino de tierra", "puente antiguo", "taller familiar"]
            keywords = ["historia", "comunidad", "viaje"] if index % 3 else ["cómic", "stickman", "narrativa"]
            connection.execute(
                """INSERT INTO scenes(project_id,order_index,start_seconds,end_seconds,narration,subject,place,era,keywords_json,match_score,visual_prompt)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, index + 1, start, end, line, subjects[index % len(subjects)], places[index % len(places)], "Siglo XIX", json.dumps(keywords, ensure_ascii=False), 91 - (index % 9), "Ilustración 2D estilo cómic web, encuadre cinematográfico, personaje stickman consistente, luz cálida"),
            )
        connection.execute(
            "INSERT INTO review_notes(project_id,scene_id,time_seconds,note,status,created_at) VALUES(?,?,?,?,?,?)",
            (project_id, 6, 18.07, "El sonido de los efectos se siente alto. Bajar los golpes y la máquina de escribir 9 dB.", "pendiente", now),
        )


initialize_database()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def get_project_payload(project_id: int = 1) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="No se encontró el proyecto.")
        scenes = connection.execute("SELECT * FROM scenes WHERE project_id=? ORDER BY order_index", (project_id,)).fetchall()
        notes = connection.execute("SELECT * FROM review_notes WHERE project_id=? ORDER BY time_seconds DESC", (project_id,)).fetchall()
        costs = connection.execute("SELECT * FROM project_costs WHERE project_id=?", (project_id,)).fetchone()
        project_data = dict(project)
        scene_data = [dict(scene) for scene in scenes]
        for scene in scene_data:
            scene["keywords"] = json.loads(scene.pop("keywords_json"))
            scene["locked"] = bool(scene["locked"])
            scene["image_url"] = media_url(scene["image_path"]) if Path(scene["image_path"]).is_file() else ""
            scene["image_path"] = bool(scene["image_url"])
            scene["audio_url"] = media_url(scene["tts_path"]) if Path(scene["tts_path"]).is_file() else ""
            if scene["audio_url"] and not scene["tts_duration"]:
                scene["tts_duration"] = scene["end_seconds"] - scene["start_seconds"]
        project_data["audio_url"] = media_url(project_data.get("audio_path"))
        project_data["music_url"] = media_url(project_data.get("music_path"))
        project_data["logo_url"] = media_url(project_data.get("logo_path"))
        project_data["last_export_url"] = media_url(project_data.get("last_export_path"))
        project_data["last_subtitle_url"] = media_url(project_data.get("last_subtitle_path"))
        styles = [dict(row) for row in connection.execute("SELECT id,name,style_prompt FROM styles ORDER BY id")]
        active_style = next((style for style in styles if style["id"] == project_data["style_id"]), None)
        assets = [
            {"id": row["id"], "name": row["name"], "url": media_url(row["path"])}
            for row in connection.execute("SELECT * FROM image_assets WHERE project_id=? ORDER BY id", (project_id,))
            if Path(row["path"]).is_file()
        ]
        effects = connection.execute("SELECT * FROM sound_effects WHERE project_id=? ORDER BY start_seconds", (project_id,)).fetchall()
        if costs:
            cost_data = dict(costs)
            cost_data["total_cost"] = round(float(cost_data["image_cost"]) + float(cost_data["voice_cost"]) + float(cost_data["llm_cost"]), 6)
        else:
            cost_data = {"image_cost": 0, "voice_cost": 0, "llm_tokens": 0, "llm_cost": 0, "total_cost": 0}
        return {
            "project": project_data,
            "scenes": scene_data,
            "notes": [dict(note) for note in notes],
            "costs": cost_data,
            "styles": styles,
            "style": active_style or (styles[0] if styles else None),
            "assets": assets,
            "sound_effects": [dict(effect) | {"audio_url": media_url(effect["audio_path"])} for effect in effects],
        }


app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
LAST_CHECKS: dict[str, dict[str, Any]] = {}
JOBS: dict[str, dict[str, Any]] = {}


def start_job(worker, *, project_job: bool = False) -> str:
    job_id = secrets.token_urlsafe(12)
    JOBS[job_id] = {"id": job_id, "status": "running", "progress": 0, "message": "Preparando…",
                    "created_at": utc_now(), "project_job": project_job}

    async def run() -> None:
        try:
            result = await worker(JOBS[job_id])
            JOBS[job_id].update({"status": "complete", "progress": 100, "message": "Listo", "result": result, "finished_at": utc_now()})
        except Exception as exc:
            JOBS[job_id].update({"status": "error", "message": str(exc), "error": str(exc), "finished_at": utc_now()})

    asyncio.create_task(run())
    return job_id


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No se encontró esa tarea local.")
    return dict(job)


async def save_media_upload(file: UploadFile, destination: Path, allowed: set[str], max_bytes: int = 80 * 1024 * 1024) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"Formato no compatible. Usa: {', '.join(sorted(allowed))}.")
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"El archivo supera {max_bytes // (1024 * 1024)} MB.")
    if not data:
        raise HTTPException(status_code=422, detail="El archivo está vacío.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


class VoiceSettings(BaseModel):
    provider: str = Field(default="edge", pattern=r"^(edge|openai|elevenlabs|cartesia|gemini)$")
    model: str = Field(default="", max_length=160)
    voice: str = Field(default="", max_length=160)
    gemini_tts_tier: str | None = Field(default=None, pattern=r"^(free|paid)$")


class VoiceGeneration(VoiceSettings):
    scene_id: int | None = None
    pending_only: bool = False


@app.put("/api/audio/voice")
def save_voice_settings(payload: VoiceSettings):
    model, voice = resolve_voice(payload)
    with db() as connection:
        connection.execute(
            """UPDATE projects SET voice_provider=?,voice_model=?,voice_name=?,
               gemini_tts_tier=COALESCE(?,gemini_tts_tier),updated_at=? WHERE id=1""",
            (payload.provider, model, voice, payload.gemini_tts_tier, utc_now()),
        )
    return {"ok": True}


def resolve_voice(payload: VoiceSettings) -> tuple[str, str]:
    model, voice = payload.model.strip(), payload.voice.strip()
    if payload.provider == "gemini":
        model, voice = model or GEMINI_TTS_MODEL, voice or GEMINI_TTS_DEFAULT_VOICE
        if model != GEMINI_TTS_MODEL or voice not in GEMINI_TTS_VOICES:
            raise HTTPException(status_code=422, detail="Elige una voz y el modelo compatible con Google Gemini TTS.")
    return model, voice


def get_voice_key(provider: str) -> str | None:
    if provider == "edge":
        return None
    secret = keyring.get_password(KEYRING_SERVICE, provider)
    if not secret:
        detail = ("Guarda tu clave de Google Gemini en Ajustes → Modelos de lenguaje."
                  if provider == "gemini" else f"Guarda primero la clave de {PROVIDERS[provider]['label']} en Ajustes → APIs.")
        raise HTTPException(status_code=409, detail=detail)
    return secret


def record_voice_usage(item: dict[str, Any]) -> None:
    add_usage(item["provider"], item["model"], "tts", item["characters"], item["cost"],
              input_tokens=item.get("input_tokens", 0), output_tokens=item.get("output_tokens", 0))


@app.post("/api/audio/voice/sample")
async def generate_voice_sample(payload: VoiceSettings):
    if payload.provider != "gemini":
        raise HTTPException(status_code=422, detail="Selecciona Google Gemini TTS para escuchar la muestra.")
    model, voice = resolve_voice(payload)
    secret = get_voice_key(payload.provider)
    with db() as connection:
        tier = payload.gemini_tts_tier or connection.execute("SELECT gemini_tts_tier FROM projects WHERE id=1").fetchone()[0]
    target = project_media_dir() / "audio" / "samples" / f"gemini-{voice.lower()}.wav"

    async def worker(job: dict[str, Any]):
        job["message"] = f"Generando muestra con {voice}…"
        cached = target.is_file()
        cost = 0.0
        if not cached:
            temp = target.with_name(f"{target.stem}-{secrets.token_hex(5)}.wav")
            try:
                item = await synthesize_speech("gemini", "Hola. Esta es una muestra de voz para tu próximo video.", temp,
                                               secret=secret, voice=voice, model=model, gemini_tier=tier)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            cost = item["cost"]
            with db() as connection:
                connection.execute("UPDATE project_costs SET voice_cost=voice_cost+? WHERE project_id=1", (cost,))
            record_voice_usage(item)
        return {"audio_url": media_url(str(target)), "voice": voice, "estimated_cost": cost, "cached": cached}

    return {"job_id": start_job(worker, project_job=True)}


def update_voice_timeline(connection: sqlite3.Connection) -> float:
    cursor = 0.0
    for scene in connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index").fetchall():
        duration = scene["tts_duration"] or (scene["end_seconds"] - scene["start_seconds"])
        end = round(cursor + duration, 9)
        connection.execute("UPDATE scenes SET start_seconds=?,end_seconds=? WHERE id=?", (cursor, end, scene["id"]))
        cursor = end
    connection.execute("UPDATE projects SET audio_path='',duration_seconds=?,updated_at=? WHERE id=1", (cursor, utc_now()))
    return cursor


def invalidate_scene_audio(connection: sqlite3.Connection, scene_id: int | None = None) -> None:
    if scene_id is not None:
        connection.execute("UPDATE scenes SET tts_path='',tts_duration=0,tts_provider='' WHERE id=?", (scene_id,))
    current = connection.execute("SELECT audio_path FROM projects WHERE id=1").fetchone()[0]
    if Path(current).name.startswith("narration-"):
        connection.execute("UPDATE projects SET audio_path='' WHERE id=1")


@app.post("/api/audio/tts")
async def generate_project_tts(payload: VoiceGeneration):
    provider = payload.provider
    model, voice = resolve_voice(payload)
    with db() as connection:
        scenes = [dict(row) for row in connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index")]
        project = connection.execute("SELECT cartesia_usd_per_1000_chars,gemini_tts_tier FROM projects WHERE id=1").fetchone()
        cartesia_rate = float(project["cartesia_usd_per_1000_chars"])
        gemini_tier = payload.gemini_tts_tier or project["gemini_tts_tier"]
    if payload.scene_id is not None:
        scenes = [scene for scene in scenes if scene["id"] == payload.scene_id]
        if not scenes:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
    if payload.pending_only:
        scenes = [scene for scene in scenes if not Path(scene["tts_path"]).is_file()]
    if not scenes:
        return {"scenes": 0}
    secret = get_voice_key(provider)
    if provider in {"elevenlabs", "cartesia"} and not voice:
        raise HTTPException(status_code=422, detail="Configura el ID de voz en Audio.")
    if payload.scene_id is None:
        save_voice_settings(payload)

    async def worker(job: dict[str, Any]):
        audio_dir = project_media_dir() / "audio" / "scenes"
        total_cost = 0.0
        for index, scene in enumerate(scenes):
            job.update({"progress": round(index * 85 / len(scenes)), "message": f"Generando voz · escena {scene['order_index']} ({index + 1}/{len(scenes)})"})
            clip = audio_dir / f"scene-{scene['id']}-{secrets.token_hex(6)}.wav"
            extension = "wav" if provider == "gemini" else "mp3"
            source = clip.with_name(f"{clip.stem}-source.{extension}")
            try:
                item = await synthesize_speech(provider, scene["narration"], source, secret=secret, voice=voice,
                                               model=model, cartesia_usd_per_1000_chars=cartesia_rate, gemini_tier=gemini_tier)
                duration = await asyncio.to_thread(normalize_voice, source, clip)
                if duration <= 0:
                    raise MediaEngineError("El archivo de voz está vacío.")
            except Exception as exc:
                clip.unlink(missing_ok=True)
                raise MediaEngineError(f"No se pudo generar la voz de la escena {scene['order_index']}: {exc}") from exc
            finally:
                source.unlink(missing_ok=True)
            total_cost += float(item["cost"])
            with db() as connection:
                connection.execute("UPDATE scenes SET tts_path=?,tts_duration=?,tts_provider=? WHERE id=?",
                                   (str(clip), duration, provider, scene["id"]))
                project_duration = update_voice_timeline(connection)
                connection.execute("UPDATE project_costs SET voice_cost=voice_cost+? WHERE project_id=1", (item["cost"],))
            record_voice_usage(item)
        master = project_media_dir() / "audio" / f"narration-{secrets.token_hex(6)}.wav"
        job.update({"progress": 88, "message": "Uniendo las voces por escena…"})
        with db() as connection:
            timeline = [dict(scene) for scene in connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index")]
        await asyncio.to_thread(assemble_scene_audio, timeline, master)
        with db() as connection:
            connection.execute("UPDATE projects SET audio_path=? WHERE id=1", (str(master),))
        return {"audio_url": media_url(str(master)), "duration_seconds": project_duration, "scenes": len(scenes),
                "estimated_cost": round(total_cost, 8), "provider": provider, "gemini_tts_tier": gemini_tier}

    return {"job_id": start_job(worker, project_job=True)}


@app.post("/api/audio/master")
async def upload_master_audio(file: UploadFile = File(...)):
    target = project_media_dir() / "audio" / f"master-{secrets.token_hex(6)}{Path(file.filename or '').suffix.lower()}"
    stored = await save_media_upload(file, target, {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"})
    try:
        duration = await asyncio.to_thread(audio_duration, stored)
    except MediaEngineError as exc:
        stored.unlink(missing_ok=True)
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    with db() as connection:
        scenes = [dict(row) for row in connection.execute("SELECT id,narration FROM scenes WHERE project_id=1 ORDER BY order_index")]

    async def worker(job: dict[str, Any]):
        job.update({"progress": 5, "message": "Analizando palabras del audio para sincronizar escenas…"})
        words = await asyncio.to_thread(transcribe_word_times, stored)
        timings = align_scenes_to_words(scenes, words, duration)
        with db() as connection:
            connection.execute("UPDATE projects SET audio_path=?,duration_seconds=?,updated_at=? WHERE id=1", (str(stored), duration, utc_now()))
            for scene, (start, end) in zip(scenes, timings):
                connection.execute("UPDATE scenes SET start_seconds=?,end_seconds=? WHERE id=?", (start, end, scene["id"]))
        return {"audio_url": media_url(str(stored)), "duration_seconds": duration, "scenes": len(scenes), "recognized_words": len(words)}

    job_id = start_job(worker, project_job=True)
    return {"ok": True, "audio_url": media_url(str(stored)), "duration_seconds": duration, "sync_job_id": job_id}


@app.post("/api/audio/sync")
async def sync_master_audio():
    with db() as connection:
        row = connection.execute("SELECT audio_path,duration_seconds FROM projects WHERE id=1").fetchone()
        scenes = [dict(item) for item in connection.execute("SELECT id,narration FROM scenes WHERE project_id=1 ORDER BY order_index").fetchall()]
    if not row or not row["audio_path"] or not Path(row["audio_path"]).is_file():
        raise HTTPException(status_code=409, detail="Sube primero el audio maestro.")
    audio_path, duration = row["audio_path"], float(row["duration_seconds"])

    async def worker(job: dict[str, Any]):
        job.update({"progress": 5, "message": "Analizando palabras del audio para sincronizar escenas…"})
        words = await asyncio.to_thread(transcribe_word_times, audio_path)
        timings = align_scenes_to_words(scenes, words, duration)
        with db() as connection:
            for scene, (start, end) in zip(scenes, timings):
                connection.execute("UPDATE scenes SET start_seconds=?,end_seconds=? WHERE id=?", (start, end, scene["id"]))
        return {"duration_seconds": duration, "scenes": len(scenes), "recognized_words": len(words)}

    return {"job_id": start_job(worker, project_job=True)}


async def upload_project_asset(file: UploadFile, kind: str, allowed: set[str]) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    target = project_media_dir() / kind / f"{kind}-{secrets.token_hex(6)}{suffix}"
    stored = await save_media_upload(file, target, allowed)
    column = {"music": "music_path", "logo": "logo_path"}[kind]
    with db() as connection:
        connection.execute(f"UPDATE projects SET {column}=?,updated_at=? WHERE id=1", (str(stored), utc_now()))
    return {"ok": True, "url": media_url(str(stored)), "path": str(stored)}


@app.post("/api/audio/music")
async def upload_music(file: UploadFile = File(...)):
    return await upload_project_asset(file, "music", {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"})


@app.post("/api/audio/logo")
async def upload_logo(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".png":
        raise HTTPException(status_code=415, detail="El logotipo debe ser PNG con transparencia.")
    return await upload_project_asset(file, "logo", {".png"})


@app.post("/api/audio/sfx")
async def upload_sound_effect(file: UploadFile = File(...), start_seconds: float = Form(0.0)):
    target = project_media_dir() / "sfx" / f"sfx-{secrets.token_hex(6)}{Path(file.filename or '').suffix.lower()}"
    stored = await save_media_upload(file, target, {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"})
    with db() as connection:
        cursor = connection.execute("INSERT INTO sound_effects(project_id,name,audio_path,start_seconds) VALUES(1,?,?,?)", (Path(file.filename or "efecto").name, str(stored), max(0, start_seconds)))
        effect_id = int(cursor.lastrowid)
    return {"ok": True, "id": effect_id, "audio_url": media_url(str(stored))}


class AudioMixInput(BaseModel):
    voice_gain_db: float = Field(default=0, ge=-60, le=12)
    music_gain_db: float = Field(default=-15, ge=-60, le=12)
    sfx_gain_db: float = Field(default=0, ge=-60, le=12)
    music_ducking: bool = True
    logo_position: str = Field(default="bottom_right", pattern=r"^(top_left|top_right|bottom_left|bottom_right)$")
    logo_scale: float = Field(default=0.14, ge=0.03, le=0.5)
    logo_opacity: float = Field(default=0.85, ge=0.05, le=1)
    cartesia_usd_per_1000_chars: float = Field(default=0, ge=0, le=100)


@app.put("/api/audio/settings")
def update_audio_settings(payload: AudioMixInput):
    with db() as connection:
        connection.execute("UPDATE projects SET voice_gain_db=?,music_gain_db=?,sfx_gain_db=?,music_ducking=?,logo_position=?,logo_scale=?,logo_opacity=?,cartesia_usd_per_1000_chars=? WHERE id=1", (payload.voice_gain_db, payload.music_gain_db, payload.sfx_gain_db, int(payload.music_ducking), payload.logo_position, payload.logo_scale, payload.logo_opacity, payload.cartesia_usd_per_1000_chars))
    return {"ok": True}


class ImageGenerationInput(BaseModel):
    provider: str = "openai"
    model: str = ""
    scene_id: int | None = None
    correction: str = Field(default="", max_length=2000)
    missing_only: bool = False


@app.post("/api/images/generate")
async def generate_project_images(payload: ImageGenerationInput):
    provider = payload.provider.lower()
    if provider not in {"openai", "replicate"}:
        raise HTTPException(status_code=422, detail="Elige OpenAI o Replicate · Flux.")
    secret = keyring.get_password(KEYRING_SERVICE, provider)
    if not secret:
        raise HTTPException(status_code=409, detail=f"Guarda primero la clave de {PROVIDERS[provider]['label']} en Ajustes → APIs.")
    with db() as connection:
        project = dict(connection.execute("SELECT * FROM projects WHERE id=1").fetchone())
        style_row = connection.execute("SELECT style_prompt FROM styles ORDER BY id IS NOT ? LIMIT 1", (project["style_id"],)).fetchone()
        scenes = [dict(row) for row in connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index").fetchall()]
        if payload.scene_id is not None:
            scenes = [scene for scene in scenes if scene["id"] == payload.scene_id]
            if not scenes:
                raise HTTPException(status_code=404, detail="No se encontró la escena.")
    if payload.scene_id is None and payload.missing_only:
        scenes = [scene for scene in scenes if not scene["image_path"]]
    if not scenes:
        raise HTTPException(status_code=409, detail="Todas las escenas ya tienen imagen.")
    model = payload.model.strip() or ("gpt-image-2.5-flare" if provider == "openai" else "black-forest-labs/flux-schnell")

    async def worker(job: dict[str, Any]):
        style_prompt = str(style_row["style_prompt"] if style_row else "")
        image_dir = project_media_dir() / "images"
        total_cost = 0.0
        for index, scene in enumerate(scenes):
            if scene["locked"]:
                continue
            job.update({"progress": round(index * 90 / len(scenes)), "message": f"Generando imagen · escena {scene['order_index']} ({index + 1}/{len(scenes)})"})
            parts = [style_prompt.strip(), scene["visual_prompt"].strip(), f"Narración de referencia: {scene['narration']}"]
            if payload.correction.strip():
                parts.append(f"Corrección solicitada para esta regeneración: {payload.correction.strip()}")
            prompt = "\n\n".join(part for part in parts if part)
            target = image_dir / f"scene-{scene['id']}-{secrets.token_hex(4)}.png"
            source = Path(scene["image_path"]) if payload.scene_id is not None and scene["image_path"] else None
            try:
                result = await generate_image(provider, prompt, target, secret=secret, model=model, aspect_ratio=project["aspect_ratio"], source_image=source)
            except MediaEngineError:
                raise
            except Exception as exc:
                raise MediaEngineError(f"No se pudo generar la imagen de la escena {scene['order_index']}: {exc}") from exc
            cost = float(result["cost"])
            total_cost += cost
            with db() as connection:
                connection.execute("UPDATE scenes SET image_path=? WHERE id=?", (result["path"], scene["id"]))
                connection.execute("INSERT INTO image_assets(project_id,name,path) VALUES(1,?,?)",
                                   (f"Escena {scene['order_index']} · {provider}", result["path"]))
                connection.execute("UPDATE project_costs SET image_cost=image_cost+? WHERE project_id=1", (cost,))
            add_usage(provider, model, "image", 1, cost)
        with db() as connection:
            connection.execute("UPDATE projects SET image_provider=?,image_model=?,updated_at=? WHERE id=1", (provider, model, utc_now()))
        return {"images": len(scenes), "estimated_cost": round(total_cost, 6), "provider": provider, "model": model}

    return {"job_id": start_job(worker, project_job=True)}


class RenderInput(BaseModel):
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16)$")
    subtitle_mode: str = Field(default="none", pattern=r"^(none|srt|burn)$")


@app.put("/api/render/settings")
def save_render_settings(payload: RenderInput):
    with db() as connection:
        connection.execute("UPDATE projects SET aspect_ratio=?,subtitle_mode=?,updated_at=? WHERE id=1",
                           (payload.aspect_ratio, payload.subtitle_mode, utc_now()))
    return {"ok": True}


@app.post("/api/render")
async def render_project(payload: RenderInput):
    with db() as connection:
        project = dict(connection.execute("SELECT * FROM projects WHERE id=1").fetchone())
        scenes = [dict(row) for row in connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index").fetchall()]
        notes = [dict(row) for row in connection.execute("SELECT * FROM review_notes WHERE project_id=1 AND status='pendiente' ORDER BY time_seconds", ()).fetchall()]
        effects = [dict(row) for row in connection.execute("SELECT * FROM sound_effects WHERE project_id=1 ORDER BY start_seconds").fetchall()]
    missing = [str(scene["order_index"]) for scene in scenes if not Path(scene["image_path"]).is_file()]
    if not scenes or missing:
        detail = f"Faltan imágenes en {', '.join(missing)}. Asígnalas desde Recursos." if missing else "Importa un guion para crear escenas."
        raise HTTPException(status_code=409, detail=detail)
    save_render_settings(payload)
    adjusted_voice_gain, adjusted_music_gain, adjusted_sfx_gain, applied_note_ids = apply_review_notes(
        notes, scenes, float(project["voice_gain_db"]), float(project["music_gain_db"]), float(project["sfx_gain_db"])
    )
    music_path = project.get("music_path") or None
    logo_path = project.get("logo_path") or None
    output = project_media_dir() / "exports" / f"{re.sub(r'[^A-Za-z0-9_-]+', '-', project['title']).strip('-') or 'video'}-{secrets.token_hex(6)}.mp4"

    async def worker(job: dict[str, Any]):
        job.update({"progress": 2, "message": "Preparando pistas y notas de revisión…"})
        voice_path = project.get("audio_path")
        rebuilt = None
        try:
            if not Path(voice_path or "").is_file() and any(Path(scene["tts_path"]).is_file() for scene in scenes):
                rebuilt = output.with_suffix(".voice.wav")
                await asyncio.to_thread(assemble_scene_audio, scenes, rebuilt)
                voice_path = str(rebuilt)
            job.update({"progress": 10, "message": "Renderizando escenas, pistas y subtítulos…"})
            result = await asyncio.to_thread(
                render_mp4, scenes, output, aspect_ratio=payload.aspect_ratio,
                voice_path=voice_path, music_path=music_path, logo_path=logo_path, sound_effects=effects,
                voice_gain_db=adjusted_voice_gain, music_gain_db=adjusted_music_gain, sfx_gain_db=adjusted_sfx_gain,
                music_ducking=bool(project["music_ducking"]), logo_position=project["logo_position"], logo_scale=float(project["logo_scale"]), logo_opacity=float(project["logo_opacity"]),
                subtitle_mode=payload.subtitle_mode,
            )
        except MediaEngineError:
            raise
        except Exception as exc:
            raise MediaEngineError(f"No se pudo exportar el MP4: {exc}") from exc
        finally:
            if rebuilt:
                rebuilt.unlink(missing_ok=True)
        with db() as connection:
            connection.execute(
                "UPDATE projects SET last_export_path=?,last_subtitle_path=?,voice_gain_db=?,music_gain_db=?,sfx_gain_db=?,updated_at=? WHERE id=1",
                (str(output), result["subtitle_path"], adjusted_voice_gain, adjusted_music_gain, adjusted_sfx_gain, utc_now()),
            )
            for scene in scenes:
                connection.execute("UPDATE scenes SET motion=? WHERE id=?", (scene.get("motion", ""), scene["id"]))
            for note_id in applied_note_ids:
                connection.execute("UPDATE review_notes SET status='aplicada' WHERE id=?", (note_id,))
        result["video_url"] = media_url(str(output))
        result["subtitle_url"] = media_url(result["subtitle_path"])
        result["applied_notes"] = len(applied_note_ids)
        return result

    return {"job_id": start_job(worker, project_job=True)}


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__LOCAL_APP_TOKEN__", APP_TOKEN))


@app.get("/api/bootstrap")
def bootstrap():
    return get_project_payload()


class StyleInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    style_prompt: str = Field(min_length=1, max_length=2000)


@app.post("/api/styles")
def create_style(payload: StyleInput):
    name, prompt = payload.name.strip(), payload.style_prompt.strip()
    if not name or not prompt:
        raise HTTPException(status_code=422, detail="Completa el nombre y el sufijo del estilo.")
    with db() as connection:
        style_id = connection.execute("INSERT INTO styles(name,style_prompt) VALUES(?,?)", (name, prompt)).lastrowid
        connection.execute("UPDATE projects SET style_id=?,updated_at=? WHERE id=1", (style_id, utc_now()))
    return get_project_payload()


@app.put("/api/styles/{style_id}/select")
def select_style(style_id: int):
    with db() as connection:
        if not connection.execute("SELECT id FROM styles WHERE id=?", (style_id,)).fetchone():
            raise HTTPException(status_code=404, detail="No se encontró el estilo.")
        connection.execute("UPDATE projects SET style_id=?,updated_at=? WHERE id=1", (style_id, utc_now()))
    return get_project_payload()


@app.get("/api/providers")
def list_providers():
    results = []
    for provider_id, config in PROVIDERS.items():
        try:
            configured = bool(keyring.get_password(KEYRING_SERVICE, provider_id))
        except Exception:
            configured = False
        results.append({
            "id": provider_id,
            **config,
            "configured": configured,
            "last_check": LAST_CHECKS.get(provider_id),
        })
    return {"providers": results, "secure_store": True}


class SecretInput(BaseModel):
    secret: str = Field(min_length=1, max_length=512)


@app.put("/api/providers/{provider_id}/secret")
def save_secret(provider_id: str, payload: SecretInput):
    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Proveedor desconocido.")
    secret_value = payload.secret.strip()
    if not secret_value:
        raise HTTPException(status_code=422, detail="La clave está vacía.")
    try:
        keyring.set_password(KEYRING_SERVICE, provider_id, secret_value)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="No fue posible guardar la clave en el almacén seguro de Windows. Instala los requisitos y vuelve a abrir la app.") from exc
    LAST_CHECKS.pop(provider_id, None)
    return {"ok": True, "configured": True}


@app.delete("/api/providers/{provider_id}/secret")
def delete_secret(provider_id: str):
    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Proveedor desconocido.")
    try:
        keyring.delete_password(KEYRING_SERVICE, provider_id)
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception as exc:
        raise HTTPException(status_code=503, detail="No fue posible eliminar la clave del almacén seguro.") from exc
    LAST_CHECKS.pop(provider_id, None)
    return {"ok": True, "configured": False}


async def provider_check(provider_id: str, secret: str) -> tuple[str, list[str]]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        if provider_id == "openai":
            response = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {secret}"})
            response.raise_for_status()
            blocked = ("audio", "embedding", "transcribe", "realtime", "image", "moderation", "whisper", "search")
            models = [item.get("id", "") for item in response.json().get("data", []) if item.get("id", "").startswith(("gpt-", "o")) and not any(token in item.get("id", "").lower() for token in blocked)]
            return "Clave válida · catálogo de modelos disponible", models[:40]
        if provider_id == "anthropic":
            response = await client.get("https://api.anthropic.com/v1/models", headers={"Authorization": f"Bearer {secret}", "anthropic-version": "2026-10-21"}, params={"limit": 100})
            response.raise_for_status()
            models = [item.get("id", "") for item in response.json().get("data", []) if item.get("lifecycle") in (None, "active")]
            return "Clave válida · catálogo de modelos disponible", models[:40]
        if provider_id == "gemini":
            response = await client.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": secret, "pageSize": 100})
            response.raise_for_status()
            models = [item.get("name", "").removeprefix("models/") for item in response.json().get("models", []) if "generateContent" in item.get("supportedGenerationMethods", [])]
            return "Clave válida · catálogo de modelos disponible", models[:40]
        if provider_id == "elevenlabs":
            response = await client.get("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": secret})
            response.raise_for_status()
            models = [item.get("model_id", "") for item in response.json() if item.get("can_do_text_to_speech")]
            return "Clave válida · modelos de voz disponibles", models[:40]
        if provider_id == "cartesia":
            response = await client.get("https://api.cartesia.ai/voices", headers={"Authorization": f"Bearer {secret}", "Cartesia-Version": "2026-08-14"}, params={"limit": 1})
            response.raise_for_status()
            return "Clave válida · catálogo de voces disponible", []
        if provider_id == "replicate":
            response = await client.get("https://api.replicate.com/v1/account", headers={"Authorization": f"Bearer {secret}"})
            response.raise_for_status()
            account = response.json()
            return f"Cuenta conectada · {account.get('username') or account.get('name') or 'Replicate'}", []
        if provider_id == "jamendo":
            response = await client.get("https://api.jamendo.com/v3.0/tracks/", params={"client_id": secret, "format": "json", "limit": 1})
            response.raise_for_status()
            body = response.json()
            if body.get("headers", {}).get("status") != "success":
                raise httpx.HTTPStatusError("Jamendo no aceptó el Client ID.", request=response.request, response=response)
            return "Client ID válido · catálogo de música disponible", []
        if provider_id == "freesound":
            response = await client.get("https://freesound.org/apiv2/search/text/", headers={"Authorization": f"Token {secret}"}, params={"query": "rain", "page_size": 1})
            response.raise_for_status()
            return "API key válida · búsqueda de efectos disponible", []
    raise HTTPException(status_code=422, detail="Este proveedor no tiene una prueba sin costo. La clave quedó guardada y se validará al generar.")


@app.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Proveedor desconocido.")
    if not PROVIDERS[provider_id]["testable"]:
        raise HTTPException(status_code=422, detail=PROVIDERS[provider_id]["note"])
    try:
        secret = keyring.get_password(KEYRING_SERVICE, provider_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="El almacén seguro de claves no está disponible en este equipo.") from exc
    if not secret:
        raise HTTPException(status_code=409, detail="Guarda una clave antes de probar la conexión.")
    started = time.perf_counter()
    try:
        message, models = await provider_check(provider_id, secret)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            detail = "El proveedor rechazó la clave. Revisa que sea una API key activa y que tenga acceso a este servicio."
        elif status == 402:
            detail = "La clave es reconocida, pero la cuenta no tiene saldo o créditos disponibles."
        elif status == 429:
            detail = "La clave llegó al proveedor, pero se alcanzó el límite de solicitudes. Intenta más tarde."
        else:
            detail = f"El proveedor respondió con HTTP {status}. Revisa los permisos y el estado de la cuenta."
        LAST_CHECKS[provider_id] = {"ok": False, "message": detail, "checked_at": utc_now()}
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.TimeoutException as exc:
        detail = "La conexión tardó demasiado. Revisa internet y vuelve a probar."
        LAST_CHECKS[provider_id] = {"ok": False, "message": detail, "checked_at": utc_now()}
        raise HTTPException(status_code=504, detail=detail) from exc
    except httpx.HTTPError as exc:
        detail = "No se pudo conectar con el proveedor. Revisa internet y la configuración de red."
        LAST_CHECKS[provider_id] = {"ok": False, "message": detail, "checked_at": utc_now()}
        raise HTTPException(status_code=502, detail=detail) from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    LAST_CHECKS[provider_id] = {"ok": True, "message": message, "checked_at": utc_now()}
    return {"ok": True, "message": message, "models": models, "elapsed_ms": elapsed_ms}


class ScriptRequest(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=5, max_length=12000)


@app.post("/api/ai/script")
async def generate_script(payload: ScriptRequest):
    provider_id = payload.provider
    if provider_id not in {"openai", "anthropic", "gemini"}:
        raise HTTPException(status_code=422, detail="Elige OpenAI, Claude o Gemini para generar el guion.")
    secret = keyring.get_password(KEYRING_SERVICE, provider_id)
    if not secret:
        raise HTTPException(status_code=409, detail="Guarda primero la API key en Ajustes → APIs.")
    prompt = payload.prompt.strip()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            if provider_id == "openai":
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {secret}"},
                    json={"model": payload.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.8},
                )
                response.raise_for_status()
                result = response.json()
                text = result["choices"][0]["message"]["content"]
                usage = result.get("usage", {})
                input_tokens = int(usage.get("prompt_tokens", 0))
                output_tokens = int(usage.get("completion_tokens", 0))
                tokens = int(usage.get("total_tokens", input_tokens + output_tokens))
            elif provider_id == "anthropic":
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"Authorization": f"Bearer {secret}", "anthropic-version": "2026-10-21"},
                    json={"model": payload.model, "max_tokens": 1800, "messages": [{"role": "user", "content": prompt}]},
                )
                response.raise_for_status()
                result = response.json()
                text = "\n".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")
                usage = result.get("usage", {})
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                tokens = input_tokens + output_tokens
            else:
                model_path = payload.model.removeprefix("models/")
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}:generateContent",
                    params={"key": secret},
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
                response.raise_for_status()
                result = response.json()
                text = "\n".join(part.get("text", "") for part in result.get("candidates", [{}])[0].get("content", {}).get("parts", []))
                usage = result.get("usageMetadata", {})
                input_tokens = int(usage.get("promptTokenCount", 0))
                output_tokens = int(usage.get("candidatesTokenCount", 0))
                tokens = int(usage.get("totalTokenCount", input_tokens + output_tokens))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            detail = "El proveedor rechazó la clave o el acceso al modelo. Vuelve a comprobar la API key."
        elif exc.response.status_code == 402:
            detail = "La cuenta del proveedor no tiene saldo suficiente."
        else:
            detail = f"El proveedor devolvió HTTP {exc.response.status_code}. Verifica el modelo elegido."
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="No se pudo completar la solicitud al servicio de IA.") from exc
    if not text.strip():
        raise HTTPException(status_code=502, detail="El proveedor respondió sin texto. Prueba otro modelo o prompt.")
    cost = llm_cost_estimate(provider_id, payload.model, input_tokens, output_tokens)
    with db() as connection:
        connection.execute("UPDATE project_costs SET llm_tokens=llm_tokens+?,llm_input_tokens=llm_input_tokens+?,llm_output_tokens=llm_output_tokens+?,llm_cost=llm_cost+? WHERE project_id=1", (tokens, input_tokens, output_tokens, cost))
    add_usage(provider_id, payload.model, "llm", tokens, cost, input_tokens=input_tokens, output_tokens=output_tokens)
    return {"text": text, "tokens": tokens, "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost": cost, "provider": provider_id, "model": payload.model}


class ScriptApplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400000)
    structured: bool = False


def parse_structured_script(text: str) -> list[tuple[str, str]]:
    headings = re.compile(r"^escena\s*#?\s*(\d+)\b(?:\s*[:.\-–—) ].*)?$", re.IGNORECASE)
    labels = re.compile(r"^(?:\*\*|__)?(locuci[oó]n|narraci[oó]n|prompt visual)\s*(?:\*\*|__)?\s*:(?:\*\*|__)?\s*(.*)$", re.IGNORECASE)
    blocks: dict[int, dict[str, list[str]]] = {}
    current = None
    field = ""
    for original in text.splitlines():
        line = re.sub(r"^(?:#{1,6}\s+|[-*]\s+)", "", original.strip()).strip()
        if not line or line.startswith("```"):
            continue
        heading = headings.fullmatch(line.strip("*_ ").strip())
        if heading:
            number = int(heading[1])
            if number < 1 or number in blocks:
                raise HTTPException(status_code=422, detail=f"El número de escena {number} no es válido o está repetido.")
            blocks[number] = {"narration": [], "prompt": []}
            current, field = number, ""
        elif current is not None:
            label = labels.fullmatch(line)
            if label:
                field = "prompt" if label[1].lower() == "prompt visual" else "narration"
                if label[2]:
                    blocks[current][field].append(label[2])
            elif field:
                blocks[current][field].append(line)
    if not blocks or len(blocks) > 120:
        raise HTTPException(status_code=422, detail="Incluye entre 1 y 120 bloques con encabezados ESCENA 1, ESCENA 2, etc.")
    if sorted(blocks) != list(range(1, len(blocks) + 1)):
        raise HTTPException(status_code=422, detail="Numera las escenas consecutivamente desde 1 para que coincidan con tus imágenes.")
    scenes = []
    for number, block in sorted(blocks.items()):
        narration, prompt = ("\n".join(block[key]).strip() for key in ("narration", "prompt"))
        if not narration or not prompt:
            raise HTTPException(status_code=422, detail=f"La escena {number} debe tener LOCUCIÓN y PROMPT VISUAL.")
        if len(narration) > 2000 or len(prompt) > 3000:
            raise HTTPException(status_code=422, detail=f"La escena {number} supera 2000 caracteres de locución o 3000 de prompt.")
        scenes.append((narration, prompt))
    return scenes


@app.post("/api/scenes/apply-script")
def apply_script(payload: ScriptApplyRequest):
    normalized = payload.text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if payload.structured or re.search(r"^\s*(?:[#*]+\s*)?escena\s*#?\d+", normalized, re.IGNORECASE | re.MULTILINE):
        rows = parse_structured_script(normalized)
    else:
        chunks = [" ".join(part.split()) for part in re.split(r"\n\s*\n", normalized) if part.strip()]
        if len(chunks) == 1:
            chunks = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
        if not chunks or len(chunks) > 120 or any(len(part) > 2000 for part in chunks):
            raise HTTPException(status_code=422, detail="Usa entre 1 y 120 fragmentos de hasta 2000 caracteres.")
        rows = [(part, None) for part in chunks]

    with db() as connection:
        existing = connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index", ()).fetchall()
        if any(scene["locked"] for scene in existing):
            raise HTTPException(status_code=409, detail="Desbloquea las escenas antes de reemplazar el guion.")
        cursor_time = 0.0
        for index, (narration, prompt) in enumerate(rows, start=1):
            previous = existing[index - 1] if index <= len(existing) else None
            same_text = previous is not None and previous["narration"] == narration
            saved_duration = previous["tts_duration"] if same_text else 0
            if same_text and not saved_duration and Path(previous["tts_path"]).is_file():
                saved_duration = audio_duration(previous["tts_path"])
            end = round(cursor_time + (saved_duration or max(1.0, len(narration.split()) / 2.5)), 9)
            if previous is None:
                connection.execute(
                    "INSERT INTO scenes(project_id,order_index,start_seconds,end_seconds,narration,visual_prompt) VALUES(1,?,?,?,?,?)",
                    (index, cursor_time, end, narration, prompt or ""),
                )
            else:
                visual = previous["visual_prompt"] if prompt is None else prompt
                image_path = previous["image_path"] if prompt is None or (same_text and visual == previous["visual_prompt"]) else ""
                connection.execute(
                    """UPDATE scenes SET order_index=?,start_seconds=?,end_seconds=?,narration=?,visual_prompt=?,image_path=?,
                       tts_path=?,tts_duration=?,tts_provider=?,
                       subject=CASE WHEN ? THEN subject ELSE '' END,place=CASE WHEN ? THEN place ELSE '' END,
                       era=CASE WHEN ? THEN era ELSE '' END,keywords_json=CASE WHEN ? THEN keywords_json ELSE '[]' END,
                       match_score=CASE WHEN ? THEN match_score ELSE 0 END WHERE id=?""",
                    (index, cursor_time, end, narration, visual, image_path, previous["tts_path"] if same_text else "",
                     saved_duration, previous["tts_provider"] if same_text else "", *(same_text,) * 5, previous["id"]),
                )
            cursor_time = end
        for previous in existing[len(rows):]:
            connection.execute("DELETE FROM scenes WHERE id=?", (previous["id"],))
        connection.execute(
            "UPDATE projects SET audio_path='',last_export_path='',last_subtitle_path='',duration_seconds=?,updated_at=? WHERE id=1",
            (cursor_time, utc_now()),
        )
        current_scenes = connection.execute("SELECT id,start_seconds,end_seconds FROM scenes WHERE project_id=1 ORDER BY order_index", ()).fetchall()
        notes = connection.execute("SELECT id,time_seconds FROM review_notes WHERE project_id=1", ()).fetchall()
        for note in notes:
            owner = next((scene for scene in current_scenes if scene["start_seconds"] <= note["time_seconds"] < scene["end_seconds"]), current_scenes[-1])
            connection.execute("UPDATE review_notes SET scene_id=? WHERE id=?", (owner["id"], note["id"]))
    return mutation_result(current_scenes[0]["id"])


class ProjectUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16)$")


@app.put("/api/projects/1")
def update_project(payload: ProjectUpdate):
    with db() as connection:
        connection.execute("UPDATE projects SET title=?,aspect_ratio=?,updated_at=? WHERE id=1", (payload.title.strip(), payload.aspect_ratio, utc_now()))
    return get_project_payload()


class SceneUpdate(BaseModel):
    narration: str = Field(min_length=1, max_length=2000)
    visual_prompt: str = Field(default="", max_length=3000)
    locked: bool = False


@app.put("/api/scenes/{scene_id}")
def update_scene(scene_id: int, payload: SceneUpdate):
    with db() as connection:
        scene = connection.execute("SELECT narration FROM scenes WHERE id=? AND project_id=1", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
        narration = payload.narration.strip()
        if not narration:
            raise HTTPException(status_code=422, detail="La narración no puede quedar vacía.")
        connection.execute("UPDATE scenes SET narration=?,visual_prompt=?,locked=? WHERE id=?",
                           (narration, payload.visual_prompt.strip(), int(payload.locked), scene_id))
        if narration != scene["narration"]:
            invalidate_scene_audio(connection, scene_id)
    return get_project_payload()


async def store_image(file: UploadFile) -> tuple[str, Path]:
    name = Path((file.filename or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    target = project_media_dir() / "images" / f"resource-{secrets.token_hex(10)}{suffix}"
    await save_media_upload(file, target, {".png", ".jpg", ".jpeg", ".webp"}, 18 * 1024 * 1024)
    try:
        with Image.open(target) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Formato de imagen no admitido")
            image.verify()
    except (ValueError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=415, detail=f"{name}: usa una imagen PNG, JPG o WebP válida.") from exc
    return name, target


@app.post("/api/scenes/{scene_id}/image")
async def upload_scene_image(scene_id: int, file: UploadFile = File(...)):
    name, target = await store_image(file)
    try:
        with db() as connection:
            result = connection.execute("UPDATE scenes SET image_path=? WHERE id=? AND project_id=1 AND locked=0", (str(target), scene_id))
            if not result.rowcount:
                raise HTTPException(status_code=409, detail="La escena no existe o está bloqueada.")
            connection.execute("INSERT INTO image_assets(project_id,name,path) VALUES(1,?,?)", (name, str(target)))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"ok": True, "image_url": media_url(str(target))}


@app.post("/api/resources/images")
async def upload_gallery_images(files: list[UploadFile] = File(...)):
    if not 1 <= len(files) <= 120:
        raise HTTPException(status_code=422, detail="Sube entre 1 y 120 imágenes por lote.")
    uploaded = []
    try:
        for file in files:
            uploaded.append(await store_image(file))
        with db() as connection:
            connection.executemany("INSERT INTO image_assets(project_id,name,path) VALUES(1,?,?)",
                                   [(name, str(target)) for name, target in uploaded])
    except Exception:
        for _, target in uploaded:
            target.unlink(missing_ok=True)
        raise
    return {"uploaded": len(uploaded)}


class AssetAssignment(BaseModel):
    scene_id: int


@app.put("/api/resources/{asset_id}/assign")
def assign_image(asset_id: int, payload: AssetAssignment):
    with db() as connection:
        asset = connection.execute("SELECT path FROM image_assets WHERE id=? AND project_id=1", (asset_id,)).fetchone()
        if not asset or not Path(asset["path"]).is_file():
            raise HTTPException(status_code=404, detail="No se encontró la imagen.")
        result = connection.execute("UPDATE scenes SET image_path=? WHERE id=? AND project_id=1 AND locked=0",
                                    (asset["path"], payload.scene_id))
        if not result.rowcount:
            raise HTTPException(status_code=409, detail="La escena no existe o está bloqueada.")
    return get_project_payload()


@app.post("/api/resources/auto-assign")
def auto_assign_images():
    def natural_key(name: str):
        return [(1, int(part)) if part.isdecimal() else (0, part) for part in re.split(r"(\d+)", name.casefold())]

    with db() as connection:
        scenes = connection.execute("SELECT * FROM scenes WHERE project_id=1 ORDER BY order_index").fetchall()
        assets = connection.execute("SELECT * FROM image_assets WHERE project_id=1 ORDER BY id DESC").fetchall()
        available = {scene["order_index"]: scene["id"] for scene in scenes if not scene["locked"] and not Path(scene["image_path"]).is_file()}
        used = {scene["image_path"] for scene in scenes if scene["image_path"]}
        unnamed, assignments = [], []
        for asset in sorted(assets, key=lambda item: natural_key(item["name"])):
            if asset["path"] in used or not Path(asset["path"]).is_file():
                continue
            match = re.match(r"^(?:escena|scene)[ _.-]*0*(\d+)(?!\d)", asset["name"], re.IGNORECASE)
            if match:
                scene_id = available.pop(int(match[1]), None)
                if scene_id is not None:
                    assignments.append((asset["path"], scene_id))
            else:
                unnamed.append(asset["path"])
        assignments.extend(zip(unnamed, available.values()))
        connection.executemany("UPDATE scenes SET image_path=? WHERE id=?", assignments)
    return {"assigned": len(assignments)}


class PromptInput(BaseModel):
    visual_prompt: str = Field(max_length=3000)


@app.patch("/api/scenes/{scene_id}/prompt")
def save_scene_prompt(scene_id: int, payload: PromptInput):
    with db() as connection:
        result = connection.execute("UPDATE scenes SET visual_prompt=? WHERE id=? AND project_id=1",
                                    (payload.visual_prompt.strip(), scene_id))
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
    return {"visual_prompt": payload.visual_prompt.strip()}


def mutation_result(selected_scene_id: int | None = None) -> dict[str, Any]:
    result = get_project_payload()
    result["selected_scene_id"] = selected_scene_id
    return result


@app.post("/api/scenes/{scene_id}/split")
def split_scene(scene_id: int):
    with db() as connection:
        scene = connection.execute("SELECT * FROM scenes WHERE id=? AND project_id=1", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
        if scene["locked"]:
            raise HTTPException(status_code=409, detail="Desbloquea la escena antes de dividirla.")
        midpoint = (scene["start_seconds"] + scene["end_seconds"]) / 2
        if scene["end_seconds"] - scene["start_seconds"] < 1.2:
            raise HTTPException(status_code=409, detail="La escena es demasiado corta para dividirla.")
        narration_words = scene["narration"].split()
        cut = max(1, len(narration_words) // 2)
        first_text = " ".join(narration_words[:cut]).strip()
        second_text = " ".join(narration_words[cut:]).strip()
        if first_text and first_text[-1] not in ".!?":
            first_text += "…"
        connection.execute("UPDATE scenes SET end_seconds=?,narration=? WHERE id=?", (midpoint, first_text or scene["narration"], scene_id))
        connection.execute("UPDATE scenes SET order_index=order_index+1 WHERE project_id=1 AND order_index>?", (scene["order_index"],))
        cursor = connection.execute(
            """INSERT INTO scenes(project_id,order_index,start_seconds,end_seconds,narration,subject,place,era,keywords_json,match_score,visual_prompt)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (1, scene["order_index"] + 1, midpoint, scene["end_seconds"], second_text or scene["narration"], scene["subject"], scene["place"], scene["era"], scene["keywords_json"], scene["match_score"], scene["visual_prompt"]),
        )
        new_scene_id = int(cursor.lastrowid)
        connection.execute("UPDATE review_notes SET scene_id=? WHERE scene_id=? AND time_seconds>=?", (new_scene_id, scene_id, midpoint))
        invalidate_scene_audio(connection, scene_id)
    return mutation_result(new_scene_id)


@app.post("/api/scenes/{scene_id}/merge")
def merge_scene(scene_id: int):
    with db() as connection:
        scene = connection.execute("SELECT * FROM scenes WHERE id=? AND project_id=1", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
        if scene["locked"]:
            raise HTTPException(status_code=409, detail="Desbloquea la escena antes de fusionarla.")
        previous = connection.execute("SELECT * FROM scenes WHERE project_id=1 AND order_index<? ORDER BY order_index DESC LIMIT 1", (scene["order_index"],)).fetchone()
        if previous is None:
            following = connection.execute("SELECT * FROM scenes WHERE project_id=1 AND order_index>? ORDER BY order_index LIMIT 1", (scene["order_index"],)).fetchone()
            if not following:
                raise HTTPException(status_code=409, detail="Debe quedar al menos una escena en el proyecto.")
            if following["locked"]:
                raise HTTPException(status_code=409, detail="Desbloquea la escena siguiente antes de fusionarla.")
            keep, remove = scene, following
            narration = f"{scene['narration'].rstrip()} {following['narration'].lstrip()}".strip()
            start, end = scene["start_seconds"], following["end_seconds"]
        else:
            if previous["locked"]:
                raise HTTPException(status_code=409, detail="Desbloquea la escena anterior antes de fusionarla.")
            keep, remove = previous, scene
            narration = f"{previous['narration'].rstrip()} {scene['narration'].lstrip()}".strip()
            start, end = previous["start_seconds"], scene["end_seconds"]
        keep_id, remove_id = int(keep["id"]), int(remove["id"])
        connection.execute("UPDATE review_notes SET scene_id=? WHERE scene_id=?", (keep_id, remove_id))
        connection.execute("UPDATE scenes SET start_seconds=?,end_seconds=?,narration=? WHERE id=?", (start, end, narration, keep_id))
        connection.execute("DELETE FROM scenes WHERE id=?", (remove_id,))
        connection.execute("UPDATE scenes SET order_index=order_index-1 WHERE project_id=1 AND order_index>?", (remove["order_index"],))
        invalidate_scene_audio(connection, keep_id)
    return mutation_result(keep_id)


@app.post("/api/scenes/{scene_id}/add")
def add_scene(scene_id: int):
    with db() as connection:
        scene = connection.execute("SELECT * FROM scenes WHERE id=? AND project_id=1", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
        if scene["locked"]:
            raise HTTPException(status_code=409, detail="Desbloquea la escena antes de insertar otra.")
        length = 2.6
        start, end = scene["end_seconds"], scene["end_seconds"] + length
        connection.execute("UPDATE scenes SET start_seconds=start_seconds+?,end_seconds=end_seconds+? WHERE project_id=1 AND order_index>?", (length, length, scene["order_index"]))
        connection.execute("UPDATE scenes SET order_index=order_index+1 WHERE project_id=1 AND order_index>?", (scene["order_index"],))
        cursor = connection.execute(
            """INSERT INTO scenes(project_id,order_index,start_seconds,end_seconds,narration,subject,place,era,keywords_json,match_score,visual_prompt)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (1, scene["order_index"] + 1, start, end, "Nueva narración para esta escena.", "Por definir", scene["place"], scene["era"], "[]", 0, "Describe el encuadre, la acción y el estilo visual."),
        )
        new_scene_id = int(cursor.lastrowid)
        connection.execute("UPDATE projects SET duration_seconds=duration_seconds+?,updated_at=? WHERE id=1", (length, utc_now()))
        invalidate_scene_audio(connection)
    return mutation_result(new_scene_id)


@app.post("/api/scenes/{scene_id}/delete")
def delete_scene(scene_id: int):
    with db() as connection:
        scene = connection.execute("SELECT * FROM scenes WHERE id=? AND project_id=1", (scene_id,)).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM scenes WHERE project_id=1").fetchone()[0]
        if not scene:
            raise HTTPException(status_code=404, detail="No se encontró la escena.")
        if count <= 1:
            raise HTTPException(status_code=409, detail="El proyecto debe conservar al menos una escena.")
        if scene["locked"]:
            raise HTTPException(status_code=409, detail="Desbloquea la escena antes de quitarla.")
        length = scene["end_seconds"] - scene["start_seconds"]
        connection.execute("UPDATE review_notes SET scene_id=NULL WHERE scene_id=?", (scene_id,))
        connection.execute("DELETE FROM scenes WHERE id=?", (scene_id,))
        connection.execute("UPDATE scenes SET start_seconds=start_seconds-?,end_seconds=end_seconds-?,order_index=order_index-1 WHERE project_id=1 AND order_index>?", (length, length, scene["order_index"]))
        connection.execute("UPDATE review_notes SET time_seconds=MAX(0,time_seconds-?) WHERE project_id=1 AND time_seconds>=?", (length, scene["end_seconds"]))
        connection.execute("UPDATE projects SET duration_seconds=MAX(0,duration_seconds-?),updated_at=? WHERE id=1", (length, utc_now()))
        invalidate_scene_audio(connection)
        next_scene = connection.execute("SELECT id FROM scenes WHERE project_id=1 AND order_index>=? ORDER BY order_index LIMIT 1", (scene["order_index"],)).fetchone()
        selected_id = int(next_scene["id"]) if next_scene else int(connection.execute("SELECT id FROM scenes WHERE project_id=1 ORDER BY order_index DESC LIMIT 1").fetchone()["id"])
    return mutation_result(selected_id)


class ReviewNoteInput(BaseModel):
    scene_id: int | None = None
    time_seconds: float = Field(ge=0, le=24 * 60 * 60)
    note: str = Field(min_length=3, max_length=2000)


@app.post("/api/review-notes")
def create_review_note(payload: ReviewNoteInput):
    note = payload.note.strip()
    if not note:
        raise HTTPException(status_code=422, detail="Escribe una nota antes de guardarla.")
    with db() as connection:
        if payload.scene_id is not None and not connection.execute("SELECT id FROM scenes WHERE id=? AND project_id=1", (payload.scene_id,)).fetchone():
            raise HTTPException(status_code=404, detail="No se encontró la escena asociada a la nota.")
        cursor = connection.execute(
            "INSERT INTO review_notes(project_id,scene_id,time_seconds,note,created_at) VALUES(1,?,?,?,?)",
            (payload.scene_id, payload.time_seconds, note, utc_now()),
        )
        note_id = cursor.lastrowid
        row = connection.execute("SELECT * FROM review_notes WHERE id=?", (note_id,)).fetchone()
    return row_dict(row)


@app.patch("/api/review-notes/{note_id}")
def resolve_review_note(note_id: int):
    with db() as connection:
        result = connection.execute("UPDATE review_notes SET status='aplicada' WHERE id=? AND project_id=1", (note_id,))
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="No se encontró la nota.")
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "data_dir": str(DATA_DIR)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
