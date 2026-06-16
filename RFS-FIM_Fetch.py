"""RFS-FIM Fetch

Pull flood-extent tiles from the public S3 bucket `floodmap-sandbox` that overlap
a bounding-box AOI, for a chosen set of return periods, then clip/mosaic to
per-return-period GeoTIFFs.
"""

import math
import os
from pathlib import Path

import s3fs
import geopandas as gpd
import numpy as np
import rioxarray
from shapely.geometry import box
from rioxarray.merge import merge_arrays

# --- Configuration ----------------------------------------------------------
BUCKET = "floodmap-sandbox"
DEM = "fabdem"
TIF_NAME = "flows_2,5,10,25,50,100.tif"
RETURN_PERIODS = [2, 5, 10, 25, 50, 100]   # band order in the tif

# Anchor all paths to this script's folder so it runs from any working directory.
SCRIPT_DIR = Path(__file__).resolve().parent

AOI_PATH = SCRIPT_DIR / "Moron.shp"
SELECTED_RPS = [2, 5, 10, 25, 50, 100]

DOWNLOAD_DIR = SCRIPT_DIR / "downloads"
RESULTS_DIR = SCRIPT_DIR / "Results"

fs = s3fs.S3FileSystem(anon=True)


# --- 1. Accept AOI ----------------------------------------------------------
def load_aoi(aoi_path):
    """Read the AOI shapefile and collapse it to its axis-aligned bounding box."""
    aoi_name = Path(aoi_path).stem

    aoi_poly = gpd.read_file(aoi_path)
    if aoi_poly.crs is None:
        raise ValueError("AOI has no CRS defined.")
    if aoi_poly.crs.to_epsg() != 4326:
        aoi_poly = aoi_poly.to_crs(4326)

    minx, miny, maxx, maxy = aoi_poly.total_bounds
    aoi = gpd.GeoDataFrame(
        {"name": [aoi_name]},
        geometry=[box(minx, miny, maxx, maxy)],
        crs="EPSG:4326",
    )

    aoi_bounds = aoi.total_bounds
    print(f"AOI '{aoi_name}' bbox (min_lon, min_lat, max_lon, max_lat):")
    print(aoi_bounds)
    return aoi, aoi_name, aoi_bounds


# --- 2. Accept return-period selection --------------------------------------
def validate_return_periods(selected_rps):
    assert all(rp in RETURN_PERIODS for rp in selected_rps), f"Pick from {RETURN_PERIODS}"
    print("Selected return periods:", selected_rps)


# --- 3. Derive the tile-boundary extents ------------------------------------
def derive_extents(aoi_bounds):
    """Floor the minimums and ceiling the maximums to tile boundary extents."""
    min_lon = math.floor(aoi_bounds[0])
    min_lat = math.floor(aoi_bounds[1])
    max_lon = math.ceil(aoi_bounds[2])
    max_lat = math.ceil(aoi_bounds[3])

    print(f"RETURN_PERIOD = {RETURN_PERIODS}")
    print(f"lon: {min_lon} -> {max_lon}")
    print(f"lat: {min_lat} -> {max_lat}")
    return min_lon, min_lat, max_lon, max_lat


# --- 4. Build candidate tile URLs and keep the ones that exist --------------
def find_tiles(min_lon, min_lat, max_lon, max_lat):
    """Loop every integer lon/lat in the AOI range (SW-corner tiles), build the
    S3 path, and keep only tiles that actually exist in the bucket.
    `range(min, max)` is exclusive on the top end, which is correct for
    SW-corner 1-degree tiles."""
    urls = []
    for lon_to_download in range(min_lon, max_lon):
        for lat_to_download in range(min_lat, max_lat):
            key = (f"{BUCKET}/tiles/lon={lon_to_download}/lat={lat_to_download}"
                   f"/floodmaps/dem={DEM}/{TIF_NAME}")
            if fs.exists(key):
                urls.append(key)
                print(f"  found: lon={lon_to_download}, lat={lat_to_download}")
            else:
                print(f"  missing: lon={lon_to_download}, lat={lat_to_download}")

    print(f"\n{len(urls)} tiles to download.")
    return urls


# --- 5. Download the tiles locally ------------------------------------------
def download_tiles(urls):
    """Copies each tile into a local downloads/ folder (bucket is only read)."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    local_paths = []
    for url in urls:
        lon = url.split("lon=")[1].split("/")[0]
        lat = url.split("lat=")[1].split("/")[0]
        local = f"{DOWNLOAD_DIR}/lon{lon}_lat{lat}_{TIF_NAME}"
        if not os.path.exists(local):
            fs.get(url, local)
        local_paths.append(local)
        print("  ", local)

    print(f"\n{len(local_paths)} files local.")
    return local_paths


# --- 6. Clip + mosaic -> result GeoTIFF -------------------------------------
def clip_and_mosaic(local_paths, aoi, aoi_name, selected_rps):
    """Mosaic the tiles, map return periods to pixel values, then for each
    selected return period build the flood-extent mask, clip to the AOI, and
    write a GeoTIFF."""
    arrays = [rioxarray.open_rasterio(p, masked=True).squeeze("band", drop=True)
              for p in local_paths]
    mosaic = merge_arrays(arrays) if len(arrays) > 1 else arrays[0]

    vals = sorted(
        (int(v) for v in np.unique(mosaic.values[~np.isnan(mosaic.values)]) if v > 0),
        reverse=True,
    )
    rp_to_value = dict(zip(sorted(RETURN_PERIODS), vals))
    print("return-period -> pixel-value map:", rp_to_value)

    os.makedirs(RESULTS_DIR, exist_ok=True)

    written = []
    for rp in selected_rps:
        thresh = rp_to_value[rp]

        mask = ((mosaic > 0) & (mosaic >= thresh)).astype("uint8")
        extent = mask.where(mask == 1, 255).astype("uint8")
        extent = extent.rio.write_crs(mosaic.rio.crs).rio.write_nodata(255)

        clipped = extent.rio.clip(aoi.geometry.values, aoi.crs, drop=True)
        out_path = os.path.join(RESULTS_DIR, f"{aoi_name}_rp{rp}.tif")
        clipped.rio.to_raster(out_path)

        n = int((clipped == 1).sum())
        written.append(out_path)
        print(f"  wrote {out_path}  ({n:,} flooded px in AOI)")

    print("\nDone. Files:", written)
    return written


# --- Main -------------------------------------------------------------------
def main():
    aoi, aoi_name, aoi_bounds = load_aoi(AOI_PATH)
    validate_return_periods(SELECTED_RPS)
    min_lon, min_lat, max_lon, max_lat = derive_extents(aoi_bounds)
    urls = find_tiles(min_lon, min_lat, max_lon, max_lat)
    local_paths = download_tiles(urls)
    clip_and_mosaic(local_paths, aoi, aoi_name, SELECTED_RPS)


if __name__ == "__main__":
    main()
