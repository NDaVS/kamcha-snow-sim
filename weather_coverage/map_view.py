"""Interactive map for the sampling frame and one weather slice at a time."""
import html

import folium
from branca.element import MacroElement, Template

from .analysis import difference, grouped, nearest_pairs, pair_data, response_groups
from .experiment import FIELDS, HERE, coordinates, read_csv


class WeatherControls(MacroElement):
    _template = Template("""
{% macro html(this, kwargs) %}
<style>
.leaflet-container {background:#f4f2ec}
#weather-panel {position:absolute;z-index:1000;top:12px;left:53px;width:min(365px,calc(100vw - 115px));
 background:#fff;color:#172633;border:1px solid #cad4dc;border-radius:10px;box-shadow:0 3px 15px #1425302b;
 padding:13px 15px;font:14px/1.45 system-ui,sans-serif}
#weather-panel strong {font-size:16px;display:block}
#weather-panel select {width:100%;margin:3px 0 5px;background:white;color:#172633}
#weather-panel small {display:block;color:#42515d}
#weather-panel .demo-mark {color:#913710;font-weight:700}
#weather-panel label {display:inline-block;margin-right:8px}
#weather-scale {height:8px;background:linear-gradient(90deg,#2155bd,#f6d65e,#bf341d);
 border-radius:5px;margin:6px 0 2px}
</style>
<section id="weather-panel" aria-label="Параметры погоды">
<strong>Погода внутри Петропавловска-Камчатского</strong>
<small class="{{ 'demo-mark' if this.demo else '' }}">{{ this.status }}</small>
{% if this.slices %}
<label for="weather-slice">Продукт и время действия (UTC)</label>
<select id="weather-slice"></select>
<label for="weather-field">Параметр</label>
<select id="weather-field"></select>
<label><input type="checkbox" id="weather-links" checked> Ближайшие пары</label>
<label><input type="checkbox" id="weather-equal"> Группы профилей</label>
<div id="weather-scale"></div>
<small id="weather-legend"></small>
{% endif %}
</section>
{% endmacro %}
{% macro script(this, kwargs) %}
{% if this.slices %}
{
 const map = {{ this._parent.get_name() }};
 const slices = {{ this.slices | tojson }};
 const fields = {{ this.fields | tojson }};
 const sliceBox = document.querySelector('#weather-slice');
 const fieldBox = document.querySelector('#weather-field');
 const legend = document.querySelector('#weather-legend');
 const links = document.querySelector('#weather-links');
 const equal = document.querySelector('#weather-equal');
 const overlay = L.layerGroup().addTo(map);
 L.DomEvent.disableClickPropagation(document.querySelector('#weather-panel'));
 L.DomEvent.disableScrollPropagation(document.querySelector('#weather-panel'));
 slices.forEach((item,i) => sliceBox.append(new Option(item.label,i)));
 Object.entries(fields).forEach(([key,title]) => fieldBox.append(new Option(title,key)));
 fieldBox.value = 'temp_c';
 function color(t) {
   const from = t <= 0.5 ? [33,85,189] : [246,214,94];
   const to = t <= 0.5 ? [246,214,94] : [191,52,29];
   const share = t <= 0.5 ? t*2 : (t-0.5)*2;
   return 'rgb(' + from.map((x,i) => Math.round(x+(to[i]-x)*share)).join(',') + ')';
 }
 const fmt = new Intl.NumberFormat('ru',{maximumFractionDigits:3});
 function labelNumber(value) { return value === null ? 'нет поля' : fmt.format(value); }
 function redraw() {
   overlay.clearLayers();
   const slice = slices[Number(sliceBox.value)];
   const key = fieldBox.value;
   const samples = Object.values(slice.points).map(p => p.values[key]).filter(v => v !== null);
   const min = Math.min(...samples), max = Math.max(...samples);
   const colors = ['#151e43','#6d1b80','#046257','#b53b5d','#683817','#005a94','#425532'];
   if (links.checked) {
     (slice.nearest[key] || []).forEach(p => {
       L.polyline([slice.points[p.a].latlon,slice.points[p.b].latlon],
         {color:p.difference ? '#9c2c28' : '#586c7b', weight:p.difference? 2.4:1.2, opacity:0.68})
         .bindTooltip(p.a+' ↔ '+p.b+' · '+fmt.format(p.distance_m)+' м · Δ '+fmt.format(p.difference))
         .addTo(overlay);
     });
   }
   Object.entries(slice.points).forEach(([id,p]) => {
     const v = p.values[key];
     const index = slice.profiles.findIndex(group => group.includes(id));
     const selected = equal.checked && index !== -1;
     const fill = v === null ? '#acb5bc' : color(max === min ? .5 : (v-min)/(max-min));
     const lines = Object.entries(fields).map(([f,title]) => title+' · '+labelNumber(p.values[f]));
     L.circleMarker(p.latlon,{radius:p.land ? 6:8,stroke:true,color:selected?colors[index%colors.length]:'#252f37',
        weight:selected?4:1.2,fillColor:fill,fillOpacity:.93})
       .bindPopup([id,p.district||'',p.land?'':'Контроль акватории',slice.time,
         selected?'Совпадающий профиль '+(index+1):'', ...lines].filter(Boolean).join('<br>'))
       .bindTooltip(id+' · '+labelNumber(v)).addTo(overlay);
   });
   legend.textContent = samples.length ? labelNumber(min)+' — '+labelNumber(max)+' · '
     +new Set(samples).size+' значений · '+samples.length+' из '+Object.keys(slice.points).length+
       ' точек, включая контроль при наличии' : 'В выбранном срезе поле отсутствует';
 }
 [sliceBox,fieldBox,links,equal].forEach(el => el.addEventListener('change',redraw));
 redraw();
}
{% endif %}
{% endmacro %}
""")

    def __init__(self, slices, source):
        super().__init__()
        self.slices = slices
        self.fields = FIELDS
        self.demo = source == "demo"
        self.status = ("Демонстрационные данные · синтетическая погода" if self.demo else
                       "Фактические ответы OpenWeather" if slices else
                       "Фактических ответов OpenWeather пока нет")


def render(source):
    doc = coordinates()
    fmap = folium.Map(location=(53.022, 158.647), zoom_start=12, control_scale=True,
                      tiles="OpenStreetMap")
    for kind, title, color, opacity, shown in (
        ("water", "Водоёмы OSM", "#8cb8c9", 0.32, True),
        ("city", "Городская граница OSM (условная)", "#173f76", 0.01, True),
        ("footprint", "Исследуемая территория", "#14867d", 0.12, True),
        ("arterials", "Основные дороги OSM", "#9f8960", 0, True),
        ("district", "Районы и картографические ориентиры", "#533591", 0.12, True),
    ):
        subset = [f for f in doc["features"] if f["properties"]["kind"] == kind]
        folium.GeoJson({"type": "FeatureCollection", "features": subset}, name=title, show=shown,
                       style_function=lambda _, c=color, o=opacity: dict(color=c, weight=1.5, fillOpacity=o),
                       marker=folium.CircleMarker(radius=3, color=color, fill=True),
                       tooltip=folium.GeoJsonTooltip(fields=["name"], aliases=["Объект"])).add_to(fmap)
    for size, color in ((500, "#4d6070"), (1000, "#356377"), (2000, "#6b5771")):
        layer = folium.FeatureGroup(name=f"Сетка {size} м", show=False).add_to(fmap)
        for feature in doc["features"]:
            p = feature["properties"]
            if p.get("size_m") != size:
                continue
            if p["kind"] == "cell":
                folium.GeoJson(feature, control=False, tooltip=p["id"],
                               style_function=lambda _, c=color: dict(color=c, weight=1, fillOpacity=0.02)
                               ).add_to(layer)
            else:
                folium.CircleMarker((p["latitude"], p["longitude"]), radius=2, color=color,
                                    tooltip=p["id"]).add_to(layer)
    for flag, title, color, show in (
        (True, "Точки пилота (план)", "#22394a", True),
        (False, "Дополнительные точки на магистралях", "#687c87", False),
    ):
        layer = folium.FeatureGroup(name=title, show=show).add_to(fmap)
        for f in doc["features"]:
            p = f["properties"]
            if ("latitude" not in p or (not flag and p["kind"] != "road")
                    or bool(p["pilot"]) != flag):
                continue
            link = ""
            if p.get("osm"):
                element = {"n": "node", "w": "way", "r": "relation"}[p["osm"][0]]
                link = f'<br><a href="https://www.openstreetmap.org/{element}/{p["osm"][1:]}">Объект OSM</a>'
            caption = " · ".join(html.escape(str(p[k])) for k in ("id", "district", "road", "terrain")
                                 if p.get(k))
            height = p.get("elevation_m_dsm")
            if height is not None:
                caption += f"<br>Copernicus DSM: {height} м"
            folium.CircleMarker((p["latitude"], p["longitude"]), radius=4, color=color, fill=True,
                                popup=caption+link, tooltip=html.escape(p["id"])).add_to(layer)
    rows = read_csv(HERE / ("observations_demo.csv" if source == "demo" else "observations.csv"))
    slices = []
    from .analysis import value
    for key, sample in grouped(rows).items():
        _, endpoint, round_id, stage, _ = key
        land = [r for r in sample if r["on_land"] == "True"]
        points = {r["point_id"]: dict(latlon=[float(r["latitude"]), float(r["longitude"])],
                  district=html.escape(r["district"]), land=r["on_land"] == "True",
                  values={field: value(r, field) for field in FIELDS}) for r in sample}
        nearest = {field: nearest_pairs(pair_data(land, field)[1]) for field in FIELDS}
        # Same parameter vector and same missing-field mask.
        profiles = response_groups(land, FIELDS)
        dt = sample[0]["valid_utc"].replace("T", " ")[:19]
        slices.append(dict(label=f"{endpoint} · {dt} · {round_id} ({len(points)})", time=dt+" UTC",
                           size=len(points), stage=stage, endpoint=endpoint, points=points, nearest=nearest,
                           profiles=profiles))
    slices.sort(key=lambda s: (s["endpoint"] != "forecast", s["stage"] != "pilot",
                               -s["size"], s["time"], s["label"]))
    folium.LayerControl(collapsed=True).add_to(fmap)
    WeatherControls(slices, source).add_to(fmap)
    footprint = next(f for f in doc["features"] if f["properties"]["kind"] == "footprint")
    fmap.fit_bounds(folium.GeoJson(footprint).get_bounds(), padding=(24, 24))
    fmap.save(str(HERE / "weather_coverage.html"))
