"""TIFF tile pages and GeoJSON labels. The raster extent is the map; no AOI file."""

from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import transform_geom
from rasterio.windows import Window
from shapely import transform as map_coords
from shapely.geometry import box, shape
from shapely.strtree import STRtree

EMPTY_LABELS = {"", "unreviewed", "none", "null", "nan"}
PALETTE = (
    "#e41a1c",
    "#377eb8",
    "#4daf4a",
    "#984ea3",
    "#ff7f00",
    "#a65628",
    "#f781bf",
    "#66c2a5",
    "#fc8d62",
    "#8da0cb",
    "#e78ac3",
    "#a6d854",
)


class SessionError(Exception):
    pass


class Session:
    def __init__(self) -> None:
        self.tiff_path: Path | None = None
        self.geojson_path: Path | None = None
        self._src: rasterio.DatasetReader | None = None
        self.collection: dict | None = None
        self.features: list[dict] = []
        self.working: list[str | None] = []
        self.dirty: set[int] = set()
        self.label_names: list[str] = []
        self.label_field = "label"
        self.tile_px = 1024
        self.origin_col = 0
        self.origin_row = 0
        self.property_keys: list[str] = []
        self.warning = ""
        self._done_pages: set[tuple[int, int]] = set()
        self._overview_jpeg: bytes | None = None
        self._pixel_geoms: list = []
        self._tree_ids: list[int] = []
        self._tree: STRtree | None = None
        self._undo: list[list[tuple[int, str | None, bool]]] = []
        self._backed_up: set[Path] = set()

    def close(self) -> None:
        if self._src is not None:
            self._src.close()
            self._src = None
        self._overview_jpeg = None

    def load(self, tiff: str, geojson: str, label_field: str = "label") -> None:
        tiff_path = Path(tiff)
        geojson_path = Path(geojson)
        if not tiff_path.is_file():
            raise SessionError(f"TIFF not found: {tiff_path}")
        if not geojson_path.is_file():
            raise SessionError(f"GeoJSON not found: {geojson_path}")
        field = _clean_field(label_field)
        src = rasterio.open(tiff_path)
        try:
            collection = json.loads(geojson_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            src.close()
            raise SessionError(f"GeoJSON is not valid JSON: {exc}") from exc
        if isinstance(collection, dict) and collection.get("type") == "Feature":
            collection = {"type": "FeatureCollection", "features": [collection]}
        features = _feature_list(collection)
        self.close()
        self._src = src
        self.tiff_path = tiff_path
        self.geojson_path = geojson_path
        self.collection = collection
        self.features = features
        self.label_field = field
        self.label_names = []
        self.working = [None] * len(features)
        self.dirty = set()
        self._undo.clear()
        self.origin_col = 0
        self.origin_row = 0
        self._done_pages = set()
        self._overview_jpeg = None
        self.warning = ""
        self.property_keys = _property_keys(features)
        self._index_features()

    def grid_size(self) -> tuple[int, int]:
        src = self._require_src()
        cols = max(1, math.ceil(src.width / self.tile_px))
        rows = max(1, math.ceil(src.height / self.tile_px))
        return cols, rows

    def move(self, dc: int, dr: int, *, page: bool = False, tile: int | None = None) -> None:
        self._require_src()
        if tile is not None:
            size = int(tile)
            if size < 256 or size > 4096:
                raise SessionError("tile size must be between 256 and 4096")
            if size != self.tile_px:
                self.tile_px = size
                self.origin_col = 0
                self.origin_row = 0
                self._done_pages.clear()
                self._clamp_origin()
                return
        if page:
            direction = 1 if dc > 0 or dr > 0 else -1 if dc < 0 or dr < 0 else 0
            self._step_page(direction)

    def set_labels(self, names: list[str], rename: dict[str, str] | None, field: str | None) -> None:
        self._require_src()
        if field:
            self.label_field = _clean_field(field)
        if rename:
            mapping = {str(old): str(new) for old, new in rename.items() if str(old) != str(new)}
            self.label_names = [mapping.get(name, name) for name in self.label_names]
            self.working = [mapping.get(label, label) if label else None for label in self.working]
        cleaned: list[str] = []
        for raw in names:
            name = str(raw).strip()
            if not name or name in cleaned:
                continue
            cleaned.append(name)
        self.label_names = cleaned
        allowed = set(cleaned)
        for index, label in enumerate(self.working):
            if label and label not in allowed:
                self.working[index] = None
                self.dirty.add(index)

    def import_column(self, column: str) -> None:
        self._require_src()
        column = column.strip()
        if not column or column == "geometry":
            raise SessionError("choose a property column to import")
        if column not in self.property_keys:
            raise SessionError(f"column {column!r} is not on these features")
        # Most workflows start by choosing a column to review (e.g. damage/review_status).
        # When you import a column, default to writing back into that same column.
        self.label_field = column
        names: list[str] = []
        seen: set[str] = set()
        working: list[str | None] = []
        for feature in self.features:
            value = _label_value((feature.get("properties") or {}).get(column))
            working.append(value)
            if value and value not in seen:
                seen.add(value)
                names.append(value)
        self.working = working
        self.label_names = names
        self.dirty = set()
        self._undo.clear()

    def assign(self, ids: list[int], label: str | None) -> None:
        self._require_src()
        if label is not None:
            label = label.strip()
            if not label:
                label = None
            elif label not in self.label_names:
                raise SessionError(f"unknown label {label!r}")
        step: list[tuple[int, str | None, bool]] = []
        for raw in ids:
            index = int(raw)
            if index < 0 or index >= len(self.features):
                continue
            step.append((index, self.working[index], index in self.dirty))
            self.working[index] = label
            self.dirty.add(index)
        if step:
            self._undo.append(step)
            if len(self._undo) > 50:
                self._undo.pop(0)

    def undo(self) -> None:
        self._require_src()
        if not self._undo:
            return
        for index, label, was_dirty in reversed(self._undo.pop()):
            self.working[index] = label
            if was_dirty:
                self.dirty.add(index)
            else:
                self.dirty.discard(index)

    def save(self, path: str | None = None) -> tuple[Path, int]:
        self._require_src()
        if self.collection is None or self.geojson_path is None:
            raise SessionError("nothing is loaded")
        dest = Path(path) if path else self.geojson_path
        written = 0
        for index, feature in enumerate(self.features):
            current = self.working[index]
            props = feature.setdefault("properties", {})
            existing = props.get(self.label_field)
            if index not in self.dirty and (current is None or existing == current):
                continue
            props[self.label_field] = "" if current is None else current
            written += 1
        if dest.exists() and dest.resolve() not in self._backed_up:
            backup = dest.with_suffix(dest.suffix + ".bak")
            if not backup.exists():
                shutil.copy2(dest, backup)
            self._backed_up.add(dest.resolve())
        dest.write_text(json.dumps(self.collection, ensure_ascii=False), encoding="utf-8")
        return dest, written

    def state(self) -> dict:
        if self._src is None:
            return {"loaded": False}
        cols, rows = self.grid_size()
        window, origin = self._view_window()
        labeled = sum(1 for label in self.working if label)
        return {
            "loaded": True,
            "tiff": str(self.tiff_path),
            "geojson": str(self.geojson_path),
            "width": self._src.width,
            "height": self._src.height,
            "crs": self._src.crs.to_string() if self._src.crs else "",
            "tile": self.tile_px,
            "label_field": self.label_field,
            "label_names": list(self.label_names),
            "colors": {name: PALETTE[i % len(PALETTE)] for i, name in enumerate(self.label_names)},
            "keys": self.property_keys,
            "warning": self.warning,
            "grid": {
                "cols": cols,
                "rows": rows,
                "origin_col": self.origin_col,
                "origin_row": self.origin_row,
                "view_cols": min(3, max(0, cols - self.origin_col)),
                "view_rows": min(3, max(0, rows - self.origin_row)),
            },
            "pages": self._page_state(),
            "view": {
                "x0": origin[0],
                "y0": origin[1],
                "width": int(window.width),
                "height": int(window.height),
                "seams_x": _seams(int(window.width), self.tile_px),
                "seams_y": _seams(int(window.height), self.tile_px),
            },
            "stats": {
                "total": len(self.features),
                "labeled": labeled,
                "unlabeled": len(self.features) - labeled,
                "by_label": dict(Counter(label for label in self.working if label)),
                "on_map": len(self._tree_ids),
            },
        }

    def render_jpeg(self) -> bytes:
        src = self._require_src()
        window, _origin = self._view_window()
        indexes = [1] if src.count == 1 else list(range(1, min(3, src.count) + 1))
        data = src.read(indexes, window=window)
        return _jpeg(_as_rgb(data))

    def render_overview_jpeg(self, max_edge: int = 480) -> bytes:
        if self._overview_jpeg is not None:
            return self._overview_jpeg
        src = self._require_src()
        long_edge = max(src.width, src.height, 1)
        scale = min(1.0, max_edge / long_edge)
        out_w = max(1, int(round(src.width * scale)))
        out_h = max(1, int(round(src.height * scale)))
        indexes = [1] if src.count == 1 else list(range(1, min(3, src.count) + 1))
        data = src.read(indexes, out_shape=(len(indexes), out_h, out_w))
        self._overview_jpeg = _jpeg(_as_rgb(data), quality=70)
        return self._overview_jpeg

    def features_in_view(self) -> list[dict]:
        self._require_src()
        window, (x0, y0) = self._view_window()
        if self._tree is None:
            return []
        query = box(x0, y0, x0 + window.width, y0 + window.height)
        hits = self._tree.query(query, predicate="intersects")
        payload = []
        for tree_index in hits:
            index = self._tree_ids[int(tree_index)]
            geom = self._pixel_geoms[index]
            kind, parts = _draw_parts(geom, x0, y0)
            if not parts:
                continue
            props = self.features[index].get("properties") or {}
            payload.append(
                {
                    "id": index,
                    "label": self.working[index],
                    "name": _feature_name(props, index),
                    "tip": _feature_tip(props, self.working[index]),
                    "type": kind,
                    "parts": parts,
                }
            )
        return payload

    def _index_features(self) -> None:
        src = self._require_src()
        source_crs = _geojson_crs(self.collection or {})
        raster_crs = src.crs
        inverse = ~src.transform
        self._pixel_geoms = []
        geoms = []
        tree_ids = []
        outside = 0
        failed = 0
        full = box(0, 0, src.width, src.height)
        for index, feature in enumerate(self.features):
            raw = feature.get("geometry")
            if not raw:
                self._pixel_geoms.append(None)
                continue
            try:
                mapped = raw
                if raster_crs is not None and source_crs is not None:
                    mapped = transform_geom(source_crs, raster_crs, raw)
                world = shape(mapped)
                if world.is_empty:
                    raise ValueError("empty geometry")
                pixel = map_coords(world, _world_to_pixel(inverse))
                if pixel.geom_type in {"Polygon", "MultiPolygon"} and not pixel.is_valid:
                    pixel = pixel.buffer(0)
                pixel = pixel.simplify(0.6, preserve_topology=True)
            except Exception:
                failed += 1
                self._pixel_geoms.append(None)
                continue
            self._pixel_geoms.append(pixel)
            if pixel.is_empty or not pixel.intersects(full):
                outside += 1
                continue
            geoms.append(pixel)
            tree_ids.append(index)
        self._tree_ids = tree_ids
        self._tree = STRtree(geoms) if geoms else None
        notes = []
        if not tree_ids:
            notes.append(
                "No features fall on this TIFF. GeoJSON without a crs is read as EPSG:4326 and reprojected onto the image."
            )
        if failed:
            notes.append(f"{failed} features had a geometry that could not be drawn.")
        if outside and tree_ids:
            notes.append(f"{outside} features sit outside the TIFF and are not drawn.")
        self.warning = " ".join(notes)

    def _view_window(self) -> tuple[Window, tuple[int, int]]:
        src = self._require_src()
        self._clamp_origin()
        x0 = self.origin_col * self.tile_px
        y0 = self.origin_row * self.tile_px
        x1 = min(src.width, x0 + 3 * self.tile_px)
        y1 = min(src.height, y0 + 3 * self.tile_px)
        return Window(x0, y0, x1 - x0, y1 - y0), (x0, y0)

    def _page_shape(self) -> tuple[int, int]:
        cols, rows = self.grid_size()
        return max(1, math.ceil(cols / 3)), max(1, math.ceil(rows / 3))

    def _step_page(self, direction: int) -> None:
        page_cols, page_rows = self._page_shape()
        pc = min(self.origin_col // 3, page_cols - 1)
        pr = min(self.origin_row // 3, page_rows - 1)
        index = pr * page_cols + pc
        count = page_cols * page_rows
        if direction > 0:
            self._done_pages.add((pc, pr))
            if index + 1 < count:
                index += 1
        elif direction < 0 and index > 0:
            index -= 1
        pr, pc = divmod(index, page_cols)
        self.origin_col = pc * 3
        self.origin_row = pr * 3
        self._clamp_origin()

    def _page_state(self) -> dict:
        page_cols, page_rows = self._page_shape()
        pc = min(self.origin_col // 3, page_cols - 1)
        pr = min(self.origin_row // 3, page_rows - 1)
        return {
            "cols": page_cols,
            "rows": page_rows,
            "col": pc,
            "row": pr,
            "index": pr * page_cols + pc,
            "count": page_cols * page_rows,
            "done": [[c, r] for c, r in sorted(self._done_pages)],
            "width": self._src.width if self._src else 0,
            "height": self._src.height if self._src else 0,
            "tile": self.tile_px,
        }

    def _clamp_origin(self) -> None:
        cols, rows = self.grid_size()
        self.origin_col = min(max(0, self.origin_col), max(0, cols - 1))
        self.origin_row = min(max(0, self.origin_row), max(0, rows - 1))

    def _require_src(self) -> rasterio.DatasetReader:
        if self._src is None:
            raise SessionError("load a TIFF and a GeoJSON first")
        return self._src


def _as_rgb(data: np.ndarray) -> np.ndarray:
    if data.shape[0] == 1:
        data = np.concatenate([data, data, data], axis=0)
    elif data.shape[0] == 2:
        data = np.concatenate([data, data[:1]], axis=0)
    if data.dtype == np.uint8:
        return np.moveaxis(data, 0, -1)
    arr = data.astype(np.float32)
    lo = float(np.percentile(arr, 2))
    hi = float(np.percentile(arr, 98))
    if hi <= lo:
        hi = lo + 1.0
    arr = np.clip((arr - lo) / (hi - lo), 0, 1)
    return (np.moveaxis(arr, 0, -1) * 255).astype(np.uint8)


def _jpeg(rgb: np.ndarray, quality: int = 82) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _world_to_pixel(inverse):
    def convert(coords):
        xs = coords[:, 0]
        ys = coords[:, 1]
        cols = inverse.a * xs + inverse.b * ys + inverse.c
        rows = inverse.d * xs + inverse.e * ys + inverse.f
        return np.column_stack((cols, rows))

    return convert


def _feature_list(collection: dict) -> list[dict]:
    if not isinstance(collection, dict):
        raise SessionError("GeoJSON root must be an object")
    if collection.get("type") != "FeatureCollection":
        raise SessionError("GeoJSON must be a FeatureCollection or a Feature")
    features = collection.get("features")
    if not isinstance(features, list):
        raise SessionError("FeatureCollection has no features list")
    return features


def _geojson_crs(collection: dict) -> str:
    raw = collection.get("crs") if isinstance(collection, dict) else None
    if not raw:
        return "EPSG:4326"
    name = raw.get("properties", {}).get("name", "") if isinstance(raw, dict) else str(raw)
    upper = str(name).upper()
    if "CRS84" in upper or "4326" in upper:
        return "EPSG:4326"
    digits = "".join(ch if ch.isdigit() else " " for ch in upper).split()
    if "EPSG" in upper and digits:
        return f"EPSG:{digits[-1]}"
    return "EPSG:4326"


def _property_keys(features: list[dict]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for feature in features:
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            continue
        for key in props:
            if key not in seen and key != "geometry":
                seen.add(key)
                keys.append(key)
    return keys


def _label_value(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in EMPTY_LABELS:
        return None
    return text


def _clean_field(field: str) -> str:
    name = (field or "label").strip()
    if not name or name == "geometry":
        raise SessionError("label field cannot be empty or named geometry")
    return name


def _feature_name(props: dict, index: int) -> str:
    for key in ("site_id", "osm_id", "id", "name"):
        value = props.get(key)
        if value not in (None, ""):
            return str(value)
    return f"#{index}"


def _feature_tip(props: dict, label: str | None) -> str:
    bits = []
    if label:
        bits.append(label)
    for key in ("damage_class", "damage_confidence", "mean_confidence", "review_status", "area_m2"):
        value = props.get(key)
        if value not in (None, ""):
            bits.append(f"{key}: {value}")
    return " · ".join(bits)


def _seams(length: int, tile: int) -> list[int]:
    seams = []
    for step in (1, 2):
        position = step * tile
        if 0 < position < length:
            seams.append(position)
    return seams


def _draw_parts(geom, x0: int, y0: int) -> tuple[str, list]:
    drawn = geom.simplify(0.8, preserve_topology=True)
    if drawn.is_empty:
        drawn = geom
    tolerance = 0.8
    while _coord_count(drawn) > 4000 and tolerance < 16:
        tolerance *= 2
        drawn = geom.simplify(tolerance, preserve_topology=True)
    kind = drawn.geom_type
    if kind == "Polygon":
        return "polygon", [_polygon_rings(drawn, x0, y0)]
    if kind == "MultiPolygon":
        return "polygon", [_polygon_rings(part, x0, y0) for part in drawn.geoms]
    if kind == "LineString":
        return "line", [_coords(drawn, x0, y0)]
    if kind == "MultiLineString":
        return "line", [_coords(part, x0, y0) for part in drawn.geoms]
    if kind == "Point":
        return "point", [[round(drawn.x - x0, 1), round(drawn.y - y0, 1)]]
    if kind == "MultiPoint":
        return "point", [[round(pt.x - x0, 1), round(pt.y - y0, 1)] for pt in drawn.geoms]
    return "line", []


def _polygon_rings(polygon, x0: int, y0: int) -> list[list[list[float]]]:
    rings = [_coords(polygon.exterior, x0, y0)]
    rings.extend(_coords(hole, x0, y0) for hole in polygon.interiors)
    return rings


def _coords(line, x0: int, y0: int) -> list[list[float]]:
    return [[round(x - x0, 1), round(y - y0, 1)] for x, y in line.coords]


def _coord_count(geom) -> int:
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords) + sum(len(ring.coords) for ring in geom.interiors)
    if hasattr(geom, "geoms"):
        return sum(_coord_count(part) for part in geom.geoms)
    return len(geom.coords)
