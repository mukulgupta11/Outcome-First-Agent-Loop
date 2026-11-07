from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import httpx

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, default=8701)
    args = parser.parse_args()

    Path("runs").mkdir(exist_ok=True)
    Path("reports").mkdir(exist_ok=True)
    mine_dir = f"runs/mine_{args.name}"
    base_dir = f"runs/base_{args.name}"
    report_file = f"reports/{args.name}.md"
    db_file = f"runs/{args.name}.db"

    if os.path.exists(db_file):
        os.remove(db_file)

    # 1. Ensure baseline exists
    if not os.path.exists(base_dir):
        print(f"Generating baseline for {args.scenario}...")
        subprocess.check_call([
            sys.executable, "-m", "sim", "baseline",
            "--scenario", args.scenario,
            "--run-dir", base_dir
        ])

    # 2. Start sim serve
    print(f"Starting simulator for {args.scenario} on port {args.port}...")
    sim_proc = subprocess.Popen([
        sys.executable, "-m", "sim", "serve",
        "--scenario", args.scenario,
        "--run-dir", mine_dir,
        "--port", str(args.port),
        "--force"
    ])

    try:
        # Wait for health
        client = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=2.0)
        for _ in range(30):
            try:
                if client.get("/health").status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("Simulator failed to start.")

        print(f"Simulator ready. Launching controller...")
        t0 = time.time()
        env = dict(os.environ)
        env["CONTROLLER_DB"] = db_file
        # Run controller
        ctrl_proc = subprocess.run([
            "./run.sh", f"http://127.0.0.1:{args.port}"
        ], env=env)
        elapsed = time.time() - t0
        print(f"Controller completed in {elapsed:.2f}s with return code {ctrl_proc.returncode}")
        assert ctrl_proc.returncode == 0, f"Controller failed with code {ctrl_proc.returncode}"

    finally:
        sim_proc.terminate()
        try:
            sim_proc.wait(timeout=3)
        except Exception:
            sim_proc.kill()

    # 3. Grade
    print(f"Grading run against {base_dir} -> {report_file}...")
    subprocess.check_call([
        sys.executable, "-m", "sim", "grade",
        "--run", mine_dir,
        "--baseline", base_dir,
        "--out", report_file
    ])
    print(f"Grading complete! Report saved to {report_file}")

if __name__ == "__main__":
    main()
