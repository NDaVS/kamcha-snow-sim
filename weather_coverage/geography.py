"""Rebuild the urban sampling frame from the project OSM extracts."""
import gzip
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import Transformer
from shapely import make_valid, union_all
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import polygonize, transform

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TO_METRIC = Transformer.from_crs(4326, 32657, always_xy=True).transform
TO_WGS84 = Transformer.from_crs(32657, 4326, always_xy=True).transform
SIZES = (500, 1000, 2000)
MAJOR = {"trunk", "primary", "secondary", "tertiary"}
DISTRICTS = (
    ("center", "Центр", "w692422663"),
    ("seroglazka", "Сероглазка", "n360831425"),
    ("ne-upper", "Верхний Северо-Восток", "w117095790"),
    ("ne-lower", "Нижний Северо-Восток", "w117095792"),
    ("horizon-n", "Горизонт-Север", "n3861863057"),
    ("horizon-s", "Горизонт-Юг", "n3864490958"),
    ("dachnaya", "Дачная", "w106795372"),
    ("srv", "СРВ", "n1323173734"),
    ("zavoyko", "Завойко", "n7218420589"),
)


def read_osm(paths):
    elements = {}
    for path in paths:
        with gzip.open(path, "rb") as stream:
            for elem in ET.parse(stream).getroot():
                if elem.tag not in {"node", "way", "relation"}:
                    continue
                key = elem.tag[0] + elem.get("id")
                prev = elements.get(key)
                if prev is None or int(elem.get("version", "0")) >= int(prev.get("version", "0")):
                    elements[key] = elem
    return elements


def tags(elem):
    return {tag.get("k"): tag.get("v") for tag in elem.findall("tag")}


def cell_id(size, x, y):
    return f"g{size}-{math.floor(x / size)}-{math.floor(y / size)}"


def geo_feature(geom, **props):
    return {"type": "Feature", "geometry": mapping(transform(TO_WGS84, geom)),
            "properties": props}


def relation_polygon(relation, lines, strict=False):
    roles = {}
    for role in ("outer", "inner"):
        keys = ["w" + member.get("ref") for member in relation.findall("member")
                if member.get("type") == "way" and member.get("role") == role]
        if any(key not in lines for key in keys):
            if strict:
                raise ValueError(f"Missing ways in OSM relation {relation.get('id')}")
            return None
        merged = list(polygonize([lines[key] for key in keys]))
        if keys and not merged:
            return None
        roles[role] = union_all(merged)
    return make_valid(roles["outer"].difference(roles["inner"]))


def build():
    osm = sorted((HERE / "osm").glob("*.osm.gz"))
    osm += [ROOT / "data/pkc" / f"{name}.osm.gz" for name in ("city", "depot-east")]
    elem = read_osm(osm)
    points = {key: Point(TO_METRIC(float(e.get("lon")), float(e.get("lat"))))
              for key, e in elem.items() if key.startswith("n")}
    lines = {}
    for key, e in elem.items():
        if key.startswith("w"):
            refs = ["n" + node.get("ref") for node in e.findall("nd")]
            if len(refs) >= 2 and all(ref in points for ref in refs):
                lines[key] = LineString([points[ref] for ref in refs])
    way_tags = {key: tags(elem[key]) for key in lines}
    city = relation_polygon(elem["r4139922"], lines, strict=True)
    bay = relation_polygon(elem["r1763103"], lines, strict=True)
    if city is None or city.is_empty or bay is None or bay.is_empty:
        raise ValueError("Could not assemble city or bay")
    water_areas = [bay]
    residential, industrial = [], []
    for key, line in lines.items():
        meta = way_tags[key]
        if not line.is_ring:
            continue
        area = make_valid(Polygon(line))
        if meta.get("natural") in {"water", "bay"} or meta.get("waterway") == "riverbank":
            water_areas.append(area)
        if meta.get("landuse") == "residential":
            residential.append(area)
        if meta.get("landuse") in {"commercial", "retail", "industrial"}:
            industrial.append(area)
    for e in elem.values():
        if e.tag == "relation":
            meta = tags(e)
            if meta.get("type") == "multipolygon" and meta.get("natural") == "water":
                area = relation_polygon(e, lines)
                if area is not None:
                    water_areas.append(area)
    water = union_all(water_areas)
    land = city.difference(water)
    residential = union_all(residential)
    industrial = union_all(industrial)
    road_areas = []
    major = []
    for key, line in sorted(lines.items()):
        meta = way_tags[key]
        highway = meta.get("highway")
        if meta.get("access") in {"private", "no"} or meta.get("bridge") not in {None, "no"}:
            continue
        if highway == "residential":
            road_areas.append(line.buffer(55))
        if highway in MAJOR:
            major.append((key, line, meta.get("name", meta.get("ref", highway))))
    developed = union_all([residential, industrial, *road_areas])
    footprint = land.intersection(developed)
    anchors = []
    for slug, label, key in DISTRICTS:
        geometry = points[key] if key.startswith("n") else Polygon(lines[key])
        point = geometry if key.startswith("n") else geometry.representative_point()
        if not land.covers(point):
            raise ValueError(f"OSM anchor outside mapped land: {label}")
        anchors.append((slug, label, key, point, geometry))

    dem_path = ROOT / "winter_audit/evidence/originals/pkc-glo30.tif"
    dem = None
    if dem_path.exists():
        from winter_audit.relief import Surface
        dem = Surface(dem_path)

    road_context = union_all([line for _, line, _ in major]).intersection(
        land.intersection(footprint.buffer(400)))
    features = [
        geo_feature(water, kind="water", name="Акватория и картированные водоёмы"),
        geo_feature(road_context, kind="arterials", name="Основные дороги у зоны исследования"),
        geo_feature(city, kind="city", osm="r4139922", name="Граница города (OSM, условная)"),
        geo_feature(footprint, kind="footprint", name="Застройка и жилые дороги OSM"),
    ]
    for slug, label, key, _, geom in anchors:
        features.append(geo_feature(geom, kind="district", id=slug, name=label, osm=key,
                                    osm_name=tags(elem[key]).get("name")))

    def add_point(pid, point, kind, pilot=False, district=None, osm_id=None, **extra):
        lon, lat = TO_WGS84(point.x, point.y)
        inferred = next((name for _, name, _, _, geom in anchors
                         if geom.geom_type == "Polygon" and geom.covers(point)), None)
        terrain = ("контроль акватории" if water.covers(point)
                   else "жилая" if residential.covers(point)
                   else "коммерческая / промышленная" if industrial.covers(point)
                   else "дорожная / застроенная")
        height = dem.sample((lon, lat)) if dem and land.covers(point) else None
        features.append(geo_feature(
            point, kind=kind, id=pid, pilot=pilot, latitude=lat, longitude=lon,
            district=district or inferred, osm=osm_id, terrain=terrain,
            inside_city=bool(city.covers(point)), on_land=bool(land.covers(point)),
            cell_ids={str(s): cell_id(s, point.x, point.y) for s in SIZES},
            elevation_m_dsm=None if height is None else round(height, 1),
            elevation_source=None if height is None else "Copernicus GLO-30 / EGM2008",
            **extra,
        ))

    for slug, label, key, point, _ in anchors:
        add_point(slug, point, "anchor", pilot=True, district=label, osm_id=key)

    for size in SIZES:
        xmin, ymin, xmax, ymax = footprint.bounds
        for x in range(math.floor(xmin / size), math.ceil(xmax / size)):
            for y in range(math.floor(ymin / size), math.ceil(ymax / size)):
                tile = box(x*size, y*size, (x+1)*size, (y+1)*size)
                urban = footprint.intersection(tile)
                if urban.area <= 0:
                    continue
                key = cell_id(size, tile.centroid.x, tile.centroid.y)
                features.append(geo_feature(tile, kind="cell", id=key, size_m=size,
                                            urban_share=round(urban.area/tile.area, 4)))
                center = tile.centroid
                fallback = not urban.covers(center)
                point = urban.representative_point() if fallback else center
                add_point(key, point, "grid", size_m=size, shifted_from_cell_center=fallback)

    # One road point per 1-km cell, drawn from named major roads on the developed land.
    roads = {}
    for key, line, name in sorted(major, key=lambda row: (-row[1].length, row[0])):
        for distance in range(60, int(line.length), 120):
            point = line.interpolate(distance)
            if footprint.covers(point):
                roads.setdefault(cell_id(1000, point.x, point.y), (key, point, name))
    # Farthest-first over corridors gives citywide spread in the pilot.
    chosen = [row[3] for row in anchors]
    pending = dict(roads)
    road_pilot = set()
    for _ in range(min(5, len(pending))):
        cell = max(pending, key=lambda key: (min(pending[key][1].distance(p) for p in chosen), key))
        road_pilot.add(cell)
        chosen.append(pending.pop(cell)[1])
    for cell, (key, point, name) in roads.items():
        add_point("road-" + cell, point, "road", pilot=cell in road_pilot, osm_id=key, road=name)

    # Search exact radii in two independent urban transects. Keep points on the mask.
    for slug in ("ne-lower", "seroglazka"):
        point = next(row[3] for row in anchors if row[0] == slug)
        for radius in (250, 500, 1000, 2000):
            candidates = [Point(point.x + radius*math.cos(math.radians(angle)),
                                point.y + radius*math.sin(math.radians(angle)))
                          for angle in range(0, 360, 5)]
            candidates = [p for p in candidates if footprint.covers(p)]
            if not candidates:
                raise ValueError(f"No developed land at {radius} m from {slug}")
            endpoint = max(candidates, key=lambda p: min(p.distance(other) for other in chosen))
            chosen.append(endpoint)
            add_point(f"{slug}-{radius}", endpoint, "transect", pilot=True,
                      pair_anchor=slug, target_distance_m=radius)

    near_city = bay.intersection(city.buffer(1800)).intersection(
        Point(TO_METRIC(158.62, 53.00)).buffer(800))
    if not near_city.is_empty:
        add_point("bay-control", near_city.representative_point(), "control", pilot=True,
                  osm_id="r1763103")

    doc = {"type": "FeatureCollection", "attribution": "© OpenStreetMap contributors (ODbL)",
           "metric_crs": "EPSG:32657",
           "sources_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in osm},
           "features": features}
    (HERE / "coordinates.geojson").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc
