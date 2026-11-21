"""Cost reconstruction in integer milli-RUB to avoid per-action rounding."""
from .contracts import VehicleRoute
from .osm import RoadGraph


def evaluate(graph: RoadGraph, tariff, routes: tuple[VehicleRoute, ...]) -> dict:
    lengths = {"service": 0, "deadhead": 0, "return": 0}
    elapsed_s = wait_s = 0
    for route in routes:
        for action in route.actions:
            seconds = action.end_s - action.start_s
            elapsed_s += seconds
            if action.mode == "wait":
                wait_s += seconds
            else:
                lengths[action.mode] += graph.edges[action.edge_id].length_m
    deadhead_m = lengths["deadhead"] + lengths["return"]
    service_m = lengths["service"]
    costs = {
        "deadhead_mrub": deadhead_m * tariff.deadhead_rub_km,
        "service_mrub": service_m * tariff.service_rub_km,
        "vehicle_time_mrub": elapsed_s * tariff.elapsed_mrub_s,
    }
    return {
        "cost_components_mrub": costs,
        "C_var_rub": sum(costs.values()) / 1000,
        "C_fixed_rub": tariff.fixed_rub,
        "deadhead_km": deadhead_m / 1000,
        "return_km": lengths["return"] / 1000,
        "service_km": service_m / 1000,
        "vehicle_seconds": elapsed_s,
        "waiting_seconds": wait_s,
        "vehicles_used": sum(bool(route.actions) for route in routes),
        "makespan_s": max((r.actions[-1].end_s for r in routes if r.actions), default=0),
    }
