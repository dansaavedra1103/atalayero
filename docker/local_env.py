"""Write the local .env Docker Compose reads (ADR-0021, ADR-0022): host settings and secrets.

Run by `make up`. It adds what is missing and never changes a value that is there, so secrets
survive every run. The file is readable by its owner only, and never committed. Standard library
only: it runs before any environment exists.
"""

import base64
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path


def wanted(root: Path, existing: dict[str, str]) -> dict[str, str]:
    api_key = existing.get("ATALAYERO_API_KEY") or secrets.token_urlsafe(32)
    return {
        "AIRFLOW_UID": str(os.getuid()),
        "ATALAYERO_ROOT": str(root),
        "AIRFLOW_POSTGRES_PASSWORD": secrets.token_hex(16),
        "AIRFLOW_JWT_SECRET": secrets.token_hex(32),
        "AIRFLOW_FERNET_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        # The key a local client sends in X-API-Key; the API only ever sees its SHA-256.
        "ATALAYERO_API_KEY": api_key,
        "ATALAYERO_API_KEY_SHA256": hashlib.sha256(api_key.encode()).hexdigest(),
    }


def main(path: Path, root: Path) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    existing = dict(
        line.split("=", 1) for line in lines if "=" in line and not line.startswith("#")
    )
    added = [f"{k}={v}" for k, v in wanted(root, existing).items() if k not in existing]
    if added:
        path.write_text("\n".join([*lines, *added]) + "\n")
        print(f"{path}: added {', '.join(a.split('=')[0] for a in added)}")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]).resolve())
