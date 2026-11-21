"""Replay an expanded plan without querying paths or trusting solver claims."""
from collections import Counter

from .contracts import ScenarioSnapshot, Validation, VehicleRoute, Violation
from .osm import RoadGraph


def validate(graph: RoadGraph, snapshot: ScenarioSnapshot,
             routes: tuple[VehicleRoute, ...]) -> Validation:
    fleet = {vehicle.vehicle_id: vehicle for vehicle in snapshot.fleet}
    tasks = {task.task_id: task for task in snapshot.tasks}
    closed = set(snapshot.closed_edges)
    turns = graph.forbidden_turns.union(snapshot.forbidden_turns)
    coverage, violations = Counter(), []
    seen, late = set(), set()
    for route in routes:
        vehicle = fleet.get(route.vehicle_id)
        if vehicle is None or route.vehicle_id in seen:
            violations.append(Violation(code="unknown_or_duplicate_vehicle", vehicle_id=route.vehicle_id))
            continue
        seen.add(route.vehicle_id)
        node, clock, incoming = vehicle.depot_node, vehicle.shift_start_s, None
        route_coverage = Counter()
        valid_route = True
        returning = False
        for index, action in enumerate(route.actions):
            codes = []
            if not (clock == action.start_s < action.end_s <= vehicle.shift_end_s):
                codes.append("timeline_or_shift")
            if action.from_node != node:
                codes.append("discontinuous_route")
            if returning and action.mode != "return":
                codes.append("action_after_return")
            if action.mode == "wait":
                if action.to_node != node or action.edge_id is not None or action.task_id is not None:
                    codes.append("invalid_wait")
            else:
                edge = graph.edges.get(action.edge_id)
                if edge is None or (edge.from_node, edge.to_node) != (action.from_node, action.to_node):
                    codes.append("wrong_or_unknown_directed_edge")
                else:
                    if edge.edge_id in closed:
                        codes.append("closed_edge")
                    if (incoming, edge.edge_id) in turns:
                        codes.append("forbidden_turn")
                    speed = vehicle.service_kmh if action.mode == "service" else vehicle.deadhead_kmh
                    if snapshot.respect_osm_speed_limits and edge.speed_limit_kmh is not None:
                        speed = min(speed, edge.speed_limit_kmh)
                    seconds = action.end_s - action.start_s
                    if seconds * 10 * speed < 36 * edge.length_m:
                        codes.append("insufficient_travel_time")
                incoming = action.edge_id
                returning = action.mode == "return"
            if action.mode == "service":
                task = tasks.get(action.task_id)
                if task is None or task.edge_id != action.edge_id:
                    codes.append("wrong_service_edge_or_task")
                else:
                    if task.capability not in vehicle.capabilities:
                        codes.append("missing_vehicle_capability")
                    if action.start_s < task.earliest_s:
                        codes.append("before_service_window")
                    if action.end_s > task.deadline_s:
                        codes.append("missed_deadline")
                        late.add(task.task_id)
                    if not codes and valid_route:
                        route_coverage[task.task_id] += 1
            elif action.task_id is not None:
                codes.append("task_on_nonservice_action")
            if codes:
                valid_route = False
            violations.extend(Violation(code=code, vehicle_id=vehicle.vehicle_id,
                                        action_index=index, task_id=action.task_id) for code in codes)
            node, clock = action.to_node, action.end_s

        if node != vehicle.depot_node:
            violations.append(Violation(code="did_not_return_to_depot", vehicle_id=vehicle.vehicle_id))
            valid_route = False
        if valid_route:
            for task_id, passes in route_coverage.items():
                if coverage[task_id]:
                    violations.append(Violation(code="task_split_across_vehicles", task_id=task_id))
                    continue
                coverage[task_id] = passes

    unserved = []
    for task in snapshot.tasks:
        difference = task.required_passes - coverage[task.task_id]
        if difference > 0:
            unserved.append(task.task_id)
            violations.append(Violation(code="unserved_required_passes", task_id=task.task_id,
                                        missing_passes=difference))
        elif difference < 0:
            violations.append(Violation(code="extra_service_passes", task_id=task.task_id))

    return Validation(violations=tuple(violations), unserved_tasks=tuple(unserved),
                      required_passes=sum(task.required_passes for task in snapshot.tasks),
                      completed_passes=sum(min(coverage[t.task_id], t.required_passes) for t in snapshot.tasks),
                      late_tasks=tuple(sorted(late)))
