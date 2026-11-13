"""Runtime paths shared by desktop bundles and the embedded Android service."""

import os
from pathlib import Path
import sys


def resource_directory():
    default = getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)
    return os.path.abspath(os.environ.get('ZOFFLINE_RESOURCE_DIR', str(default)))


def storage_directory():
    default = (Path(sys.executable).resolve().parent / 'storage' if
               getattr(sys, 'frozen', False) else Path(resource_directory()) / 'storage')
    return os.path.abspath(os.environ.get('ZOFFLINE_STORAGE_DIR', str(default)))


def logs_directory():
    default = (Path(sys.executable).resolve().parent / 'logs' if
               getattr(sys, 'frozen', False) else Path(resource_directory()) / 'logs')
    return os.path.abspath(os.environ.get('ZOFFLINE_LOGS_DIR', str(default)))
