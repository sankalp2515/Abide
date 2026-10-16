"""IntentSeal Core Engine."""

import os

__version__ = "0.1.0"


def adopt_legacy_env() -> None:
    """Settings were named ASG_* before 8 Oct 2026 (D-038). An old name still works when the new one is unset."""
    for name, value in list(os.environ.items()):
        if name.startswith("ASG_"):
            os.environ.setdefault("INTENTSEAL_" + name[4:], value)


adopt_legacy_env()
