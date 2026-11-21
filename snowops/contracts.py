"""Versioned names, units and boundaries shared by planners and consumers."""
from datetime import timedelta
from hashlib import sha256
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[int, Field(gt=0)]
Nonnegative = Annotated[int, Field(ge=0)]
Source = Literal["observed", "official", "osm", "assumed", "synthetic"]
Mode = Literal["deadhead", "service", "wait", "return"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CityProfile(Contract):
    city_id: str
    timezone: str
    graph_version: str
    crs: Literal["EPSG:4326"] = "EPSG:4326"


class ServiceTask(Contract):
    task_id: str
    edge_id: str
    priority: Nonnegative
    required_passes: Positive = 1
    earliest_s: Nonnegative = 0
    deadline_s: Positive
    capability: Literal["plow", "compact_plow", "grader"] = "plow"
    source: Source = "synthetic"


class Vehicle(Contract):
    vehicle_id: str
    depot_node: Positive
    shift_start_s: Nonnegative = 0
    shift_end_s: Positive
    capabilities: tuple[str, ...] = ("plow",)
    profile_id: Literal["demo-plow", "truck-plow", "compact-plow", "grader"] = "demo-plow"
    deadhead_kmh: Positive = 25
    service_kmh: Positive = 12
    source: Source = "synthetic"


class Tariff(Contract):
    version: str
    deadhead_rub_km: Nonnegative
    service_rub_km: Nonnegative
    elapsed_mrub_s: Nonnegative
    fixed_rub: Nonnegative | None = None
    source: Source = "synthetic"


class PrioritySite(Contract):
    site_id: str
    kind: str
    name: str
    lon_lat: tuple[float, float] | None
    rank: Positive
    deadline_s: Positive
    location_source: Source | None = None
    reference_url: str | None = None
    access_node: Positive | None = None
    snap_m: Nonnegative | None = None
    approach_edges: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    source: Source = "osm"


class ScenarioSnapshot(Contract):
    schema_version: Literal["0.1"] = "0.1"
    scenario_id: str
    city: CityProfile
    as_of: AwareDatetime
    constraints_version: str
    seed: int
    tasks: tuple[ServiceTask, ...] = Field(min_length=1)
    fleet: tuple[Vehicle, ...] = Field(min_length=1)
    tariff: Tariff
    closed_edges: tuple[str, ...] = ()
    forbidden_turns: tuple[tuple[str, str], ...] = ()
    observations: tuple[()] = ()
    weather: None = None
    data_mode: Literal["mixed-with-provenance"] = "mixed-with-provenance"

    priority_sites: tuple[PrioritySite, ...] = ()
    respect_osm_speed_limits: bool = False

    @model_validator(mode="after")
    def check_snapshot(self):
        if self.as_of.utcoffset() != timedelta(0):
            raise ValueError("as_of must be UTC")
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate task IDs")
        if len({vehicle.vehicle_id for vehicle in self.fleet}) != len(self.fleet):
            raise ValueError("duplicate vehicle IDs")
        if any(task.earliest_s >= task.deadline_s for task in self.tasks):
            raise ValueError("empty service window")
        if any(vehicle.shift_start_s >= vehicle.shift_end_s for vehicle in self.fleet):
            raise ValueError("empty shift")
        return self

    @property
    def input_hash(self) -> str:
        # Empty new fields preserve the canonical v0.1 fingerprint of archived plans.
        exclude = set()
        if not self.priority_sites:
            exclude.add("priority_sites")
        if not self.respect_osm_speed_limits:
            exclude.add("respect_osm_speed_limits")
        return sha256(self.model_dump_json(exclude=exclude).encode()).hexdigest()


class Action(Contract):
    mode: Mode
    from_node: Positive
    to_node: Positive
    edge_id: str | None = None
    task_id: str | None = None
    start_s: Nonnegative
    end_s: Positive


class VehicleRoute(Contract):
    vehicle_id: str
    actions: tuple[Action, ...]


class SolverResult(Contract):
    solver_id: str
    solver_version: str
    routes: tuple[VehicleRoute, ...]
    termination: str
    pool_size: Nonnegative = 0
    pool_gap: float | None = None


class Violation(Contract):
    code: str
    vehicle_id: str | None = None
    task_id: str | None = None
    action_index: Nonnegative | None = None
    missing_passes: Positive | None = None


class Validation(Contract):
    violations: tuple[Violation, ...]
    unserved_tasks: tuple[str, ...]
    completed_passes: Nonnegative
    required_passes: Positive
    late_tasks: tuple[str, ...]


class HaulRequest(Contract):
    input_hash: str
    load_sites: tuple[str, ...]
    disposal_sites: tuple[str, ...]


class HaulResult(Contract):
    status: Literal["not_implemented"] = "not_implemented"


class HaulSolver(Protocol):
    def solve(self, request: HaulRequest) -> HaulResult: ...


class Metrics(Contract):
    cost_components_mrub: dict[str, Nonnegative]
    C_var_rub: float
    C_fixed_rub: Nonnegative | None
    deadhead_km: float
    return_km: float
    service_km: float
    vehicle_seconds: Nonnegative
    waiting_seconds: Nonnegative
    vehicles_used: Nonnegative
    makespan_s: Nonnegative


class InfeasibilityCertificate(Contract):
    kind: Literal["service_time_exceeds_fleet_shift_budget"] = "service_time_exceeds_fleet_shift_budget"
    minimum_service_seconds: Positive
    total_shift_seconds: Positive


class Plan(Contract):
    schema_version: Literal["0.1"] = "0.1"
    plan_id: str
    scenario_id: str
    graph_version: str
    as_of: AwareDatetime
    input_hash: str
    status: Literal["ok", "partial", "invalid_input"]
    feasibility_status: Literal["feasible_candidate", "no_feasible_candidate", "proven_infeasible"]
    infeasibility_certificate: InfeasibilityCertificate | None = None
    simulation_only: Literal[True] = True
    solver: SolverResult
    validation: Validation
    metrics: Metrics
    gap_if_proven: None = None
    Q_dry: None = None
    capacity_ratio: None = None
    risk_proxy: None = None
    haul: HaulResult = Field(default_factory=HaulResult)
    provenance: dict[str, Source]
    warnings: tuple[str, ...]
