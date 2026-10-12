import json

import numpy as np
import rasterio
from rasterio.transform import from_origin

from label_kit.session import Session


def _write_tiff(path):
    data = np.zeros((3, 500, 500), dtype=np.uint8)
    data[:, 40:120, 40:120] = 200
    transform = from_origin(500000, 8500000, 1, 1)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=500,
        height=500,
        count=3,
        dtype="uint8",
        crs="EPSG:32736",
        transform=transform,
    ) as dst:
        dst.write(data)


def _square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def test_label_roundtrip_and_tile_pages(tmp_path):
    tiff = tmp_path / "scene.tif"
    geojson = tmp_path / "buildings.geojson"
    _write_tiff(tiff)
    # Pixel (50, 50) is world (500050, 8499950) with a north-up 1 m grid.
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:32736"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"osm_id": 10, "review_status": "true"},
                "geometry": {"type": "Polygon", "coordinates": [_square(500050, 8499950, 500080, 8499920)]},
            },
            {
                "type": "Feature",
                "properties": {"osm_id": 11, "review_status": "unreviewed"},
                "geometry": {"type": "LineString", "coordinates": [[500200, 8499800], [500240, 8499780]]},
            },
            {
                "type": "Feature",
                "properties": {"osm_id": 12},
                "geometry": {"type": "Polygon", "coordinates": [_square(0, 0, 1, 1)]},
            },
        ],
    }
    geojson.write_text(json.dumps(collection), encoding="utf-8")

    session = Session()
    session.tile_px = 200
    session.load(str(tiff), str(geojson), "label")
    assert session.grid_size() == (3, 3)
    assert len(session.features_in_view()) == 2
    assert "outside" in session.warning

    session.move(1, 0, page=True)
    assert session.origin_col == 0
    assert (0, 0) in session._done_pages
    session.tile_px = 100
    session.origin_col = 0
    session._done_pages.clear()
    session.move(1, 0, page=True)
    assert session.origin_col == 3
    assert (0, 0) in session._done_pages

    assert len(session.render_jpeg()) > 1000
    overview = session.render_overview_jpeg()
    assert overview[:2] == b"\xff\xd8"
    assert session.render_overview_jpeg() is overview
    session.import_column("review_status")
    assert session.label_names == ["true"]
    assert session.working[0] == "true"
    assert session.working[1] is None

    session.set_labels(["true", "false"], None, "review_status")
    session.assign([1], "false")
    session.assign([0], None)
    dest, written = session.save()
    assert written == 2
    saved = json.loads(dest.read_text(encoding="utf-8"))
    assert saved["features"][0]["properties"]["review_status"] == ""
    assert saved["features"][1]["properties"]["review_status"] == "false"
    assert "label" not in saved["features"][2]["properties"]
    assert dest.with_suffix(dest.suffix + ".bak").is_file()
    session.close()
