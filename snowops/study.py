"""Run the scale, SLA, infrastructure and sensitivity study on the frozen OSM mosaic."""
import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from time import perf_counter

from .contracts import ScenarioSnapshot
from .economics import evaluate
from .infrastructure import DATA, Infrastructure
from .osm import load_osm
from .reporting import make_plan
from .solvers import GreedySolver, PoolSolver


SENSITIVITY_CASES = {
    "reference": {}, "fleet-4": {"fleet_size": 4}, "fleet-8": {"fleet_size": 8},
    "fleet-12": {"fleet_size": 12}, "shift-45min": {"shift_s": 2700},
    "shift-60min": {"shift_s": 3600}, "shift-90min": {"shift_s": 5400},
    "tariff-60": {"price": 60}, "tariff-240": {"price": 240},
    "critical-30min": {"top_deadline": 1800}, "critical-45min": {"top_deadline": 2700},
    "education-rank-1": {"promote_kinds": ("school", "kindergarten")},
    "double-pass": {"passes": 2}, "fleet-8-shift-60min": {"fleet_size": 8, "shift_s": 3600},
}


def write_csv(path, rows):
    if rows:
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def service_quality(snapshot, routes):
    targets = {t.task_id: t for t in snapshot.tasks}
    completed = Counter(a.task_id for r in routes for a in r.actions
                        if a.mode == "service" and a.task_id in targets
                        and a.end_s <= targets[a.task_id].deadline_s)
    on_time = {key for key, task in targets.items() if completed[key] >= task.required_passes}
    rows = []
    for site in sorted(snapshot.priority_sites, key=lambda s: (s.rank, s.kind, s.name, s.site_id)):
        covered = sum(task_id in on_time for task_id in site.task_ids)
        expected = len(site.approach_edges)
        status = ("unmapped" if not expected else
                  "outside_workload" if expected != len(site.task_ids) else
                  "on_time" if covered == expected else "missed")
        rows.append({
            "rank": site.rank, "kind": site.kind, "name": site.name, "site_id": site.site_id,
            "status": status, "deadline_s": site.deadline_s, "access_node": site.access_node,
            "snap_m": site.snap_m, "mapped_edges": expected, "scheduled_edges": len(site.task_ids),
            "on_time_edges": covered, "source": site.source, "coordinates_source": site.location_source,
            "lon": site.lon_lat[0] if site.lon_lat else None,
            "lat": site.lon_lat[1] if site.lon_lat else None,
            "approach_edges": " ".join(site.approach_edges),
            "task_ids": " ".join(site.task_ids), "reference_url": site.reference_url,
        })
    return on_time, rows


def score(graph, reference, result):
    on_time, sites = service_quality(reference, result.routes)
    costs = evaluate(graph, reference.tariff, result.routes)
    return {
        "reference_cost_rub": costs["C_var_rub"],
        "reference_critical_sites_missed": sum(s["rank"] == 1 and s["status"] == "missed" for s in sites),
        "reference_critical_tasks_missed": sum(t.priority == 0 and t.task_id not in on_time for t in reference.tasks),
        "reference_all_tasks_missed": len(reference.tasks) - len(on_time),
    }


def evaluate_case(graph, snapshot, reference, output, limit, method_names):
    (output / f"{snapshot.scenario_id}.scenario.json").write_text(snapshot.model_dump_json(), encoding="utf-8")
    rows = []
    for method in method_names:
        solver = GreedySolver() if method == "B0" else PoolSolver()
        start = perf_counter()
        result = solver.solve(graph, snapshot, limit)
        elapsed = perf_counter() - start
        plan = make_plan(graph, snapshot, result)
        on_time, sites = service_quality(snapshot, result.routes)
        key = f"{snapshot.scenario_id}-{method}"
        (output / f"{key}.plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
        if snapshot.scenario_id == "scale-1200" and method == "O1":
            write_csv(output / "ranked-infrastructure.csv", sites)
        row = {
            "scenario": snapshot.scenario_id, "method": method, "input_hash": snapshot.input_hash,
            "tasks": len(snapshot.tasks), "required_passes": plan.validation.required_passes,
            "passes_on_time": plan.validation.completed_passes, "fleet": len(snapshot.fleet),
            "used": plan.metrics.vehicles_used, "shift_s": snapshot.fleet[0].shift_end_s,
            "top_deadline_s": min(t.deadline_s for t in snapshot.tasks if t.priority == 0),
            "deadhead_rub_km": snapshot.tariff.deadhead_rub_km, "status": plan.feasibility_status,
            "termination": result.termination, "pool": result.pool_size,
            "unserved": len(snapshot.tasks) - len(on_time),
            "critical_missed": sum(t.priority == 0 and t.task_id not in on_time for t in snapshot.tasks),
            "critical_sites_missed": sum(s["rank"] == 1 and s["status"] == "missed" for s in sites),
            "critical_sites_on_time": sum(s["rank"] == 1 and s["status"] == "on_time" for s in sites),
            "deadhead_km": plan.metrics.deadhead_km, "service_km": plan.metrics.service_km,
            "cost_rub": plan.metrics.C_var_rub, "seconds": round(elapsed, 3),
            **score(graph, reference, result),
        }
        print(snapshot.scenario_id, method, row["status"], row["passes_on_time"], row["required_passes"],
              row["critical_missed"], row["critical_sites_missed"], row["cost_rub"], row["seconds"], flush=True)
        rows.append(row)
    return rows


def pareto(rows):
    """Select nondominated observations within one fixed evaluation workload."""
    keys = ("reference_cost_rub", "reference_critical_sites_missed", "reference_critical_tasks_missed",
            "reference_all_tasks_missed")
    points = [tuple(row[key] for key in keys) for row in rows]
    return [row for row, point in zip(rows, points)
            if not any(other != point and all(a <= b for a, b in zip(other, point)) for other in points)]


def decomposed_deadhead(graph, snapshot, routes):
    task_edges = {task.edge_id for task in snapshot.tasks}
    rows = []
    for route in routes:
        sums = Counter()
        last_task = None
        pending = []
        for action in route.actions:
            if action.mode == "wait":
                continue
            metres = graph.edges[action.edge_id].length_m
            if action.mode == "service":
                kind = ("to_first_task" if last_task is None else
                        "repeat_same_direction" if last_task == action.task_id else "between_tasks")
                sums[kind] += sum(pending)
                pending.clear()
                last_task = action.task_id
                sums["service"] += metres
            elif action.mode == "return":
                sums["return_to_depot"] += metres
            else:
                pending.append(metres)
            if action.mode != "service" and action.edge_id in task_edges:
                sums["deadhead_on_task_arc"] += metres
        vehicle = next(v for v in snapshot.fleet if v.vehicle_id == route.vehicle_id)
        rows.append({
            "vehicle_id": route.vehicle_id, "depot_node": vehicle.depot_node,
            **{key + "_m": sums[key] for key in (
                "to_first_task", "between_tasks", "repeat_same_direction", "return_to_depot",
                "deadhead_on_task_arc", "service")},
        })
    return rows


def depot_audit(source, output):
    from .pilot import scenario_factory

    graph = load_osm(source)
    saved = Path(__file__).resolve().parent.parent / "out/pkc-first/base_snowfall/scenario.json"
    original = (ScenarioSnapshot.model_validate_json(saved.read_bytes()) if saved.is_file()
                else scenario_factory(graph))
    local = Infrastructure(graph)
    centre, _ = local.nearest((158.6395, 53.040))
    task_origins = [graph.edges[task.edge_id].from_node for task in original.tasks]
    medoid = min(set(task_origins), key=lambda node: sum(
        (graph.nodes[node][0]-graph.nodes[other][0])**2 * 0.36
        + (graph.nodes[node][1]-graph.nodes[other][1])**2 for other in task_origins))
    lodging = (("original", ()), ("common_centre", (centre,)), ("service_medoid", (medoid,)))
    rows = []
    for label, depots in lodging:
        snapshot = (original.model_copy(update={"fleet": tuple(v.model_copy(update={"depot_node": depots[0]})
                                                                for v in original.fleet)})
                    if depots else original)
        for method, solver in (("B0", GreedySolver()), ("O1", PoolSolver())):
            result = solver.solve(graph, snapshot, 30)
            plan = make_plan(graph, snapshot, result)
            for row in decomposed_deadhead(graph, snapshot, result.routes):
                rows.append({"placement": label, "method": method, "total_passes": plan.validation.completed_passes,
                             **row})
    write_csv(output / "depot-deadhead.csv", rows)


def operational_depot_case(output, limit):
    from tempfile import TemporaryDirectory
    from .mosaic import merge

    with TemporaryDirectory(prefix="snowops-depot-") as tmp:
        city_path = Path(tmp) / "extended.osm.gz"
        merge((DATA / "city.osm.gz", DATA / "depot-east.osm.gz"), city_path)
        graph = load_osm(city_path)
    source = json.loads((DATA / "operational-reference.json").read_text(encoding="utf-8"))["depot"]
    infrastructure = Infrastructure(graph)
    depot, snap_m = infrastructure.nearest(source["street_reference"]["lon_lat"])
    if snap_m > 180:
        raise ValueError(f"recorded depot address is {snap_m}m from the routing component")
    original = infrastructure.scenario(1200, scenario_id="depot-reference")
    verified_asset = original.model_copy(update={
        "scenario_id": "depot-pogranichnaya", "fleet": tuple(
            vehicle.model_copy(update={"depot_node": depot}) for vehicle in original.fleet),
    })
    rows = []
    for snapshot in (original, verified_asset):
        rows.extend(evaluate_case(graph, snapshot, original, output, limit, ("B0", "O1")))
    write_csv(output / "operational-depot.csv", rows)
    (output / "operational-depot.json").write_text(json.dumps({
        "graph": graph.audit, "public_road_node": depot, "snap_m": snap_m, **source,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--osm", type=Path, default=DATA / "city.osm.gz")
    parser.add_argument("--policy", type=Path, default=DATA / "infrastructure-policy.json")
    parser.add_argument("--limit", type=float, default=18)
    parser.add_argument("--only", choices=("scale", "sensitivity", "depots", "operational-depot", "all"), default="all")
    args = parser.parse_args()
    if not math.isfinite(args.limit) or args.limit <= 0:
        parser.error("--limit must be finite and positive")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.only == "operational-depot":
        operational_depot_case(args.output, args.limit)
        return
    if args.only == "depots":
        depot_audit(DATA / "network.osm.gz", args.output)
        return
    start = perf_counter()
    graph = load_osm(args.osm)
    infrastructure = Infrastructure(graph, args.policy)
    details = {**graph.audit, "component_arcs": len(infrastructure.edges),
               "ranked_sites": len(infrastructure.sites), "preparation_s": round(perf_counter()-start, 3)}
    (args.output / "network-audit.json").write_text(json.dumps(details, ensure_ascii=False, indent=2))
    if args.only in {"scale", "all"}:
        rows = []
        for count in (300, 1200, 2400):
            snapshot = infrastructure.scenario(count, scenario_id=f"scale-{count}")
            rows.extend(evaluate_case(graph, snapshot, snapshot, args.output, args.limit, ("B0", "O1")))
        write_csv(args.output / "scale.csv", rows)
    if args.only in {"sensitivity", "all"}:
        rows, comparable = [], []
        reference = infrastructure.scenario(1200, scenario_id="sweep-reference")
        for case, settings in SENSITIVITY_CASES.items():
            snapshot = infrastructure.scenario(1200, scenario_id=f"sweep-{case}", **settings)
            required = {(t.task_id, t.required_passes) for t in snapshot.tasks}
            same_workload = required == {(t.task_id, t.required_passes) for t in reference.tasks}
            results = evaluate_case(graph, snapshot, reference if same_workload else snapshot,
                                    args.output, args.limit, ("B0", "O1"))
            rows.extend(results)
            if same_workload:
                comparable.extend(results)
        write_csv(args.output / "sensitivity.csv", rows)
        write_csv(args.output / "frontier.csv", pareto(comparable))
    if args.only == "all":
        depot_audit(DATA / "network.osm.gz", args.output)


if __name__ == "__main__":
    main()
