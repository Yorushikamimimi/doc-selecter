"""Entry shipped in Resources; Python -I ignores cwd, PYTHONPATH and user site."""
from pathlib import Path
import sys

RESOURCES = Path(__file__).resolve().parent
sys.path.insert(0, str(RESOURCES))
from doc_selecter.app_startup import app_main

if __name__ == '__main__':
    raise SystemExit(app_main())
