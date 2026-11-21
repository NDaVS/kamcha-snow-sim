"""Rank infrastructure and nominate directional approaches on the road graph."""
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import json

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

from .contracts import CityProfile, PrioritySite, ScenarioSnapshot, ServiceTask, Tariff, Vehicle
from .osm import length_m
from .pilot import AS_OF
from .routing import Routing

DATA = Path(__file__).resolve().parent.parent / "data/pkc"
CENTRE = (158.645, 53.032)
DEPOTS = (CENTRE, (158.636, 53.057), (158.616, 53.074), (158.622, 53.095))
ARTERIAL = {"primary", "secondary", "primary_link", "secondary_link"}
PROFILES = (
    ("truck-plow", 30, 16, ("plow",)),
    ("compact-plow", 24, 12, ("plow", "compact_plow")),
    ("grader", 23, 10, ("plow", "grader")),
)


def metric(point):
    return point[0] * 66800, point[1] * 111200


class Infrastructure:
    def __init__(self, graph, policy_path=DATA / "infrastructure-policy.json"):
        self.graph = graph
        self.policy = json.loads(policy_path.read_text(encoding="utf-8"))
        network = Routing(graph, SimpleNamespace(fleet=(), closed_edges=(), forbidden_turns=())).network
        component = max(nx.strongly_connected_components(network), key=lambda group: (len(group), min(group)))
        self.network = network.subgraph(component)
        self.edges = {edge_id: graph.edges[edge_id] for edge_id in sorted(component)}
        self.incoming, branches = defaultdict(list), defaultdict(set)
        for edge in self.edges.values():
            self.incoming[edge.to_node].append(edge.edge_id)
            branches[edge.from_node].add(edge.to_node)
            branches[edge.to_node].add(edge.from_node)
        self.nodes = sorted(self.incoming)
        self.tree = cKDTree([metric(graph.nodes[node]) for node in self.nodes])
        self.junctions = {
            node for node, neighbors in branches.items() if len(neighbors) >= 3
            and any(self.edges[e].road_class in ARTERIAL for e in self.incoming[node])
        }
        self.sites = self._sites()

    def nearest(self, point):
        _, index = self.tree.query(metric(point))
        node = self.nodes[index]
        return node, length_m(point, self.graph.nodes[node])

    def deadline(self, rank):
        return self.policy["rank_deadline_s"][str(rank)]

    def _approach(self, node):
        # Search backward from an incoming arc, keeping directional turn constraints.
        reverse = self.network.reverse(copy=False)
        source = tuple(self.incoming[node])
        distances, paths = nx.multi_source_dijkstra(
            reverse, source, cutoff=500, weight="metres")
        options = [(distance, arc) for arc, distance in distances.items()
                   if self.edges[arc].from_node in self.junctions]
        if options:
            _, best = min(options)
            return tuple(reversed(paths[best]))
        return ()

    def _sites(self):
        unique = []
        for facility in sorted(self.graph.facilities,
                               key=lambda item: (item.kind, item.name, item.site_id)):
            if facility.name and any(other.name == facility.name and other.kind == facility.kind
                                     and length_m(other.lon_lat, facility.lon_lat) < 180 for other in unique):
                continue
            unique.append(facility)
        records = [{**asdict(site), "source": "osm", "location_source": "osm"} for site in unique]
        records.extend(self.policy.get("manual_sites", []))
        records.extend({"site_id": f"osm:junction:{node}", "kind": "arterial_junction",
                        "name": f"Перекрёсток {node}", "lon_lat": self.graph.nodes[node],
                        "source": "osm", "location_source": "osm"} for node in sorted(self.junctions))
        sites = []
        for raw in records:
            rank = raw.get("rank", self.policy["site_rank"].get(raw["kind"]))
            if rank is None:
                continue
            point = raw["lon_lat"]
            node, distance = self.nearest(point) if point is not None else (None, None)
            if node is None or distance > 180:
                approaches = ()
            elif raw["kind"] == "arterial_junction":
                approaches = tuple(edge for edge in self.incoming[node]
                                   if self.edges[edge].road_class in ARTERIAL)
            else:
                approaches = self._approach(node)
            sites.append(PrioritySite(**{k: v for k, v in raw.items() if k != "rank"},
                                      rank=rank, deadline_s=self.deadline(rank),
                                      access_node=node if approaches else None, snap_m=distance,
                                      approach_edges=approaches))
        return tuple(sites)

    def scenario(self, count, *, seed=42, fleet_size=16, shift_s=14400, passes=1,
                 price=120, top_deadline=None, scenario_id=None, promote_kinds=()):
        ranked_sites = tuple(site.model_copy(update={"rank": 1, "deadline_s": self.deadline(1)})
                             if site.kind in promote_kinds else site for site in self.sites)
        protected = {}
        for site in ranked_sites:
            for edge in site.approach_edges:
                protected[edge] = min(protected.get(edge, site.rank), site.rank)
        edges = list(self.edges.values())
        if not 1 <= count <= len(edges):
            raise ValueError(f"task count must be in [1, {len(edges)}]")
        rng = np.random.default_rng(seed)
        tie = {edge.edge_id: number for edge, number in zip(edges, rng.permutation(len(edges)))}
        edges.sort(key=lambda edge: (edge.edge_id not in protected, protected.get(edge.edge_id, 5),
                                     tie[edge.edge_id]))
        selected = edges[:count]
        tasks = []
        for edge in selected:
            road_rank = self.policy["road_rank"][edge.road_class]
            rank = min(road_rank, protected.get(edge.edge_id, road_rank))
            capability = ("compact_plow" if edge.road_class == "living_street" else
                          "grader" if edge.road_class == "residential" and tie[edge.edge_id] % 7 == 0 else "plow")
            tasks.append(ServiceTask(task_id=f"svc:{edge.edge_id}", edge_id=edge.edge_id,
                                     deadline_s=top_deadline if rank == 1 and top_deadline else self.deadline(rank),
                                     priority=rank - 1, required_passes=passes if rank == 1 else 1,
                                     capability=capability))
        selected_ids = {task.edge_id: task.task_id for task in tasks}
        sites = tuple(site.model_copy(update={
            "task_ids": tuple(selected_ids[e] for e in site.approach_edges if e in selected_ids),
            "deadline_s": top_deadline if site.rank == 1 and top_deadline else site.deadline_s,
        }) for site in ranked_sites)
        fleet = []
        for i in range(fleet_size):
            profile, deadhead, service, capabilities = PROFILES[i % len(PROFILES)]
            depot, _ = self.nearest(DEPOTS[i % len(DEPOTS)])
            fleet.append(Vehicle(vehicle_id=f"unit-{i + 1:02}", depot_node=depot, shift_end_s=shift_s,
                                 profile_id=profile, deadhead_kmh=deadhead, service_kmh=service,
                                 capabilities=capabilities))
        return ScenarioSnapshot(
            scenario_id=scenario_id or f"pkc-scale-{count}", as_of=AS_OF, seed=seed,
            city=CityProfile(city_id="petropavlovsk_kamchatsky", timezone="Asia/Kamchatka",
                             graph_version=self.graph.version),
            constraints_version=self.policy["version"], tasks=tuple(tasks), fleet=tuple(fleet),
            tariff=Tariff(version=f"pkc-scale-deadhead-{price}", deadhead_rub_km=price,
                          service_rub_km=170, elapsed_mrub_s=180),
            priority_sites=sites, respect_osm_speed_limits=True,
        )
