"""Build the self-contained weather atlas, PNG frames, and H.264 videos."""
import argparse
from collections import Counter
from datetime import datetime
import json

import numpy as np
from scipy.stats import spearmanr

from .experiment import HERE
from .heatmap_collect import OUT
from .heatmap_data import FIELDS, loaded, shown_value
from .heatmap_maps import geometry_model, interpolate, packed
from .heatmap_history import COLUMNS, DIRECTORY as HISTORY_DIR, read_csv
from .heatmap_render import color_scale, render_all


def prepare():
    plan, times, frames, stats = loaded()
    fields = list(FIELDS)
    point_ids = {p["id"]: index for index,p in enumerate(plan)}
    cube = np.full((len(fields), len(times), len(plan)), np.nan, dtype=np.float32)
    for r in frames:
        for f, name in enumerate(fields):
            value = shown_value(r,name)
            if value is not None:
                cube[f,r["frame_index"],point_ids[r["point_id"]]] = value

    model = geometry_model(plan)
    all_scales = {}
    for index,(key,spec) in enumerate(FIELDS.items()):
        values = cube[index]
        valid = np.isfinite(values)
        all_scales[key] = color_scale(values,spec,valid)
        counts = [len(set(row[np.isfinite(row)].tolist())) for row in values]
        first = values[0][valid[0]]
        stats.setdefault("parameters",{})[key] = {
            "native_available":int(valid.sum()), "times_available":int(sum(c>0 for c in counts)),
            "distinct_min":min(counts), "distinct_max":max(counts),
            "first_count":int(valid[0].sum()),"first_distinct":counts[0],
            "first_min":float(first.min()) if first.size else None,
            "first_max":float(first.max()) if first.size else None,
            "first_groups":Counter(np.round(first.astype(float), 4).tolist()).most_common(4),
            "coverage_first_model_pixels":int(np.isfinite(interpolate(model,values[0],circular=key=="wind_deg")).sum()),
            "range":all_scales[key],
        }

    heights = np.array([p["elevation_m_dsm"] if p["elevation_m_dsm"] is not None else np.nan for p in plan])
    temperature = cube[fields.index("temp")]
    valid = np.isfinite(heights)&np.isfinite(temperature[0])
    if valid.sum() >= 3:
        stats["temp_dsm_first"] = {"n":int(valid.sum()),"spearman":float(spearmanr(heights[valid],temperature[0][valid]).statistic)}
    stats["native_first_groups"] = {
        key: [{"value":float(value),"points":int(count),
               "south_of_53":int(sum(p["latitude"]<53 and np.isclose(cube[fields.index(key),0,i],value,atol=1e-6)
                                     for i,p in enumerate(plan)))}
              for value,count in stats["parameters"][key]["first_groups"]]
        for key in ("snow","visibility","wind_speed","pop")
    }
    return model, times, cube, all_scales, stats


def load_history(model, first_forecast, extents):
    path=HISTORY_DIR/"historical_frames.csv"
    if not path.is_file():
        return None
    points={p["id"]:index for index,p in enumerate(model["points"])}
    rows=read_csv(path)
    dates={value:int(datetime.fromisoformat(value).timestamp()) for value in
           {r["time_utc"] for r in rows}}
    times=sorted(t for t in dates.values() if t<first_forecast)
    if not times:
        return None
    time_index={t:i for i,t in enumerate(times)}
    cube=np.full((len(FIELDS),len(times),len(points)),np.nan,dtype=np.float32)
    for row in rows:
        stamp=dates[row["time_utc"]]
        if stamp not in time_index or row["point_id"] not in points:
            continue
        for f,field in enumerate(FIELDS):
            raw=row[COLUMNS[field]]
            if raw:
                value=float(raw)
                cube[f,time_index[stamp],points[row["point_id"]]]=value/1000 if field=="visibility" else value
    fields={key:(name,unit,extents[key],colors) for key,(name,unit,_,colors) in FIELDS.items()}
    fields["snow"]=("Снегопад за час","см / ч",(0,4),FIELDS["snow"][3])
    fields["rain"]=("Дождь за час","мм / ч",(0,4),FIELDS["rain"][3])
    fields["precip"]=("Интенсивность осадков","мм/ч",extents["precip"],FIELDS["precip"][3])
    ranges={field:color_scale(cube[f],spec,np.isfinite(cube[f]))
            for f,(field,spec) in enumerate(fields.items())}
    # Report exact past visibility > 10 km in point details, with a saturated scale
    # shared with the OpenWeather display for useful contrast in low visibility.
    ranges["visibility"]=extents["visibility"]
    return times,cube,fields,ranges


def write_html(model, times, cube, extents, stats, history):
    payload = {key:val for key,val in model.items() if key not in {"pixels","indices","dist"}}
    payload.update(times=times, received=stats["received"],
                   fields=[dict(id=field,title=spec[0],unit=spec[1],range=extents[field],colors=spec[3])
                           for field,spec in FIELDS.items()],
                   pixels=packed(model["pixels"],"<u4"),
                   indices=packed(model["indices"],"<i2"),
                   dist=packed(np.rint(np.where(np.isfinite(model["dist"]),model["dist"],6553.5)*10),"<u2"),
                   values=packed(cube,"<f4"))
    if history:
        hist_times,hist_cube,hist_fields,hist_extents=history
        payload["history"]={
            "times":hist_times,"values":packed(hist_cube,"<f4"),
            "fields":[dict(id=field,title=spec[0],unit=spec[1],range=hist_extents[field],colors=spec[3])
                      for field,spec in hist_fields.items()],
        }
    data=json.dumps(payload,ensure_ascii=False,separators=(",",":")).replace("<","\\u003c")
    page=(HERE/"heatmap_page.html").read_text(encoding="utf-8")
    (OUT/"weather_coverage_full.html").write_text(page.replace("__ATLAS_DATA__",data),encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-media",action="store_true",help="Пересобрать карту и данные без PNG и MP4")
    parser.add_argument("--history-media",action="store_true",help="Собрать также PNG и MP4 исторического слоя")
    args=parser.parse_args()
    model,times,cube,extents,stats=prepare()
    history=load_history(model,times[0],extents)
    write_html(model,times,cube,extents,stats,history)
    (OUT/"coverage.json").write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f'Карта и данные: {stats["received"]}/{stats["planned"]} точек · {len(times)} сроков',flush=True)
    if not args.skip_media:
        render_all(model,cube,extents,times)
    if args.history_media and history:
        hist_times,hist_values,hist_fields,hist_extents=history
        render_all(model,hist_values,hist_extents,hist_times,fields=hist_fields,
                   directory=HISTORY_DIR,series="history",source="Open-Meteo Historical Forecast")


if __name__=="__main__":
    main()
