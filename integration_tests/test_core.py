import unittest
from pathlib import Path

from snowops.osm import load_osm
from snowops.pilot import scenario_factory
from snowops.reporting import geojson, make_plan
from snowops.routing import Routing
from snowops.solvers import GreedySolver
from snowops.validation import validate


class PilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "data/pkc/network.osm.gz"
        cls.graph = load_osm(path)
        cls.snapshot = scenario_factory(cls.graph)
        cls.result = GreedySolver().solve(cls.graph, cls.snapshot, 30)

    def test_real_graph_full_coverage_and_round_trip(self):
        plan = make_plan(self.graph, self.snapshot, self.result)
        self.assertEqual(plan.validation.required_passes, 28)
        self.assertEqual(plan.validation.completed_passes, 28)
        self.assertFalse(plan.validation.violations)
        self.assertGreater(plan.metrics.deadhead_km, plan.metrics.return_km)
        self.assertGreater(plan.metrics.return_km, 0)
        self.assertEqual(plan.metrics.C_var_rub * 1000, sum(plan.metrics.cost_components_mrub.values()))
        features = geojson(self.graph, plan)["features"]
        self.assertEqual(sum(f["properties"]["mode"] == "service" for f in features), 28)
        self.assertTrue(features[0]["properties"]["start_utc"].endswith("Z"))

    def test_turn_override_blocks_existing_transition_in_replay_and_routing(self):
        actions = self.result.routes[0].actions
        pair = actions[0].edge_id, actions[1].edge_id
        modified = self.snapshot.model_copy(update={"forbidden_turns": (pair,)})
        self.assertNotIn(pair[1], Routing(self.graph, modified).network.successors(pair[0]))
        replay = validate(self.graph, modified, self.result.routes)
        self.assertIn("forbidden_turn", {v.code for v in replay.violations})

    def test_deadhead_does_not_count_as_service(self):
        route = self.result.routes[0]
        index = next(i for i, action in enumerate(route.actions) if action.mode == "service")
        action = route.actions[index]
        transit = action.model_copy(update={"mode": "deadhead", "task_id": None})
        modified = route.model_copy(update={"actions": route.actions[:index] + (transit,) + route.actions[index + 1:]})
        replay = validate(self.graph, self.snapshot, (modified,) + self.result.routes[1:])
        self.assertIn(action.task_id, replay.unserved_tasks)
        self.assertEqual(replay.completed_passes, 27)

    def test_reversing_an_arc_invalidates_a_plan(self):
        route = self.result.routes[0]
        action = route.actions[0]
        flipped = action.model_copy(update={"from_node": action.to_node, "to_node": action.from_node})
        invalid = route.model_copy(update={"actions": (flipped,) + route.actions[1:]})
        replay = validate(self.graph, self.snapshot, (invalid,) + self.result.routes[1:])
        self.assertIn("wrong_or_unknown_directed_edge", {v.code for v in replay.violations})

    def test_resource_shortage_has_independent_bound(self):
        snapshot = scenario_factory(self.graph, fleet_size=1, shift_s=120)
        result = GreedySolver().solve(self.graph, snapshot, 30)
        plan = make_plan(self.graph, snapshot, result)
        self.assertEqual(plan.feasibility_status, "proven_infeasible")
        self.assertGreater(plan.infeasibility_certificate.minimum_service_seconds, 120)
        self.assertTrue(plan.validation.unserved_tasks)


if __name__ == "__main__":
    unittest.main()
