"""Bounded-memory directed shortest paths and a large-instance greedy constructor."""
from functools import lru_cache
from time import monotonic

import numpy as np
from scipy.sparse import csr_array
from scipy.sparse.csgraph import dijkstra

from .contracts import VehicleRoute
from .routing import Routing


class SparseRouting(Routing):
    def __init__(self, graph, scenario):
        super().__init__(graph, scenario)
        self.states = list(self.network)
        self.index = {state: i for i, state in enumerate(self.states)}
        self._arcs = [(self.index[u], self.index[v], info["metres"], info["cap"] or 0)
                      for u, v, info in self.network.edges(data=True)]
        self.matrix = lru_cache(maxsize=16)(self._matrix)
        self.transpose = lru_cache(maxsize=16)(lambda speed: self.matrix(speed).T.tocsr())
        self.forward = lru_cache(maxsize=96)(self._forward)
        self.reverse = lru_cache(maxsize=48)(self._reverse)
        self.cycle = lru_cache(maxsize=None)(self._cycle)

    def _matrix(self, speed):
        if not self._arcs:
            return csr_array((len(self), len(self)), dtype=float)
        rows, columns, metres, caps = map(np.asarray, zip(*self._arcs))
        speeds = np.minimum(speed, np.where(caps > 0, caps, speed))
        seconds = (36 * metres + 10 * speeds - 1) // (10 * speeds)
        return csr_array((seconds.astype(float), (rows, columns)), shape=(len(self), len(self)))

    def __len__(self):
        return len(self.states)

    def _forward(self, state, speed):
        return dijkstra(self.matrix(speed), indices=self.index[state], return_predecessors=True)

    def _reverse(self, state, speed):
        return dijkstra(self.transpose(speed), indices=self.index[state])

    def _cycle(self, state, speed):
        matrix = self.matrix(speed)
        home = self.reverse(state, speed)
        here = self.index[state]
        start, stop = matrix.indptr[here:here + 2]
        peers = matrix.indices[start:stop]
        if not len(peers):
            return np.inf, None
        costs = matrix.data[start:stop] + home[peers]
        best = int(costs.argmin())
        return costs[best], self.states[peers[best]] if np.isfinite(costs[best]) else None

    def _path(self, position, target, speed):
        if position not in self.index or target not in self.index:
            return None
        costs, preds = self.forward(position, speed)
        root, current = self.index[position], self.index[target]
        if not np.isfinite(costs[current]):
            return None
        reversed_path = [target]
        while current != root:
            current = preds[current]
            reversed_path.append(self.states[current])
        return list(reversed(reversed_path))

    def to_service(self, position, edge_id, speed):
        if position != edge_id:
            return super().to_service(position, edge_id, speed)
        _, following = self.cycle(edge_id, speed)
        return tuple(self._path(following, edge_id, speed)[:-1]) if following is not None else None


def sparse_greedy(builder, fleet, tasks, stop, rng=None):
    """Score arrivals in vectors. Expand storable actions only for selected candidates."""
    paths = builder.routing
    targets = np.array([paths.index.get(task.edge_id, -1) for task in tasks])
    priorities = np.array([task.priority for task in tasks])
    passes = np.array([task.required_passes for task in tasks])
    releases = np.array([task.earliest_s for task in tasks])
    deadlines = np.array([task.deadline_s for task in tasks])
    active = targets >= 0
    routes, prefixes, profiles = {}, {}, {}
    for vehicle in fleet:
        if monotonic() >= stop:
            return (), True
        speed = vehicle.deadhead_kmh
        transit = np.array([paths.seconds(task.edge_id, speed) for task in tasks])
        service = np.array([paths.seconds(task.edge_id, vehicle.service_kmh) for task in tasks])
        cycles = np.zeros(len(tasks))
        for i in np.flatnonzero(active & (passes > 1)):
            if monotonic() >= stop:
                return (), True
            cycles[i] = paths.cycle(tasks[i].edge_id, speed)[0] - transit[i]
        available = np.array([task.capability in vehicle.capabilities for task in tasks])
        home = paths.reverse(-vehicle.depot_node, speed)[targets]
        extra = (passes - 1) * cycles
        jitter = np.array([rng.uniform(0.68, 1.32) for _ in tasks]) if rng else 1.0
        profiles[vehicle.vehicle_id] = transit, service * passes, extra, home, available, jitter
        prefixes[vehicle.vehicle_id] = ()

    while active.any() and monotonic() < stop:
        choices = []
        for vehicle in fleet:
            key = vehicle.vehicle_id
            prefix = prefixes[key]
            state = prefix[-1].edge_id if prefix else vehicle.depot_node
            clock = prefix[-1].end_s if prefix else vehicle.shift_start_s
            speed = vehicle.deadhead_kmh
            transit, service, extra, home, available, jitter = profiles[key]
            approach = paths.forward(state, speed)[0][targets] - transit
            if prefix:
                task_here = targets == paths.index[state]
                if task_here.any():
                    approach[task_here] += paths.cycle(state, speed)[0]
            finish = np.maximum(clock + approach, releases) + service + extra
            valid = active & available & (finish <= deadlines) & (finish + home <= vehicle.shift_end_s)
            indices = np.flatnonzero(valid)
            if not len(indices):
                continue
            ranked_cost = (approach + extra) * jitter
            selected = indices[np.lexsort((indices, finish[indices], ranked_cost[indices],
                                           priorities[indices]))[0]]
            choices.append(((priorities[selected], ranked_cost[selected], finish[selected],
                             tasks[selected].task_id, key), vehicle, selected))
        if not choices:
            break
        _, vehicle, selected = min(choices, key=lambda item: item[0])
        key = vehicle.vehicle_id
        result = builder.extend(vehicle, prefixes[key], tasks[selected])
        if result is None:
            raise RuntimeError("distance score and expanded route disagree")
        prefixes[key], finished, _ = result
        routes[key] = VehicleRoute(vehicle_id=key, actions=finished)
        active[selected] = False
    return tuple(routes[key] for key in sorted(routes)), bool(active.any() and monotonic() >= stop)
