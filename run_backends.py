"""Start the configured demo backends as separate processes; Ctrl+C stops them."""

import argparse
from pathlib import Path
import subprocess
import sys
import time

from lb_config import load_config

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Invalid configuration: {exc}\n")
    processes = []
    try:
        for backend in config.backends.values():
            process = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "demo_backend.py"),
                 "--host", backend.host, "--port", str(backend.port),
                 "--name", backend.name],
                stdout=sys.stdout, stderr=sys.stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            processes.append((backend.name, process))
        print("Starting configured demo backends. Ctrl+C stops this group.", flush=True)
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    parser.exit(1, f"{name} exited. Check its startup error above; "
                                "stop any old server using the same port.\n")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping demo backends.", flush=True)
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
