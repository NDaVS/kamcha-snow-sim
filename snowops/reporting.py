"""Validate, price and export the same directed actions."""
from datetime import timedelta
from hashlib import sha256

from .contracts import InfeasibilityCertificate, Plan
from .economics import evaluate
from .osm import OSM_ATTRIBUTION, OSM_LICENSE_URL
from .validation import validate


def shift_budget_certificate(graph, snapshot):
    # Even assigning every pass to the fastest compatible vehicle and ignoring transit,
    # the fleet must provide at least this many seconds of service.
    minimum = 0
    for task in snapshot.tasks:
        speeds = [v.service_kmh for v in snapshot.fleet if task.capability in v.capabilities]
        if not speeds:
            return None
        numerator = 36 * graph.edges[task.edge_id].length_m
        denominator = 10 * max(speeds)
        minimum += task.required_passes * ((numerator + denominator - 1) // denominator)
    total = sum(v.shift_end_s - v.shift_start_s for v in snapshot.fleet)
    if minimum > total:
        return InfeasibilityCertificate(minimum_service_seconds=minimum, total_shift_seconds=total)
    return None


def make_plan(graph, snapshot, result) -> Plan:
    validation = validate(graph, snapshot, result.routes)
    structural_errors = set(v.code for v in validation.violations) - {"unserved_required_passes"}
    if structural_errors:
        raise RuntimeError(f"solver emitted invalid actions: {sorted(structural_errors)}")
    feasible = not validation.violations
    certificate = shift_budget_certificate(graph, snapshot) if not feasible else None
    feasibility = ("feasible_candidate" if feasible else
                   "proven_infeasible" if certificate else "no_feasible_candidate")
    digest = sha256((snapshot.input_hash + result.model_dump_json()).encode()).hexdigest()
    return Plan(
        plan_id=digest[:24], scenario_id=snapshot.scenario_id,
        graph_version=graph.version, as_of=snapshot.as_of, input_hash=snapshot.input_hash,
        status="ok" if feasible else "partial",
        feasibility_status=feasibility, infeasibility_certificate=certificate,
        solver=result, validation=validation, metrics=evaluate(graph, snapshot.tariff, result.routes),
        provenance={"graph": "osm", "operational_profile": "assumed", "tariff": snapshot.tariff.source},
        warnings=("Road geometry is OSM. Vehicle restrictions and field conditions require verification.",
                  "Weather, observed pavement state, safety and capacity metrics are unavailable.",
                  "Gap, when available, applies to the generated route pool only."),
    )


def geojson(graph, plan: Plan) -> dict:
    def iso(seconds):
        return (plan.as_of + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    features = []
    for route in plan.solver.routes:
        for index, action in enumerate(route.actions):
            edge = graph.edges.get(action.edge_id)
            start, end = graph.nodes[action.from_node], graph.nodes[action.to_node]
            geometry = ({"type": "LineString", "coordinates": [start, end]} if edge
                        else {"type": "Point", "coordinates": start})
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    **action.model_dump(), "vehicle_id": route.vehicle_id, "sequence": index,
                    "start_utc": iso(action.start_s), "end_utc": iso(action.end_s),
                    "osm_way_id": edge.way_id if edge else None,
                    "physical_road_id": edge.physical_road_id if edge else None,
                    "name": edge.name if edge else None,
                },
            })
    return {"type": "FeatureCollection", "features": features, "status": plan.feasibility_status,
            "simulation_only": plan.simulation_only, "attribution": OSM_ATTRIBUTION,
            "license_url": OSM_LICENSE_URL, "graph_version": graph.version, "plan_id": plan.plan_id}
