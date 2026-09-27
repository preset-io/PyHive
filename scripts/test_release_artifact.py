"""The existence check and the upload share one normalized artifact name."""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / 'release_artifact.py'


def run(*args):
    return subprocess.check_output([sys.executable, str(SCRIPT)] + list(args),
                                   universal_newlines=True).strip()


@pytest.mark.parametrize('name,version,expected', [
    ('PyHive', '0.7.0.3', 'pyhive-0.7.0.3.tar.gz'),
    ('PyHive', '0.7.0.2', 'pyhive-0.7.0.2.tar.gz'),
    ('Py.Hive--x_y', '0.7.0.3', 'py_hive_x_y-0.7.0.3.tar.gz'),
    ('PyHive', '0.7.0.3+PR-6.gABCDEF1', 'pyhive-0.7.0.3+pr.6.gabcdef1.tar.gz'),
    ('PyHive', '0.7.0.3+pr.6.g0123456', 'pyhive-0.7.0.3+pr.6.g0123456.tar.gz'),
])
def test_sdist_filename(name, version, expected):
    assert run('sdist', name, version) == expected


@pytest.mark.parametrize('branch,revision,expected', [
    ('PR-6', 'abcdef1', '0.7.0.3+pr.6.gabcdef1'),
    ('PR-6', 'ABCDEF1', '0.7.0.3+pr.6.gabcdef1'),
    # A numeric revision keeps its leading zeros.
    ('PR-6', '0123456', '0.7.0.3+pr.6.g0123456'),
    ('PR-6', '1234567', '0.7.0.3+pr.6.g1234567'),
    ('master', 'abcdef1', '0.7.0.3'),
    ('fix/some-branch', 'abcdef1', '0.7.0.3'),
])
def test_release_version(branch, revision, expected):
    assert run('version', '0.7.0.3', branch, revision) == expected


def test_pr_version_ends_with_the_git_revision():
    version = run('version', '0.7.0.3', 'PR-6', '0012abc')
    assert version.endswith('.g0012abc')
    assert run('sdist', 'PyHive', version) == 'pyhive-0.7.0.3+pr.6.g0012abc.tar.gz'


def test_the_unnormalized_key_is_not_what_gets_built():
    # The previous check looked for PyHive/PyHive-<version>.tar.gz.
    assert run('sdist', 'PyHive', '0.7.0.3') != 'PyHive-0.7.0.3.tar.gz'


def test_setuptools_builds_the_computed_name(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.check_call(
        [sys.executable, 'setup.py', '-q', 'sdist', '--formats=gztar',
         '--dist-dir', str(tmp_path)],
        cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    version = subprocess.check_output(
        [sys.executable, 'setup.py', '--version'], cwd=str(root),
        universal_newlines=True).strip().splitlines()[-1]
    assert [p.name for p in tmp_path.iterdir()] == [run('sdist', 'PyHive', version)]


def test_usage_error():
    assert subprocess.call([sys.executable, str(SCRIPT), 'bogus'],
                           stderr=subprocess.DEVNULL) == 2
