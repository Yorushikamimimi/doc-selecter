"""Production frozen entry. PyInstaller supplies the interpreter and import paths."""
import sys
from doc_selecter.app_startup import app_main

if __name__ == '__main__':
    raise SystemExit(app_main())
