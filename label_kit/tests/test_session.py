import json

import numpy as np
import rasterio
from rasterio.transform import from_origin

from label_kit.session import Session


def _write_tiff(path, size=500):
    data = np.zeros((3, size, size), dtype=np.uint8)
    if size >= 120:
        data[:, 40:120, 40:120] = 200
    transform = from_origin(500000, 8500000, 1, 1)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=size,
        height=size,
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
    dest, written, removed = session.save()
    assert written == 2
    assert removed == 0
    saved = json.loads(dest.read_text(encoding="utf-8"))
    assert saved["features"][0]["properties"]["review_status"] == ""
    assert saved["features"][1]["properties"]["review_status"] == "false"
    assert "label" not in saved["features"][2]["properties"]
    assert dest.with_suffix(dest.suffix + ".bak").is_file()
    session.delete([0])
    assert 0 in session._deleted
    _dest, _written, removed_now = session.save()
    assert removed_now == 1
    saved_again = json.loads(dest.read_text(encoding="utf-8"))
    assert [feature["properties"]["osm_id"] for feature in saved_again["features"]] == [11, 12]
    session.undo()
    assert 0 not in session._deleted
    copy, _written_copy, _removed_copy = session.save("copy.geojson")
    assert copy.parent == geojson.parent
    assert copy.name == "copy.geojson"
    assert copy.is_file()
    session.close()


def test_pre_image_must_match_and_can_render(tmp_path):
    from label_kit.session import SessionError

    tiff = tmp_path / "post.tif"
    pre = tmp_path / "pre.tif"
    small = tmp_path / "small.tif"
    geojson = tmp_path / "buildings.geojson"
    _write_tiff(tiff)
    _write_tiff(pre)
    _write_tiff(small, 32)
    geojson.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    session = Session()
    try:
        session.load(str(tiff), str(geojson), "label", str(small))
        raise AssertionError("different sizes should be rejected")
    except SessionError as exc:
        assert "same width" in str(exc)
    session.load(str(tiff), str(geojson), "label", str(pre))
    assert session.state()["has_pre"] is True
    assert session.render_jpeg("pre")[:2] == b"\xff\xd8"
    session.reset()
    assert session.state() == {"loaded": False}
    session.close()
