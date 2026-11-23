from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_engine import audio_duration, ffmpeg_exe, ffmpeg_run


SCRIPT = """## ESCENA 3
**LOCUCIÓN:** FFmpeg mezcla el sonido y exporta un video válido.
**PROMPT VISUAL:** Horizonte en un día soleado.

## ESCENA 1
**LOCUCIÓN:** Hola, esta es una prueba local del estudio.
**PROMPT VISUAL:** Ilustración de un puente.

ESCENA 2
NARRACIÓN:
Cada escena recibe tiempos medidos desde su propia locución.
PROMPT VISUAL: 
Un río tranquilo.
Plano general.
"""


async def offline_voice(provider, narration, output, **settings):
    duration = 1.25 + len(narration.split()) / 16
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_run(["-f", "lavfi", "-i", f"sine=frequency=350:duration={duration}", str(output)])
    return {"duration": audio_duration(output), "characters": len(narration), "cost": 0, "model": "", "provider": provider}


def expect(response, status=200):
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def wait_for_job(client, response):
    job = expect(response)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        status = expect(client.get(f"/api/jobs/{job['job_id']}"))
        if status["status"] == "error":
            raise AssertionError(status["message"])
        if status["status"] == "complete":
            return status["result"]
        time.sleep(.4)
    raise AssertionError("La tarea no terminó en cuatro minutos.")


def exercise(root, offline):
    os.environ["LOCALAPPDATA"] = str(root)
    import main

    if offline:
        main.synthesize_speech = offline_voice
    print(f"FFmpeg: {ffmpeg_exe()}", flush=True)
    headers = {"X-Local-App-Token": main.APP_TOKEN}
    with TestClient(main.app, headers=headers) as client:
        assert client.get("/").status_code == 200
        for asset in ("app.js", "app.css", "app-overrides.css", "media.css"):
            assert client.get(f"/static/{asset}").status_code == 200
        expect(client.get("/api/health"))
        expect(client.post("/api/scenes/apply-script", json={"text": "ESCENA 1\nLOCUCIÓN: Hola", "structured": True}), 422)
        imported = expect(client.post("/api/scenes/apply-script", json={"text": SCRIPT, "structured": True}))
        first, second, third = imported["scenes"]
        assert first["narration"].startswith("Hola")
        assert second["visual_prompt"] == "Un río tranquilo.\nPlano general."
        first_id = first["id"]

        expect(client.patch(f"/api/scenes/{first_id}/prompt", json={"visual_prompt": "Puente en plano general"}))
        preset = expect(client.post("/api/styles", json={"name": "Cómic", "style_prompt": "Tinta oscura"}))
        assert preset["style"]["name"] == "Cómic"
        original_style = imported["style"]["id"]
        assert expect(client.put(f"/api/styles/{original_style}/select"))["style"]["id"] == original_style
        expect(client.put(f"/api/styles/{preset['style']['id']}/select"))

        for index in (3, 1, 2):
            path = root / f"escena_{index}.png"
            card = Image.new("RGB", (1536, 1024), ["#d8a87c", "#37657f", "#815a50"][index - 1])
            ImageDraw.Draw(card).ellipse((350, 100, 1100, 920), fill="#edddaa")
            card.save(path)
        uploaded = [("files", (f"escena_{i}.png", (root / f"escena_{i}.png").read_bytes(), "image/png")) for i in (3, 1, 2)]
        assert expect(client.post("/api/resources/images", files=uploaded))["uploaded"] == 3
        assert expect(client.post("/api/resources/auto-assign"))["assigned"] == 3
        assert expect(client.post("/api/resources/auto-assign"))["assigned"] == 0
        project = expect(client.get("/api/bootstrap"))
        images_by_name = {item["name"]: item["url"] for item in project["assets"]}
        for i, scene in enumerate(project["scenes"], start=1):
            assert scene["image_url"] == images_by_name[f"escena_{i}.png"]
            assert client.get(scene["image_url"]).status_code == 200
        asset_id = next(item["id"] for item in project["assets"] if item["name"] == "escena_3.png")
        reassigned = expect(client.put(f"/api/resources/{asset_id}/assign", json={"scene_id": third["id"]}))
        assert reassigned["scenes"][2]["image_url"] == images_by_name["escena_3.png"]
        uploaded_scene = expect(client.post(f"/api/scenes/{third['id']}/image", files={"file": uploaded[0][1]}))
        assert client.get(uploaded_scene["image_url"]).status_code == 200

        print("Generando voces de prueba…" if offline else "Generando voces reales con Edge‑TTS…", flush=True)
        assert wait_for_job(client, client.post("/api/audio/tts", json={"provider": "edge", "scene_id": second["id"]}))["scenes"] == 1
        saved = expect(client.get("/api/bootstrap"))["scenes"][1]["audio_url"]
        assert wait_for_job(client, client.post("/api/audio/tts", json={"provider": "edge", "pending_only": True}))["scenes"] == 2
        assert expect(client.post("/api/audio/tts", json={"provider": "edge", "pending_only": True}))["scenes"] == 0
        project = expect(client.get("/api/bootstrap"))
        assert project["scenes"][1]["audio_url"] == saved
        cursor = 0
        for scene in project["scenes"]:
            assert scene["audio_url"] and abs(scene["start_seconds"] - cursor) < 1e-8
            actual = audio_duration(scene["tts_path"])
            assert abs(scene["end_seconds"] - scene["start_seconds"] - actual) < 1e-6
            cursor = scene["end_seconds"]
        assert abs(project["project"]["duration_seconds"] - cursor) < 1e-8
        assert project["costs"]["total_cost"] == 0
        assert client.get(project["project"]["audio_url"]).status_code == 200

        music = root / "music.wav"
        ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=170:duration=1", str(music)])
        expect(client.post("/api/audio/music", files={"file": ("music.wav", music.read_bytes(), "audio/wav")}))
        expect(client.post("/api/audio/sfx", data={"start_seconds": "0.5"},
                           files={"file": ("effect.wav", music.read_bytes(), "audio/wav")}))
        logo = root / "logo.png"
        Image.new("RGBA", (150, 50), (30, 40, 50, 160)).save(logo)
        expect(client.post("/api/audio/logo", files={"file": ("logo.png", logo.read_bytes(), "image/png")}))
        note = expect(client.post("/api/review-notes", json={"scene_id": second["id"], "time_seconds": project["scenes"][1]["start_seconds"], "note": "cambia el zoom a paneo a la derecha"}))
        sidecar = wait_for_job(client, client.post("/api/render", json={"aspect_ratio": "16:9", "subtitle_mode": "srt"}))
        assert (sidecar["width"], sidecar["height"]) == (1920, 1080)
        assert sidecar["applied_notes"] >= 1
        reviewed = expect(client.get("/api/bootstrap"))
        assert reviewed["scenes"][1]["motion"] == "pan_right"
        assert any(item["id"] == note["id"] and item["status"] == "aplicada" for item in reviewed["notes"])
        subtitle_response = client.get(sidecar["subtitle_url"])
        assert subtitle_response.status_code == 200
        assert "00:00:00,000 -->" in subtitle_response.text and "Hola" in subtitle_response.text
        assert "00:00:0" in subtitle_response.text and "sonido" in subtitle_response.text
        vertical = wait_for_job(client, client.post("/api/render", json={"aspect_ratio": "9:16", "subtitle_mode": "burn"}))
        assert (vertical["width"], vertical["height"]) == (1080, 1920)
        assert not vertical["subtitle_url"]
        for result in (sidecar, vertical):
            assert abs(result["duration"] - cursor) <= 1 / 30
            response = client.get(result["video_url"], headers={"Range": "bytes=0-7"})
            assert response.status_code == 206 and response.content[4:] == b"ftyp"
            ffmpeg_run(["-v", "error", "-i", result["path"], "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])

    main.initialize_database()
    with TestClient(main.app, headers=headers) as client:
        reopened = expect(client.get("/api/bootstrap"))
        assert reopened["style"]["name"] == "Cómic"
        assert reopened["scenes"][0]["visual_prompt"] == "Puente en plano general"
        assert len(reopened["assets"]) == 4
        assert reopened["project"]["last_export_url"] == vertical["video_url"]
        assert reopened["project"]["subtitle_mode"] == "burn"
    print(f"PASS: SQLite, importación, recursos, presets, voz por escena, lote pendiente y MP4 de {cursor:.2f} s en ambos formatos.")


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="Sustituye Edge‑TTS por tonos de prueba generados localmente.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ai-video-studio-e2e-") as directory, patch.dict(os.environ):
        exercise(Path(directory), args.offline)


if __name__ == "__main__":
    run()
