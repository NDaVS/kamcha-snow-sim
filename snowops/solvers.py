"""Baseline and a route-pool set partitioning adapter for HiGHS."""
from collections import defaultdict
from random import Random
from time import monotonic
from typing import Protocol

import numpy as np
from scipy import __version__ as scipy_version
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csc_array

from .contracts import Action, ScenarioSnapshot, SolverResult, VehicleRoute
from .economics import evaluate
from .osm import RoadGraph
from .routing import Routing, check_references
from .scalable import SparseRouting, sparse_greedy


class ArcSolver(Protocol):
    def solve(self, graph: RoadGraph, snapshot: ScenarioSnapshot, time_limit_s: float) -> SolverResult: ...


class RouteBuilder:
    def __init__(self, graph, scenario):
        self.graph = graph
        use_sparse = len(scenario.tasks) > 100 or len(graph.edges) > 5000
        engine = SparseRouting if use_sparse else Routing
        self.routing = engine(graph, scenario)

    def move(self, actions, edge_id, seconds, speed, mode, task_id=None):
        edge = self.graph.edges[edge_id]
        end = seconds + self.routing.seconds(edge_id, speed)
        actions.append(Action(mode=mode, from_node=edge.from_node, to_node=edge.to_node,
                              edge_id=edge_id, task_id=task_id, start_s=seconds, end_s=end))
        return end

    def build(self, vehicle, order):
        prefix = finished = ()
        for task in order:
            candidate = self.extend(vehicle, prefix, task)
            if candidate is None:
                return None
            prefix, finished, _ = candidate
        return VehicleRoute(vehicle_id=vehicle.vehicle_id, actions=finished)

    def extend(self, vehicle, prefix, task):
        if task.capability not in vehicle.capabilities:
            return None
        actions = list(prefix)
        seconds = actions[-1].end_s if actions else vehicle.shift_start_s
        position = actions[-1].edge_id if actions else vehicle.depot_node
        transit_m = 0
        for _ in range(task.required_passes):
            path = self.routing.to_service(position, task.edge_id, vehicle.deadhead_kmh)
            if path is None:
                return None
            for edge_id in path:
                seconds = self.move(actions, edge_id, seconds, vehicle.deadhead_kmh, "deadhead")
                transit_m += self.graph.edges[edge_id].length_m
            if seconds < task.earliest_s:
                node = self.graph.edges[task.edge_id].from_node
                actions.append(Action(mode="wait", from_node=node, to_node=node,
                                      start_s=seconds, end_s=task.earliest_s))
                seconds = task.earliest_s
            seconds = self.move(actions, task.edge_id, seconds, vehicle.service_kmh, "service", task.task_id)
            if seconds > task.deadline_s:
                return None
            position = task.edge_id
        home = self.routing.to_depot(position, vehicle.depot_node, vehicle.deadhead_kmh)
        if home is None:
            return None
        finished = list(actions)
        for edge_id in home:
            seconds = self.move(finished, edge_id, seconds, vehicle.deadhead_kmh, "return")
        if seconds > vehicle.shift_end_s:
            return None
        return tuple(actions), tuple(finished), transit_m


def greedy(builder, fleet, tasks, stop, rng=None):
    if isinstance(builder.routing, SparseRouting):
        return sparse_greedy(builder, fleet, tasks, stop, rng)
    routes, prefixes = {}, defaultdict(tuple)
    remaining = list(tasks)
    while remaining and monotonic() < stop:
        options = []
        for task in remaining:
            for vehicle in fleet:
                if monotonic() >= stop:
                    return tuple(routes[key] for key in sorted(routes)), True
                candidate = builder.extend(vehicle, prefixes[vehicle.vehicle_id], task)
                if candidate:
                    prefix, finished, metres = candidate
                    key = (task.priority, metres, prefix[-1].end_s, task.task_id, vehicle.vehicle_id)
                    options.append((key, task, vehicle, prefix, finished))
        if not options:
            break
        options.sort(key=lambda option: option[0])
        best = options[0]
        if rng:
            shortlist = [option for option in options[:5] if option[0][0] == best[0][0]]
            best = rng.choice(shortlist)
        _, task, vehicle, prefix, finished = best
        prefixes[vehicle.vehicle_id] = prefix
        routes[vehicle.vehicle_id] = VehicleRoute(vehicle_id=vehicle.vehicle_id, actions=finished)
        remaining.remove(task)
    return tuple(routes[key] for key in sorted(routes)), bool(remaining and monotonic() >= stop)


class GreedySolver:
    def solve(self, graph, snapshot, time_limit_s):
        stop = monotonic() + time_limit_s
        check_references(graph, snapshot)
        builder = RouteBuilder(graph, snapshot)
        routes, timed_out = greedy(builder, snapshot.fleet, snapshot.tasks, stop)
        version = "greedy-sparse-1" if isinstance(builder.routing, SparseRouting) else "greedy-1"
        return SolverResult(solver_id="B0", solver_version=version, routes=routes,
                            termination="timeout" if timed_out else "search_finished")


class PoolSolver:
    def __init__(self, trials=24):
        self.trials = trials

    def solve(self, graph, snapshot, time_limit_s):
        stop = monotonic() + time_limit_s
        check_references(graph, snapshot)
        builder = RouteBuilder(graph, snapshot)
        large = isinstance(builder.routing, SparseRouting)
        baseline_stop = min(stop, monotonic() + 0.52 * time_limit_s) if large else stop
        baseline, timed_out = greedy(builder, snapshot.fleet, snapshot.tasks, baseline_stop)
        if large:
            return self._large_pool(graph, snapshot, builder, baseline, timed_out, stop, time_limit_s)
        unique = {}

        def add(routes):
            for route in routes:
                sequence = tuple(action.task_id for action in route.actions if action.mode == "service")
                unique.setdefault((route.vehicle_id, sequence), route)

        add(baseline)
        tasks = {task.task_id: task for task in snapshot.tasks}
        vehicles = {vehicle.vehicle_id: vehicle for vehicle in snapshot.fleet}
        for route in baseline:
            service = (action.task_id for action in route.actions if action.mode == "service")
            order = [tasks[key] for key in dict.fromkeys(service)]
            vehicle = vehicles[route.vehicle_id]
            for i in range(len(order) - 1):
                if monotonic() >= stop:
                    break
                candidate = builder.build(vehicle, order[:i] + [order[i + 1], order[i]] + order[i + 2:])
                if candidate:
                    add((candidate,))
        for vehicle in snapshot.fleet:
            for task in snapshot.tasks:
                if monotonic() >= stop:
                    break
                candidate = builder.build(vehicle, [task])
                if candidate:
                    add((candidate,))
        rng = Random(snapshot.seed)
        for trial in range(self.trials):
            if monotonic() >= stop:
                break
            fleet = (snapshot.fleet[trial],) if trial < len(snapshot.fleet) else snapshot.fleet
            add(greedy(builder, fleet, snapshot.tasks, stop, rng)[0])
        pool = tuple(unique.values())
        chosen, gap = baseline, None
        if monotonic() >= stop:
            termination = "timeout"
        elif pool:
            chosen, termination, gap = self._select(graph, snapshot, pool, baseline, stop)
        else:
            termination = "pool_empty"
        return SolverResult(solver_id="O1", solver_version=f"pool-1/scipy-{scipy_version}",
                            routes=chosen, termination=termination, pool_size=len(pool), pool_gap=gap)

    def _large_pool(self, graph, snapshot, builder, baseline, timed_out, stop, limit):
        pool = {(route.vehicle_id, route.actions): route for route in baseline}
        search_stop = stop - min(2, limit * 0.18)
        rng = Random(snapshot.seed)
        for trial in range(min(self.trials, 4)):
            remaining = search_stop - monotonic()
            if remaining <= 0:
                break
            partial_stop = min(search_stop, monotonic() + max(0.5, remaining / (2 if trial == 0 else 1)))
            routes, interrupted = greedy(builder, snapshot.fleet, snapshot.tasks, partial_stop, rng)
            timed_out |= interrupted
            pool.update(((route.vehicle_id, route.actions), route) for route in routes)
            if not interrupted and len(pool) > 3 * len(snapshot.fleet):
                break
        if pool and monotonic() < stop:
            chosen, termination, gap = self._select(graph, snapshot, tuple(pool.values()), baseline, stop)
        else:
            chosen, termination, gap = baseline, "timeout" if timed_out else "pool_empty", None
        if timed_out and termination == "pool_optimal":
            termination = "pool_optimal_generation_truncated"
        return SolverResult(solver_id="O1", solver_version=f"pool-sparse-1/scipy-{scipy_version}",
                            routes=chosen, termination=termination, pool_size=len(pool), pool_gap=gap)

    @staticmethod
    def _select(graph, snapshot, pool, baseline, stop):
        tasks = {task.task_id: i for i, task in enumerate(snapshot.tasks)}
        vehicles = {v.vehicle_id: len(tasks) + i for i, v in enumerate(snapshot.fleet)}
        row_ids, column_ids, costs, covered = [], [], [], []
        for col, route in enumerate(pool):
            service_ids = {action.task_id for action in route.actions if action.mode == "service"}
            covered.append(service_ids)
            rows = sorted(tasks[task_id] for task_id in service_ids) + [vehicles[route.vehicle_id]]
            row_ids.extend(rows)
            column_ids.extend([col] * len(rows))
            costs.append(sum(evaluate(graph, snapshot.tariff, (route,))["cost_components_mrub"].values()))
        sites = [site for site in snapshot.priority_sites
                 if site.task_ids and len(site.task_ids) == len(site.approach_edges)]
        n_routes = len(pool)
        variables = n_routes + len(sites)
        capacity = csc_array((np.ones(len(row_ids)), (row_ids, column_ids)),
                             shape=(len(tasks) + len(vehicles), variables))
        constraints = [LinearConstraint(capacity, 0, 1)]
        # Link a site indicator to the coverage of every edge on its nominated approach.
        site_rows, site_columns, site_values = [], [], []
        columns_by_task = defaultdict(list)
        for col, ids in enumerate(covered):
            for task_id in ids:
                columns_by_task[task_id].append(col)
        link_count = 0
        for i, site in enumerate(sites):
            for task_id in site.task_ids:
                route_columns = columns_by_task[task_id]
                site_rows.extend([link_count] * (len(route_columns) + 1))
                site_columns.extend([*route_columns, n_routes + i])
                site_values.extend([-1] * len(route_columns) + [1])
                link_count += 1
        if link_count:
            links = csc_array((site_values, (site_rows, site_columns)),
                              shape=(link_count, variables))
            constraints.append(LinearConstraint(links, -np.inf, 0))

        objectives = []
        priorities = {task.priority for task in snapshot.tasks} | {site.rank - 1 for site in sites}
        for p in sorted(priorities):
            site_score = np.r_[np.zeros(n_routes), [site.rank == p + 1 for site in sites]]
            if site_score.any():
                objectives.append(-site_score)
            group = {task.task_id for task in snapshot.tasks if task.priority == p}
            task_score = np.r_[[len(group & service_ids) for service_ids in covered], np.zeros(len(sites))]
            if task_score.any():
                objectives.append(-task_score)
        objectives.append(np.r_[costs, np.zeros(len(sites))])
        index = {(route.vehicle_id, route.actions): i for i, route in enumerate(pool)}
        incumbent = np.zeros(variables)
        baseline_coverage = set()
        for route in baseline:
            column = index[(route.vehicle_id, route.actions)]
            incumbent[column] = 1
            baseline_coverage.update(covered[column])
        for i, site in enumerate(sites):
            incumbent[n_routes + i] = set(site.task_ids) <= baseline_coverage

        termination, gap = "pool_optimal", None
        for objective in objectives:
            remaining = stop - monotonic()
            if remaining <= 0:
                termination = "timeout"
                break
            result = milp(objective, integrality=np.ones(variables), bounds=Bounds(0, 1),
                          constraints=constraints,
                          options={"time_limit": remaining, "mip_rel_gap": 0.0})
            if result.x is not None:
                selected = np.rint(result.x)
                if (not np.allclose(selected, result.x, atol=1e-6, rtol=0)
                        or np.any(selected < 0) or np.any(selected > 1)
                        or any(np.any(constraint.A @ selected < constraint.lb)
                               or np.any(constraint.A @ selected > constraint.ub)
                               for constraint in constraints)):
                    termination = "invalid_pool_result"
                    break
                if tuple(o @ selected for o in objectives) < tuple(o @ incumbent for o in objectives):
                    incumbent = selected
            if result.status != 0 or result.x is None:
                termination = "timeout" if result.status == 1 else "solver_error"
                break
            value = objective @ incumbent
            constraints.append(LinearConstraint(objective, value, value))
            if objective is objectives[-1]:
                gap = result.mip_gap
        chosen = tuple(route for route, take in zip(pool, incumbent) if take)
        return chosen, termination, gap
