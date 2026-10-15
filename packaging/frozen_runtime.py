"""Early failure guard, before PyInstaller's Tcl/Tk runtime hook; no external loader."""
import os
import sys
from pathlib import Path

# A test environment handoff failure must not open the real support directory.
_diagnose = '--diagnose' in sys.argv
if _diagnose and not os.environ.get('DOCSELECTER_TEST_ROOT'):
    raise SystemExit(64)

# PyInstaller's normal hook binds TCL_LIBRARY/TK_LIBRARY to these data directories.
# Diagnose damaged copies before it raises, retaining our human-readable log path.
_missing = [name for name in ('_tcl_data/init.tcl', '_tk_data/tk.tcl')
            if not (Path(sys._MEIPASS) / name).is_file()]
if _missing:
    from doc_selecter.app_startup import _write_diagnostic, _failure_notice
    _message = '应用内置 Tcl／Tk 资源不完整，请重新复制完整应用；不需要另装 Python。'
    _path = _write_diagnostic(_message + '\n缺少内置资源：' + ', '.join(_missing))
    if not _diagnose:
        _failure_notice(_message, _path)
    raise SystemExit(1)
