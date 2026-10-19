"""One JSON object per log line, suitable for terminal output or saved evidence."""

from datetime import datetime, timezone
import json
import logging

LOG = logging.getLogger("load_balancer")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(event, **fields):
    LOG.info(event, extra={"event_data": {"event": event, **fields}})


class JSONFormatter(logging.Formatter):
    def format(self, record):
        data = {"timestamp": utc_now(), "level": record.levelname}
        data.update(getattr(record, "event_data", {"event": record.getMessage()}))
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=True, allow_nan=False)
