from __future__ import annotations

import csv
import json
from pathlib import Path


def save_outputs(items: list[dict], json_path: str, csv_path: str) -> None:
    json_file = Path(json_path)
    csv_file = Path(csv_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fieldnames = sorted({key for item in items for key in item}) if items else []
    with csv_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            for item in items:
                writer.writerow(item)
