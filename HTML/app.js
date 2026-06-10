// ---------------------------------------------------------------------------
// Config — mirrors the notebook
// ---------------------------------------------------------------------------
const BUCKET_URL = "https://floodmap-sandbox.s3.amazonaws.com";
const DEM = "fabdem";
const TIF_NAME = "flows_2,5,10,25,50,100.tif";
const RETURN_PERIODS = [2, 5, 10, 25, 50, 100];

// Pixel value is INVERSE to return period (brightest 100 = 2-yr core,
// darkest 16 = 100-yr fringe). A pixel floods at RP if value >= rpToValue[RP].
const RP_TO_VALUE = {2:100, 5:83, 10:66, 25:50, 50:33, 100:16};

// color per return period (RGB) — dark = frequent, light = rare
const PALETTE = {2:[8,48,107], 5:[8,81,156], 10:[33,113,181],
                 25:[66,146,198], 50:[107,174,214], 100:[189,215,231]};

// Google Hybrid basemap (satellite + labels). lyrs=y -> hybrid.
function googleHybrid() {
  return L.tileLayer("https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
                     { attribution: "Google", maxZoom: 20 });
}

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------
const drawMap = L.map("draw-map").setView([22.10, -78.60], 10);
googleHybrid().addTo(drawMap);

const resultMap = L.map("result-map").setView([22.10, -78.60], 10);
googleHybrid().addTo(resultMap);

// layer to hold the drawn AOI
const drawnItems = new L.FeatureGroup().addTo(drawMap);
const drawControl = new L.Control.Draw({
  edit: { featureGroup: drawnItems },
  draw: { rectangle: {}, polygon: {}, polyline: false, circle: false,
          circlemarker: false, marker: false }
});
drawMap.addControl(drawControl);

let aoiBounds = null;   // [minLon, minLat, maxLon, maxLat]

drawMap.on(L.Draw.Event.CREATED, (e) => {
  drawnItems.clearLayers();
  drawnItems.addLayer(e.layer);
  const b = e.layer.getBounds();                 // Leaflet LatLngBounds
  aoiBounds = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  log(`AOI bbox  lon ${aoiBounds[0].toFixed(4)} -> ${aoiBounds[2].toFixed(4)},  ` +
      `lat ${aoiBounds[1].toFixed(4)} -> ${aoiBounds[3].toFixed(4)}`);
  document.getElementById("load-btn").disabled = false;
});

// ---------------------------------------------------------------------------
// Return-period checkboxes
// ---------------------------------------------------------------------------
const rpBoxes = document.getElementById("rp-boxes");
RETURN_PERIODS.forEach(rp => {
  const id = "rp-" + rp;
  const lbl = document.createElement("label");
  lbl.innerHTML = `<input type="checkbox" id="${id}" value="${rp}" ` +
                  `${[2,100].includes(rp) ? "checked" : ""}/> ${rp}-yr`;
  rpBoxes.appendChild(lbl);
});
function selectedRPs() {
  return RETURN_PERIODS.filter(rp => document.getElementById("rp-" + rp).checked);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function log(msg) {
  const el = document.getElementById("log");
  el.textContent += "\n" + msg;
  el.scrollTop = el.scrollHeight;
}

// integer tile range from bbox (floor mins, ceil maxs) — same as notebook Block 3
function tileRange(bounds) {
  return {
    minLon: Math.floor(bounds[0]), maxLon: Math.ceil(bounds[2]),
    minLat: Math.floor(bounds[1]), maxLat: Math.ceil(bounds[3])
  };
}

function tileUrl(lon, lat) {
  return `${BUCKET_URL}/tiles/lon=${lon}/lat=${lat}/floodmaps/dem=${DEM}/${TIF_NAME}`;
}

// GeoJSON polygon of the AOI bbox (used to clip the raster display)
function bboxGeoJSON(b) {
  return { type: "Feature", properties: {}, geometry: { type: "Polygon",
    coordinates: [[[b[0],b[1]],[b[2],b[1]],[b[2],b[3]],[b[0],b[3]],[b[0],b[1]]]] } };
}

let resultLayers = [];   // track overlays so we can clear them

// ---------------------------------------------------------------------------
// Main: load flood extents for selected return periods
// ---------------------------------------------------------------------------
document.getElementById("load-btn").addEventListener("click", async () => {
  if (!aoiBounds) { log("Draw an AOI first."); return; }
  const rps = selectedRPs();
  if (rps.length === 0) { log("Select at least one return period."); return; }

  // clear previous results
  resultLayers.forEach(l => resultMap.removeLayer(l));
  resultLayers = [];

  const r = tileRange(aoiBounds);
  log(`\nTiles  lon ${r.minLon}..${r.maxLon}  lat ${r.minLat}..${r.maxLat}  |  RPs: ${rps.join(", ")}`);

  const mask = bboxGeoJSON(aoiBounds);

  // loop candidate tiles
  for (let lon = r.minLon; lon < r.maxLon; lon++) {
    for (let lat = r.minLat; lat < r.maxLat; lat++) {
      const url = tileUrl(lon, lat);
      try {
        log(`fetching ${url}`);
        const buf = await fetch(url).then(res => {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.arrayBuffer();
        });
        const georaster = await parseGeoraster(buf);

        // one layer per selected RP (rarest first so frequent sits on top)
        rps.slice().sort((a,b) => b - a).forEach(rp => {
          const thresh = RP_TO_VALUE[rp];
          const [cr, cg, cb] = PALETTE[rp];
          const layer = new GeoRasterLayer({
            georaster,
            opacity: 0.85,
            resolution: 256,
            mask,                       // clip display to the AOI bbox
            mask_strategy: "inside",
            pixelValuesToColorFn: (vals) => {
              const v = vals[0];
              return (v > 0 && v >= thresh) ? `rgb(${cr},${cg},${cb})` : null;
            }
          });
          layer.addTo(resultMap);
          resultLayers.push(layer);
        });
        log(`  loaded tile lon=${lon}, lat=${lat}`);
      } catch (err) {
        log(`  skip lon=${lon}, lat=${lat}  (${err.message})`);
      }
    }
  }

  // zoom results map to the AOI
  resultMap.fitBounds([[aoiBounds[1], aoiBounds[0]], [aoiBounds[3], aoiBounds[2]]]);
  log("Done.");
});
