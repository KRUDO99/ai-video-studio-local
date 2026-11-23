from __future__ import annotations

import threading
import time

import webview
import uvicorn

from main import app


def open_studio() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.15)
    if not server.started:
        raise RuntimeError("No se pudo iniciar el servicio local del estudio.")
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window("AI Video Studio Local", "http://127.0.0.1:8765", width=1500, height=960, min_size=(1100, 720), background_color="#0b0d11")
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    open_studio()
