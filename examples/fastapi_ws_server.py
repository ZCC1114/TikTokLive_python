"""Compatibility entry point: existing uvicorn/systemd commands keep working."""

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_service.app import create_app
from live_service.manager import ConnectionManager

manager = ConnectionManager()
app = create_app(manager)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
