from dataclasses import dataclass, fields
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = "https://wall-street-skill.com"


@dataclass(frozen=True)
class Config:
    root: Path = ROOT
    interval_seconds: int = 120
    request_timeout: int = 20
    max_pages: int = 10
    jitter_ratio: float = 0.1
    max_backoff_seconds: int = 1800
    port: int = 8765

    @property
    def database(self):
        return self.root / "data" / "monitor.sqlite3"

    @property
    def token_file(self):
        return self.root / "secrets" / "token"

    def prepare(self):
        for directory in ("data", "logs", "secrets", "service"):
            folder = self.root / directory
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)


def load_config():
    filename = ROOT / "config.json"
    data = json.loads(filename.read_text()) if filename.exists() else {}
    allowed = {f.name for f in fields(Config)} - {"root"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("config.json 包含未知配置项或格式不正确")
    integer_bounds = {
        "interval_seconds": (60, 86400), "request_timeout": (1, 120),
        "max_pages": (1, 100), "max_backoff_seconds": (60, 86400),
        "port": (1024, 65535),
    }
    for key, (low, high) in integer_bounds.items():
        if key in data and (type(data[key]) is not int or not low <= data[key] <= high):
            raise ValueError(f"{key} 必须是 {low} 到 {high} 之间的整数")
    jitter = data.get("jitter_ratio", 0.1)
    if type(jitter) not in (int, float) or not 0 <= jitter <= 0.5:
        raise ValueError("jitter_ratio 必须在 0 到 0.5 之间")
    config = Config(**data)
    config.prepare()
    return config
