"""Real native Windows checks (no model calls, no user settings)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "baton" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import baton
import baton_platform as win

win.utf8_stdio()


@unittest.skipUnless(os.name == "nt", "native Windows only")
class WindowsTests(unittest.TestCase):
    def test_atomic_state_replace_waits_for_windows_reader(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            baton.write_json(str(path), {'revision': 1})
            reader = path.open('rb')
            release = threading.Timer(0.1, reader.close)
            release.start()
            try:
                baton.write_json(str(path), {'revision': 2})
            finally:
                reader.close()
                release.join()
            self.assertEqual(baton.read_json(str(path)), {'revision': 2})
            errors = []
            def concurrent_reader():
                try:
                    for _ in range(150):
                        self.assertIn('revision', json.loads(win.read_text(path)))
                except BaseException as exc:
                    errors.append(exc)
            reader_thread = threading.Thread(target=concurrent_reader)
            reader_thread.start()
            try:
                for revision in range(100):
                    baton.write_json(str(path), {'revision': revision})
            finally:
                reader_thread.join(timeout=5)
            self.assertFalse(reader_thread.is_alive())
            self.assertFalse(errors, errors)
            denied = OSError('persistent sharing violation')
            denied.winerror = 32
            with mock.patch.object(win.os, 'replace', side_effect=denied) as replace:
                with self.assertRaises(OSError):
                    win.atomic_replace('source', 'destination', timeout=0)
                self.assertEqual(replace.call_count, 1)

    def test_windows_defaults_and_hermes_fail_before_start(self):
        import contextlib
        import io
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as directory:
            project = baton.Project(directory)
            self.assertEqual(project.config['executor']['backend'], 'codex')
            self.assertEqual(project.config['memory']['backend'], 'local')
            self.assertEqual(project.config['freeze']['check_seconds'], 60)
            messages = []
            with mock.patch.object(baton, 'memory_venv_python', side_effect=AssertionError('local must not probe mem0')):
                self.assertFalse(baton.doctor_memory(project, project.config, lambda *m: messages.append(m)))
            self.assertEqual(messages[0][0], 'WARN')
            baton.write_json(project.baton_config_path, {'executor': {'backend': 'hermes'}})
            with self.assertRaisesRegex(baton.ConfigError, 'unsupported on Windows'):
                baton.cmd_start(project, Namespace(task='unused'))
            out = io.StringIO()
            with mock.patch.object(baton, 'safe_quota', return_value=None), contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as stopped:
                    baton.cmd_doctor(project, Namespace(monitor_probe=False))
            self.assertEqual(stopped.exception.code, 1)
            self.assertIn('[FAIL] ' + baton.WINDOWS_HERMES_ERROR, out.getvalue())

    def test_shadow_repository_external_git_dir_and_contaminated_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project space \u6d4b\u8bd5'
            root.mkdir()
            (root / 'source.txt').write_text('snapshot\n', encoding='utf-8')
            with mock.patch.dict(os.environ, BATON_HOME=str(Path(directory) / 'external'),
                                 GIT_DIR=str(Path(directory) / 'wrong.git'), GIT_WORK_TREE=str(Path(directory) / 'wrong')):
                project = baton.Project(str(root))
                store = project.store()
                ref = store.snapshot('Windows shadow')
                self.assertEqual(store.git('show', ref + ':source.txt'), 'snapshot\n')
                self.assertFalse((root / '.git').exists())
                self.assertFalse(Path(project.snap_git_dir).is_relative_to(root))

    def test_v2_state_launch_shadow_and_ledger_locks_exclude_children(self):
        from baton_memory.ledger import MemoryLedger, _file_lock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            with mock.patch.dict(os.environ, BATON_HOME=str(Path(directory) / 'state')):
                project = baton.Project(str(root))
                project.update_state('locks', lambda s: s.update(task='locks'))
                ledger = MemoryLedger(project)
                paths = [project.state_lock_path('locks'), project.launch_lock_path(),
                         str(Path(project.state_dir) / 'snap.lock'),
                         str(Path(baton.ledger_dir(project)) / 'ledger.lock'), ledger.lock_path]
                for path in paths:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    code = "import sys; sys.path.insert(0,sys.argv[1]); import baton_platform as w; f=open(sys.argv[2],'a+b'); print(w.lock_file(f,blocking=False))"
                    with self.subTest(path=path), baton._locked(path):
                        result = subprocess.check_output([sys.executable, '-c', code, str(SCRIPTS), path], text=True)
                        self.assertEqual(result.strip(), 'False')
                with _file_lock(ledger.lock_path, exclusive=False):
                    result = subprocess.check_output([sys.executable, '-c', code, str(SCRIPTS), ledger.lock_path], text=True)
                    self.assertEqual(result.strip(), 'False')

    def test_detached_v2_monitor_launch_records_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            project = baton.Project(directory)
            project.update_state('monitor', lambda s: s.update(legs=[{'n': 1}]))
            original = win.detached_popen
            launched = []
            def fake_model(args, **kwargs):
                launched.append(args)
                proc = original([sys.executable, '-c', 'import time; time.sleep(0.5)'], **kwargs)
                self.addCleanup(self.reap, proc)
                return proc
            with mock.patch.object(win, 'detached_popen', side_effect=fake_model):
                baton._start_monitor_process(project, 'monitor', 'monitor-leg-001')
            state = project.read_state('monitor')['legs'][0]['monitor']
            self.assertTrue(win.pid_alive(state['pid']))
            self.assertIn('_monitor', launched[0])
            self.assertEqual(state['marker'], '_monitor_run monitor monitor-leg-001')
            for proc in baton._DETACHED_RUNNERS:
                if proc.pid == state['pid']:
                    proc.wait(timeout=5)
            baton.reap_detached_runners()

    def test_frozen_check_timer_interrupts_detached_executor(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as directory:
            project = baton.Project(directory)
            Path(project.runs_dir('timer')).mkdir(parents=True)
            frozen = Path(directory) / 'conftest.py'
            frozen.write_text('before\n', encoding='utf-8')
            scope = {'allow': [], 'deny': [], 'acceptance': [], 'frozen': [], 'verify': 'none'}
            cfg = baton.deep_merge(project.config, {'monitor': {'enabled': False}, 'freeze': {'check_seconds': 0.1}})
            manifest = baton.write_freeze_baseline(project, 'timer', scope, cfg)
            leg = dict(n=1, status='running', started_at=time.time(), log_size_at_start=0)
            for key, suffix in (('prompt', 'prompt.md'), ('events', 'events.jsonl'), ('stderr', 'stderr.log'), ('last', 'last.json')):
                leg[key] = project.leg_path('timer', 1, suffix)
            Path(leg['prompt']).write_text('offline', encoding='utf-8')
            leg['cmd'] = [sys.executable, '-c', "import pathlib,time; pathlib.Path('conftest.py').write_text('changed'); time.sleep(20)"]
            project.update_state('timer', lambda s: s.update(legs=[leg], status='running', scope=scope,
                                 freeze_manifest=manifest, config_snapshot=cfg))
            with mock.patch.object(baton, 'safe_quota', return_value=None):
                baton.cmd_run(project, Namespace(task='timer', n='1'))
            state = project.read_state('timer')
            self.assertEqual(state['legs'][0]['status'], 'violation')
            self.assertTrue(state['legs'][0]['codex_exited'])

    def sleeper(self, code="import time; time.sleep(120)"):
        p = win.detached_popen([sys.executable, "-c", code, "baton-test-marker"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, p)
        return p

    @staticmethod
    def reap(p):
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)

    def test_live_dead_pid_and_marker(self):
        p = self.sleeper()
        with mock.patch("os.kill", side_effect=AssertionError("unsafe probe")):
            self.assertTrue(baton.pid_alive(p.pid))
            self.assertTrue(baton.pid_alive(p.pid, "baton-test-marker"))
            self.assertFalse(baton.pid_alive(p.pid, "not-the-marker"))
            p.kill()
            p.wait(timeout=5)
            self.assertFalse(baton.pid_alive(p.pid))

    def test_cmdline_query_failure_is_not_dead(self):
        with mock.patch.object(win, "powershell_json", side_effect=subprocess.TimeoutExpired("CIM", 8)):
            with self.assertRaises(subprocess.TimeoutExpired):
                baton.pid_alive(os.getpid(), "marker")
        with mock.patch.object(win, "powershell_json", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "live PID"):
                win.proc_cmdline(os.getpid())

    def test_lock_exclusivity_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.lock")
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import baton_platform as w; "
                    "f=open(sys.argv[2], 'a+b'); print(w.lock_file(f, blocking=False)); f.close()")
            def contender():
                return subprocess.check_output([sys.executable, "-c", code, str(SCRIPTS), path],
                                               text=True, encoding="utf-8").strip()
            with win.exclusive_lock(path):
                self.assertEqual(contender(), "False")
            self.assertEqual(contender(), "True")

    def test_chinese_prompt_utf8_roundtrip(self):
        text = "\u4f60\u597d\uff0cBaton\u3002\nASCII line\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.json"
            baton.write_json(str(path), {"prompt": text})
            self.assertEqual(baton.read_json(str(path))["prompt"], text)
            self.assertIn(text.splitlines()[0].encode("utf-8"), path.read_bytes())
            p = subprocess.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                               input=text, capture_output=True, text=True, encoding="utf-8", check=True)
            self.assertEqual(p.stdout, text)

    def test_native_resolution_ignores_shims(self):
        self.assertTrue(win.executable("codex").lower().endswith("codex.exe"))
        self.assertTrue(win.executable("git").lower().endswith("git.exe"))
        with self.assertRaises(ValueError):
            win.executable("codex.cmd")

    def test_break_delivery_to_private_console(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = ("import signal,time,pathlib,sys; "
                    "signal.signal(signal.SIGBREAK, lambda s,f: sys.exit(42)); "
                    f"pathlib.Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
                    "while True: time.sleep(0.05)")
            p = self.sleeper(code)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            self.assertTrue(win.send_break(p.pid))
            self.assertEqual(p.wait(timeout=5), 42)

    def test_force_stop_includes_child(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "child"
            code = ("import signal,time,pathlib,subprocess,sys; signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
                    "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGBREAK, signal.SIG_IGN); time.sleep(120)']); "
                    f"pathlib.Path({str(ready)!r}).write_text(str(p.pid), encoding='utf-8'); time.sleep(120)")
            p = self.sleeper(code)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            child = int(ready.read_text(encoding="utf-8"))
            win.stop_tree(p.pid, (0.2, 3))
            self.assertFalse(win.pid_alive(p.pid))
            self.assertFalse(win.pid_alive(child))

    def test_pipe_ready_is_bounded(self):
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1); print('ok')"],
                              stdout=subprocess.PIPE) as p:
            self.assertFalse(win.pipe_ready(p.stdout.fileno(), 0.05))
            self.assertTrue(win.pipe_ready(p.stdout.fileno(), 3))
            self.assertEqual(p.stdout.readline().strip(), b"ok")

    def test_old_parent_pid_does_not_make_unrelated_process_a_child(self):
        rows = [dict(ProcessId=10, ParentProcessId=1, CreationDate='/Date(200)/'),
                dict(ProcessId=11, ParentProcessId=10, CreationDate='/Date(100)/'),
                dict(ProcessId=12, ParentProcessId=10, CreationDate='/Date(201)/')]
        with mock.patch.object(win, 'powershell_json', return_value=rows):
            self.assertEqual(set(win.process_tree(10)), {10, 12})

    def test_breakaway_denial_is_explicit_and_only_access_denied_retries(self):
        denied = OSError('job denies breakaway')
        denied.winerror = 5
        with mock.patch.object(win.subprocess, 'Popen', side_effect=[denied, 'child']) as popen:
            with mock.patch('sys.stderr') as stderr:
                self.assertEqual(win.detached_popen(['test.exe']), 'child')
                self.assertTrue(stderr.write.called)
            self.assertTrue(popen.call_args_list[0].kwargs['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
            self.assertFalse(popen.call_args_list[1].kwargs['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        other = OSError('missing executable')
        other.winerror = 2
        with mock.patch.object(win.subprocess, 'Popen', side_effect=other) as popen:
            with self.assertRaises(OSError):
                win.detached_popen(['test.exe'])
            self.assertEqual(popen.call_count, 1)

    def test_statusline_with_space_and_unicode_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'path with spaces \u6d4b\u8bd5' / 'scripts'
            scripts.mkdir(parents=True)
            for name in ('statusline.py', 'codex_quota.py', 'baton_platform.py'):
                shutil.copyfile(SCRIPTS / name, scripts / name)
            (scripts.parent / 'config.json').write_text('{"executor":{}}', encoding='utf-8')
            cache = root / 'cache'
            cache.mkdir()
            (cache / 'codex-quota.json').write_text(json.dumps(dict(fetched_at=time.time(), windows=[])), encoding='utf-8')
            with mock.patch.object(baton, 'statusline_script_path', return_value=str(scripts/'statusline.py')):
                command = baton.statusline_entry(baton.Project(str(root)), {'refreshInterval': 60})['command']
            env = dict(os.environ, BATON_BASE_STATUSLINE="printf 'base line\\n'", BATON_CACHE_DIR=str(cache))
            result = subprocess.run([win.executable('bash'), '-c', command], input='{}',
                                    capture_output=True, text=True, encoding='utf-8', env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 2, result.stdout)
            self.assertTrue(result.stdout.startswith('base line\n'))

    def test_runner_break_records_failure_and_cleans_executor(self):
        self.exercise_runner(force=False)

    def test_hard_killed_runner_is_recovered_on_refresh(self):
        self.exercise_runner(force=True)

    def exercise_runner(self, force):
        with tempfile.TemporaryDirectory() as directory:
            project = baton.Project(directory)
            os.makedirs(project.task_dir('signal'))
            os.makedirs(project.runs_dir('signal'))
            leg = dict(n=1, status='running', started_at=time.time(), log_size_at_start=0)
            for key, suffix in (('prompt', 'prompt.md'), ('events', 'events.jsonl'),
                                ('stderr', 'stderr.log'), ('last', 'last.json')):
                leg[key] = project.leg_path('signal', 1, suffix)
            Path(leg['prompt']).write_text('UTF-8 \u4f60\u597d', encoding='utf-8')
            leg['cmd'] = [sys.executable, '-c', 'import time; time.sleep(120)', leg['last']]
            project.update_state('signal', lambda s: s.update(legs=[leg], status='running',
                config_snapshot=baton.deep_merge(project.config, {'monitor': {'enabled': False}})))
            code = (f'import sys; sys.path.insert(0, {str(SCRIPTS)!r}); '
                    'import baton; from argparse import Namespace; baton.safe_quota=lambda **kw: None; '
                    f'baton.cmd_run(baton.Project({directory!r}), Namespace(task="signal", n="1"))')
            with open(Path(directory)/'runner.log', 'wb') as log:
                runner = win.detached_popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log)
            child = None
            try:
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    state = baton.read_json(project.state_path('signal'))
                    child = state['legs'][0].get('codex_pid')
                    if child:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(child)
                if force:
                    project.update_state('signal', lambda s: s['legs'][0].update(runner_pid=runner.pid))
                    runner.kill()
                    runner.wait(timeout=5)
                    baton.refresh_runner_liveness(project, 'signal', project.load_state('signal'))
                else:
                    self.assertTrue(win.send_break(runner.pid))
                    runner.wait(timeout=45)
                result = baton.read_json(project.state_path('signal'))['legs'][0]
                self.assertEqual(result['status'], 'failed', result)
                if not force:
                    self.assertIn('_Terminated', result['error'])
                self.assertTrue(result['codex_exited'])
                self.assertFalse(win.pid_alive(child))
            finally:
                if runner.poll() is None:
                    runner.kill()
                runner.wait(timeout=5)
                if child and win.pid_alive(child):
                    win.stop_tree(child, (0, 3))


if __name__ == "__main__":
    unittest.main()
