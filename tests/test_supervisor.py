"""Run isolated supervisor shell functions; never touch host services or packages."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')


def shell_path(path):
    if os.name == 'nt':
        return subprocess.check_output(['cygpath', '-u', str(path)], text=True).strip()
    return str(path)


@unittest.skipUnless(BASH, 'requires bash')
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scanner-supervisor-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.state = self.project / '.supervisor'
        self.state.mkdir()
        self.calls = self.base / 'calls'
        self.source = (ROOT / 'install/supervise.sh').read_text(encoding='utf-8')

    def fake_env(self, path):
        (path / 'bin').mkdir(parents=True, exist_ok=True)
        python = path / 'bin/python'
        python.write_text('#!/bin/bash\nif [ "${1:-}" = -V ]; then echo Python-test; fi\n'
                          'if [ "${1:-}" = -m ]; then echo "$*" >> "$CALLS"; fi\n', encoding='utf-8')
        python.chmod(0o755)
        (path / 'keep-marker').write_text('keep', encoding='utf-8')

    def run_env(self):
        functions = self.source[self.source.index('python_ok()'):self.source.index('# A project-local ffmpeg')]
        script = '''set -eu
DIR="$1"
STATE="$DIR/.supervisor"
PYENV="$DIR/pyenv"
VENV="$DIR/venv"
DURABLE_VENV="$2"
ENV_LOCK="$STATE/env.lock"
log() { :; }
# Serialize is tested on Linux by flock itself; Git Bash has no flock.
if ! command -v flock >/dev/null; then flock() { return 0; }; fi
'''
        env = dict(os.environ, CALLS=shell_path(self.calls))
        if os.name == 'nt':
            env['MSYS'] = 'winsymlinks:nativestrict'
        assert BASH is not None
        return subprocess.run([BASH, '-c', script + functions + '\nensure_env\n',
                               'supervisor-test', shell_path(self.project), shell_path(self.base / 'durable')],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_preserves_external_venv_symlink(self):
        external = self.base / 'external'
        self.fake_env(external)
        (self.project / 'venv').symlink_to(external, target_is_directory=True)
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'venv').resolve(), external.resolve())
        self.assertFalse((self.project / 'pyenv').exists())
        self.assertTrue((external / 'keep-marker').exists())


    def test_reuses_existing_durable_env(self):
        durable = self.base / 'durable'
        self.fake_env(durable)
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.project / 'venv').is_symlink())
        self.assertEqual((self.project / 'venv').resolve(), durable.resolve())
        self.assertFalse((self.project / 'pyenv').exists())
        self.assertTrue((durable / 'keep-marker').exists())

    def test_external_symlink_takes_priority_over_default_durable_env(self):
        external = self.base / 'external'
        self.fake_env(external)
        self.fake_env(self.base / 'durable')
        (self.project / 'venv').symlink_to(external, target_is_directory=True)
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'venv').resolve(), external.resolve())

    def test_project_local_env_remains_fallback(self):
        self.fake_env(self.project / 'pyenv')
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([BASH, '-c', 'test -L "$1/venv" && test "$(readlink "$1/venv")" = pyenv',
                                 'supervisor-test', shell_path(self.project)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.project / 'pyenv/keep-marker').exists())

    def test_sshd_repair_uses_apt_even_when_cached_debs_exist(self):
        function = self.source[self.source.index('prepare_sshd() {'):self.source.index('# No init system')]
        script = '''set -eu
DIR="$1"
STATE="$DIR/.supervisor"
log() { :; }
# Stop after package selection; no host SSH configuration is written.
apt_install() { echo "apt $*" >> "$CALLS"; return 1; }
dpkg() { echo "dpkg $*" >> "$CALLS"; return 1; }
# Simulate missing /usr/sbin/sshd and an available offline package cache.
[() { if test "${2:-}" = /usr/sbin/sshd; then return 1; fi; builtin [ "$@"; }
ls() { return 0; }
'''
        result = subprocess.run([BASH, '-c', script + function + '\nprepare_sshd\n',
                                 'supervisor-test', shell_path(self.project)],
                                env=dict(os.environ, CALLS=shell_path(self.calls)),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.calls.read_text(encoding='utf-8').splitlines(), ['apt openssh-server'])


if __name__ == '__main__':
    unittest.main()
