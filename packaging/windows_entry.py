import os
import sys

if '--backend-self-test' in sys.argv:
    if not os.environ.get('DOCSELECTER_TEST_ROOT'):
        raise SystemExit(64)
    try:
        from doc_selecter.windows.smoke import main
        result = main()
    except Exception:
        # A failed background check must not open PyInstaller's windowed
        # exception dialog. Keep diagnostics only in the injected fake root.
        import traceback
        detail = traceback.format_exc()
        try:
            from doc_selecter.windows.io import open, os as fs
            path = fs.path.join(os.environ['DOCSELECTER_TEST_ROOT'], 'backend-self-test-error.log')
            with open(path, 'x', encoding='utf-8') as stream:
                stream.write(detail)
        except Exception:
            pass
        result = 1
    raise SystemExit(result)

from doc_selecter.app_startup import app_main
raise SystemExit(app_main())
