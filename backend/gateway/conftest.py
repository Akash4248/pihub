import os
import sys
from pathlib import Path

# Test/dev parity with run_backend.sh (which exports ALLOW_ANONYMOUS_DEV=true):
# the shared Settings default is fail-closed (False), so opt the suite into
# dev mode before any settings import. Must run before collection imports app.
os.environ.setdefault("ALLOW_ANONYMOUS_DEV", "true")

# Automatically add pihub/backend root and service root to sys.path
service_dir = Path(__file__).resolve().parent
backend_dir = service_dir.parent

if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(service_dir) not in sys.path:
    sys.path.insert(0, str(service_dir))
