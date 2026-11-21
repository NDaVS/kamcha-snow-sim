"""Synthetic work orders and fleet on an archived Petropavlovsk street network."""
from datetime import datetime, timezone
from random import Random
from types import SimpleNamespace

import networkx as nx

from .contracts import CityProfile, ScenarioSnapshot, ServiceTask, Tariff, Vehicle
from .routing import Routing

CORRIDORS = ("Ленинградская улица", "Ключевская улица")
LOCAL_STREETS = ("улица Осипенко", "улица Фрунзе")
DEPOT_HINTS = ((158.640, 53.039), (158.644, 53.033), (158.647, 53.029))
AS_OF = datetime(2026, 11, 20, 0, tzinfo=timezone.utc)


def scenario_factory(graph, seed=42, fleet_size=3, shift_s=1500,
                     deadhead_rate=120, critical_deadline_s=900, scenario_id="base_snowfall"):
    road_network = Routing(graph, SimpleNamespace(fleet=(), forbidden_turns=(), closed_edges=())).network
    components = sorted(nx.strongly_connected_components(road_network),
                        key=lambda group: (-len(group), min(group)))
    component = components[0]
    connected = [graph.edges[edge_id] for edge_id in sorted(component)]
    candidates = sorted({edge.from_node for edge in connected})
    depots = []
    for lon, lat in DEPOT_HINTS:
        node = min(candidates, key=lambda n: ((graph.nodes[n][0] - lon)**2 * 0.36
                                             + (graph.nodes[n][1] - lat)**2, n))
        depots.append(node)
    rng = Random(seed)
    tasks = []
    for street, count in zip(CORRIDORS + LOCAL_STREETS, (7, 7, 5, 5)):
        eligible = [edge for edge in connected if edge.name == street and edge.length_m >= 45]
        if len(eligible) < count:
            raise ValueError(f"insufficient connected service arcs for {street}")
        for index, edge in enumerate(rng.sample(eligible, count)):
            priority = 0 if street in CORRIDORS else 1
            tasks.append(ServiceTask(task_id=f"plow-{len(tasks) + 1:02d}", edge_id=edge.edge_id,
                                     priority=priority, required_passes=2 if index == 0 else 1,
                                     deadline_s=critical_deadline_s if priority == 0 else 1800))
    return ScenarioSnapshot(
        scenario_id=scenario_id, city=CityProfile(city_id="petropavlovsk_kamchatsky",
                                                   timezone="Asia/Kamchatka",
                                                   graph_version=graph.version),
        as_of=AS_OF, constraints_version="pkc-pilot-1", seed=seed, tasks=tuple(tasks),
        fleet=tuple(Vehicle(vehicle_id=f"plow-{i + 1}", depot_node=depots[i % len(depots)],
                            shift_end_s=shift_s) for i in range(fleet_size)),
        tariff=Tariff(version=f"pilot-mrub-1-deadhead-{deadhead_rate}", deadhead_rub_km=deadhead_rate,
                      service_rub_km=170, elapsed_mrub_s=180),
    )
