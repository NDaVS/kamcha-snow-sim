"""Run the paired road experiment or replay an exported plan."""
import argparse
import csv
import json
import math
import platform
import resource
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from .contracts import Plan, ScenarioSnapshot
from .osm import load_osm
from .pilot import scenario_factory
from .reporting import geojson, make_plan
from .routing import check_references
from .solvers import GreedySolver, PoolSolver
from .validation import validate

DEFAULT_OSM = Path(__file__).resolve().parent.parent / "data/pkc/network.osm.gz"
POLICIES = {"B0": GreedySolver, "O1": PoolSolver}
CASES = {
    "base_snowfall": {},
    "few_vehicles": {"fleet_size": 1, "shift_s": 120},
    "fleet_five": {"fleet_size": 5},
    "short_shift": {"shift_s": 900},
    "high_deadhead_tariff": {"deadhead_rate": 240},
    "tight_priority": {"critical_deadline_s": 400},
}


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(graph, snapshot, output, policies, limit_s):
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "scenario.json", snapshot.model_dump(mode="json"))
    plans, timings = {}, {}
    for name in policies:
        start = perf_counter()
        result = POLICIES[name]().solve(graph, snapshot, limit_s)
        timings[name] = round((perf_counter() - start) * 1000, 3)
        plan = make_plan(graph, snapshot, result)
        plans[name] = plan
        write_json(output / f"{name}.plan.json", plan.model_dump(mode="json"))
        write_json(output / f"{name}.geojson", geojson(graph, plan))
    return plans, timings


def rows_for(snapshot, plans):
    saving = None
    if all(name in plans and plans[name].status == "ok" for name in POLICIES):
        baseline = plans["B0"].metrics.C_var_rub
        if baseline > 0:
            saving = round((baseline - plans["O1"].metrics.C_var_rub) / baseline, 9)
    for name, plan in plans.items():
        metrics = plan.metrics.model_dump()
        costs = metrics.pop("cost_components_mrub")
        yield {
            "scenario": snapshot.scenario_id, "method": name, "seed": snapshot.seed,
            "graph_version": plan.graph_version, "input_hash": plan.input_hash,
            "fleet_size": len(snapshot.fleet), "status": plan.feasibility_status,
            "termination": plan.solver.termination, "pool_size": plan.solver.pool_size,
            "pool_gap": plan.solver.pool_gap, "required_tasks": len(snapshot.tasks),
            "unserved_tasks": len(plan.validation.unserved_tasks),
            "late_tasks": len(plan.validation.late_tasks),
            "passes": plan.validation.completed_passes, "required_passes": plan.validation.required_passes,
            **metrics, **costs,
            "relative_saving_vs_B0": saving if name == "O1" else None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", default=DEFAULT_OSM, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--case", choices=(*CASES, "all"), default="all")
    demo.add_argument("--seed", default=42, type=int)
    demo.add_argument("--time-limit-s", default=30, type=float)
    solve = commands.add_parser("solve")
    solve.add_argument("scenario", type=Path)
    solve.add_argument("--output", type=Path, required=True)
    solve.add_argument("--method", choices=tuple(POLICIES), default="O1")
    solve.add_argument("--time-limit-s", default=30, type=float)
    replay = commands.add_parser("validate")
    replay.add_argument("scenario", type=Path)
    replay.add_argument("plan", type=Path)

    args = parser.parse_args()
    if args.command != "validate" and (not math.isfinite(args.time_limit_s) or args.time_limit_s <= 0):
        parser.error("--time-limit-s must be finite and positive")

    start = perf_counter()
    graph = load_osm(args.osm)
    import_ms = round((perf_counter() - start) * 1000, 3)
    if args.command == "validate":
        snapshot = ScenarioSnapshot.model_validate_json(args.scenario.read_bytes())
        check_references(graph, snapshot)
        plan = Plan.model_validate_json(args.plan.read_bytes())
        if plan.input_hash != snapshot.input_hash or plan.graph_version != graph.version:
            parser.error("plan and scenario fingerprints differ")
        validation = validate(graph, snapshot, plan.solver.routes)
        print(validation.model_dump_json(indent=2))
        return int(bool(validation.violations))

    rows, timings = [], {}
    if args.command == "demo":
        cases = CASES if args.case == "all" else (args.case,)
        for case in cases:
            snapshot = scenario_factory(graph, seed=args.seed, scenario_id=case, **CASES[case])
            plans, timings[case] = run(graph, snapshot, args.output / case, POLICIES, args.time_limit_s)
            rows.extend(rows_for(snapshot, plans))
    else:
        snapshot = ScenarioSnapshot.model_validate_json(args.scenario.read_bytes())
        plans, timings[snapshot.scenario_id] = run(graph, snapshot, args.output,
                                                   (args.method,), args.time_limit_s)
        rows.extend(rows_for(snapshot, plans))
    with (args.output / "metrics.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write_json(args.output / "graph_audit.json", graph.audit)
    write_json(args.output / "scenario.schema.json", ScenarioSnapshot.model_json_schema())
    write_json(args.output / "plan.schema.json", Plan.model_json_schema())
    write_json(args.output / "run_environment.json", {
        "graph_import_ms": import_ms, "solver_ms": timings, "time_limit_s": args.time_limit_s,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if platform.system() == "Darwin" else 1024),
        "python": platform.python_version(), "platform": platform.platform(),
        "packages": {name: version(name) for name in ("networkx", "numpy", "pydantic", "scipy")},
    })
    for row in rows:
        print(f"{row['scenario']} {row['method']} {row['status']} "
              f"{row['passes']}/{row['required_passes']} passes, {row['C_var_rub']:.2f} RUB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
