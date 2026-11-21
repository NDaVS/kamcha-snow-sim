"""Run explicitly requested, quota-limited samples on the free endpoints."""
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENDPOINTS = ("weather", "forecast")
HOST = "https://api.openweathermap.org/data/2.5/"
FIELDS = {
    "temp_c": "Температура, °C",
    "wind_ms": "Скорость ветра, м/с",
    "wind_deg": "Направление ветра, °",
    "gust_ms": "Порывы ветра, м/с",
    "rain_mm": "Дождь за интервал, мм",
    "snow_mm": "Снег за интервал, мм API",
    "precip_mm_h": "Средняя интенсивность осадков, мм/ч",
    "rain_mm_h": "Средняя интенсивность дождя, мм/ч",
    "snow_mm_h": "Средняя интенсивность снега, мм API/ч",
    "humidity_pct": "Влажность, %",
    "visibility_m": "Видимость, м",
    "cloud_pct": "Облачность, %",
    "pop": "Вероятность осадков, 0–1",
}
COLUMNS = ["source", "endpoint", "round", "stage", "point_id", "latitude", "longitude",
           "district", "terrain", "inside_city", "on_land", "api_latitude", "api_longitude", "api_city_id",
           "retrieved_utc",
           "valid_utc", "valid_unix", "lead_h", "accumulation_h", "weather_id", *FIELDS,
           "missing_fields", "raw_file"]
ROUND_RE = re.compile(r"[A-Za-z0-9_-]{1,48}")


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def coordinates():
    return json.loads((HERE / "coordinates.geojson").read_text(encoding="utf-8"))


def sampling_points(doc):
    return [f["properties"] for f in doc["features"] if f["geometry"]["type"] == "Point"
            and "latitude" in f["properties"]]


def coordinate_key(point):
    return f'{point["latitude"]:.7f},{point["longitude"]:.7f}'


def numeric(value):
    return value if type(value) in (float, int) and math.isfinite(value) else None


def normalize(payload, point, endpoint, round_id, stage, retrieved, raw, source):
    records = [payload] if endpoint == "weather" else payload.get("list", [])
    location = payload if endpoint == "weather" else (payload.get("city") or {})
    returned = location.get("coord") or {}
    for record in records:
        if not isinstance(record, dict) or type(record.get("dt")) is not int:
            continue
        dt = record["dt"]
        hours = 1 if endpoint == "weather" else 3
        main = record.get("main") or {}
        wind = record.get("wind") or {}
        # Current publishes mm/h over one hour. Numerically that is also mm over one hour.
        rain = numeric((record.get("rain") or {}).get(f"{hours}h"))
        snow = numeric((record.get("snow") or {}).get(f"{hours}h"))
        weather = record.get("weather") or [{}]
        values = dict(temp_c=numeric(main.get("temp")), wind_ms=numeric(wind.get("speed")),
                      wind_deg=numeric(wind.get("deg")), gust_ms=numeric(wind.get("gust")),
                      rain_mm=rain, snow_mm=snow,
                      precip_mm_h=None if rain is None or snow is None else (rain + snow)/hours,
                      rain_mm_h=None if rain is None else rain/hours,
                      snow_mm_h=None if snow is None else snow/hours,
                      humidity_pct=numeric(main.get("humidity")), visibility_m=numeric(record.get("visibility")),
                      cloud_pct=numeric((record.get("clouds") or {}).get("all")),
                      pop=numeric(record.get("pop")) if endpoint == "forecast" else None)
        yield dict(source=source, endpoint=endpoint, round=round_id, stage=stage,
                   point_id=point["id"], **{k: point[k] for k in
                   ("latitude", "longitude", "district", "terrain", "inside_city", "on_land")},
                   api_latitude=numeric(returned.get("lat")),
                   api_longitude=numeric(returned.get("lon")),
                   api_city_id=numeric(location.get("id")),
                   retrieved_utc=iso(retrieved), valid_utc=iso(dt), valid_unix=dt,
                   lead_h=round((dt-retrieved)/3600, 4) if endpoint == "forecast" else None,
                   accumulation_h=hours, weather_id=weather[0].get("id"), **values,
                   missing_fields="|".join(name for name, value in values.items() if value is None),
                   raw_file=raw)


def write_csv(path, rows):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def database(path):
    db = sqlite3.connect(path, timeout=60)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS rounds (
            id TEXT PRIMARY KEY, signature TEXT NOT NULL, started INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            round TEXT NOT NULL, endpoint TEXT NOT NULL, coord TEXT NOT NULL,
            epoch REAL NOT NULL, status TEXT NOT NULL, filename TEXT,
            PRIMARY KEY (round, endpoint, coord)
        );
        CREATE TABLE IF NOT EXISTS targets (
            round TEXT NOT NULL, endpoint TEXT NOT NULL, coord TEXT NOT NULL,
            point_id TEXT NOT NULL, stage TEXT NOT NULL, point_json TEXT NOT NULL,
            PRIMARY KEY (round, endpoint, point_id)
        );
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return db


def pilot_has_signal(rows):
    # Three land points at the same valid time, with a nonzero difference within 2 km.
    from .analysis import grouped, pair_data
    for key, sample in grouped(rows).items():
        if key[0] != "openweather" or key[3] != "pilot":
            continue
        land = [row for row in sample if str(row["on_land"]) == "True"]
        for field in ("temp_c", "wind_ms", "rain_mm", "snow_mm", "precip_mm_h"):
            valid, pairs = pair_data(land, field)
            if len(valid) >= 3 and any(p["distance_m"] <= 2000 and p["difference"] > 0 for p in pairs):
                return True
    return False


def select_points(points, stage, size, max_points):
    if stage == "pilot":
        return [point for point in points if point.get("pilot")]
    grid = [point for point in points if point["kind"] == "grid" and point["size_m"] == size]
    anchors = [point for point in points if point["kind"] == "anchor"]
    if max_points >= len(grid):
        return grid

    # Spread the seeds, then add neighbors around the requested physical spacing.
    from .analysis import xy
    locations = {p["id"]: xy(p) for p in grid + anchors}
    pivot = locations[anchors[0]["id"]]
    seeds = []
    while len(seeds) < (max_points + 1) // 2:
        point = max(grid, key=lambda p: (min(math.dist(locations[p["id"]], q)
                                             for q in [pivot, *(locations[s["id"]] for s in seeds)]),
                                         p["id"]))
        grid.remove(point)
        seeds.append(point)

    selected = seeds.copy()
    for point in seeds[:max_points - len(seeds)]:
        origin = locations[point["id"]]
        neighbor = min(grid, key=lambda p: (abs(math.dist(locations[p["id"]], origin) - size),
                                             p["id"]))
        grid.remove(neighbor)
        selected.append(neighbor)
    return selected


def export_actual(db, state):
    rows = []
    query = """SELECT t.round, t.endpoint, t.stage, t.point_json, a.epoch, a.filename
               FROM targets t JOIN attempts a USING (round, endpoint, coord)
               WHERE a.status='ok' ORDER BY t.round, t.endpoint, t.point_id"""
    for round_id, endpoint, stage, raw_point, epoch, filename in db.execute(query):
        file = state / filename
        rows.extend(normalize(json.loads(file.read_bytes()), json.loads(raw_point), endpoint,
                              round_id, stage, epoch, filename, "openweather"))
    write_csv(HERE / "observations.csv", rows)
    return rows


def request_sample(session, key, endpoint, coord):
    lat, lon = coord.split(",")
    try:
        response = session.get(HOST + endpoint, params=dict(lat=lat, lon=lon, appid=key,
                               units="metric"), timeout=(5, 12), allow_redirects=False)
    except requests.RequestException:
        return "network_error", None
    if response.status_code != 200:
        return f"http_{response.status_code}", None
    # Keep the original JSON bytes. Discard unexpected bodies containing the credential.
    if key.encode() in response.content:
        return "credential_in_body", None
    try:
        payload = response.json()
    except ValueError:
        return "invalid_json", None
    if not isinstance(payload, dict) or str(payload.get("cod", 200)) != "200":
        return "invalid_payload", None
    if endpoint == "weather" and type(payload.get("dt")) is not int:
        return "invalid_payload", None
    if endpoint == "forecast" and not isinstance(payload.get("list"), list):
        return "invalid_payload", None
    return "ok", response.content


def run(args):
    import fcntl

    if not ROUND_RE.fullmatch(args.round):
        raise ValueError("Идентификатор среза должен содержать 1–48 символов A–Z, 0–9, _ или -.")
    state = HERE / ".state"
    state.mkdir(exist_ok=True)
    with (state / "experiment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(database(state / "ledger.sqlite")) as db:
            try:
                collect(db, state, args)
            finally:
                rows = export_actual(db, state)
                print(f"Фактических записей в observations.csv: {len(rows)}")


def collect(db, state, args):
    key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENWEATHER_API_KEY отсутствует. Для проверки обработки используйте demo.")
    points = select_points(sampling_points(coordinates()), args.stage, args.grid, args.max_points)
    endpoints = tuple(dict.fromkeys(args.endpoints))
    signature = hashlib.sha256(json.dumps([args.stage, points, endpoints]).encode()).hexdigest()
    now = int(time.time())
    old = db.execute("SELECT signature, started FROM rounds WHERE id=?", (args.round,)).fetchone()
    if old and old[0] != signature:
        raise ValueError("У среза уже зафиксирован другой план выборки.")
    stored = db.execute("SELECT value FROM config WHERE key='budget'").fetchone()
    if stored is None:
        budget = args.budget if args.budget is not None else 100
    else:
        budget = int(stored[0])
        if args.budget is not None and budget != args.budget:
            raise ValueError(f"Бюджет эксперимента уже зафиксирован ({budget}).")
    access = dict(db.execute("""
        SELECT endpoint, status FROM attempts
        WHERE status IN ('ok', 'http_401', 'http_403')
        ORDER BY epoch
    """))
    blocked = {endpoint for endpoint, status in access.items() if status != "ok"}
    if args.recheck_access:
        if old:
            raise ValueError("Для повторной проверки доступа нужен новый идентификатор среза.")
        blocked.clear()
    previous = {(ep, coord) for ep, coord in db.execute(
        "SELECT endpoint, coord FROM attempts WHERE round=?", (args.round,))}
    if db.execute("SELECT 1 FROM attempts WHERE round=? AND status='http_429'",
                  (args.round,)).fetchone():
        print("Срез остановлен после HTTP 429.")
        return
    plan = [(ep, coordinate_key(point)) for ep in endpoints if ep not in blocked for point in points]
    pending = [pair for pair in dict.fromkeys(plan) if pair not in previous]
    if not pending:
        print("Запланированные запросы уже учтены или продукт заблокирован.")
        return
    if old and now - old[1] > 720:
        raise ValueError("Окно среза истекло. Для нового опроса укажите новый --round.")
    used = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    if used + len(pending) > budget:
        raise ValueError(f"Остаток бюджета {budget-used}, выборка требует {len(pending)} запросов.")
    if args.stage == "dense" and not pilot_has_signal(export_actual(db, state)):
        raise ValueError("Для плотного опроса требуются фактические пространственные различия в пилоте.")
    with db:
        db.execute("INSERT OR IGNORE INTO rounds VALUES (?,?,?)", (args.round, signature, now))
        db.execute("INSERT OR IGNORE INTO config VALUES ('budget',?)", (str(budget),))
        db.executemany("INSERT OR IGNORE INTO targets VALUES (?,?,?,?,?,?)",
                       [(args.round, ep, coordinate_key(p), p["id"], args.stage, json.dumps(p))
                        for ep in endpoints if ep not in blocked for p in points])
    raw = state / "raw" / args.round
    raw.mkdir(parents=True, exist_ok=True)
    last = db.execute("SELECT MAX(epoch) FROM attempts").fetchone()[0] or 0
    with requests.Session() as session:
        for endpoint, coord in pending:
            if endpoint in blocked:
                continue
            time.sleep(max(0, 2.05 - (time.time()-last)))
            last = time.time()
            if last - (old[1] if old else now) > 720:
                print("Достигнута граница 12-минутного окна среза.")
                break
            with db:
                db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,NULL)",
                           (args.round, endpoint, coord, last, "reserved"))
            status, content = request_sample(session, key, endpoint, coord)
            filename = None
            if content is not None:
                out = raw / f"{endpoint}-{coord.replace(',', '_')}.json"
                out.write_bytes(content)
                filename = str(out.relative_to(state))
            with db:
                db.execute("UPDATE attempts SET status=?,filename=? WHERE round=? AND endpoint=? AND coord=?",
                           (status, filename, args.round, endpoint, coord))
            if status in {"http_401", "http_403", "http_429"}:
                blocked.add(endpoint)
            print(endpoint, coord, status)
            if status == "http_429":
                break


def demo():
    """Synthetic example for method QA. Deliberately includes missing values."""
    points = [p for p in sampling_points(coordinates()) if p.get("pilot")]
    rows = []
    base = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
    for take in range(2):
        retrieval = base + take * 21600
        for i, point in enumerate(points):
            cluster = i % 5
            for endpoint in ENDPOINTS:
                hours = 1 if endpoint == "weather" else 3
                recs = []
                for h in ([0] if endpoint == "weather" else [3, 6, 9]):
                    dt = retrieval + 3600*h + (360 if endpoint == "weather" and i % 8 == 0 else 0)
                    rec = dict(dt=dt, main=dict(temp=-4 + 0.35*cluster + take + h/10,
                                              humidity=88 + cluster),
                               wind=dict(speed=round(3.4 + 0.4*cluster, 1), deg=(350+10*cluster)%360),
                               weather=[{"id": 600}], clouds={"all": 92 + cluster},
                               visibility=10000-1200*cluster)
                    rec["main"]["temp"] = round(rec["main"]["temp"], 2)
                    if i % 3:
                        rec["wind"]["gust"] = 6 + cluster
                    if i % 7:
                        rec["snow"] = {f"{hours}h": round((0.4 + 0.1*cluster)*hours, 2)}
                    if i % 5:
                        rec["rain"] = {f"{hours}h": 0}
                    if endpoint == "forecast":
                        rec["pop"] = round(0.6 + 0.08*cluster, 2)
                    recs.append(rec)
                payload = recs[0] if endpoint == "weather" else dict(list=recs)
                rows.extend(normalize(payload, point, endpoint, f"demo-{take+1}", "pilot",
                                      retrieval, "", "demo"))
    write_csv(HERE / "observations_demo.csv", rows)
    return rows
