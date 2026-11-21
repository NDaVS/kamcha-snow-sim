import unittest
from pathlib import Path
from time import monotonic

from snowops.carp_compare import external_routes, instance, project
from snowops.contracts import PrioritySite
from snowops.infrastructure import Infrastructure
from snowops.osm import load_osm
from snowops.reporting import make_plan
from snowops.solvers import GreedySolver, PoolSolver, RouteBuilder
from snowops.study import pareto, service_quality
from snowops.validation import validate


class ScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "data/pkc/city.osm.gz"
        cls.graph = load_osm(path)
        cls.infrastructure = Infrastructure(cls.graph)

    def test_large_greedy_plan_obeys_ranked_windows_caps_and_capabilities(self):
        snapshot = self.infrastructure.scenario(300)
        result = GreedySolver().solve(self.graph, snapshot, 30)
        report = validate(self.graph, snapshot, result.routes)
        self.assertFalse(report.violations)
        self.assertEqual(report.completed_passes, 300)
        self.assertEqual(len({v.profile_id for v in snapshot.fleet}), 3)
        self.assertTrue({"hospital", "fire_station", "school", "substation", "arterial_junction"}
                        <= {site.kind for site in snapshot.priority_sites})
        _, rows = service_quality(snapshot, result.routes)
        self.assertTrue(any(row["kind"] == "fire_station" and row["status"] == "unmapped" for row in rows))
        self.assertTrue(any(row["kind"] == "hospital" and row["status"] == "on_time" for row in rows))

    def test_priority_site_beats_additional_tasks_of_same_rank(self):
        snapshot = instance(self.infrastructure, 5)
        first, second, *others = snapshot.tasks
        site = PrioritySite(site_id="test-hospital", kind="hospital", name="Hospital",
                            lon_lat=None, rank=1, deadline_s=86400,
                            approach_edges=(first.edge_id, second.edge_id),
                            task_ids=(first.task_id, second.task_id))
        snapshot = snapshot.model_copy(update={"fleet": snapshot.fleet[:1], "priority_sites": (site,)})
        builder = RouteBuilder(self.graph, snapshot)
        complete_access = builder.build(snapshot.fleet[0], snapshot.tasks[:2])
        more_tasks = builder.build(snapshot.fleet[0], others)
        routes, status, _ = PoolSolver._select(self.graph, snapshot, (complete_access, more_tasks),
                                               (more_tasks,), monotonic() + 10)
        self.assertEqual(status, "pool_optimal")
        self.assertEqual(routes, (complete_access,))

    def test_common_projection_reprices_expanded_routes(self):
        snapshot = instance(self.infrastructure, 12)
        matrix, correction = project(self.graph, snapshot)
        result = external_routes("test", self.graph, snapshot, [range(1, 13)], "test",
                                 matrix, correction)
        self.assertFalse(make_plan(self.graph, snapshot, result).validation.violations)

    def test_frontier_keeps_quality_cost_tradeoffs(self):
        keys = ("reference_cost_rub", "reference_critical_sites_missed",
                "reference_critical_tasks_missed", "reference_all_tasks_missed")
        rows = [dict(zip(keys, point)) for point in ((5, 0, 0, 0), (2, 1, 1, 3), (6, 1, 1, 3))]
        self.assertEqual(pareto(rows), rows[:2])


if __name__ == "__main__":
    unittest.main()
