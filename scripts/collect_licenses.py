"""Collect installed distribution notices from the isolated build environment."""

from importlib import metadata
from pathlib import Path
import re
import shutil
import sys


def main(destination):
    destination.mkdir(parents=True, exist_ok=True)
    distributions = sorted(metadata.distributions(), key=lambda item: item.metadata['Name'].casefold())
    notices = ['Bundled Python dependencies and build tools; original notices follow.\n']
    for distribution in distributions:
        name = distribution.metadata['Name']
        folder = re.sub(r'[^A-Za-z0-9_.-]', '_', name)
        notices.append('%s==%s' % (name, distribution.version))
        for entry in distribution.files or ():
            if not entry.name.upper().startswith(('LICENSE', 'NOTICE', 'COPYING')):
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            # Only package-provided notice files are copied; no user directories.
            target = destination / folder / str(entry).replace('..', '_')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copyfile(python_license, destination / 'PYTHON-LICENSE.txt')
    (destination / 'NOTICE.txt').write_text('\n'.join(notices) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main(Path(sys.argv[1]))
