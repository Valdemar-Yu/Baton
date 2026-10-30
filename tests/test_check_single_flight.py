import copy
import builtins
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "baton", "scripts"))
import baton  # noqa: E402


class FakeProject:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.root = tempfile.mkdtemp(prefix="baton-check-")
        self.state_dir = self.root

    def load_state(self, task):
        return copy.deepcopy(self.state)

    read_state = load_state

    def legacy_state_in_flight(self, task):
        return False

    def update_state(self, task, fn):
        fn(self.state)
        return copy.deepcopy(self.state)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def leg_path(self, task, n, name):
        return self.p(f"leg-{n}-{name}")


class FailRunnerWritebackProject(FakeProject):
    def __init__(self, state):
        super().__init__(state)
        self.update_count = 0

    def update_state(self, task, fn):
        self.update_count += 1
        if self.update_count == 3:
            raise OSError("state writeback failed")
        return super().update_state(task, fn)


def running_state(**leg_updates):
    leg = {"n": 1, "status": "running", "started_at": baton.now() - 120,
           "next_check_at": baton.now() - 1,
           "check_interval_minutes": 10, "alerts": [], "alert_keys": []}
    leg.update(leg_updates)
    return {"task": "t", "status": "running", "legs": [leg],
            "config_snapshot": {"mode": "standard", "monitor": {"enabled": True},
                                 "supervision": {"first_check_minutes": 10}}}


class CheckSingleFlightTests(unittest.TestCase):
    def test_parent_start_window_is_single_flight(self):
        project = FakeProject(running_state())
        popen = SimpleNamespace(pid=101, poll=lambda: 0)
        with patch.object(baton, "build_check_dossier", return_value="t-leg-001-c01"), \
                patch.object(baton.platform_support, "detached_popen", return_value=popen) as detached, \
                patch.object(baton, "verify_dossier", return_value=(True, "")):
            self.assertTrue(baton.maybe_start_monitor_check(project, "t", project.load_state("t"),
                                                              project.load_state("t")["legs"][0], force=True))
            self.assertFalse(baton.maybe_start_monitor_check(project, "t", project.load_state("t"),
                                                               project.load_state("t")["legs"][0], force=True))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "starting")
        self.assertTrue(project.state["legs"][0]["check_pending"])
        detached.assert_called_once()

    def test_child_running_preserves_runner_identity_for_liveness(self):
        state = running_state(monitor_check={"id": "t-leg-001-c01", "status": "starting",
                                              "pid": 101, "runner_pid": 101,
                                              "marker": "_monitor_check t t-leg-001-c01",
                                              "started_at": baton.now() - baton.STARTUP_GRACE_SECONDS - 1})
        baton._set_check_running(state, 1, "t-leg-001-c01", 202, 1, baton.now())
        check = state["legs"][0]["monitor_check"]
        self.assertEqual(check["pid"], 101)
        self.assertEqual(check["runner_pid"], 101)
        self.assertEqual(check["marker"], "_monitor_check t t-leg-001-c01")
        project = FakeProject(state)
        with patch.object(baton, "pid_alive", return_value=True) as alive:
            baton.refresh_runner_liveness(project, "t", project.load_state("t"))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "running")
        alive.assert_any_call(101, "_monitor_check t t-leg-001-c01")

    def test_capacity_wait_uses_live_runner_when_codex_pid_is_dead(self):
        project = FakeProject(running_state(monitor_check={"id": "t-leg-001-c01", "status": "running",
                                                            "pid": 101, "runner_pid": 202,
                                                            "marker": "_monitor_check t t-leg-001-c01",
                                                            "started_at": baton.now() - baton.STARTUP_GRACE_SECONDS - 1,
                                                            "capacity_wait": {"retry": 1}}))
        leg = project.load_state("t")["legs"][0]
        self.assertFalse(baton.monitor_check_due(project.load_state("t"), leg, now_value=baton.now() + 9999))
        with patch.object(baton, "pid_alive", side_effect=lambda pid, marker=None: pid == 202), \
                patch.object(baton, "build_check_dossier") as build, \
                patch.object(baton.platform_support, "detached_popen") as detached:
            baton.refresh_runner_liveness(project, "t", project.load_state("t"))
            self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "running")
            self.assertFalse(baton.maybe_start_monitor_check(project, "t", project.load_state("t"), leg, force=True))
        build.assert_not_called()
        detached.assert_not_called()

    def test_starting_without_runner_pid_within_grace_is_not_failed(self):
        state = running_state(monitor_check={"id": "t-leg-001-c01", "status": "starting",
                                              "pid": None, "runner_pid": None,
                                              "marker": "_monitor_check t t-leg-001-c01",
                                              "started_at": baton.now() - baton.STARTUP_GRACE_SECONDS + 1})
        project = FakeProject(state)
        with patch.object(baton, "pid_alive", return_value=False):
            baton.refresh_runner_liveness(project, "t", project.load_state("t"))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "starting")

    def test_starting_without_runner_pid_after_grace_is_failed(self):
        state = running_state(monitor_check={"id": "t-leg-001-c01", "status": "starting",
                                              "pid": None, "runner_pid": None,
                                              "marker": "_monitor_check t t-leg-001-c01",
                                              "started_at": baton.now() - baton.STARTUP_GRACE_SECONDS - 1})
        project = FakeProject(state)
        with patch.object(baton, "pid_alive", return_value=False):
            baton.refresh_runner_liveness(project, "t", project.load_state("t"))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "failed")

    def test_starting_runner_exit_after_grace_is_failed(self):
        state = running_state(monitor_check={"id": "t-leg-001-c01", "status": "starting",
                                              "pid": 202, "runner_pid": 202,
                                              "marker": "_monitor_check t t-leg-001-c01",
                                              "started_at": baton.now() - baton.STARTUP_GRACE_SECONDS - 1})
        project = FakeProject(state)
        with patch.object(baton, "pid_alive", return_value=False):
            baton.refresh_runner_liveness(project, "t", project.load_state("t"))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "failed")
        self.assertIn("意外退出", project.state["legs"][0]["monitor_check"]["error"])

    def test_starting_launch_error_is_failed_with_backoff(self):
        project = FakeProject(running_state())
        with patch.object(baton, "build_check_dossier", return_value="t-leg-001-c01"), \
                patch.object(baton.platform_support, "detached_popen", side_effect=OSError("spawn failed")):
            self.assertFalse(baton.maybe_start_monitor_check(project, "t", project.load_state("t"),
                                                              project.load_state("t")["legs"][0], force=True))
        check = project.state["legs"][0]["monitor_check"]
        self.assertEqual(check["status"], "failed")
        self.assertIsNone(check["pid"])
        self.assertIn("spawn failed", check["error"])

    def test_starting_log_open_error_is_failed(self):
        project = FakeProject(running_state())
        with patch.object(baton, "build_check_dossier", return_value="t-leg-001-c01"), \
                patch.object(builtins, "open", side_effect=OSError("log open failed")):
            self.assertFalse(baton.maybe_start_monitor_check(project, "t", project.load_state("t"),
                                                              project.load_state("t")["legs"][0], force=True))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "failed")
        self.assertIn("log open failed", project.state["legs"][0]["monitor_check"]["error"])

    def test_starting_state_writeback_error_is_failed(self):
        project = FailRunnerWritebackProject(running_state())
        popen = SimpleNamespace(pid=101, poll=lambda: 0)
        with patch.object(baton, "build_check_dossier", return_value="t-leg-001-c01"), \
                patch.object(baton.platform_support, "detached_popen", return_value=popen):
            self.assertFalse(baton.maybe_start_monitor_check(project, "t", project.load_state("t"),
                                                              project.load_state("t")["legs"][0], force=True))
        self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], "failed")
        self.assertIn("state writeback failed", project.state["legs"][0]["monitor_check"]["error"])

    def test_child_without_parent_runner_uses_own_pid_and_marker(self):
        state = running_state(task="wrong", monitor_check={"id": "t-leg-001-c01", "status": "starting"})
        state["task"] = "t"
        with patch.object(baton.os, "getpid", return_value=303):
            baton._set_check_running(state, 1, "t-leg-001-c01", 404, 1, baton.now())
        check = state["legs"][0]["monitor_check"]
        self.assertEqual(check["pid"], 303)
        self.assertEqual(check["runner_pid"], 303)
        self.assertEqual(check["marker"], "_monitor_check t t-leg-001-c01")

    def test_parent_pid_writeback_does_not_overwrite_terminal_check(self):
        for terminal in ("completed", "failed"):
            with self.subTest(terminal=terminal):
                project = FakeProject(running_state())
                popen = SimpleNamespace(pid=101, poll=lambda: 0)
                def terminal_popen(*args, **kwargs):
                    project.state["legs"][0]["monitor_check"] = {
                        "id": "t-leg-001-c01", "status": terminal, "pid": None,
                        "marker": "_monitor_check t t-leg-001-c01"}
                    return popen
                with patch.object(baton, "build_check_dossier", return_value="t-leg-001-c01"), \
                        patch.object(baton.platform_support, "detached_popen", side_effect=terminal_popen):
                    self.assertTrue(baton.maybe_start_monitor_check(project, "t", project.load_state("t"),
                                                                      project.load_state("t")["legs"][0], force=True))
                self.assertEqual(project.state["legs"][0]["monitor_check"]["status"], terminal)
                self.assertIsNone(project.state["legs"][0]["monitor_check"]["pid"])

    def test_failed_check_has_backoff_and_force_only_marks_pending(self):
        project = FakeProject(running_state(monitor_check={"id": "t-leg-001-c01", "status": "running", "pid": 101}))
        baton._check_failure_update(project, "t", 1, "t-leg-001-c01", "boom")
        state = project.load_state("t")
        self.assertFalse(baton.monitor_check_due(state, state["legs"][0], now_value=baton.now()))
        with patch.object(baton, "build_check_dossier") as build, \
                patch.object(baton.platform_support, "detached_popen") as detached:
            self.assertFalse(baton.maybe_start_monitor_check(project, "t", state, state["legs"][0], force=True))
        build.assert_not_called()
        detached.assert_not_called()
        self.assertTrue(project.state["legs"][0]["check_pending"])
        self.assertFalse(baton.monitor_check_due(project.load_state("t"), project.load_state("t")["legs"][0]))


if __name__ == "__main__":
    unittest.main()
