import os
import sys
from pathlib import Path

# Test/dev parity with run_backend.sh (exports ALLOW_ANONYMOUS_DEV=true):
# pihub api.main fails closed at import without it.
os.environ.setdefault("ALLOW_ANONYMOUS_DEV", "true")

# Automatically add pihub/backend root and pihub root to sys.path
service_dir = Path(__file__).resolve().parent
backend_dir = service_dir.parent

if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(service_dir) not in sys.path:
    sys.path.insert(0, str(service_dir))
