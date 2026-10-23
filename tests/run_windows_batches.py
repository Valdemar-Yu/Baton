"""Run one upstream test file per process with isolated homes and no live model calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*")
    parser.add_argument("--output", default="test-results")
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(exist_ok=True)
    failures = False
    for name in args.files or sorted(p.name for p in (ROOT / "tests").glob("test_*.py")):
        with tempfile.TemporaryDirectory(prefix="baton-tests-") as raw:
            home = Path(raw)
            env = dict(os.environ, HOME=raw, USERPROFILE=raw, BATON_HOME=str(home / "state"),
                       BATON_CACHE_DIR=str(home / "cache"), BATON_DESKTOP_NOTIFY="0",
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", BATON_MEMORY_LIVE="0",
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       PYTHONPATH=raw + os.pathsep + str(ROOT / "tests"))
            # All fake model executables live under TEMP. Block accidental fall-through
            # to the logged-in real CLI, including quota app-server network refreshes.
            (home / "sitecustomize.py").write_text('''import os, shutil, subprocess
_original = subprocess.Popen
class OfflinePopen(_original):
    def __init__(self, args, *a, **kw):
        if isinstance(args, (list, tuple)) and args:
            name = os.path.basename(str(args[0])).lower()
            if name in ('codex', 'codex.exe', 'claude', 'claude.exe') and '--version' not in args:
                resolved = shutil.which(str(args[0])) or str(args[0])
                temp = os.path.normcase(os.path.abspath(os.environ['TEMP'])) + os.sep
                if not os.path.normcase(os.path.abspath(resolved)).startswith(temp):
                    raise RuntimeError('Tests forbid live model/quota calls: ' + name)
        super().__init__(args, *a, **kw)
subprocess.Popen = OfflinePopen
''', encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                                     "-p", name, "-v"], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=900)
            log = result.stdout + result.stderr
            (output / (name + ".log")).write_text(log, encoding="utf-8")
            import re
            ran = re.search(r"Ran (\d+) tests?", log)
            skip = re.search(r"skipped=(\d+)", log)
            fail = re.search(r"failures=(\d+)", log)
            error = re.search(r"errors=(\d+)", log)
            counts = dict(tests=int(ran[1]) if ran else 0, skipped=int(skip[1]) if skip else 0,
                          failures=int(fail[1]) if fail else 0, errors=int(error[1]) if error else 0,
                          exit_code=result.returncode)
            counts['passed'] = counts['tests'] - counts['skipped'] - counts['failures'] - counts['errors']
            (output / (name + ".json")).write_text(json.dumps(counts), encoding="utf-8")
            print(name, json.dumps(counts), flush=True)
            failures |= result.returncode != 0
    return int(failures)


if __name__ == "__main__":
    raise SystemExit(main())
