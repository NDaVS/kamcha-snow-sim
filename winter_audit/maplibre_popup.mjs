// Import into the existing MapLibre map after its style has loaded.
export function addContractScope(map, MapLibre, geojson) {
  const source = "pkc-contract-scope";
  const lineStates = ["unknown", "preliminary", "confirmed"].map(status => [status, source + "-" + status]);
  const point = source + "-point";
  const polygon = source + "-polygon";
  const layerIds = [polygon, ...lineStates.map(([, id]) => id), point];

  map.addSource(source, { type: "geojson", data: geojson });
  const status = ["get", "responsibility_status"];
  const palette = geojson.responsibility_states;
  const color = ["match", status, "confirmed", palette.confirmed.color,
    "preliminary", palette.preliminary.color, palette.unknown.color];
  map.addLayer({
    id: polygon, source, type: "fill", filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "fill-color": color, "fill-opacity": 0.16, "fill-outline-color": color }
  });
  const line = ["==", ["geometry-type"], "LineString"];
  for (const [state, id] of lineStates) {
    const paint = { "line-color": palette[state].color, "line-width": state === "unknown" ? 2 : 4 };
    if (state === "preliminary") paint["line-dasharray"] = [2, 2];
    map.addLayer({ id, source, type: "line", filter: ["all", line, ["==", status, state]], paint });
  }
  map.addLayer({
    id: point, source, type: "circle", filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-color": color, "circle-radius": 5, "circle-stroke-width": 1, "circle-stroke-color": "#fff" }
  });

  const popup = new MapLibre.Popup({ maxWidth: "420px" });
  const render = (event) => {
    const {x, y} = event.point;
    const hits = map.queryRenderedFeatures([[x - 4, y - 4], [x + 4, y + 4]], {layers: layerIds});
    if (!hits.length) return;
    const container = document.createElement("div");
    const seen = new Set();
    const rank = { confirmed: 2, preliminary: 1, unknown: 0 };
    const sorted = [...hits].sort((a, b) =>
      rank[b.properties.responsibility_status] - rank[a.properties.responsibility_status] ||
      Number(Boolean(b.properties.physical_road_id)) - Number(Boolean(a.properties.physical_road_id)));
    for (const {id, properties: props} of sorted) {
      const key = props.object_id ?? id;
      if (seen.has(key)) continue;
      seen.add(key);
      const details = document.createElement("details");
      details.open = seen.size === 1;
      const title = document.createElement("summary");
      title.textContent = props.name + " · " + props.responsibility_status_label;
      details.append(title);
      const rows = document.createElement("dl");
      for (const {field, label} of geojson.popup_fields) {
        if (field === "name" || (field.includes("0902") && props[field] == null)) continue;
        const term = document.createElement("dt");
        term.textContent = label;
        const desc = document.createElement("dd");
        const value = props[field];
        if (field.endsWith("_url") && typeof value === "string" && value.startsWith("https://")) {
          const link = document.createElement("a");
          link.href = value;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = "Открыть источник";
          desc.append(link);
        } else {
          desc.textContent = value == null || value === "" ? "Не установлено" : String(value);
        }
        rows.append(term, desc);
      }
      details.append(rows);
      container.append(details);
    }
    popup.setLngLat(event.lngLat).setDOMContent(container).addTo(map);
  };
  map.on("click", render);
  return () => {
    map.off("click", render);
    popup.remove();
    for (const id of layerIds) if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(source)) map.removeSource(source);
  };
}
