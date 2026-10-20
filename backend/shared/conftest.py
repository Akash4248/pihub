import os
import sys
from pathlib import Path

# Mirror the service conftests: backend root on sys.path, dev-mode env to
# match run_backend.sh (shared Settings default is fail-closed).
os.environ.setdefault("ALLOW_ANONYMOUS_DEV", "true")

shared_dir = Path(__file__).resolve().parent
backend_dir = shared_dir.parent

if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(shared_dir) not in sys.path:
    sys.path.insert(0, str(shared_dir))
