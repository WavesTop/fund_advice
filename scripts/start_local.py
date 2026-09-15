"""Start the local API and web app together for development review."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    processes = [
        subprocess.Popen(
            [
                "uv", "run", "--locked", "--offline", "uvicorn",
                "backend.api.main:app", "--host", "127.0.0.1", "--port", "8000",
            ],
            cwd=ROOT,
        ),
        subprocess.Popen(["npm", "run", "dev"], cwd=ROOT / "frontend"),
    ]

    def stop(_signum: int, _frame: object) -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while all(process.poll() is None for process in processes):
            for process in processes:
                try:
                    return_code = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
                return return_code
    finally:
        stop(signal.SIGTERM, None)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
