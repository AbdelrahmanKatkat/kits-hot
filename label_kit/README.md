# label kit

Local map for labeling a GeoJSON on top of a GeoTIFF. One page shows nine tiles of the image stitched together. You move that page across the file, select features, and write a label you defined in the side panel.

The TIFF extent is the map. There is no area-of-interest file.

## Run

From this folder:

```bash
uv sync
uv run label-kit --tiff path\to\post_aligned.tif --geojson path\to\damage.geojson --field label
```

Or open the page and browse to the files:

```bash
uv sync
uv run label-kit
```

The app listens on `http://127.0.0.1:8765/`. The TIFF and GeoJSON are read only on this machine.

## How to label

1. Open a TIFF and GeoJSON (type paths or use **📁**).
2. Choose an existing property column and click **Use that column**. That column becomes the output column too (you can still change **Column** manually).
3. Add label names if you want extra options beyond what was found in the column.
2. Click a feature to select it. Ctrl+click adds or removes. Right-drag a rectangle to select every feature the rectangle touches. Right-click the map to apply a label.
Keys 1–9 apply the labels in order. 0 clears the label. Next and Prev walk the image one 3×3 block at a time, row by row. The thumbnail in the top-left is the whole TIFF with the page grid drawn on it. The current block has a yellow outline, and a block turns green after you leave it with Next. Prev and Next sit at the bottom-left.
4. Drag to pan and use the wheel to zoom inside the current nine tiles.
5. Save. The first save of an existing file copies the original to a `.bak` next to it. Only the label property is written; other fields stay as they were.

Buildings, damage footprints, and waste outlines (polygons or lines) use the same tools. A GeoJSON without a `crs` member is read as EPSG:4326 and reprojected onto the TIFF.
