"""Copy the files an installed package needs into the package folders (PyPI builds cannot reach outside them).

  config/providers.yaml, config/models.yaml, config/policy-templates/  ->  packages/core/intentseal_core/defaults/
  LICENSE, NOTICE                                                      ->  packages/core/, packages/sdk/

`config/` stays the source of truth; tests/test_packaging.py fails when a copy is out of date.
Usage: python scripts/sync_package_data.py [--check]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "packages" / "core" / "intentseal_core" / "defaults"


def pairs() -> list[tuple[Path, Path]]:
    out = [(ROOT / "config" / n, DEFAULTS / n) for n in ("providers.yaml", "models.yaml")]
    out += [(f, DEFAULTS / "policy-templates" / f.name) for f in sorted((ROOT / "config" / "policy-templates").glob("*.yaml"))]
    out += [(ROOT / n, ROOT / "packages" / pkg / n) for pkg in ("core", "sdk") for n in ("LICENSE", "NOTICE")]
    return out


def stale() -> list[str]:
    bad = [str(dst.relative_to(ROOT)) for src, dst in pairs() if not dst.exists() or dst.read_bytes() != src.read_bytes()]
    extra = {f.name for f in (DEFAULTS / "policy-templates").glob("*.yaml")} - \
            {f.name for f in (ROOT / "config" / "policy-templates").glob("*.yaml")}
    return bad + [f"packages/core/intentseal_core/defaults/policy-templates/{n} (no longer in config/)" for n in sorted(extra)]


def main() -> int:
    if "--check" in sys.argv:
        bad = stale()
        print("\n".join(bad) or "package data in sync")
        return 1 if bad else 0
    for src, dst in pairs():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    for f in (DEFAULTS / "policy-templates").glob("*.yaml"):
        if not (ROOT / "config" / "policy-templates" / f.name).exists():
            f.unlink()
    print(f"synced {len(pairs())} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
