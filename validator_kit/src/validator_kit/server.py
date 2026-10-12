"""Local browser UI. The TIFF and GeoJSON stay on this machine."""

from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from validator_kit.session import Session, SessionError

STATIC = Path(__file__).resolve().parent / "static"
_lock = threading.Lock()
_session = Session()


def main() -> None:
    parser = argparse.ArgumentParser(description="Label GeoJSON features on a GeoTIFF.")
    parser.add_argument("--tiff", help="post GeoTIFF drawn under the features")
    parser.add_argument("--pre", help="optional pre GeoTIFF with the same width and height")
    parser.add_argument("--geojson", help="FeatureCollection to label")
    parser.add_argument("--field", default="label", help="property name written on save (default: label)")
    parser.add_argument("--tile", type=int, default=1024, help="pixel size of one tile in the 3x3 page")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    _session.tile_px = args.tile
    if args.tiff and args.geojson:
        try:
            _session.load(args.tiff, args.geojson, args.field, args.pre or "")
        except SessionError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.tiff or args.geojson:
        raise SystemExit("pass both --tiff and --geojson, or neither and choose them in the page")

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Validator_Kit {url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        _session.close()
        server.server_close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
        elif path == "/app.css":
            self._file(STATIC / "app.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._file(STATIC / "app.js", "text/javascript; charset=utf-8")
        elif path == "/api/state":
            self._json(self._state())
        elif path == "/api/raster":
            self._raster()
        elif path == "/api/overview":
            self._overview()
        elif path == "/api/features":
            self._features()
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/load":
                self._load(body)
            elif path == "/api/browse":
                self._browse(body)
            elif path == "/api/move":
                self._move(body)
            elif path == "/api/labels":
                self._labels(body)
            elif path == "/api/import":
                self._import(body)
            elif path == "/api/assign":
                self._assign(body)
            elif path == "/api/delete":
                self._delete(body)
            elif path == "/api/reset":
                self._reset()
            elif path == "/api/undo":
                self._undo()
            elif path == "/api/save":
                self._save(body)
            else:
                self.send_error(404)
        except SessionError as exc:
            self._json({"error": str(exc)}, status=400)
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def log_message(self, fmt: str, *args) -> None:
        if args and str(args[1]).startswith("4"):
            super().log_message(fmt, *args)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise SessionError("JSON body must be an object")
        return data

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _state(self) -> dict:
        with _lock:
            return _session.state()

    def _raster(self) -> None:
        layer = (parse_qs(urlparse(self.path).query).get("layer") or ["post"])[0]
        with _lock:
            try:
                payload = _session.render_jpeg(layer)
            except SessionError as exc:
                self._json({"error": str(exc)}, status=400)
                return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _overview(self) -> None:
        with _lock:
            try:
                payload = _session.render_overview_jpeg()
            except SessionError as exc:
                self._json({"error": str(exc)}, status=400)
                return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _features(self) -> None:
        with _lock:
            try:
                features = _session.features_in_view()
            except SessionError as exc:
                self._json({"error": str(exc)}, status=400)
                return
        self._json({"features": features})

    def _load(self, body: dict) -> None:
        with _lock:
            _session.load(
                body.get("tiff", ""),
                body.get("geojson", ""),
                body.get("field") or "label",
                body.get("pre") or "",
            )
            state = _session.state()
        self._json(state)

    def _browse(self, body: dict) -> None:
        self._json({"path": _pick_file(str(body.get("kind") or ""))})

    def _move(self, body: dict) -> None:
        with _lock:
            tile = body.get("tile")
            _session.move(
                int(body.get("dc") or 0),
                int(body.get("dr") or 0),
                page=bool(body.get("page")),
                tile=int(tile) if tile else None,
            )
            state = _session.state()
        self._json(state)

    def _labels(self, body: dict) -> None:
        with _lock:
            _session.set_labels(
                list(body.get("names") or []),
                dict(body.get("rename") or {}),
                body.get("field"),
            )
            state = _session.state()
        self._json(state)

    def _import(self, body: dict) -> None:
        with _lock:
            _session.import_column(str(body.get("column") or ""))
            state = _session.state()
        self._json(state)

    def _assign(self, body: dict) -> None:
        with _lock:
            _session.assign(list(body.get("ids") or []), body.get("label"))
            state = _session.state()
        self._json(state)

    def _delete(self, body: dict) -> None:
        with _lock:
            _session.delete(list(body.get("ids") or []))
            state = _session.state()
        self._json(state)

    def _reset(self) -> None:
        with _lock:
            _session.reset()
            state = _session.state()
        self._json(state)

    def _undo(self) -> None:
        with _lock:
            _session.undo()
            state = _session.state()
        self._json(state)

    def _save(self, body: dict) -> None:
        with _lock:
            dest, written, removed = _session.save(body.get("name") or None)
        self._json({"path": str(dest), "written": written, "removed": removed})


def _pick_file(kind: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    root.update()
    if kind in {"tiff", "pre"}:
        path = filedialog.askopenfilename(
            parent=root,
            title="Pre GeoTIFF" if kind == "pre" else "Post GeoTIFF",
            filetypes=[("GeoTIFF", "*.tif *.tiff"), ("All files", "*.*")],
        )
    else:
        path = filedialog.askopenfilename(
            parent=root,
            title="GeoJSON",
            filetypes=[("GeoJSON", "*.geojson *.json"), ("All files", "*.*")],
        )
    root.destroy()
    return path or ""


if __name__ == "__main__":
    main()
