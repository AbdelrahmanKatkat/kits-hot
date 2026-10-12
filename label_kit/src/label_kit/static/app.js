const canvas = document.querySelector("#map");
const ctx = canvas.getContext("2d");
const menu = document.querySelector("#menu");
const selection = new Set();

let state = { loaded: false };
let features = [];
let image = null;
let rasterToken = 0;
let camera = { scale: 1, x: 0, y: 0 };
let drag = null;
let box = null;
let hoverTip = "";
const labelDrafts = [];

const $ = (sel) => {
  if (!sel) return null;
  if (sel.startsWith("#") || sel.startsWith(".") || sel.startsWith("[") || sel.includes(" ")) return document.querySelector(sel);
  return document.querySelector(`#${sel}`);
};

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function resize() {
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width));
  canvas.height = Math.max(1, Math.floor(rect.height));
  draw();
}

function fit() {
  if (!image) return;
  const scale = Math.min(canvas.width / image.width, canvas.height / image.height);
  camera.scale = scale || 1;
  camera.x = (canvas.width - image.width * camera.scale) / 2;
  camera.y = (canvas.height - image.height * camera.scale) / 2;
}

function imagePoint(ev) {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const y = ev.clientY - rect.top;
  return { sx: x, sy: y, ix: (x - camera.x) / camera.scale, iy: (y - camera.y) / camera.scale };
}

function applyState(next) {
  state = next;
  $("gate").classList.toggle("hidden", !!next.loaded);
  $("side").classList.toggle("hidden", !next.loaded);
  $("overview").classList.toggle("hidden", !next.loaded);
  $("pager").classList.toggle("hidden", !next.loaded);
  if (!next.loaded) return;
  const pages = next.pages || { index: 0, count: 1 };
  $("place").textContent = `Block ${pages.index + 1} of ${pages.count}`;
  $("page-text").textContent = `${next.stats.on_map} features on this image`;
  syncOverview(next);
  $("field-side").value = next.label_field;
  $("tile").value = String(next.tile);
  if (!$("save-path").value) $("save-path").value = next.geojson;
  $("warning").textContent = next.warning || "";
  const total = next.stats.total || 0;
  const labeled = next.stats.labeled || 0;
  $("bar-fill").style.width = total ? `${(100 * labeled) / total}%` : "0";
  const bits = Object.entries(next.stats.by_label || {}).map(([name, count]) => `${name} ${count}`);
  $("stats").textContent = `${labeled} labeled, ${next.stats.unlabeled} unlabeled, ${total} features. ${bits.join(" · ")}`;
  fillColumns(next.keys || []);
  fillFilter(next.label_names || []);
  renderLabels(next);
  renderApply(next);
  $("selected-count").textContent = String(selection.size);
}

function fillColumns(keys) {
  const select = $("column");
  const current = select.value;
  select.replaceChildren(new Option("None", ""));
  for (const key of keys) select.append(new Option(key, key));
  if (keys.includes(current)) select.value = current;
}

function fillFilter(names) {
  const select = $("filter");
  const current = select.value;
  select.replaceChildren(new Option("All features", "all"), new Option("Unlabeled only", "unlabeled"));
  for (const name of names) select.append(new Option(name, name));
  const values = ["all", "unlabeled", ...names];
  select.value = values.includes(current) ? current : "all";
}

function renderLabels(next) {
  const list = $("label-list");
  const active = document.activeElement;
  if (active && active.tagName === "INPUT" && list.contains(active)) return;
  list.replaceChildren();
  labelDrafts.length = 0;
  next.label_names.forEach((name, index) => {
    labelDrafts.push(name);
    const item = document.createElement("li");
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = next.colors[name];
    const input = document.createElement("input");
    input.type = "text";
    input.value = name;
    input.addEventListener("change", () => renameLabel(index, input.value));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeLabel(index));
    item.append(swatch, input, remove);
    list.append(item);
  });
}

function renderApply(next) {
  const box = $("apply-list");
  box.replaceChildren();
  for (const name of next.label_names) {
    const button = document.createElement("button");
    button.type = "button";
    button.innerHTML = `<span class="swatch" style="background:${next.colors[name]}"></span>${name}`;
    button.addEventListener("click", () => assign(name));
    box.append(button);
  }
}

function namesFromDrafts() {
  return [...labelDrafts];
}

async function syncLabels(rename) {
  const next = await post("/api/labels", {
    names: namesFromDrafts().filter((name) => name.trim()),
    rename: rename || {},
    field: $("field-side").value,
  });
  applyState(next);
  draw();
}

async function renameLabel(index, value) {
  const previous = labelDrafts[index];
  labelDrafts[index] = value.trim();
  const rename = {};
  if (previous && previous !== labelDrafts[index]) rename[previous] = labelDrafts[index];
  await syncLabels(rename);
}

async function removeLabel(index) {
  labelDrafts.splice(index, 1);
  await syncLabels();
}

async function refreshView(refit) {
  const token = ++rasterToken;
  const raster = await fetch("/api/raster");
  let payload = { features: [] };
  try {
    const resp = await fetch("/api/features");
    payload = resp.ok ? await resp.json() : { features: [] };
  } catch {
    payload = { features: [] };
  }
  if (!raster.ok) {
    const err = await raster.json();
    throw new Error(err.error || "could not read the TIFF window");
  }
  const blob = await raster.blob();
  if (token !== rasterToken) return;
  features = payload.features || [];
  const img = new Image();
  img.onload = () => {
    if (token !== rasterToken) return;
    image = img;
    if (refit) fit();
    draw();
    $("hud").textContent = hoverTip || `${features.length} features in this page`;
  };
  img.src = URL.createObjectURL(blob);
}

function visible(feature) {
  const filter = $("filter").value;
  if (filter === "unlabeled") return !feature.label;
  if (filter !== "all") return feature.label === filter;
  return true;
}

function colorOf(feature, selected) {
  if (selected) return "#ffe14a";
  if (feature.label && state.colors) return state.colors[feature.label];
  return "#9aa4b2";
}

function drawFeature(feature, selected) {
  const color = colorOf(feature, selected);
  ctx.lineWidth = (selected ? 3 : 1.6) / camera.scale;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  if (feature.type === "line") {
    for (const line of feature.parts) strokeLine(line, false);
  } else if (feature.type === "point") {
    for (const [x, y] of feature.parts) {
      ctx.beginPath();
      ctx.arc(x, y, 5 / camera.scale, 0, Math.PI * 2);
      ctx.fill();
    }
  } else {
    for (const rings of feature.parts) {
      ctx.beginPath();
      for (const ring of rings) {
        ring.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
      }
      ctx.globalAlpha = selected ? 0.35 : 0.18;
      ctx.fill("evenodd");
      ctx.globalAlpha = 1;
      ctx.stroke();
    }
  }
}

function strokeLine(line, close) {
  if (!line.length) return;
  ctx.beginPath();
  line.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  if (close) ctx.closePath();
  ctx.stroke();
}

function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.imageSmoothingEnabled = camera.scale < 2;
  ctx.setTransform(camera.scale, 0, 0, camera.scale, camera.x, camera.y);
  if (image) ctx.drawImage(image, 0, 0);
  if (state.view) {
    ctx.strokeStyle = "rgba(255,255,255,0.7)";
    ctx.lineWidth = 1 / camera.scale;
    ctx.beginPath();
    for (const x of state.view.seams_x || []) {
      ctx.moveTo(x, 0);
      ctx.lineTo(x, image ? image.height : 0);
    }
    for (const y of state.view.seams_y || []) {
      ctx.moveTo(0, y);
      ctx.lineTo(image ? image.width : 0, y);
    }
    ctx.stroke();
  }
  const selected = [];
  for (const feature of features) {
    if (!visible(feature)) continue;
    if (selection.has(feature.id)) selected.push(feature);
    else drawFeature(feature, false);
  }
  for (const feature of selected) drawFeature(feature, true);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  if (box) {
    ctx.strokeStyle = "#4c9aff";
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(box.x, box.y, box.w, box.h);
    ctx.setLineDash([]);
  }
}

function boundsOf(feature) {
  let minx = Infinity;
  let miny = Infinity;
  let maxx = -Infinity;
  let maxy = -Infinity;
  const visit = (x, y) => {
    minx = Math.min(minx, x);
    miny = Math.min(miny, y);
    maxx = Math.max(maxx, x);
    maxy = Math.max(maxy, y);
  };
  if (feature.type === "point") feature.parts.forEach(([x, y]) => visit(x, y));
  else if (feature.type === "line") feature.parts.forEach((line) => line.forEach(([x, y]) => visit(x, y)));
  else feature.parts.forEach((rings) => rings.forEach((ring) => ring.forEach(([x, y]) => visit(x, y))));
  return [minx, miny, maxx, maxy];
}

function hitsBox(feature, x0, y0, x1, y1) {
  const [minx, miny, maxx, maxy] = boundsOf(feature);
  return maxx >= x0 && minx <= x1 && maxy >= y0 && miny <= y1;
}

function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    const intersect = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi + 0.0) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy || 1;
  let t = ((px - ax) * dx + (py - ay) * dy) / len;
  t = Math.max(0, Math.min(1, t));
  const x = ax + t * dx;
  const y = ay + t * dy;
  return Math.hypot(px - x, py - y);
}

function hitTest(ix, iy) {
  const limit = 8 / camera.scale;
  for (let index = features.length - 1; index >= 0; index -= 1) {
    const feature = features[index];
    if (!visible(feature)) continue;
    if (feature.type === "point") {
      if (feature.parts.some(([x, y]) => Math.hypot(ix - x, iy - y) <= limit)) return feature;
    } else if (feature.type === "line") {
      for (const line of feature.parts) {
        for (let i = 1; i < line.length; i += 1) {
          const [ax, ay] = line[i - 1];
          const [bx, by] = line[i];
          if (distToSegment(ix, iy, ax, ay, bx, by) <= limit) return feature;
        }
      }
    } else if (feature.parts.some((rings) => rings[0] && pointInRing(ix, iy, rings[0]))) {
      return feature;
    }
  }
  return null;
}

function hideMenu() {
  menu.classList.add("hidden");
}

function showMenu(sx, sy) {
  menu.replaceChildren();
  if (!state.label_names || !state.label_names.length) {
    const note = document.createElement("div");
    note.textContent = "Add a label in the side panel first.";
    menu.append(note);
  } else {
    for (const name of state.label_names) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = name;
      button.addEventListener("click", () => {
        hideMenu();
        assign(name);
      });
      menu.append(button);
    }
    const clear = document.createElement("button");
    clear.type = "button";
    clear.textContent = "Clear label";
    clear.addEventListener("click", () => {
      hideMenu();
      assign(null);
    });
    menu.append(clear);
  }
  menu.style.left = `${Math.min(sx, canvas.width - 180)}px`;
  menu.style.top = `${Math.min(sy, canvas.height - 40)}px`;
  menu.classList.remove("hidden");
}

async function assign(label) {
  if (!selection.size) return;
  const ids = [...selection];
  const next = await post("/api/assign", { ids, label });
  const chosen = new Set(ids);
  for (const feature of features) {
    if (chosen.has(feature.id)) feature.label = label;
  }
  applyState(next);
  draw();
}

canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());
canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const point = imagePoint(ev);
  const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
  const scale = Math.min(24, Math.max(0.05, camera.scale * factor));
  camera.x = point.sx - point.ix * scale;
  camera.y = point.sy - point.iy * scale;
  camera.scale = scale;
  draw();
}, { passive: false });

canvas.addEventListener("mousedown", (ev) => {
  hideMenu();
  const point = imagePoint(ev);
  drag = { button: ev.button, ...point, moved: false, camx: camera.x, camy: camera.y, ctrl: ev.ctrlKey };
});

window.addEventListener("mousemove", (ev) => {
  if (!drag) {
    const point = imagePoint(ev);
    const hit = hitTest(point.ix, point.iy);
    hoverTip = hit ? `${hit.name}${hit.tip ? " · " + hit.tip : ""}` : `${features.length} features in this page`;
    $("hud").textContent = hoverTip;
    return;
  }
  const point = imagePoint(ev);
  if (Math.hypot(point.sx - drag.sx, point.sy - drag.sy) > 4) drag.moved = true;
  if (!drag.moved) return;
  if (drag.button === 0) {
    camera.x = drag.camx + (point.sx - drag.sx);
    camera.y = drag.camy + (point.sy - drag.sy);
    box = null;
  } else if (drag.button === 2) {
    box = {
      x: Math.min(drag.sx, point.sx),
      y: Math.min(drag.sy, point.sy),
      w: Math.abs(point.sx - drag.sx),
      h: Math.abs(point.sy - drag.sy),
    };
  }
  draw();
});

window.addEventListener("mouseup", (ev) => {
  if (!drag) return;
  const point = imagePoint(ev);
  const ended = drag;
  drag = null;
  if (ended.button === 2 && ended.moved && box) {
    const x0 = Math.min(ended.ix, point.ix);
    const x1 = Math.max(ended.ix, point.ix);
    const y0 = Math.min(ended.iy, point.iy);
    const y1 = Math.max(ended.iy, point.iy);
    if (!ended.ctrl) selection.clear();
    for (const feature of features) {
      if (visible(feature) && hitsBox(feature, x0, y0, x1, y1)) selection.add(feature.id);
    }
    box = null;
  } else if (ended.button === 2 && !ended.moved) {
    box = null;
    showMenu(ended.sx, ended.sy);
  } else if (ended.button === 0 && !ended.moved) {
    const hit = hitTest(ended.ix, ended.iy);
    if (ended.ctrl && hit) {
      if (selection.has(hit.id)) selection.delete(hit.id);
      else selection.add(hit.id);
    } else if (hit) {
      selection.clear();
      selection.add(hit.id);
    } else if (!ended.ctrl) {
      selection.clear();
    }
  }
  $("selected-count").textContent = String(selection.size);
  draw();
});

window.addEventListener("keydown", (ev) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(ev.target.tagName);
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
    ev.preventDefault();
    $("save").click();
    return;
  }
  if (typing) return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") {
    ev.preventDefault();
    $("undo").click();
    return;
  }
  if (ev.key === "Escape") {
    selection.clear();
    hideMenu();
    $("selected-count").textContent = "0";
    draw();
  } else if (ev.key >= "1" && ev.key <= "9") {
    const name = (state.label_names || [])[Number(ev.key) - 1];
    if (name) assign(name);
  } else if (ev.key === "0") {
    assign(null);
  } else if (ev.key === "ArrowLeft" || ev.key === "PageUp") move(-1);
  else if (ev.key === "ArrowRight" || ev.key === "PageDown") move(1);
});

let overviewImage = null;
let overviewKey = "";

function syncOverview(next) {
  if (!next.loaded) {
    overviewImage = null;
    overviewKey = "";
    return;
  }
  if (overviewImage && overviewKey === next.tiff) {
    drawOverview(next);
    return;
  }
  const key = next.tiff;
  overviewKey = key;
  const img = new Image();
  img.onload = () => {
    if (overviewKey !== key) return;
    overviewImage = img;
    drawOverview(state);
  };
  img.src = `/api/overview?v=${encodeURIComponent(key)}`;
}

function drawOverview(next) {
  const canvas = $("overview-map");
  const ctx = canvas.getContext("2d");
  const pages = next.pages;
  if (!overviewImage || !pages || !pages.width || !pages.height) return;
  const maxEdge = 240;
  const fit = Math.min(maxEdge / overviewImage.width, maxEdge / overviewImage.height, 1);
  canvas.width = Math.max(1, Math.round(overviewImage.width * fit));
  canvas.height = Math.max(1, Math.round(overviewImage.height * fit));
  ctx.drawImage(overviewImage, 0, 0, canvas.width, canvas.height);
  const sx = canvas.width / pages.width;
  const sy = canvas.height / pages.height;
  const done = new Set((pages.done || []).map(([c, r]) => `${c},${r}`));
  for (let row = 0; row < pages.rows; row += 1) {
    for (let col = 0; col < pages.cols; col += 1) {
      const x0 = col * 3 * pages.tile;
      const y0 = row * 3 * pages.tile;
      const x1 = Math.min(pages.width, x0 + 3 * pages.tile);
      const y1 = Math.min(pages.height, y0 + 3 * pages.tile);
      const current = col === pages.col && row === pages.row;
      const finished = done.has(`${col},${row}`);
      if (finished) {
        ctx.fillStyle = "rgba(46, 125, 50, 0.55)";
        ctx.fillRect(x0 * sx, y0 * sy, (x1 - x0) * sx, (y1 - y0) * sy);
      }
      ctx.strokeStyle = current ? "#ffe14a" : "rgba(255,255,255,0.9)";
      ctx.lineWidth = current ? 2 : 1;
      ctx.strokeRect(x0 * sx + 0.5, y0 * sy + 0.5, (x1 - x0) * sx - 1, (y1 - y0) * sy - 1);
    }
  }
}

async function move(direction) {
  const next = await post("/api/move", { dc: direction, dr: 0, page: true, tile: Number($("tile").value) });
  applyState(next);
  await refreshView(true);
}

$("prev-page").addEventListener("click", () => move(-1));
$("next-page").addEventListener("click", () => move(1));
$("tile").addEventListener("change", () => move(0));
$("filter").addEventListener("change", draw);
$("fit").addEventListener("click", () => { fit(); draw(); });
$("undo").addEventListener("click", async () => {
  applyState(await post("/api/undo"));
  await refreshView(false);
});
$("save").addEventListener("click", async () => {
  try {
    const result = await post("/api/save", { path: $("save-path").value });
    $("hud").textContent = `Saved ${result.written} features to ${result.path}`;
  } catch (err) {
    $("warning").textContent = err.message;
  }
});
$("add-label").addEventListener("click", async () => {
  labelDrafts.push(`label ${labelDrafts.length + 1}`);
  await syncLabels();
});
$("field-side").addEventListener("change", () => syncLabels());
$("import-column").addEventListener("click", async () => {
  const column = $("column").value;
  if (!column) return;
  applyState(await post("/api/import", { column }));
  await refreshView(false);
});
$("clear-label").addEventListener("click", () => assign(null));
$("select-unlabeled").addEventListener("click", () => {
  selection.clear();
  for (const feature of features) {
    if (!feature.label) selection.add(feature.id);
  }
  $("selected-count").textContent = String(selection.size);
  draw();
});

$("open-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("gate-error").textContent = "";
  const tiff = $("tiff").value.trim();
  const geojson = $("geojson").value.trim();
  if (!tiff || !geojson) {
    $("gate-error").textContent = "Choose a TIFF and a GeoJSON first.";
    return;
  }
  try {
    applyState(await post("/api/load", { tiff, geojson, field: "label" }));
    $("save-path").value = state.geojson || "";
    await refreshView(true);
  } catch (err) {
    $("gate").classList.remove("hidden");
    $("gate-error").textContent = err.message;
  }
});

document.querySelectorAll("[data-browse]").forEach((button) => {
  button.addEventListener("click", async () => {
    $("gate-error").textContent = "Opening the file dialog…";
    try {
      const result = await post("/api/browse", { kind: button.dataset.browse });
      $("gate-error").textContent = "";
      if (result.path) $(button.dataset.browse).value = result.path;
    } catch (err) {
      $("gate-error").textContent = err.message;
    }
  });
});

window.addEventListener("resize", resize);
resize();
fetch("/api/state").then((response) => response.json()).then(async (next) => {
  applyState(next);
  if (next.loaded) {
    $("tiff").value = next.tiff;
    $("geojson").value = next.geojson;
    $("save-path").value = next.geojson;
    await refreshView(true);
  }
}).catch((err) => {
  $("gate-error").textContent = err.message;
});
