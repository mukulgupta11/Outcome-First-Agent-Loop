from __future__ import annotations
import tempfile
import os
import pytest
from controller.db import DB
from controller.detector import DetectorManager
from controller.stats import StatsTracker

def test_hallucination_two_window_requirement():
    detector = DetectorManager(window_size=30, hallucination_threshold=0.03)
    wf_spec = {"options": {"prompt_version": ["P1", "P2"]}}
    workflows = {"w1": wf_spec}

    # Window 1: 30 calls, 2 hallucinations (6.7% > 3%)
    for i in range(30):
        detector.process_call({
            "workflow_id": "w1",
            "connected": True,
            "variant": {"prompt_version": "P1"},
            "qa": {"hallucination_flag": (i < 2)}
        })

    # Window 1 finished: should NOT trigger yet! (Needs 2 consecutive windows)
    reps = detector.check_hallucinations(workflows)
    assert len(reps) == 0

    # Window 2: clean window (0 hallucinations out of 30)
    for _ in range(30):
        detector.process_call({
            "workflow_id": "w1",
            "connected": True,
            "variant": {"prompt_version": "P1"},
            "qa": {"hallucination_flag": False}
        })
    # Reset elevated window count on clean window
    assert len(detector.check_hallucinations(workflows)) == 0

    # Now provide 2 consecutive elevated windows:
    for _ in range(2):
        for i in range(30):
            detector.process_call({
                "workflow_id": "w1",
                "connected": True,
                "variant": {"prompt_version": "P1"},
                "qa": {"hallucination_flag": (i < 2)}
            })

    # Now it should trigger!
    reps = detector.check_hallucinations(workflows)
    assert len(reps) == 1
    assert reps[0].anomaly_type == "hallucination_spike"
    assert reps[0].target == "P1"

def test_caller_id_detection():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        stats = StatsTracker(db)
        detector = DetectorManager()
        workflows = {"w1": {"options": {"caller_id": ["D1", "D2"]}}}

        # D1: 100 dials, 40 connected (40% pickup)
        for i in range(100):
            stats.record_call({
                "workflow_id": "w1", "cohort_id": "*",
                "variant": {"caller_id": "D1"},
                "connected": (i < 40), "engaged": False
            }, sim_minute=60)

        # D2: 100 dials, 10 connected (10% pickup -> 75% drop from D1)
        for i in range(100):
            stats.record_call({
                "workflow_id": "w1", "cohort_id": "*",
                "variant": {"caller_id": "D2"},
                "connected": (i < 10), "engaged": False
            }, sim_minute=60)

        stats.flush()
        reports = detector.check_caller_ids(workflows, stats)
        assert len(reports) == 1
        assert reports[0].anomaly_type == "caller_id_degraded"
        assert reports[0].target == "D2"
        assert reports[0].evidence["dials"] == 100
        assert round(reports[0].evidence["pickup_rate"], 2) == 0.10

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
