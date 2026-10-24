"""Common ownership-map regressions, vendored with the policy utility."""

import copy
import json
import unittest
from pathlib import Path

from scripts.ownership_policy import MAP_PATH, parse_map, resolve


class OwnershipPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = parse_map(
            (Path(__file__).resolve().parents[1] / MAP_PATH).read_text()
        )

    def test_app_intelligence_and_platform_are_distinct(self):
        for path in (
            "app/main.py",
            "app/agent/service.py",
            "app/agent/conversation.py",
            "app/agent/semantic_answer.py",
            "app/static/new/nested/icon.svg",
            "app/templates/new/view.html",
        ):
            with self.subTest(path=path):
                rule = resolve(self.policy, path)
                self.assertEqual(
                    (rule["owner"], rule["private_presence"]), ("public", "frozen")
                )
        for path in self.policy["required_private_paths"] + [
            "prompts/new/system.txt",
            "evals/cases/new.json",
        ]:
            self.assertEqual(resolve(self.policy, path)["owner"], "private")
        for path in (
            "app/agent/semantics.py",
            "app/agent/new_engine.py",
            "app/repository/_bigquery.py",
            "season.yml",
            "dags/new.py",
        ):
            self.assertEqual(
                resolve(self.policy, path)["private_presence"], "forbidden"
            )

    def test_public_tests_and_private_cases_have_different_owners(self):
        self.assertEqual(
            resolve(self.policy, "tests_browser/new.spec.js")["owner"], "public"
        )
        self.assertEqual(
            resolve(self.policy, "tests/test_new_public_behavior.py")["owner"], "public"
        )
        self.assertEqual(
            resolve(self.policy, "tests/test_product_runtime.py")["owner"], "private"
        )
        self.assertEqual(
            resolve(self.policy, "evals/cases/regression.json")["owner"], "private"
        )

    def test_shared_policy_tools_and_tests_remain_public(self):
        for path in (
            "scripts/agent_startup.py",
            "scripts/ownership_policy.py",
            "tests/test_agent_startup.py",
            "tests/test_ownership_policy.py",
        ):
            rule = resolve(self.policy, path)
            self.assertEqual(
                (rule["owner"], rule["private_presence"]), ("public", "vendored")
            )

    def test_specific_rule_overrides_broad_app_pattern(self):
        self.assertEqual(
            resolve(self.policy, "app/agent/planner.py")["private_presence"], "owned"
        )
        self.assertIsNone(resolve(self.policy, "unmapped/new.py"))

    def test_equal_priority_overlap_is_rejected(self):
        self.policy["rules"].append(
            dict(
                patterns=["app/agent/*.py"],
                owner="public",
                private_presence="forbidden",
                priority=100,
                description="Conflicting rule",
            )
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_map(json.dumps(self.policy))

    def test_invalid_maps_and_required_ownership_fail_closed(self):
        for edit in (
            lambda p: p.update(schema="old"),
            lambda p: p.update(rules=[]),
            lambda p: p["rules"][0].update(priority="100"),
            lambda p: p["rules"][0].update(patterns=["../outside/**"]),
            lambda p: p["rules"][0].update(private_presence="frozen"),
            lambda p: p.update(required_private_paths=["app/agent/semantics.py"]),
        ):
            policy = copy.deepcopy(self.policy)
            edit(policy)
            with self.assertRaises(ValueError):
                parse_map(json.dumps(policy))
        for content in ("not JSON", "[]", "null"):
            with self.assertRaises(ValueError):
                parse_map(content)

    def test_duplicate_patterns_and_nonrelative_paths_are_rejected(self):
        self.policy["rules"][1]["patterns"].append("app/agent/planner.py")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_map(json.dumps(self.policy))
        for path in (
            "/app/main.py",
            "app/../main.py",
            "app//main.py",
            "app\\main.py",
            "app/*.py",
        ):
            with self.assertRaises(ValueError):
                resolve(self.policy, path)
