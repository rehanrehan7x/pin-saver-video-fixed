"""
Pin Saver - a small local website for downloading original-quality photos and
videos from Pinterest pins.

Run:   python app.py
Open:  http://127.0.0.1:5000
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import tempfile
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, render_template, request

import pinterest_core as core

app = Flask(__name__)

MAX_LINKS_PER_REQUEST = 20


def lookup_one(raw: str) -> dict:
    raw = raw.strip()
    try:
        info = core.get_pin_info(raw)
    except core.PinError as exc:
        return {"input": raw, "ok": False, "error": str(exc)}
    except Exception:  # noqa: BLE001 - never leak a stack trace to the page
        app.logger.exception("Lookup failed for %s", raw)
        return {"input": raw, "ok": False, "error": "Something went wrong looking up this pin."}
    return {"input": raw, "ok": True, **info.public()}


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/lookup")
def api_lookup():
    payload = request.get_json(silent=True) or {}
    links = [s for s in (payload.get("urls") or []) if isinstance(s, str) and s.strip()]
    if not links:
        return jsonify(error="Paste at least one pin link."), 400
    if len(links) > MAX_LINKS_PER_REQUEST:
        return jsonify(error=f"Please paste {MAX_LINKS_PER_REQUEST} links or fewer at a time."), 400

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lookup_one, links))
    return jsonify(results=results)


@app.get("/download")
def download():
    raw = request.args.get("url", "")
    tmp_dir = tempfile.mkdtemp(prefix="pinsaver_")
    try:
        info = core.get_pin_info(raw)
        path = core.download_media(info, tmp_dir)
    except core.PinError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify(error=str(exc)), exc.status
    except Exception:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        app.logger.exception("Download failed for %s", raw)
        return jsonify(error="Something went wrong downloading this pin."), 500

    filename = os.path.basename(path)

    def stream_then_clean_up():
        # send_file() skips Flask's close hooks, so stream the file ourselves and
        # delete the temp copy when the transfer ends (or the browser cancels).
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1 << 16):
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    response = Response(
        stream_then_clean_up(),
        mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )
    response.headers.add("Content-Disposition", "attachment", filename=filename)
    response.headers["Content-Length"] = str(os.path.getsize(path))
    return response


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    app.run(host=host, port=port, debug=False, threaded=True)
