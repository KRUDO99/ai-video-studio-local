"""Exercise the voice routes with simulated Gemini/ElevenLabs responses."""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_engine import GEMINI_TTS_MODEL, audio_duration, ffmpeg_run
from test_media_e2e import expect, offline_voice, wait_for_job


def exercise(root: Path, edge_live: bool) -> None:
    with patch.dict("os.environ", {"LOCALAPPDATA": str(root)}):
        import main

    pcm = b"".join(struct.pack("<h", round(2000 * math.sin(2 * math.pi * 300 * i / 24000)))
                   for i in range(12000))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    fixture = root / "voice.mp3"
    wav_fixture = root / "voice.wav"
    wav_fixture.write_bytes(buffer.getvalue())
    ffmpeg_run(["-v", "error", "-i", str(wav_fixture), str(fixture)])

    calls = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.host == "generativelanguage.googleapis.com":
            assert request.url.path == f"/v1beta/models/{GEMINI_TTS_MODEL}:generateContent"
            assert request.headers["x-goog-api-key"] == "stored-gemini-key"
            assert not request.url.query
            settings = payload["generationConfig"]
            assert settings["responseModalities"] == ["AUDIO"]
            voice = settings["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
            calls.append(voice)
            text = payload["contents"][0]["parts"][0]["text"]
            if "CASO DE CUOTA" in text:
                return httpx.Response(429, json={"error": {"message": "Cuota agotada"}})
            if voice == "Aoede":
                data, mime = buffer.getvalue(), "audio/wav"
            else:
                data, mime = pcm, "audio/L16;codec=pcm;rate=24000"
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(data).decode(), "mimeType": mime}}]}}],
                "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 15},
            })
        assert request.url.host == "api.elevenlabs.io"
        assert request.url.path == "/v1/text-to-speech/test-voice-id"
        assert request.headers["xi-api-key"] == "stored-elevenlabs-key"
        assert payload["model_id"] == "eleven_multilingual_v2"
        calls.append("elevenlabs")
        return httpx.Response(200, content=fixture.read_bytes())

    secrets = {"elevenlabs": "stored-elevenlabs-key"}

    def get_key(service, account):
        assert service == main.KEYRING_SERVICE
        return secrets.get(account)

    actual_synth = main.synthesize_speech
    actual_client = httpx.AsyncClient

    async def synthesis(provider, *args, **kwargs):
        engine = offline_voice if provider == "edge" and not edge_live else actual_synth
        return await engine(provider, *args, **kwargs)

    with (
        patch("main.keyring.get_password", side_effect=get_key),
        patch("main.synthesize_speech", synthesis),
        patch("media_engine.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(
            transport=httpx.MockTransport(provider_response), **kwargs)),
        TestClient(main.app, headers={"X-Local-App-Token": main.APP_TOKEN}) as client,
    ):
        script = """ESCENA 1
LOCUCIÓN: Una voz para esta primera escena.
PROMPT VISUAL: Un río.
ESCENA 2
LOCUCIÓN: La siguiente escena ocurre en el valle.
PROMPT VISUAL: Un valle.
"""
        project = expect(client.post("/api/scenes/apply-script", json={"text": script, "structured": True}))
        first, second = [scene["id"] for scene in project["scenes"]]
        gemini = {"provider": "gemini", "voice": "Kore", "gemini_tts_tier": "free"}
        missing = expect(client.post("/api/audio/tts", json={**gemini, "scene_id": first}), 409)
        assert "Modelos de lenguaje" in missing["detail"]
        expect(client.post("/api/audio/voice/sample", json=gemini), 409)
        secrets["gemini"] = "stored-gemini-key"
        provider = next(provider for provider in expect(client.get("/api/providers"))["providers"] if provider["id"] == "gemini")
        assert provider["configured"] and set(provider["speech"]["voices"]) == {"Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda"}
        expect(client.put("/api/audio/voice", json=gemini))
        expect(client.post("/api/audio/voice/sample", json={**gemini, "voice": "Invalid"}), 422)

        sample = wait_for_job(client, client.post("/api/audio/voice/sample", json={**gemini, "voice": "Aoede", "gemini_tts_tier": "paid"}))
        sample_response = client.get(sample["audio_url"])
        assert sample_response.status_code == 200 and sample["estimated_cost"] == .00016
        with wave.open(io.BytesIO(sample_response.content), "rb") as audio:
            assert audio.getframerate() == 24000 and audio.readframes(12000) == pcm
        cached = wait_for_job(client, client.post("/api/audio/voice/sample", json={**gemini, "voice": "Aoede"}))
        assert cached["cached"] and cached["estimated_cost"] == 0 and calls.count("Aoede") == 1

        one = wait_for_job(client, client.post("/api/audio/tts", json={**gemini, "scene_id": first, "voice": "Puck"}))
        assert one["scenes"] == 1 and one["estimated_cost"] == 0
        bulk = wait_for_job(client, client.post("/api/audio/tts", json=gemini))
        project = expect(client.get("/api/bootstrap"))
        assert bulk["scenes"] == 2 and bulk["duration_seconds"] == 1
        for scene in project["scenes"]:
            assert scene["tts_provider"] == "gemini"
            assert scene["tts_duration"] == audio_duration(scene["tts_path"]) == .5
            ffmpeg_run(["-v", "error", "-i", scene["tts_path"], "-f", "null", "-"])
        assert project["project"]["gemini_tts_tier"] == "free"
        assert project["scenes"][1]["start_seconds"] == .5

        expect(client.put(f"/api/scenes/{first}", json={"narration": "CASO DE CUOTA"}))
        failed = expect(client.post("/api/audio/tts", json={**gemini, "scene_id": first}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = expect(client.get(f"/api/jobs/{failed['job_id']}"))
            if job["status"] == "error":
                assert "429" in job["error"] and "Cuota agotada" in job["error"]
                break
            time.sleep(.1)
        else:
            raise AssertionError("La respuesta 429 no se propagó a la tarea.")
        expect(client.put(f"/api/scenes/{first}", json={"narration": "Locución de prueba con Edge."}))
        assert wait_for_job(client, client.post("/api/audio/tts", json={"provider": "edge", "scene_id": first}))["duration_seconds"] > .5
        assert wait_for_job(client, client.post("/api/audio/tts", json={
            "provider": "elevenlabs", "voice": "test-voice-id", "scene_id": second,
        }))["estimated_cost"] > 0
        final = expect(client.get("/api/bootstrap"))
        assert final["project"]["voice_provider"] == "gemini"
        assert [scene["tts_provider"] for scene in final["scenes"]] == ["edge", "elevenlabs"]
        assert calls.count("Kore") == 3 and "Puck" in calls and "elevenlabs" in calls


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-live", action="store_true", help="Usar Edge-TTS real para una escena de prueba.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="studio-voice-test-") as directory:
        exercise(Path(directory), args.edge_live)
    print(f"PASS: Gemini PCM y WAV, muestra, clave compartida, cuota, lote, ElevenLabs simulado y Edge {'real' if args.edge_live else 'simulado'}.")
