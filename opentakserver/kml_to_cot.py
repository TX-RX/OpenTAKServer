"""Convert KML attachments on a TAK mission into CoT XML events.

Standalone pure-stdlib module (no new dependencies). KMLs uploaded as
mission contents are parsed here and rendered as CoT events so the
``GET /Marti/api/missions/<name>/cot`` endpoint can serve them to
clients (CloudTAK, ATAK, iTAK, WinTAK) for map display.

Public entrypoints:
    parse_kml(bytes) -> list[dict]
    features_to_cot_events(features, source_hash) -> list[Element]
    kml_attachment_to_cot_events(bytes, source_hash) -> list[Element]
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

KML_NS = "{http://www.opengis.net/kml/2.2}"

# Bright pink fallback so missing-color bugs are visually obvious on the map.
DEFAULT_COLOR = -65281

# CoT type for an open polyline (KML LineString -> bike/hike route etc.).
# NOTE: we deliberately use u-d-f, NOT u-d-r. node-cot (used by CloudTAK) treats
# u-d-r as a CoT rectangle (4-point closed shape) and force-renders it as a
# Polygon; u-d-f falls through to LineString as long as the first and last
# coords differ. We also defensively perturb the last point in _ensure_open()
# so a closed-loop KML doesn't trip the "first==last -> Polygon" branch either.
COT_TYPE_ROUTE = "u-d-f"
# CoT type for a spot marker.
COT_TYPE_POI = "b-m-p-s-m"
# CoT type for an explicitly-closed free-form polygon.
COT_TYPE_POLYGON = "u-d-f"


def _ensure_open(coords: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """If the polyline's first and last points are identical, nudge the last
    point ~1cm so node-cot's first==last check falls through to LineString."""
    if len(coords) < 2:
        return coords
    if coords[0][0] == coords[-1][0] and coords[0][1] == coords[-1][1]:
        lng, lat, hae = coords[-1]
        coords = coords[:-1] + [(lng + 1e-7, lat, hae)]
    return coords


def _kml_color_to_android(kml_color: str) -> int:
    """KML AABBGGRR hex -> Android-style signed int32 ARGB."""
    if not kml_color or len(kml_color) != 8:
        return DEFAULT_COLOR
    try:
        a = int(kml_color[0:2], 16)
        b = int(kml_color[2:4], 16)
        g = int(kml_color[4:6], 16)
        r = int(kml_color[6:8], 16)
    except ValueError:
        return DEFAULT_COLOR
    argb = (a << 24) | (r << 16) | (g << 8) | b
    if argb >= 0x80000000:
        argb -= 0x100000000
    return argb


def _stable_uid(prefix: str, *parts: str) -> str:
    """Deterministic UUID-shaped string. Same inputs -> same UID across runs."""
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _iso(d: datetime.datetime) -> str:
    return d.replace(microsecond=0).isoformat().replace("+00:00", "") + "Z"


def _parse_coords(text: str) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lng = float(parts[0])
            lat = float(parts[1])
            hae = float(parts[2]) if len(parts) >= 3 else 0.0
        except ValueError:
            continue
        out.append((lng, lat, hae))
    return out


def _resolve_color(placemark: ET.Element, styles: dict[str, int]) -> int:
    """Find a color for this placemark: inline LineStyle/color first, then styleUrl reference."""
    inline = placemark.find(f".//{KML_NS}LineStyle/{KML_NS}color")
    if inline is not None and (inline.text or "").strip():
        return _kml_color_to_android(inline.text.strip())
    url = placemark.findtext(KML_NS + "styleUrl")
    if url:
        return styles.get(url.lstrip("#"), DEFAULT_COLOR)
    return DEFAULT_COLOR


def parse_kml(kml_bytes: bytes) -> list[dict]:
    """Parse KML bytes into a list of feature dicts.

    Each feature dict has: kind ("route"|"point"|"polygon"), name, description,
    coords [(lng,lat,hae)...], color (Android int32).
    """
    features: list[dict] = []
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as exc:
        logger.warning("KML parse failed: %s", exc)
        return features

    # Top-level Style id -> color (only LineStyle for now)
    styles: dict[str, int] = {}
    for style in root.iter(KML_NS + "Style"):
        sid = style.get("id")
        if not sid:
            continue
        color_el = style.find(f"./{KML_NS}LineStyle/{KML_NS}color")
        if color_el is not None and (color_el.text or "").strip():
            styles[sid] = _kml_color_to_android(color_el.text.strip())

    for placemark in root.iter(KML_NS + "Placemark"):
        name = (placemark.findtext(KML_NS + "name") or "").strip()
        description = (placemark.findtext(KML_NS + "description") or "").strip()
        color = _resolve_color(placemark, styles)

        ls = placemark.find(KML_NS + "LineString")
        if ls is not None:
            coords = _parse_coords(ls.findtext(KML_NS + "coordinates") or "")
            if coords:
                features.append({
                    "kind": "route",
                    "name": name,
                    "description": description,
                    "coords": coords,
                    "color": color,
                })
            continue

        pt = placemark.find(KML_NS + "Point")
        if pt is not None:
            coords = _parse_coords(pt.findtext(KML_NS + "coordinates") or "")
            if coords:
                features.append({
                    "kind": "point",
                    "name": name,
                    "description": description,
                    "coords": coords[:1],
                    "color": color,
                })
            continue

        poly = placemark.find(KML_NS + "Polygon")
        if poly is not None:
            coords_text = poly.findtext(f".//{KML_NS}LinearRing/{KML_NS}coordinates") or ""
            coords = _parse_coords(coords_text)
            if coords:
                features.append({
                    "kind": "polygon",
                    "name": name,
                    "description": description,
                    "coords": coords,
                    "color": color,
                })

    return features


def _add_point_summary(event: ET.Element, coords: list[tuple[float, float, float]]) -> None:
    avg_lng = sum(c[0] for c in coords) / len(coords)
    avg_lat = sum(c[1] for c in coords) / len(coords)
    ET.SubElement(event, "point", {
        "lat": f"{avg_lat:.6f}",
        "lon": f"{avg_lng:.6f}",
        "hae": "9999999.0",
        "ce": "9999999.0",
        "le": "9999999.0",
    })


def _add_links(detail: ET.Element, uid: str, coords: list[tuple[float, float, float]]) -> None:
    for i, (lng, lat, hae) in enumerate(coords):
        ET.SubElement(detail, "link", {
            "uid": f"{uid}-pt-{i}",
            "point": f"{lat:.6f},{lng:.6f},{hae}",
            "relation": "c",
        })


def _build_route(f: dict, source_hash: str, idx: int, time_str: str, stale: str) -> ET.Element:
    uid = _stable_uid("kml-route", source_hash, str(idx), f.get("name", ""))
    coords = _ensure_open(f["coords"])
    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": COT_TYPE_ROUTE,
        "how": "h-e",
        "time": time_str,
        "start": time_str,
        "stale": stale,
    })
    _add_point_summary(event, coords)
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": f.get("name") or "Route"})
    if f.get("description"):
        ET.SubElement(detail, "remarks").text = f["description"]
    _add_links(detail, uid, coords)
    color = f.get("color", DEFAULT_COLOR)
    ET.SubElement(detail, "strokeColor", {"value": str(color)})
    ET.SubElement(detail, "strokeWeight", {"value": "5.0"})
    ET.SubElement(detail, "strokeStyle", {"value": "solid"})
    ET.SubElement(detail, "archive")
    return event


def _build_point(f: dict, source_hash: str, idx: int, time_str: str, stale: str) -> ET.Element:
    uid = _stable_uid("kml-poi", source_hash, str(idx), f.get("name", ""))
    lng, lat, hae = f["coords"][0]
    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": COT_TYPE_POI,
        "how": "h-e",
        "time": time_str,
        "start": time_str,
        "stale": stale,
    })
    ET.SubElement(event, "point", {
        "lat": f"{lat:.6f}",
        "lon": f"{lng:.6f}",
        "hae": str(hae),
        "ce": "9999999.0",
        "le": "9999999.0",
    })
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": f.get("name") or "POI"})
    if f.get("description"):
        ET.SubElement(detail, "remarks").text = f["description"]
    ET.SubElement(detail, "archive")
    return event


def _build_polygon(f: dict, source_hash: str, idx: int, time_str: str, stale: str) -> ET.Element:
    uid = _stable_uid("kml-poly", source_hash, str(idx), f.get("name", ""))
    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": COT_TYPE_POLYGON,
        "how": "h-e",
        "time": time_str,
        "start": time_str,
        "stale": stale,
    })
    _add_point_summary(event, f["coords"])
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": f.get("name") or "Polygon"})
    if f.get("description"):
        ET.SubElement(detail, "remarks").text = f["description"]
    _add_links(detail, uid, f["coords"])
    color = f.get("color", DEFAULT_COLOR)
    ET.SubElement(detail, "strokeColor", {"value": str(color)})
    ET.SubElement(detail, "strokeWeight", {"value": "5.0"})
    ET.SubElement(detail, "strokeStyle", {"value": "solid"})
    fill = (color & 0x00FFFFFF) | 0x40000000
    if fill >= 0x80000000:
        fill -= 0x100000000
    ET.SubElement(detail, "fillColor", {"value": str(fill)})
    ET.SubElement(detail, "archive")
    return event


_BUILDERS = {
    "route": _build_route,
    "point": _build_point,
    "polygon": _build_polygon,
}


def features_to_cot_events(
    features: list[dict],
    source_hash: str,
    *,
    stale_after_days: int = 365 * 10,
    now: Optional[datetime.datetime] = None,
) -> list[ET.Element]:
    """Build CoT Elements from parsed features.

    Stable per-feature UIDs derive from source_hash+index+name so re-parsing
    the same KML yields identical UIDs (clients can dedupe on re-fetch).
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    time_str = _iso(now)
    stale_str = _iso(now + datetime.timedelta(days=stale_after_days))
    events: list[ET.Element] = []
    for idx, f in enumerate(features):
        if not f.get("coords"):
            continue
        builder = _BUILDERS.get(f.get("kind", ""))
        if not builder:
            continue
        try:
            events.append(builder(f, source_hash, idx, time_str, stale_str))
        except Exception:
            logger.exception("Failed to build CoT for KML feature idx=%s name=%r", idx, f.get("name"))
    return events


def kml_attachment_to_cot_events(kml_bytes: bytes, source_hash: str) -> list[ET.Element]:
    """Public one-shot: parse a KML attachment and return its CoT events."""
    features = parse_kml(kml_bytes)
    return features_to_cot_events(features, source_hash)


# Mirrors the UID+type logic inside the _build_* helpers so the /layers endpoint
# can group features by their source KML. (Stable hash-derived UIDs let us
# re-derive the same UIDs without round-tripping through the CoT XML.)
def _feature_summary(f: dict, source_hash: str, idx: int) -> Optional[dict]:
    kind = f.get("kind")
    coords = f.get("coords") or []
    if not coords:
        return None
    if kind == "route":
        uid = _stable_uid("kml-route", source_hash, str(idx), f.get("name", ""))
        cot_type = COT_TYPE_ROUTE
        lng = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
    elif kind == "point":
        uid = _stable_uid("kml-poi", source_hash, str(idx), f.get("name", ""))
        cot_type = COT_TYPE_POI
        lng, lat, _ = coords[0]
    elif kind == "polygon":
        uid = _stable_uid("kml-poly", source_hash, str(idx), f.get("name", ""))
        cot_type = COT_TYPE_POLYGON
        lng = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
    else:
        return None
    return {
        "uid": uid,
        "type": cot_type,
        "callsign": f.get("name") or kind,
        "lat": lat,
        "lon": lng,
    }


def kml_attachment_to_layer(
    kml_bytes: bytes,
    source_hash: str,
    source_name: str,
    *,
    creator_uid: str = "kml-attachment",
    now: Optional[datetime.datetime] = None,
) -> Optional[dict]:
    """Return a MissionLayer dict (TAK Marti shape) grouping the CoT events
    generated from a single KML attachment. Used by /Marti/api/missions/<name>/layers
    so CloudTAK groups our generated features under a tidy named tree node
    instead of listing every route+POI as separate orphan entries.
    """
    features = parse_kml(kml_bytes)
    if not features:
        return None

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    time_str = _iso(now)

    uids: list[dict] = []
    for idx, f in enumerate(features):
        summary = _feature_summary(f, source_hash, idx)
        if not summary:
            continue
        uids.append({
            "data": summary["uid"],
            "timestamp": time_str,
            "creatorUid": creator_uid,
            "details": {
                "type": summary["type"],
                "callsign": summary["callsign"],
                "location": {"lat": summary["lat"], "lon": summary["lon"]},
            },
        })

    if not uids:
        return None

    # Use the bare filename as the layer label.
    layer_name = (source_name or "KML attachment").rsplit("/", 1)[-1]
    if "." in layer_name:
        layer_name = layer_name.rsplit(".", 1)[0]

    return {
        "name": layer_name,
        "type": "UID",
        "uid": _stable_uid("kml-layer", source_hash, source_name or ""),
        "uids": uids,
    }
