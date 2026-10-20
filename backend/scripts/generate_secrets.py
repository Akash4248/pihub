#!/usr/bin/env python3
"""Generate the shared secrets required by the backend services.

Usage:
    python scripts/generate_secrets.py          # fill missing/insecure values
    python scripts/generate_secrets.py --force  # rotate every managed secret

The script updates backend/.env atomically and never prints secret values.
It is safe to re-run: existing strong values are preserved unless --force.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = BACKEND_ROOT / ".env"
ENV_EXAMPLE_PATH = BACKEND_ROOT / ".env.example"

INSECURE_VALUES = {
    "",
    "change-me",
    "change-me-too",
    "change-me-super-secret-jwt-key-min32chars",
    "default-secret-key-change-in-production",
    "postgres",
}

SECRET_KEYS = (
    "INTERNAL_SERVICE_KEY",
    "JWT_SECRET_KEY",
    "PIHUB_ADMIN_TOKEN",
    "PIHUB_DEVICE_TOKEN_SECRET",
)


def _is_strong(value: str) -> bool:
    return bool(value) and value.strip() not in INSECURE_VALUES and len(value.strip()) >= 32


def _read_template() -> list[str]:
    source = ENV_PATH if ENV_PATH.exists() else ENV_EXAMPLE_PATH
    if not source.exists():
        return []
    return source.read_text(encoding="utf-8").splitlines()


def _update(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            if match.group(2).strip() == value:
                return lines, False
            lines[index] = f"{match.group(1)}{value}"
            return lines, True
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{key}={value}")
    return lines, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="rotate all managed secrets")
    args = parser.parse_args()

    if not ENV_PATH.exists():
        example = ENV_EXAMPLE_PATH
        if not example.exists():
            print(f"error: neither {ENV_PATH} nor {example} exists")
            return 1
        print(f"creating {ENV_PATH} from {example}")

    lines = _read_template()
    changed: list[str] = []

    for key in SECRET_KEYS:
        current = ""
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
        for line in lines:
            match = pattern.match(line)
            if match:
                current = match.group(1).strip()
                break
        if args.force or not _is_strong(current):
            lines, did_change = _update(lines, key, secrets.token_hex(32))
            if did_change:
                changed.append(key)

    fd, temp_name = tempfile.mkstemp(dir=str(BACKEND_ROOT), prefix=".env.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, ENV_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    if changed:
        print("updated secrets: " + ", ".join(changed))
    else:
        print("secrets already configured; nothing changed")
    print(f"wrote {ENV_PATH} (mode 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
