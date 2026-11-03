"""Early sandbox guard; no user state access before frozen test dispatch."""
import os
import sys

if any(arg in sys.argv for arg in ('--diagnose', '--backend-self-test')):
    if not os.environ.get('DOCSELECTER_TEST_ROOT'):
        raise SystemExit(64)
