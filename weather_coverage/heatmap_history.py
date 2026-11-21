"""Cache Open-Meteo hourly historical forecasts for the atlas's prior two local days."""
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta
import json
import time

import requests

from .experiment import iso, numeric
from .heatmap_collect import OUT
from .heatmap_data import LOCAL

HOST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
DIRECTORY = OUT / "history"
MAPPING = {
    "temp": ("temperature_2m", "°C"),
    "feels_like": ("apparent_temperature", "°C"),
    "wind_speed": ("wind_speed_10m", "m/s"),
    "wind_gust": ("wind_gusts_10m", "m/s"),
    "wind_deg": ("wind_direction_10m", "°"),
    "snow": ("snowfall", "cm"),
    "rain": ("rain", "mm"),
    "visibility": ("visibility", "m"),
    "humidity": ("relative_humidity_2m", "%"),
    "clouds": ("cloud_cover", "%"),
    "pop": ("precipitation_probability", "%"),
    "precip": ("precipitation", "mm"),
}
COLUMNS = {field: name for field,name in zip(MAPPING, (
    "temp_c", "feels_like_c", "wind_ms", "gust_ms", "wind_deg", "snowfall_cm_1h",
    "rain_mm_1h", "visibility_m", "humidity_pct", "cloud_pct", "pop_pct", "precip_mm_1h"
))}
BATCH_SIZE = 20


def read_csv(path):
    with path.open(encoding="utf-8",newline="") as stream:
        return list(csv.DictReader(stream))


def query(batch, raw, expected):
    cache, batch_plan = batch
    download = raw/cache
    locations=batch_plan
    dates=expected[0][:10],expected[-1][:10]
    params = dict(
        latitude=",".join(f'{float(p["lat"]):.6f}' for p in locations),
        longitude=",".join(f'{float(p["lon"]):.6f}' for p in locations),
        hourly=",".join(name for name,_ in MAPPING.values()),
        start_date=dates[0], end_date=dates[1], timezone="Asia/Kamchatka", wind_speed_unit="ms",
    )
    if download.is_file():
        body=download.read_bytes()
    else:
        response=requests.get(HOST,params=params,timeout=(8,42))
        response.raise_for_status()
        body=response.content
    payload=json.loads(body)
    if len(locations)==1 and isinstance(payload,dict):
        payload=[payload]
    if not isinstance(payload,list) or len(payload)!=len(locations):
        raise ValueError(f"{cache}: unexpected number of locations")
    for i,item in enumerate(payload):
        point=locations[i]
        if item.get("location_id",i)!=i or (
            abs(item["latitude"]-float(point["lat"]))>0.25 or
            abs(item["longitude"]-float(point["lon"]))>0.35
        ):
            raise ValueError(f"{cache}: unexpected coordinate order at location {i}")
        units=item["hourly_units"]
        if any(units[name]!=unit for name,unit in MAPPING.values()):
            raise ValueError(f"{cache}: unexpected units")
        time_axis=item["hourly"]["time"]
        if time_axis[:len(expected)]!=expected:
            raise ValueError(f"{cache}: incomplete requested hours")
    if not download.exists():
        temp=download.with_suffix(".tmp")
        temp.write_bytes(body)
        temp.replace(download)
    return payload


def collect():
    points=read_csv(OUT/"point_metadata.csv")
    with (OUT/"forecast_frames.csv").open(encoding="utf-8",newline="") as stream:
        first=datetime.fromisoformat(next(csv.DictReader(stream))["local_time_kamchatka"])
    first=first.astimezone(LOCAL)
    start=(first-timedelta(days=2)).replace(hour=0,minute=0,second=0,microsecond=0)
    hours=int((first-start).total_seconds()/3600)
    times=[start+timedelta(hours=h) for h in range(hours)]
    expected=[date.strftime("%Y-%m-%dT%H:%M") for date in times]
    folder=DIRECTORY/"raw"/start.date().isoformat()
    folder.mkdir(parents=True,exist_ok=True)
    batches=[]
    for offset in range(0,len(points),BATCH_SIZE):
        chunk=points[offset:offset+BATCH_SIZE]
        batches.append((f"batch-{offset//BATCH_SIZE+1:02d}.json",chunk))
    cache_plan=folder/"points_by_batch.json"
    plan={batch:[p["point_id"] for p in chunk] for batch,chunk in batches}
    if cache_plan.exists() and json.loads(cache_plan.read_text())!=plan:
        raise ValueError("Cached coordinates differ from the current atlas plan")
    cache_plan.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    started=iso(time.time())
    rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=pool.map(lambda b:query(b,folder,expected),batches)
        for (filename,chunk), result in zip(batches,results):
            for point, reply in zip(chunk,result):
                for t,date in enumerate(times):
                    hourly=reply["hourly"]
                    rows.append({
                        "point_id":point["point_id"],"latitude":point["lat"],"longitude":point["lon"],
                        "source":"Open-Meteo Historical Forecast", "time_utc":iso(date.timestamp()),
                        "local_time_kamchatka":date.isoformat(),
                        "api_latitude":reply["latitude"],"api_longitude":reply["longitude"],
                        "api_elevation_m":reply.get("elevation"),
                        "raw_file":str((folder/filename).relative_to(OUT)),
                        **{COLUMNS[field]:numeric(hourly[name][t]) for field,(name,_) in MAPPING.items()}
                    })
            print(f"{filename}: {len(result)} locations",flush=True)
    rows.sort(key=lambda r:(r["time_utc"],r["point_id"]))
    output=DIRECTORY/"historical_frames.csv"
    temp=output.with_suffix(".tmp")
    with temp.open("w",encoding="utf-8",newline="") as stream:
        writer=csv.DictWriter(stream,list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(output)
    (DIRECTORY/"manifest.json").write_text(json.dumps({
        "source":"Open-Meteo Historical Forecast",
        "license":"CC BY 4.0",
        "request_profile":"best_match (API default)",
        "processed_utc":started,
        "sites":len(points),"hours":len(times),"batches":len(batches),"rows":len(rows),
        "returned_coordinate_pairs":len({(r["api_latitude"],r["api_longitude"]) for r in rows}),
        "start_local":times[0].isoformat(),"end_local":times[-1].isoformat(),
        "units":{COLUMNS[field]:unit for field,(_,unit) in MAPPING.items()},
    },ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"{len(points)} sites · {len(times)} hours · {len(rows)} rows",flush=True)


if __name__=="__main__":
    collect()
