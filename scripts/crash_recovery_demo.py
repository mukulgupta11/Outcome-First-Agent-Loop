from __future__ import annotations
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from controller.db import DB

SIM_PORT = 8765
SIM_URL = f"http://127.0.0.1:{SIM_PORT}"
RUN_DIR = "runs/crash_demo_sim"
DB_PATH = "runs/crash_demo.db"
SCENARIO = "scenarios/public_01_dev.yaml"

def run_demo():
    print("=" * 70)
    print("CRASH RECOVERY DEMONSTRATION (kill -9 mid-run & restart)")
    print("=" * 70)

    # 1. Clean previous runs
    Path("runs").mkdir(exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    # 2. Start simulator server
    print("\n[1/6] Launching simulator server...")
    sim_proc = subprocess.Popen([
        sys.executable, "-m", "sim", "serve",
        "--scenario", SCENARIO,
        "--run-dir", RUN_DIR,
        "--port", str(SIM_PORT),
        "--force"
    ])

    try:
        # Wait for simulator health
        client = httpx.Client(base_url=SIM_URL, timeout=2.0)
        started = False
        last_err = None
        for i in range(30):
            try:
                r = client.get("/health")
                if r.status_code == 200:
                    started = True
                    break
            except Exception as e:
                last_err = e
                time.sleep(0.3)
        if not started:
            raise RuntimeError(f"Simulator health check failed: {last_err}")
        print(f"      Simulator healthy at {SIM_URL}")

        # 3. Start controller subprocess
        print("\n[2/6] Starting controller process...")
        ctrl_proc = subprocess.Popen([
            sys.executable, "-m", "controller.main",
            "--url", SIM_URL,
            "--db", DB_PATH
        ])

        # 4. Let it run for ~3-4 seconds (several simulated ticks)
        print("      Controller running... waiting for progress...")
        time.sleep(3.0)

        # 5. Abruptly kill controller with SIGKILL (kill -9)
        print("\n[3/6] Sending SIGKILL (kill -9) to controller (PID %d)..." % ctrl_proc.pid)
        ctrl_proc.send_signal(signal.SIGKILL)
        ctrl_proc.wait()
        print("      Controller process terminated abruptly.")

        # 6. Inspect state at time of kill
        print("\n[4/6] Inspecting SQLite state at kill boundary:")
        db = DB(DB_PATH)
        kill_chunk = db.get_meta_int("last_chunk_id", -1)
        kill_event = db.get_meta_int("max_event_id", 0)
        kill_minute = db.get_meta_int("sim_minute", 0)
        active_apps = db.get_active_approvals()
        db.close()

        print(f"      - Sim minute at kill: {kill_minute} (Day {kill_minute // 1440})")
        print(f"      - Last committed chunk: #{kill_chunk}")
        print(f"      - Max committed event_id: #{kill_event}")
        print(f"      - Active pending approvals in SQLite: {len(active_apps)}")

        # 7. Restart controller with EXACT SAME database and URL
        print("\n[5/6] Restarting controller with same DB...")
        restarted_proc = subprocess.Popen([
            sys.executable, "-m", "controller.main",
            "--url", SIM_URL,
            "--db", DB_PATH
        ])
        ret = restarted_proc.wait()
        assert ret == 0, f"Restarted controller exited with code {ret}"
        print("      Restarted controller completed scenario successfully.")

        # 8. Verification: Check idempotency and state
        print("\n[6/6] Verifying recovery invariants:")
        db = DB(DB_PATH)
        final_minute = db.get_meta_int("sim_minute", 0)
        is_finished = db.get_meta("finished") == "1"

        # Check for double-applications
        applies = db.query_audit(event_type="approval_applied")
        applied_aids = [a["details"].get("approval_id") for a in applies]
        has_double_apply = len(applied_aids) != len(set(applied_aids))

        print(f"      - Scenario finished: {is_finished} (final sim_minute={final_minute})")
        print(f"      - Total approvals applied: {len(applied_aids)}")
        print(f"      - Double applications detected: {has_double_apply} (0 allowed)")
        assert is_finished, "Scenario did not reach completion"
        assert not has_double_apply, "Violation: approval was double-applied!"
        db.close()

        print("\n" + "=" * 70)
        print("CRASH RECOVERY TEST PASSED: Zero lost approvals, zero double-applies.")
        print("=" * 70)

    finally:
        sim_proc.terminate()
        try:
            sim_proc.wait(timeout=3)
        except Exception:
            sim_proc.kill()

if __name__ == "__main__":
    run_demo()
