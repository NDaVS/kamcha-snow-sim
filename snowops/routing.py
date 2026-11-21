"""Expand directed service transitions and preserve the incoming arc at every stop."""
from collections import defaultdict

import networkx as nx

from .contracts import ScenarioSnapshot
from .osm import RoadGraph


def duration_s(length_m: int, speed_kmh: int) -> int:
    return (36 * length_m + 10 * speed_kmh - 1) // (10 * speed_kmh)


def check_references(graph: RoadGraph, scenario: ScenarioSnapshot) -> None:
    if graph.version != scenario.city.graph_version:
        raise ValueError("graph_version does not match the snapshot")
    missing = {task.edge_id for task in scenario.tasks} | set(scenario.closed_edges)
    missing.difference_update(graph.edges)
    if missing:
        raise ValueError(f"unknown edges: {sorted(missing)}")
    for vehicle in scenario.fleet:
        if vehicle.depot_node not in graph.nodes:
            raise ValueError(f"unknown depot for {vehicle.vehicle_id}")
    tasks = {task.task_id: task for task in scenario.tasks}
    if len({site.site_id for site in scenario.priority_sites}) != len(scenario.priority_sites):
        raise ValueError("duplicate priority site IDs")
    for site in scenario.priority_sites:
        if not set(site.approach_edges) <= graph.edges.keys():
            raise ValueError(f"unknown approach edge for {site.site_id}")
        if len(set(site.approach_edges)) != len(site.approach_edges) or len(set(site.task_ids)) != len(site.task_ids):
            raise ValueError(f"duplicate approach edges or tasks for {site.site_id}")
        if any(key not in tasks or tasks[key].edge_id not in site.approach_edges
               or tasks[key].deadline_s > site.deadline_s for key in site.task_ids):
            raise ValueError(f"invalid task reference for {site.site_id}")
    for incoming, outgoing in scenario.forbidden_turns:
        if (incoming not in graph.edges or outgoing not in graph.edges
                or graph.edges[incoming].to_node != graph.edges[outgoing].from_node):
            raise ValueError(f"invalid turn override: {(incoming, outgoing)}")


class Routing:
    def __init__(self, graph: RoadGraph, scenario: ScenarioSnapshot):
        self.graph = graph
        self.use_limits = getattr(scenario, "respect_osm_speed_limits", False)
        self.network = nx.DiGraph()
        outgoing = defaultdict(list)
        forbidden = graph.forbidden_turns.union(scenario.forbidden_turns)
        closed = set(scenario.closed_edges)
        depots = {vehicle.depot_node for vehicle in scenario.fleet}
        for edge_id, edge in graph.edges.items():
            if edge_id not in closed:
                self.network.add_node(edge_id)
                outgoing[edge.from_node].append(edge_id)
        for depot in sorted(depots):
            self.network.add_nodes_from([depot, -depot])
            for edge_id in outgoing[depot]:
                self.network.add_edge(depot, edge_id, **self.arc_info(edge_id))
        for edge_id in tuple(self.network):
            if isinstance(edge_id, int):
                continue
            edge = graph.edges[edge_id]
            if edge.to_node in depots:
                self.network.add_edge(edge_id, -edge.to_node, metres=0, cap=None)
            for following in outgoing[edge.to_node]:
                if (edge_id, following) not in forbidden:
                    self.network.add_edge(edge_id, following, **self.arc_info(following))
        self.cache = {}

    def arc_info(self, edge_id):
        edge = self.graph.edges[edge_id]
        return {"metres": edge.length_m, "cap": edge.speed_limit_kmh if self.use_limits else None}

    def seconds(self, edge_id, speed):
        arc = self.arc_info(edge_id)
        return duration_s(arc["metres"], min(speed, arc["cap"] or speed))

    def _path(self, position: int | str, target: int | str, speed: int):
        key = (position, speed)
        if key not in self.cache:
            self.cache[key] = nx.single_source_dijkstra_path(
                self.network, position, weight=lambda u, v, data: duration_s(data["metres"], min(speed, data["cap"] or speed)))
        return self.cache[key].get(target)

    def to_service(self, position: int | str, edge_id: str, speed: int):
        if position == edge_id:
            # Revisiting the same oriented arc requires a legal nonempty cycle.
            candidates = []
            for following in self.network.successors(edge_id):
                suffix = self._path(following, edge_id, speed)
                if suffix:
                    metres = sum(self.graph.edges[arc].length_m for arc in suffix[:-1])
                    seconds = sum(self.seconds(arc, speed) for arc in suffix[:-1])
                    candidates.append((seconds, metres, tuple(suffix[:-1])))
            return min(candidates)[2] if candidates else None
        path = self._path(position, edge_id, speed)
        return tuple(path[1:-1]) if path else None

    def to_depot(self, position: int | str, depot: int, speed: int):
        if position == depot:
            return ()
        path = self._path(position, -depot, speed)
        return tuple(path[1:-1]) if path else None
