from __future__ import annotations
import argparse
import os
import sys
from .client import SimClient
from .db import DB
from .loop import ControllerLoop

def main():
    parser = argparse.ArgumentParser(description="Formi Outcome-First Controller")
    parser.add_argument("--url", default="http://127.0.0.1:8700", help="Simulator base URL")
    parser.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "controller.db"), help="SQLite database path")
    args = parser.parse_args()

    client = SimClient(args.url)
    db = DB(args.db)

    try:
        loop = ControllerLoop(client, db)
        loop.run()
    except Exception as e:
        print(f"Controller crashed: {e}", file=sys.stderr)
        raise
    finally:
        client.close()
        db.close()

if __name__ == "__main__":
    main()
