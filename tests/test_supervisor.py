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
        script = '''set -u
DIR="$1"
STATE="$DIR/.supervisor"
PYENV="$DIR/pyenv"
VENV="$DIR/venv"
DURABLE_VENV="$2"
ENV_LOCK="$STATE/env.lock"
log() { :; }
# Simulate venv creation without installing packages or using host Python paths.
python3() {
    test "$1" = -m && test "$2" = venv || return 1
    echo "create $3" >> "$CALLS"
    mkdir -p "$3/bin"
    cp "$FAKE_PYTHON" "$3/bin/python"
    chmod +x "$3/bin/python"
}
# Serialize is tested on Linux by flock itself; Git Bash has no flock.
if ! command -v flock >/dev/null; then flock() { return 0; }; fi
'''
        template = self.base / 'template'
        self.fake_env(template)
        env = dict(os.environ, CALLS=shell_path(self.calls),
                   FAKE_PYTHON=shell_path(template / 'bin/python'))
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

    def test_missing_env_is_created_in_durable_path_with_dependencies(self):
        (self.project / 'requirements.txt').write_text('flask\n', encoding='utf-8')
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        durable = self.base / 'durable'
        self.assertEqual((self.project / 'venv').resolve(), durable.resolve())
        self.assertFalse((self.project / 'pyenv').exists())
        self.assertTrue((durable / '.scanner-requirements').exists())
        calls = self.calls.read_text(encoding='utf-8')
        self.assertIn('create ' + shell_path(durable), calls)
        self.assertIn('-m pip install -q -r ', calls)

    def test_broken_external_target_is_not_recreated_at_old_location(self):
        external = self.base / 'external'
        external.mkdir()
        (external / 'keep-marker').write_text('keep', encoding='utf-8')
        (self.project / 'venv').symlink_to(external, target_is_directory=True)
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'venv').resolve(), (self.base / 'durable').resolve())
        self.assertTrue((external / 'keep-marker').exists())
        self.assertFalse((external / 'bin/python').exists())

    def test_broken_project_env_is_replaced_by_durable_env(self):
        (self.project / 'pyenv').mkdir()
        (self.project / 'venv').symlink_to(self.project / 'pyenv', target_is_directory=True)
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'venv').resolve(), (self.base / 'durable').resolve())
        self.assertFalse((self.project / 'pyenv/bin/python').exists())

    def test_broken_durable_env_is_rebuilt_and_reused_on_next_run(self):
        durable = self.base / 'durable'
        durable.mkdir()
        (self.project / 'requirements.txt').write_text('flask\n', encoding='utf-8')
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        first_calls = self.calls.read_text(encoding='utf-8')
        self.assertTrue((durable / '.scanner-requirements').exists())
        result = self.run_env()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text(encoding='utf-8'), first_calls)
        self.assertEqual((self.project / 'venv').resolve(), durable.resolve())

    def test_sshd_repair_tries_cached_debs_before_apt(self):
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
        calls = self.calls.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].startswith('dpkg -i ') and calls[0].endswith('/debs/openssh/*.deb'), calls)
        self.assertEqual(calls[1], 'apt openssh-server')


if __name__ == '__main__':
    unittest.main()
