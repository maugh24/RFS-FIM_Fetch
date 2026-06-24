"""RFS-FIM Fetch

Pull flood-extent tiles from the public S3 bucket `floodmap-sandbox` that overlap
a bounding-box AOI, for a chosen set of return periods, then mosaic/clip to
per-return-period GeoTIFFs. Tiles are read straight from S3 with gdal.Warp over
/vsis3/ (no local download), and results are clipped to the true AOI polygon.
"""

import math
import os
from pathlib import Path

import s3fs
import geopandas as gpd
import numpy as np
import rioxarray
from shapely.geometry import box

# Read tiles straight from the public S3 bucket: no signed credentials, no local download.
os.environ["AWS_NO_SIGN_REQUEST"] = "Yes"
from osgeo import gdal
gdal.UseExceptions()

# --- Configuration ----------------------------------------------------------
BUCKET = "floodmap-sandbox"
DEM = "fabdem"
TIF_NAME = "flows_2,5,10,25,50,100.tif"
RETURN_PERIODS = [2, 5, 10, 25, 50, 100]   # band order in the tif

# Anchor all paths to this script's folder so it runs from any working directory.
SCRIPT_DIR = Path(__file__).resolve().parent

AOI_PATH = SCRIPT_DIR / "Moron.shp"
SELECTED_RPS = [2, 5, 10, 25, 50, 100]

RESULTS_DIR = SCRIPT_DIR / "Results"

fs = s3fs.S3FileSystem(anon=True)


# --- 1. Accept AOI ----------------------------------------------------------
def load_aoi(aoi_path):
    """Read the AOI shapefile, reproject to EPSG:4326, and return both the true
    polygon and its axis-aligned bounding box."""
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
    return aoi_poly, aoi_name, aoi_bounds


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


# --- 4. Build candidate tile keys and keep the ones that exist --------------
def find_tiles(min_lon, min_lat, max_lon, max_lat):
    """Loop every integer lon/lat in the AOI range (SW-corner tiles), build the
    S3 key, and keep only tiles that actually exist in the bucket.
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

    print(f"\n{len(urls)} tiles to read from S3.")
    return urls


# --- 5. Mosaic + clip directly from S3 (gdal.Warp, no download) -------------
def warp_mosaic(urls, aoi_name, aoi_bounds):
    """Read each existing tile over /vsis3/, mosaic them, and clip to the AOI
    bbox in a single Warp pass. Only the clipped mosaic is written to disk."""
    os.makedirs(RESULTS_DIR, exist_ok=True)

    vsis3_paths = [f"/vsis3/{key}" for key in urls]
    minx, miny, maxx, maxy = aoi_bounds
    mosaic_path = os.path.join(RESULTS_DIR, f"{aoi_name}_mosaic.tif")

    gdal.Warp(
        mosaic_path,
        vsis3_paths,
        outputBounds=(minx, miny, maxx, maxy),  # clip to AOI bbox (EPSG:4326)
        dstSRS="EPSG:4326",
        resampleAlg="near",
        multithread=True,
    )
    print(f"wrote clipped mosaic: {mosaic_path}")
    return mosaic_path


# --- 6. Threshold each return period -> result GeoTIFF ----------------------
def threshold_extents(mosaic_path, aoi_poly, aoi_name, selected_rps):
    """Open the clipped mosaic, map each return period to its pixel value, then
    for each selected return period build the flood-extent mask, clip to the
    true AOI polygon, and write a GeoTIFF."""
    mosaic = rioxarray.open_rasterio(mosaic_path, masked=True).squeeze("band", drop=True)

    vals = sorted(
        (int(v) for v in np.unique(mosaic.values[~np.isnan(mosaic.values)]) if v > 0),
        reverse=True,
    )
    rp_to_value = dict(zip(sorted(RETURN_PERIODS), vals))
    print("return-period -> pixel-value map:", rp_to_value)

    written = []
    for rp in selected_rps:
        thresh = rp_to_value[rp]

        mask = ((mosaic > 0) & (mosaic >= thresh)).astype("uint8")
        extent = mask.where(mask == 1, 255).astype("uint8")
        extent = extent.rio.write_crs(mosaic.rio.crs).rio.write_nodata(255)

        # clip to the true AOI polygon (not the bbox) so flood pixels outside it are dropped
        clipped = extent.rio.clip(aoi_poly.geometry.values, aoi_poly.crs, drop=True)
        out_path = os.path.join(RESULTS_DIR, f"{aoi_name}_rp{rp}.tif")
        clipped.rio.to_raster(out_path)

        n = int((clipped == 1).sum())
        written.append(out_path)
        print(f"  wrote {out_path}  ({n:,} flooded px in AOI)")

    print("\nDone. Files:", written)
    return written


# --- Main -------------------------------------------------------------------
def main():
    aoi_poly, aoi_name, aoi_bounds = load_aoi(AOI_PATH)
    validate_return_periods(SELECTED_RPS)
    min_lon, min_lat, max_lon, max_lat = derive_extents(aoi_bounds)
    urls = find_tiles(min_lon, min_lat, max_lon, max_lat)
    mosaic_path = warp_mosaic(urls, aoi_name, aoi_bounds)
    threshold_extents(mosaic_path, aoi_poly, aoi_name, SELECTED_RPS)


if __name__ == "__main__":
    main()
