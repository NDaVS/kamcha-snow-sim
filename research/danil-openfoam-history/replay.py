"""Replay three archived weather hours through Danil's public HTTP API."""
import gzip
import json
from pathlib import Path
from statistics import fmean
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
INPUTS = json.loads((ROOT / "inputs.json").read_text())
OUTPUT = ROOT.parents[1] / "out/danil-openfoam-history"
API = "http://127.0.0.1:8011/api/snow"


def calculate(payload):
    request = Request(API, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=600) as response:
        return json.load(response)


def paired_metrics(cfd, hybrid):
    assert cfd["model"] == "openfoam_tile" and cfd["foam_run"]
    assert hybrid["model"] == "hybrid" and hybrid["cfd_coverage"] == 1
    assert cfd["bbox_wgs84"] == hybrid["bbox_wgs84"]
    pairs = []
    for cfd_row, hybrid_row in zip(cfd["snow_depth_cm"], hybrid["snow_depth_cm"], strict=True):
        for direct, template in zip(cfd_row, hybrid_row, strict=True):
            assert (direct is None) == (template is None)
            if direct is not None:
                pairs.append((direct, template))
    direct, template = zip(*pairs)
    return {
        "comparable_cells": len(pairs),
        "buildings_on_tile": cfd["building_count"],
        "hybrid_coverage": hybrid["cfd_coverage"],
        "cfd_mean_cm": fmean(direct),
        "hybrid_mean_cm": fmean(template),
        "cfd_max_cm": cfd["max_snow_depth_cm"],
        "hybrid_max_cm": hybrid["max_snow_depth_cm"],
        "mean_abs_difference_cm": fmean(abs(a - b) for a, b in pairs),
        "max_abs_difference_cm": max(abs(a - b) for a, b in pairs),
        "cfd_compute_ms": cfd["compute_ms"],
        "hybrid_compute_ms": hybrid["compute_ms"],
        "foam_run": cfd["foam_run"],
    }


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    summary = {"commit": INPUTS["commit"], "tile": INPUTS["tile"], "openfoam": "v2512", "runs": []}
    for scenario in INPUTS["scenarios"]:
        weather = scenario["weather"]
        payload = {
            "tile": INPUTS["tile"],
            "city_cache_only": True,
            "hours": 1,
            "resolution": 120,
            "manual_weather": {
                "wind_from_deg": weather["wind_from_deg"] % 360,
                "wind_speed_ms": weather["wind_speed_ms"],
                "snowfall_cm_h": weather["snowfall_cm"],
            },
        }
        print(f"Running {scenario['episode']} at {weather['valid_at']}", flush=True)
        cfd = calculate({**payload, "run_foam": True})
        hybrid = calculate(payload)
        metrics = paired_metrics(cfd, hybrid)
        entry = {**scenario, **metrics}
        filename = f"{scenario['episode']}.json.gz"
        archive = json.dumps({"scenario": scenario, "request": payload, "cfd": cfd, "hybrid": hybrid})
        (OUTPUT / filename).write_bytes(gzip.compress(archive.encode(), mtime=0))
        summary["runs"].append({**entry, "result_file": filename})
        (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(entry, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
