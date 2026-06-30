"""Generic CSV sink (schema-driven by the sensor module)."""

import csv
import time
from pathlib import Path
from typing import Any, Iterable, Sequence


class CsvSink:
    """
        Buffered CSV writer. The header and per-frame row format are
        provided by the sensor module via CSV_HEADER + csv_rows().
        Line-buffered text I/O so Ctrl+C doesn't lose recent rows.
    """

    def __init__(self, path: Path, header: Sequence[str]):
        self.path = Path(path)
        self.fh = open(self.path, "w", newline="",
                       encoding="utf-8", buffering=1)
        self.w = csv.writer(self.fh)
        self.w.writerow(list(header))

        self.t0 = time.monotonic()
        self.rows = 0
        self.frames = 0

    def write_rows(self, rows: Iterable[tuple]) -> None:
        """Append a frame's rows. `rows` is any iterable of tuples."""
        n = 0
        for row in rows:
            self.w.writerow(row)
            n += 1

        if n > 0:
            self.frames += 1
            self.rows += n

    def stats(self) -> dict:
        """
            Function that provides stats
        """

        return {
            "path":       str(self.path),
            "frames":     self.frames,
            "rows":       self.rows,
            "duration_s": time.monotonic() - self.t0,
        }

    def close(self) -> Any:
        """
            Function called on closing
        """

        try:
            self.fh.flush()
        finally:
            if not self.fh.closed:
                self.fh.close()
