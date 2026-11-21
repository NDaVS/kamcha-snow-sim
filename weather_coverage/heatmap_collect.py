"""Collect missing Forecast coordinates for the city atlas using the free endpoint."""
import csv
import fcntl
import math
import os
import sqlite3
import time

import requests

from .analysis import xy
from .experiment import HERE, coordinate_key, coordinates, iso, request_sample, sampling_points

OUT = HERE / "heatmaps"
PRIOR_ROUNDS = ("pilot-02", "dense-01")
RUN = "city-20261120"
LIMIT = 260


def point_plan():
    points = sampling_points(coordinates())
    chosen = [
        {**p, "atlas_kind": p["kind"]}
        for p in points if p["on_land"] and
        (p["kind"] in {"road", "anchor", "transect"} or p.get("size_m") == 500)
    ]
    coarse = [p for p in points if p.get("size_m") in (1000, 2000) and p["on_land"]]
    locations = {p["id"]: xy(p) for p in points}
    occupied = [locations[p["id"]] for p in chosen]

    def pick(candidates, n, label, threshold):
        for point in candidates:
            location = locations[point["id"]]
            if any(math.dist(location, other) < threshold for other in occupied):
                continue
            chosen.append({**point, "atlas_kind": label})
            occupied.append(location)
            n -= 1
            if not n:
                break

    south = [p for p in coarse if p["latitude"] < 53.013]
    pick(sorted(south, key=lambda p: (p["latitude"],p["id"])), 8, "south", 125)
    for a, b in [(53.0,53.045),(53.045,53.067),(53.067,54)]:
        candidates = [p for p in coarse if a <= p["latitude"] < b and p["elevation_m_dsm"] is not None]
        candidates.sort(key=lambda p: (p["elevation_m_dsm"],p["id"]))
        pick(reversed(candidates), 2, "relief_high", 135)
        pick(candidates, 2, "relief_low", 135)
    order = {"grid":0,"road":1,"anchor":2,"transect":3,"south":4,"relief_high":5,"relief_low":6}
    return sorted(chosen, key=lambda p:(order[p["atlas_kind"]], p["id"]))


def ledger():
    db = sqlite3.connect(OUT / "collection.sqlite")
    db.execute("""CREATE TABLE IF NOT EXISTS attempts(
        coord TEXT PRIMARY KEY, epoch REAL NOT NULL, status TEXT NOT NULL, filename TEXT)""")
    return db


def prior_responses():
    state = HERE / ".state"
    if not (state / "ledger.sqlite").exists():
        return []
    with sqlite3.connect(state/"ledger.sqlite") as db:
        rows = db.execute(
            "SELECT round,endpoint,coord,epoch,filename FROM attempts WHERE status='ok' AND round IN (?,?)",
            PRIOR_ROUNDS,
        ).fetchall()
    return [(round_id, ep, coord, epoch, state / file) for round_id, ep, coord, epoch, file in rows]


def retained_responses(db):
    records = {(r,e,c):(r,e,c,t,p) for r,e,c,t,p in prior_responses() if p.is_file()}
    for coord,epoch,file in db.execute("SELECT coord,epoch,filename FROM attempts WHERE status='ok'"):
        path=OUT/file
        if path.is_file():
            records[(RUN,"forecast",coord)]=(RUN,"forecast",coord,epoch,path)
    archived=OUT/"raw_index.csv"
    if archived.is_file():
        with archived.open(encoding="utf-8",newline="") as stream:
            for row in csv.DictReader(stream):
                path=OUT/row["raw_file"]
                key=row["round"],row["product"],row["coordinate"]
                if path.is_file():
                    records.setdefault(key,(*key,float(row["request_epoch"]),path))
    return list(records.values())


def write_attempts(db):
    rows = db.execute("SELECT coord,epoch,status,filename FROM attempts ORDER BY epoch").fetchall()
    with (OUT / "requests.csv").open("w",encoding="utf-8",newline="") as file:
        writer=csv.writer(file)
        writer.writerow(["round","product","latitude","longitude","request_time_utc","status","raw_file"])
        for coordinate, epoch, status, filename in rows:
            writer.writerow([RUN,"forecast",*coordinate.split(","),iso(epoch),status,filename])


def collect():
    key = os.getenv("OPENWEATHER_API_KEY","").strip()
    if not key:
        raise SystemExit("Установите OPENWEATHER_API_KEY.")
    OUT.mkdir(exist_ok=True)
    with (OUT / ".collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db=ledger()
        try:
            previous = {c for _,ep,c,_,_ in retained_responses(db) if ep=="forecast"}
            attempted = dict(db.execute("SELECT coord,status FROM attempts"))
            blocked = any(v in {"http_401","http_403","http_429"} for v in attempted.values())
            plan = list(dict.fromkeys(coordinate_key(p) for p in point_plan()))
            pending=[c for c in plan if c not in previous and c not in attempted]
            if blocked:
                raise SystemExit("Этот раунд остановлен после отказа доступа или ограничения квоты.")
            if len(attempted)+len(pending)>LIMIT:
                raise SystemExit(f"Запланированный сбор превышает {LIMIT} сетевых попыток.")
            print(f"План: {len(plan)} координат. Из кэша {len(set(plan)&previous)}, осталось {len(pending)} запросов.", flush=True)
            dest=OUT/"raw"/"forecast"/RUN
            dest.mkdir(parents=True,exist_ok=True)
            last=db.execute("SELECT MAX(epoch) FROM attempts").fetchone()[0] or 0
            with requests.Session() as session:
                for number, coord in enumerate(pending,1):
                    time.sleep(max(0,1.55-(time.time()-last)))
                    epoch=last=time.time()
                    with db:
                        db.execute("INSERT INTO attempts VALUES (?,?,?,NULL)",(coord,epoch,"reserved"))
                    status, content=request_sample(session,key,"forecast",coord)
                    filename=None
                    if content is not None:
                        path=dest/f"forecast-{coord.replace(',','_')}.json"
                        path.write_bytes(content)
                        filename=str(path.relative_to(OUT))
                    with db:
                        db.execute("UPDATE attempts SET status=?,filename=? WHERE coord=?",(status,filename,coord))
                    if number%20==0 or number==len(pending) or status!="ok":
                        print(f"{number}/{len(pending)} · {status}",flush=True)
                    if status in {"http_401","http_403","http_429"}:
                        break
        finally:
            write_attempts(db)
            db.close()


if __name__=="__main__":
    collect()
