# -*- coding: utf-8 -*-
"""Zip the plugin and (optionally) drop it into Calibre's plugin folder.

    python build.py            # writes AI Book Summarizer.zip here
    python build.py --install  # also copies it into Calibre's plugins dir

Calibre must be closed when installing: it reads the zip at startup.
"""

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_FILES = [
    '__init__.py', 'action.py', 'config.py', 'jobs.py', 'providers.py',
    'plugin-import-name-ai_summarizer.txt', 'icon.png', 'images/icon.png',
]
ZIP_NAME = 'AI Book Summarizer.zip'


def calibre_plugin_dir():
    if sys.platform == 'win32':
        return Path(os.environ['APPDATA']) / 'calibre' / 'plugins'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Preferences' / 'calibre' / 'plugins'
    return Path.home() / '.config' / 'calibre' / 'plugins'


def build():
    out = HERE / ZIP_NAME
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in PLUGIN_FILES:
            path = HERE / name
            if not path.exists():
                raise SystemExit('missing plugin file: %s' % name)
            zf.write(path, name)
    print('built %s (%d files)' % (out, len(PLUGIN_FILES)))
    return out


def install(zip_path):
    target = calibre_plugin_dir() / ZIP_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix('.zip.bak')
        shutil.copy2(target, backup)
        print('backed up previous plugin to %s' % backup)
    shutil.copy2(zip_path, target)
    print('installed to %s — restart Calibre' % target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true', help="copy into Calibre's plugin folder")
    args = parser.parse_args()
    built = build()
    if args.install:
        install(built)
