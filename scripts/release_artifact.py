"""Compute the release version and source distribution filename in one place.

The publish-time existence check and the upload must name the same object, and
that name must be the one the build tool writes. setuptools normalizes both
parts of an sdist filename (PEP 625: the project name as in PEP 503 with runs of
``-_.`` collapsed to ``_`` and lowercased; the version as in PEP 440), so
``PyHive`` 0.7.0.3 is built as ``pyhive-0.7.0.3.tar.gz``. Deriving the key from
the unnormalized project name makes the existence check look for an object that
can never exist.

Usage:
    python scripts/release_artifact.py version <base-version> <branch> <revision>
    python scripts/release_artifact.py sdist <project-name> <version>
"""
from __future__ import print_function

import re
import sys

from packaging.version import Version


def release_version(base, branch, revision):
    """Return the version to build: the base version, or a PR-local version."""
    if branch.startswith('PR-'):
        return str(Version('{}+{}.{}'.format(base, branch, revision)))
    return str(Version(base))


def sdist_filename(name, version):
    """Return the normalized sdist filename the build tool produces."""
    project = re.sub(r'[-_.]+', '_', name).lower()
    return '{}-{}.tar.gz'.format(project, Version(version))


def main(argv):
    if len(argv) == 4 and argv[0] == 'version':
        print(release_version(*argv[1:]))
    elif len(argv) == 3 and argv[0] == 'sdist':
        print(sdist_filename(*argv[1:]))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
