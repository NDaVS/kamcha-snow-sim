"""Run external CARP solvers on a common, homogeneous OSM projection."""
import argparse
import csv
from hashlib import sha256
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np

from .contracts import SolverResult, Tariff, Vehicle
from .infrastructure import DATA, Infrastructure
from .osm import load_osm
from .reporting import make_plan
from .scalable import SparseRouting
from .solvers import GreedySolver, PoolSolver, RouteBuilder

HGS_REVISION = "08fde6a5672c3814dca1b4450aa9e5968b1d273a"
ARC_REVISION = "13aa09c04a553bb66ea133ad97b46e66cffabb2c"


def instance(infrastructure, count):
    graph = infrastructure.graph
    scenario = infrastructure.scenario(count)
    prohibited_dep = {graph.edges[b].from_node for _, b in graph.forbidden_turns}
    depot = next(v.depot_node for v in scenario.fleet if v.depot_node not in prohibited_dep)
    fleet = tuple(Vehicle(vehicle_id=f"common-{n}", depot_node=depot, shift_end_s=86400,
                          deadhead_kmh=25, service_kmh=12) for n in range(1, 5))
    return scenario.model_copy(update={
        "scenario_id": f"carp-common-{count}", "constraints_version": "carp-common-1",
        "priority_sites": (), "fleet": fleet,
        "tasks": tuple(task.model_copy(update={"capability": "plow", "priority": 0, "deadline_s": 86400})
                       for task in scenario.tasks),
        "tariff": Tariff(version="common-time-only", deadhead_rub_km=0,
                         service_rub_km=0, elapsed_mrub_s=1000),
    })


def project(graph, scenario):
    """Costs include entering the target service arc, charged at transit speed."""
    routing = SparseRouting(graph, scenario)
    depot = scenario.fleet[0].depot_node
    states = [depot, *(task.edge_id for task in scenario.tasks)]
    targets = [routing.index[-depot], *(routing.index[state] for state in states[1:])]
    matrix = np.array([routing.forward(state, 25)[0][targets] for state in states])
    matrix[0, 0] = 0
    if not np.isfinite(matrix).all():
        raise ValueError("common fixture must have all service transitions reachable")
    correction = [0, *(routing.seconds(edge_id, 12) - routing.seconds(edge_id, 25) for edge_id in states[1:])]
    return matrix.astype(int), np.array(correction)


def write_hgs(path, matrix, fleet):
    size = len(matrix)
    header = [f"Name: {path.stem}", "Optimal value: 0", f"#Vehicles: {fleet}",
              f"Capacity: {size}", "Depot Node: 1", f"#Nodes: {size}", "#Edges: 0",
              f"#Arcs: {size*(size-1)}", f"#Required N: {size-1}", "#Required E: 0",
              "#Required A: 0", "", "ReN. DEMAND S.COST"]
    header.extend(f"N{i} 1 0" for i in range(2, size+1))
    header.extend(["", "ReE. FROM TO T.COST DEMAND S.COST", "EDGE FROM TO T.COST",
                   "ReA. FROM TO T.COST DEMAND S.COST", "ARC FROM TO T.COST"])
    arcs = (f"A {i+1} {j+1} {matrix[i,j]}" for i in range(size) for j in range(size) if i != j)
    path.write_text("\n".join([*header, *arcs, ""]))


def external_routes(name, graph, scenario, orders, version, matrix, correction):
    visits = Counter(index for order in orders for index in order)
    if set(visits) != set(range(1, len(scenario.tasks)+1)) or set(visits.values()) != {1}:
        raise ValueError("external output does not cover each task once")
    if len(orders) > len(scenario.fleet):
        raise ValueError("more routes than allocated vehicles")
    builder = RouteBuilder(graph, scenario)
    routes = tuple(builder.build(vehicle, [scenario.tasks[i-1] for i in order])
                   for vehicle, order in zip(scenario.fleet, orders) if order)
    if any(route is None for route in routes):
        raise ValueError("external route could not be expanded under the common constraints")
    expected = sum(int(sum(matrix[a,b] for a, b in zip([0, *order], [*order, 0]))
                       + correction[order].sum()) for order in orders if order)
    elapsed = sum(route.actions[-1].end_s for route in routes)
    if expected != elapsed:
        raise ValueError(f"matrix cost {expected}s differs from independent expanded time {elapsed}s")
    return SolverResult(solver_id=name, solver_version=version, routes=routes,
                        termination="external_candidate")


def arc_worker():
    # Invoked in a separate interpreter using the audited ArcRoute source tree.
    from solvers.meta import ILSHCARP
    from solvers.evaluator import SolutionEvaluator

    data = np.load(sys.argv[2])
    matrix, correction = data["matrix"], data["correction"]
    size, fleet = len(matrix), int(data["fleet"])
    demands = np.r_[0, np.full(size-1, 1/size)].astype(np.float32)
    classes = np.r_[0, np.ones(size-1)].astype(np.int32)
    solver = ILSHCARP()
    solver._load_data(matrix, [1], list(range(fleet)), demands, classes, correction,
                      np.zeros(size), np.zeros((size, 2), dtype=int))
    solver(max_iter=int(sys.argv[4]), variant="P", num_init_sample=3, seed=42)
    decoded, _ = SolutionEvaluator(variant="P", pos_val=(1,))(
        solver._last_best_action,
        {"adj": matrix, "service_times": correction, "clss": classes, "demand": demands},
        M=fleet,
    )
    if any(len(trips) > 1 for trips in decoded):
        raise ValueError("multiple depot-return trips lie outside this common fixture")
    orders = [[int(i) for i in trips[0]] for trips in decoded if trips]
    Path(sys.argv[3]).write_text(json.dumps([order for order in orders if order]))


def compare(graph, counts, output, hgs, arc_repo, arc_python, limit):
    output.mkdir(parents=True, exist_ok=True)
    infrastructure = Infrastructure(graph)
    records = []
    metadata = {
        "graph_version": graph.version, "hgs_expected_revision": HGS_REVISION,
        "hgs_binary_sha256": sha256(hgs.read_bytes()).hexdigest() if hgs and hgs.is_file() else None,
        "arc_expected_revision": ARC_REVISION, "arc_python": arc_python, "wall_limit_s": limit,
    }
    if arc_repo and arc_repo.is_dir():
        revision = subprocess.run(["git", "-C", str(arc_repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True)
        metadata["arc_checkout_revision"] = revision.stdout.strip() or None
    (output / "environment.json").write_text(json.dumps(metadata, indent=2))
    for count in counts:
        scenario = instance(infrastructure, count)
        (output / f"{count}.scenario.json").write_text(scenario.model_dump_json(indent=2))
        start = perf_counter()
        matrix, correction = project(graph, scenario)
        prep = perf_counter() - start
        np.savez_compressed(output / f"{count}.projection.npz", matrix=matrix, correction=correction)
        for name in ("B0", "O1", "HGS-CARP", "ArcRoute"):
            start = perf_counter()
            row = {"tasks": count, "input_hash": scenario.input_hash, "method": name,
                   "projection_s": round(prep, 3) if name in {"HGS-CARP", "ArcRoute"} else 0,
                   "budget_s": limit, "status": "", "passes": "", "deadhead_km": "",
                   "vehicle_seconds": "", "last_service_s": "", "cost_rub": "", "error": ""}
            try:
                if name in {"B0", "O1"}:
                    result = (GreedySolver() if name == "B0" else PoolSolver()).solve(graph, scenario, limit)
                else:
                    with TemporaryDirectory(prefix="snowops-carp-") as folder:
                        work = Path(folder)
                        raw = work / "solution"
                        if name == "HGS-CARP":
                            if hgs is None:
                                raise ValueError("HGS-CARP executable not supplied")
                            case = work / "common.dat"
                            write_hgs(case, matrix, len(scenario.fleet))
                            cmd = [str(hgs.resolve()), str(case), "-type", "31", "-sol", str(raw),
                                   "-seed", "42", "-t", str(max(1, int(limit)//3))]
                            env = None
                        else:
                            if arc_repo is None:
                                raise ValueError("ArcRoute source not supplied")
                            import os
                            env = {**os.environ, "PYTHONPATH": str(arc_repo.resolve() / "src") + os.pathsep + str(Path(__file__).resolve().parents[1]),
                                   "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
                            case = work / "common.npz"
                            np.savez(case, matrix=matrix, correction=correction, fleet=len(scenario.fleet))
                            cmd = [arc_python, "-m", "snowops.carp_compare", "--arc-worker",
                                   str(case), str(raw), "24"]
                        proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True,
                                              timeout=limit)
                        (output / f"{count}-{name}.log").write_text(proc.stdout + proc.stderr)
                        if proc.returncode or not raw.exists():
                            raise ValueError(f"native exit={proc.returncode}; no usable route file")
                        if name == "HGS-CARP":
                            orders = [[int(i) for i in re.findall(r"\(S (\d+),", line)]
                                      for line in raw.read_text().splitlines()[4:] if "(S " in line]
                        else:
                            orders = json.loads(raw.read_text())
                        result = external_routes(name, graph, scenario, orders,
                                                 HGS_REVISION if name == "HGS-CARP" else ARC_REVISION,
                                                 matrix, correction)
                plan = make_plan(graph, scenario, result)
                status = plan.feasibility_status if not result.termination.startswith("timeout") else "timeout"
                last = max((a.end_s for route in result.routes for a in route.actions if a.mode == "service"),
                           default=0)
                row.update(status=status, passes=plan.validation.completed_passes,
                           deadhead_km=plan.metrics.deadhead_km, vehicle_seconds=plan.metrics.vehicle_seconds,
                           last_service_s=last, cost_rub=plan.metrics.C_var_rub)
                (output / f"{count}-{name}.plan.json").write_text(plan.model_dump_json(indent=2))
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or b""
                stderr = exc.stderr or b""
                (output / f"{count}-{name}.log").write_bytes(stdout + stderr)
                row.update(status="timeout", error=f"native process exceeded {exc.timeout}s")
            except (OSError, ValueError, RuntimeError) as exc:
                row.update(status="incompatible_or_unavailable", error=str(exc))
            row["wall_s"] = round(perf_counter()-start, 3)
            print(row, flush=True)
            records.append(row)
    with (output / "comparison.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=(60, 300))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--osm", type=Path, default=DATA / "city.osm.gz")
    parser.add_argument("--hgs", type=Path)
    parser.add_argument("--arc-repo", type=Path)
    parser.add_argument("--arc-python", default=sys.executable)
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    compare(load_osm(args.osm), args.counts, args.output, args.hgs, args.arc_repo, args.arc_python, args.limit)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--arc-worker"]:
        arc_worker()
    else:
        main()
