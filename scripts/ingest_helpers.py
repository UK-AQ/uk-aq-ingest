from typing import Any, Dict, Optional, Tuple


def coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def maybe_swap_coords(
    lon: Optional[float], lat: Optional[float], bbox: Optional[Dict[str, float]]
) -> Tuple[Optional[float], Optional[float]]:
    if lon is None or lat is None or bbox is None:
        return lon, lat
    # If values look swapped for the bbox, swap them.
    if (
        bbox["south"] <= lon <= bbox["north"]
        and bbox["west"] <= lat <= bbox["east"]
        and not (bbox["west"] <= lon <= bbox["east"])
        and not (bbox["south"] <= lat <= bbox["north"])
    ):
        return lat, lon
    return lon, lat


def station_coords(
    station: Dict[str, Any], bbox: Optional[Dict[str, float]] = None
) -> Tuple[Optional[float], Optional[float]]:
    coords = None
    geometry = station.get("geometry")
    if isinstance(geometry, dict):
        coords = geometry.get("coordinates")
    if coords is None:
        props = station.get("properties", {}) if isinstance(station.get("properties"), dict) else {}
        geometry = props.get("geometry")
        if isinstance(geometry, dict):
            coords = geometry.get("coordinates")
    if coords and len(coords) >= 2:
        lon = coerce_float(coords[0])
        lat = coerce_float(coords[1])
        lon, lat = maybe_swap_coords(lon, lat, bbox)
        return lon, lat

    props = station.get("properties", {}) if isinstance(station.get("properties"), dict) else {}
    lon = coerce_float(props.get("longitude") or props.get("lon") or props.get("lng"))
    lat = coerce_float(props.get("latitude") or props.get("lat"))
    lon, lat = maybe_swap_coords(lon, lat, bbox)
    return lon, lat


def station_in_bbox(station: Dict[str, Any], bbox: Dict[str, float]) -> bool:
    lon, lat = station_coords(station, bbox=bbox)
    if lon is None or lat is None:
        return False
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return False
    return bbox["west"] <= lon <= bbox["east"] and bbox["south"] <= lat <= bbox["north"]


def station_in_bbox_or_missing_coords(station: Dict[str, Any], bbox: Dict[str, float]) -> bool:
    lon, lat = station_coords(station, bbox=bbox)
    if lon is None or lat is None:
        return True
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return False
    return bbox["west"] <= lon <= bbox["east"] and bbox["south"] <= lat <= bbox["north"]
