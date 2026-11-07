from __future__ import annotations
import tempfile
import os
import pytest
from controller.db import DB
from controller.experimenter import ExperimentManager
from controller.stats import StatsTracker

def test_experiment_planning_and_split():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        stats = StatsTracker(db)
        exp_mgr = ExperimentManager(db, stats)

        wf_spec = {
            "options": {"voice": ["V1", "V2", "V3"], "welcome_message": ["W1"]},
            "daily_batch": {"size": 1000},
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}
        }
        current_cfg = {
            "version": 1,
            "cohorts": [],
            "allocations": {"default": {"voice": {"V1": 1.0}, "welcome_message": {"W1": 1.0}}}
        }

        plan = exp_mgr.plan_experiment("w1", wf_spec, current_cfg, sim_minute=60)
        assert plan is not None
        assert plan["action_type"] == "experiment_start"
        alloc = plan["proposed_config"]["allocations"]["default"]["voice"]
        assert alloc["V1"] == 0.7
        assert alloc["V2"] == 0.3
        assert 0.0 <= plan["impact_score"] <= 1.0
        assert plan["affected_leads"] == 300
        assert "observed_successes" in plan["dry_run"]

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

def test_winner_promotion_and_guardrail_gate():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        stats = StatsTracker(db)
        exp_mgr = ExperimentManager(db, stats)

        wf_spec = {
            "options": {"voice": ["V1", "V2"]},
            "daily_batch": {"size": 1000},
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}
        }
        current_cfg = {
            "version": 1,
            "cohorts": [],
            "allocations": {"default": {"voice": {"V1": 0.7, "V2": 0.3}}}
        }

        # Plan experiment
        plan = exp_mgr.plan_experiment("w1", wf_spec, current_cfg, sim_minute=60)

        # Record calls for V1 (baseline: 200 connected, 100 engaged -> 50%)
        # and pickup: 500 dials, 200 connected -> 40%
        for i in range(500):
            stats.record_call({
                "workflow_id": "w1", "cohort_id": "default",
                "variant": {"voice": "V1"},
                "connected": (i < 200),
                "engaged": (i < 100)
            }, sim_minute=120)

        # Record calls for V2 (candidate: 200 connected, 150 engaged -> 75% lift!)
        # Pickup: 500 dials, 200 connected -> 40% pickup (guardrail safe)
        for i in range(500):
            stats.record_call({
                "workflow_id": "w1", "cohort_id": "default",
                "variant": {"voice": "V2"},
                "connected": (i < 200),
                "engaged": (i < 150)
            }, sim_minute=120)

        stats.flush()
        proposals = exp_mgr.evaluate_active_experiments("w1", wf_spec, current_cfg, sim_minute=120)
        assert len(proposals) == 1
        prop = proposals[0]
        assert prop["action_type"] == "winner_promotion"
        assert prop["proposed_config"]["allocations"]["default"]["voice"] == {"V2": 1.0}

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

def test_guardrail_gate_blocks_promotion():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        stats = StatsTracker(db)
        exp_mgr = ExperimentManager(db, stats)

        wf_spec = {
            "options": {"voice": ["V1", "V2"]},
            "daily_batch": {"size": 1000},
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}
        }
        current_cfg = {
            "version": 1,
            "cohorts": [],
            "allocations": {"default": {"voice": {"V1": 0.7, "V2": 0.3}}}
        }

        plan = exp_mgr.plan_experiment("w1", wf_spec, current_cfg, sim_minute=60)

        # V1: 500 dials, 200 connected (40% pickup), 100 engaged (50% eng)
        for i in range(500):
            stats.record_call({
                "workflow_id": "w1", "cohort_id": "default",
                "variant": {"voice": "V1"},
                "connected": (i < 200),
                "engaged": (i < 100)
            }, sim_minute=120)

        # V2: Higher engagement (120/150 = 80%), BUT terrible pickup (1500 dials, 150 connected = 10% pickup)
        # Drop is 40% -> 10% (0.30 drop > 0.02 max drop limit!)
        for i in range(1500):
            stats.record_call({
                "workflow_id": "w1", "cohort_id": "default",
                "variant": {"voice": "V2"},
                "connected": (i < 150),
                "engaged": (i < 120)
            }, sim_minute=120)

        stats.flush()
        # Even with high engagement, guardrail drop must block promotion and revert!
        proposals = exp_mgr.evaluate_active_experiments("w1", wf_spec, current_cfg, sim_minute=120)
        assert len(proposals) == 1
        prop = proposals[0]
        assert prop["action_type"] == "experiment_revert"
        assert prop["proposed_config"]["allocations"]["default"]["voice"] == {"V1": 1.0}

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
