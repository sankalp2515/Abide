"""Production entry point: `python -m intentseal_gateway` (the container's command).

- Seeds a writable policy directory (INTENTSEAL_POLICY_DIR, e.g. on a persistent volume) from the bundled
  policies the first time, so policy edits made through the API survive restarts and redeploys.
- Runs one uvicorn worker: sessions, rate limits and the proxy's per-session state live in the process
  (scale by running more containers behind sticky sessions, with PostgreSQL as the shared store).
- Honours X-Forwarded-For only from INTENTSEAL_TRUSTED_PROXIES (default: none), so per-client rate limits
  cannot be dodged with a forged header.
- Graceful shutdown: in-flight requests get INTENTSEAL_SHUTDOWN_GRACE_S seconds (default 20).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import uvicorn

from intentseal_core.config import repo_root


def seed_policies() -> None:
    target = os.environ.get("INTENTSEAL_POLICY_DIR")
    bundled = repo_root() / "policies"
    if not target or Path(target).resolve() == bundled.resolve():
        return
    dest = Path(target)
    dest.mkdir(parents=True, exist_ok=True)
    if not any(dest.glob("*.yaml")):
        for f in bundled.glob("*.yaml"):
            shutil.copy2(f, dest / f.name)
        print(f"intentseal: seeded {dest} with the bundled policies", flush=True)


def main() -> None:
    seed_policies()
    uvicorn.run(
        "intentseal_gateway.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT") or "8000"),
        workers=1,
        proxy_headers=bool(os.environ.get("INTENTSEAL_TRUSTED_PROXIES")),
        forwarded_allow_ips=os.environ.get("INTENTSEAL_TRUSTED_PROXIES") or "127.0.0.1",
        timeout_graceful_shutdown=int(os.environ.get("INTENTSEAL_SHUTDOWN_GRACE_S") or "20"),
        log_level=os.environ.get("INTENTSEAL_LOG_LEVEL", "info"),
        server_header=False,
    )


if __name__ == "__main__":
    main()
