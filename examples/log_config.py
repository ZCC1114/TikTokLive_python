import os
import logging
from logging.handlers import TimedRotatingFileHandler


def setup_logging(log_dir: str = "logs", log_filename: str = "server.log") -> None:
    """
    Configure logging: output to file, rotate daily.
    """
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_path = os.path.join(log_dir, log_filename)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    handler = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(formatter)

    loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
    ]

    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
