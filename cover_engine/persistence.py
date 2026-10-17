"""atomic finite-json persistence; callers serialize writes to each path."""

import json
import os
from pathlib import Path


def write_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(value, file, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
