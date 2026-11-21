"""Exact-time, within-product spatial comparisons."""
import itertools
import math
from collections import Counter, defaultdict

from .geography import TO_METRIC

THRESHOLDS = {
    "temp_c": 0.5, "wind_ms": 1, "wind_deg": 30, "gust_ms": 2, "rain_mm": None,
    "snow_mm": None, "precip_mm_h": 0.2, "rain_mm_h": 0.2, "snow_mm_h": 0.2,
    "humidity_pct": 5, "visibility_m": 1000, "cloud_pct": 10, "pop": 0.2,
}
BINS = (250, 500, 1000, 2000, 4000, 8000, math.inf)


def xy(row):
    return TO_METRIC(float(row["longitude"]), float(row["latitude"]))


def grouped(rows):
    groups = defaultdict(dict)
    for row in rows:
        key = tuple(row[col] for col in ("source", "endpoint", "round", "stage", "valid_unix"))
        groups[key].setdefault(row["point_id"], row)
    return {key: list(sample.values()) for key, sample in groups.items()}


def value(row, field):
    if field in ("rain_mm_h", "snow_mm_h"):
        mm = value(row, field[:-2])
        return None if mm is None else mm / float(row["accumulation_h"])
    raw = row.get(field)
    return None if raw in (None, "") else float(raw)


def difference(a, b, field):
    return abs((a-b+180) % 360-180) if field == "wind_deg" else abs(a-b)


def pair_data(sample, field):
    valid = {r["point_id"]: value(r, field) for r in sample if value(r, field) is not None}
    locations = {r["point_id"]: xy(r) for r in sample if r["point_id"] in valid}
    pairs = []
    for a, b in itertools.combinations(valid, 2):
        distance = math.dist(locations[a], locations[b])
        if distance < 0.02:
            continue
        pairs.append(dict(a=a, b=b, distance_m=distance, difference=difference(valid[a], valid[b], field)))
    return valid, pairs


def nearest_pairs(pairs):
    nearest = {}
    for pair in sorted(pairs, key=lambda p: p["distance_m"]):
        nearest.setdefault(pair["a"], pair)
        nearest.setdefault(pair["b"], pair)
    return list({tuple(sorted((p["a"], p["b"]))): p for p in nearest.values()}.values())


def exact_groups(valid):
    by_value = defaultdict(list)
    for key, v in valid.items():
        by_value[v].append(key)
    return dict(by_value)


def response_groups(sample, fields):
    counts = defaultdict(list)
    for row in sample:
        vector = tuple(value(row, field) for field in fields)
        if any(v is not None for v in vector):
            counts[vector].append(row["point_id"])
    return [ids for ids in counts.values() if len(ids) >= 2]


def rarefaction(valid):
    """Expected number of unique values in a uniform sample without replacement."""
    frequencies = Counter(valid.values())
    n = len(valid)
    return [sum(1 - (math.comb(n-f, k) / math.comb(n, k) if k <= n-f else 0)
                for f in frequencies.values()) for k in range(1, n+1)]


def distance_summary(pairs):
    from statistics import median
    buckets = defaultdict(list)
    for pair in pairs:
        distance = round(pair["distance_m"], 2)
        upper = next(edge for edge in BINS if distance <= edge)
        buckets[upper].append(pair["difference"])
    return [(upper, len(deltas), sum(d > 0 for d in deltas), median(deltas))
            for upper, deltas in sorted(buckets.items())]


def transect_summary(groups, points):
    """Count repeated threshold exceedances per endpoint, pair and distinct UTC time."""
    by_pair = defaultdict(dict)
    for key, sample in groups.items():
        source, endpoint, _, stage, dt = key
        if stage != "pilot":
            continue
        rows = {r["point_id"]: r for r in sample}
        for point in points:
            if point.get("pair_anchor") not in rows or point["id"] not in rows:
                continue
            a, b = rows[point["pair_anchor"]], rows[point["id"]]
            for field, threshold in THRESHOLDS.items():
                if threshold is None:
                    continue
                v1, v2 = value(a, field), value(b, field)
                if v1 is None or v2 is None:
                    continue
                delta = difference(v1, v2, field)
                by_pair[(source, endpoint, point["pair_anchor"], point["target_distance_m"], field)
                        ].setdefault(int(dt), []).append(delta)
    result = []
    for (source, endpoint, anchor, radius, field), epochs in sorted(by_pair.items()):
        # Multiple updates for the same valid time count once. Require consistency across updates.
        hits = [epoch for epoch, deltas in epochs.items() if min(deltas) + 1e-9 >= THRESHOLDS[field]]
        result.append(dict(source=source, endpoint=endpoint, anchor=anchor,
                           radius_m=radius, field=field, available_times=len(epochs),
                           exceeding_times=len(hits), repeat_candidate=len(hits) >= 2))
    return result


def cross_product(rows, fields):
    """Match exact time and point, selecting the latest forecast issued before valid time."""
    forecasts, current = defaultdict(list), defaultdict(list)
    for row in rows:
        if str(row["on_land"]) != "True":
            continue
        key = row["source"], row["point_id"], row["valid_utc"]
        if row["endpoint"] == "forecast" and float(row["lead_h"]) > 0:
            forecasts[key].append(row)
        if row["endpoint"] == "weather":
            current[key].append(row)
    for key in sorted(forecasts.keys() & current.keys()):
        f = max(forecasts[key], key=lambda r: r["retrieved_utc"])
        c = max(current[key], key=lambda r: r["retrieved_utc"])
        for field in fields:
            a, b = value(f, field), value(c, field)
            if a is not None and b is not None:
                yield key + (field, f["lead_h"], difference(a, b, field))
