import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class AcceptanceParamIdTests(unittest.TestCase):
    def test_all_parameterized_cases_pass(self):
        tests = [
            {"nodeid": "tests/rules/yaml_test_cases_test.py::test__rule_test_case[L060]",
             "status": "passed"},
            {"nodeid": "tests/rules/yaml_test_cases_test.py::test__rule_test_case[L061]",
             "status": "passed"},
        ]

        self.assertEqual(
            baton._acceptance_failures(
                tests, ["tests/rules/yaml_test_cases_test.py::test__rule_test_case"]),
            [],
        )

    def test_failed_parameterized_case_is_reported(self):
        tests = [
            {"nodeid": "tests/rules/yaml_test_cases_test.py::test__rule_test_case[L060]",
             "status": "passed"},
            {"nodeid": "tests/rules/yaml_test_cases_test.py::test__rule_test_case[L061]",
             "status": "failed"},
        ]

        self.assertEqual(
            baton._acceptance_failures(
                tests, ["tests/rules/yaml_test_cases_test.py::test__rule_test_case"]),
            [{"nodeid": "tests/rules/yaml_test_cases_test.py::test__rule_test_case[L061]",
              "reason": "failed"}],
        )

    def test_function_prefix_does_not_match_other_parameterized_function(self):
        tests = [{
            "nodeid": "tests/test_rules.py::test_func_other[case]",
            "status": "failed",
        }]

        self.assertEqual(
            baton._acceptance_failures(tests, ["tests/test_rules.py::test_func"]),
            [{"nodeid": "tests/test_rules.py::test_func", "reason": "not collected"}],
        )

    def test_original_exact_slash_and_colon_matches_still_work(self):
        tests = [
            {"nodeid": "tests/acceptance", "status": "failed"},
            {"nodeid": "tests/acceptance/test_a.py", "status": "skipped"},
            {"nodeid": "tests/acceptance::TestCase::test_case", "status": "xfailed"},
            {"nodeid": "tests/acceptance-other::test_case", "status": "error"},
        ]

        failures = baton._acceptance_failures(tests, ["tests/acceptance"])

        self.assertEqual(
            failures,
            [
                {"nodeid": "tests/acceptance", "reason": "failed"},
                {"nodeid": "tests/acceptance/test_a.py", "reason": "skipped"},
                {"nodeid": "tests/acceptance::TestCase::test_case", "reason": "xfailed"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
