from __future__ import annotations
import os
import tempfile
import pytest
from controller.cohort import Cohort, CohortEvaluator
from controller.db import DB
from controller.stats import StatsTracker, compute_sample_size, two_proportion_z_test

def test_cohort_evaluator():
    c1 = Cohort(id="senior_ta", where={"age_band": ["60+"], "lang": ["ta"]})
    c2 = Cohort(id="young", where={"age_band": ["18-30"]})
    evaluator = CohortEvaluator([c1, c2])

    assert evaluator.evaluate({"age_band": "60+", "lang": "ta", "city_tier": "1"}) == "senior_ta"
    assert evaluator.evaluate({"age_band": "18-30", "lang": "hi"}) == "young"
    assert evaluator.evaluate({"age_band": "46-60", "lang": "hi"}) == "default"

def test_power_analysis_sample_size():
    # Power analysis with 8% relative MDE
    n = compute_sample_size(baseline_p=0.55, relative_mde=0.08, alpha=0.05, power=0.80)
    assert n >= 150

    # Low baseline proportion still enforces minimum floor
    n_floor = compute_sample_size(baseline_p=0.05, relative_mde=0.08, n_min_floor=150)
    assert n_floor >= 150

def test_two_proportion_z_test():
    # Identical proportions -> not significant
    res_null = two_proportion_z_test(s1=50, n1=100, s2=50, n2=100)
    assert not res_null["significant"]
    assert res_null["z_score"] == 0.0

    # Large clear difference -> statistically significant
    res_sig = two_proportion_z_test(s1=40, n1=200, s2=120, n2=200)
    assert res_sig["significant"]
    assert res_sig["p_value"] < 0.001

def test_stats_tracker_and_caller_health():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        tracker = StatsTracker(db)

        # Record 100 calls for D1 on workflow w1 (30 connected, 15 engaged)
        for _ in range(70):
            tracker.record_call({
                "workflow_id": "w1", "cohort_id": "c1",
                "variant": {"voice": "V1", "caller_id": "D1"},
                "connected": False, "engaged": False
            }, sim_minute=60)
        for _ in range(15):
            tracker.record_call({
                "workflow_id": "w1", "cohort_id": "c1",
                "variant": {"voice": "V1", "caller_id": "D1"},
                "connected": True, "engaged": False
            }, sim_minute=60)
        for _ in range(15):
            tracker.record_call({
                "workflow_id": "w1", "cohort_id": "c1",
                "variant": {"voice": "V1", "caller_id": "D1"},
                "connected": True, "engaged": True
            }, sim_minute=60)

        # Flush to DB and query
        tracker.flush()
        c_stats = tracker.get_cohort_dim_stats("w1", "c1", "voice")
        assert c_stats["V1"]["dials"] == 100
        assert c_stats["V1"]["connected"] == 30
        assert c_stats["V1"]["engaged"] == 15
        assert round(c_stats["V1"]["pickup_rate"], 2) == 0.30
        assert round(c_stats["V1"]["engagement_rate"], 2) == 0.50

        # Caller ID health query
        did_health = tracker.get_caller_id_health("w1", "D1")
        assert did_health["dials"] == 100
        assert did_health["connected"] == 30
        assert round(did_health["pickup_rate"], 2) == 0.30

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
