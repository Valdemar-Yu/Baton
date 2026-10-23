"""Offline CLI smoke in TEMP; never starts an executor or installs anything."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/baton/scripts"))
import baton_platform


def main():
    with tempfile.TemporaryDirectory(prefix="baton-v2-smoke-") as raw:
        temp = Path(raw)
        project = temp / 'project space \u6d4b\u8bd5'
        project.mkdir()
        cache = temp / 'cache'
        cache.mkdir()
        # A fresh, explicitly synthetic cache avoids any quota network refresh.
        (cache / 'codex-quota.json').write_text(json.dumps(dict(
            fetched_at=time.time(), observed_at=time.time(), source='offline-smoke', windows=[])), encoding='utf-8')
        env = dict(os.environ, BATON_HOME=str(temp / 'state'), BATON_CACHE_DIR=str(cache),
                   BATON_DESKTOP_NOTIFY='0', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                   USERPROFILE=str(temp / 'home'), HOME=str(temp / 'home'),
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        brief = temp / 'brief.md'
        brief.write_text('# Offline smoke\nInspect README only.\n```baton-scope\n' + json.dumps(dict(
            allow=['README.md'], deny=[], acceptance=[], frozen=[], verify='none')) + '\n```\n', encoding='utf-8')
        commands = [['git', 'init', '-q'],
                    [sys.executable, str(ROOT / 'skills/baton/scripts/baton.py'), 'init', '--no-statusline'],
                    [str(ROOT / 'skills/baton/bin/baton.cmd'), 'doctor'],
                    [sys.executable, str(ROOT / 'skills/baton/scripts/baton.py'), 'new', 'smoke', '--brief', str(brief)],
                    [sys.executable, str(ROOT / 'skills/baton/scripts/baton.py'), 'config']]
        log = ['TEMP root: ' + raw, 'Quota cache: synthetic offline-smoke, no network or model call.']
        for command in commands:
            result = subprocess.run(command, cwd=project, env=env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            log += ['$ ' + subprocess.list2cmdline(command), 'exit=' + str(result.returncode),
                    result.stdout, result.stderr]
            if result.returncode:
                raise RuntimeError('\n'.join(log))
        config = json.loads((project / 'baton.json').read_text(encoding='utf-8'))
        assert config['executor']['backend'] == 'codex', config
        assert config['memory']['backend'] == 'local', config
        # Verify the actual Git Bash launcher, too, without an executor call.
        script = str(ROOT / 'skills/baton/bin/baton').replace('\\', '/')
        result = subprocess.run([baton_platform.executable('bash'), script, 'config', '--json'], cwd=project, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout[result.stdout.index('{'):])['executor']['backend'] == 'codex'
        log += ['Git Bash bin/baton config --json: PASS', 'No baton start: no dry-run flag exists.']
        output = ROOT / 'test-results'
        output.mkdir(exist_ok=True)
        (output / 'smoke.txt').write_text('\n'.join(log), encoding='utf-8')
        print('\n'.join(log))


if __name__ == '__main__':
    main()
