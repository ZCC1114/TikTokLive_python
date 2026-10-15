import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str = "logs", log_filename: str = "server.log") -> None:
    """Install one file handler; repeated startup does not multiply log output."""
    path = Path(os.getenv("LOG_DIR", log_dir)) / log_filename
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(handler, "_live_service_handler", False) for handler in root.handlers):
        handler = TimedRotatingFileHandler(path, when="midnight", backupCount=30, encoding="utf-8")
        handler._live_service_handler = True
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # The uvicorn children propagate to this logger; don't attach the same file
    # handler at every level of that hierarchy.
    logging.getLogger("uvicorn").propagate = True
