import sys
from pathlib import Path

# Automatically add pihub/backend root and service root to sys.path
service_dir = Path(__file__).resolve().parent
backend_dir = service_dir.parent

if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(service_dir) not in sys.path:
    sys.path.insert(0, str(service_dir))
