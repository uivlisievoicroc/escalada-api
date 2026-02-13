from escalada.api.ranking_time_tiebreak import resolve_rankings_with_time_tiebreak


def _ctx(**kwargs):
    defaults = {
        "scores": {
            "Ana": [10.0],
            "Bob": [10.0],
            "Cris": [9.0],
            "Dan": [8.0],
        },
        "times": {
            "Ana": [120],
            "Bob": [140],
            "Cris": [150],
            "Dan": [160],
        },
        "route_count": 1,
        "active_route_index": 1,
        "box_id": 0,
        "time_criterion_enabled": True,
        "active_holds_count": 10,
        "resolved_fingerprint": None,
        "resolved_decision": None,
    }
    defaults.update(kwargs)
    return resolve_rankings_with_time_tiebreak(**defaults)


def test_unresolved_podium_tie_exposes_pending_previous_rounds_event():
    result = _ctx()
    assert result["has_eligible_tie"] is True
    assert result["fingerprint"]
    rows = result["overall_rows"]
    assert rows[0]["rank"] == 1
    assert rows[1]["rank"] == 1
    events = result["eligible_groups"]
    assert events
    assert events[0]["stage"] == "previous_rounds"
    assert events[0]["affects_podium"] is True
    assert events[0]["status"] in {"pending", "error"}
    assert result["is_resolved"] is False


def test_previous_rounds_yes_splits_top3_tie():
    initial = _ctx()
    fp = initial["eligible_groups"][0]["fingerprint"]
    resolved = _ctx(
        prev_resolved_decisions={fp: "yes"},
        prev_ranks_by_fingerprint={fp: {"Ana": 1, "Bob": 2}},
    )
    by_name = {row["name"]: row for row in resolved["overall_rows"]}
    assert by_name["Ana"]["rank"] == 1
    assert by_name["Bob"]["rank"] == 2
    assert by_name["Ana"]["tb_prev"] is True
    assert resolved["is_resolved"] is True


def test_previous_rounds_no_then_time_yes_splits_tie():
    initial = _ctx()
    fp = initial["eligible_groups"][0]["fingerprint"]
    resolved = _ctx(
        prev_resolved_decisions={fp: "no"},
        resolved_decisions={fp: "yes"},
    )
    by_name = {row["name"]: row for row in resolved["overall_rows"]}
    assert by_name["Ana"]["rank"] == 1
    assert by_name["Bob"]["rank"] == 2
    assert by_name["Ana"]["tb_time"] is True
    assert by_name["Bob"]["tb_time"] is True
    assert resolved["is_resolved"] is True


def test_three_way_partial_previous_rounds_then_time_for_subgroup():
    initial = _ctx(
        scores={"Ana": [10.0], "Bob": [10.0], "Cris": [10.0], "Dan": [8.0]},
        times={"Ana": [100], "Bob": [130], "Cris": [150], "Dan": [200]},
    )
    fp_prev = initial["eligible_groups"][0]["fingerprint"]
    after_prev = _ctx(
        scores={"Ana": [10.0], "Bob": [10.0], "Cris": [10.0], "Dan": [8.0]},
        times={"Ana": [100], "Bob": [130], "Cris": [150], "Dan": [200]},
        prev_resolved_decisions={fp_prev: "yes"},
        prev_ranks_by_fingerprint={fp_prev: {"Cris": 1, "Ana": 2, "Bob": 2}},
    )
    time_events = [ev for ev in after_prev["eligible_groups"] if ev["stage"] == "time"]
    assert time_events
    fp_time = time_events[0]["fingerprint"]
    resolved = _ctx(
        scores={"Ana": [10.0], "Bob": [10.0], "Cris": [10.0], "Dan": [8.0]},
        times={"Ana": [100], "Bob": [130], "Cris": [150], "Dan": [200]},
        prev_resolved_decisions={fp_prev: "yes"},
        prev_ranks_by_fingerprint={fp_prev: {"Cris": 1, "Ana": 2, "Bob": 2}},
        resolved_decisions={fp_time: "yes"},
    )
    assert [row["name"] for row in resolved["overall_rows"][:3]] == ["Cris", "Ana", "Bob"]
    assert [row["rank"] for row in resolved["overall_rows"][:3]] == [1, 2, 3]


def test_non_podium_tie_stays_shared_and_is_not_exposed_for_resolution():
    result = _ctx(
        scores={
            "Ana": [10.0],
            "Bob": [9.0],
            "Cris": [8.0],
            "Dan": [7.0],
            "Ema": [7.0],
        },
        times={
            "Ana": [100],
            "Bob": [110],
            "Cris": [120],
            "Dan": [130],
            "Ema": [140],
        },
    )
    assert result["has_eligible_tie"] is True
    assert result["is_resolved"] is True
    assert result["eligible_groups"] == []


def test_invalid_previous_rounds_input_reports_error():
    initial = _ctx()
    fp = initial["eligible_groups"][0]["fingerprint"]
    result = _ctx(
        prev_resolved_decisions={fp: "yes"},
        prev_ranks_by_fingerprint={fp: {"Ana": 1}},
    )
    assert result["is_resolved"] is False
    assert result["errors"] == []
    rows = {row["name"]: row for row in result["overall_rows"]}
    assert rows["Ana"]["rank"] == 1
    assert rows["Bob"]["rank"] == 2


def test_old_podium_decision_does_not_split_when_tie_moves_below_podium():
    initial = _ctx(
        scores={"Top": [40.0], "Ana": [30.0], "Bob": [30.0]},
        times={"Top": [80], "Ana": [100], "Bob": [120]},
        active_holds_count=100,
    )
    fp = initial["eligible_groups"][0]["fingerprint"]
    resolved = _ctx(
        scores={"Top": [40.0], "Ana": [30.0], "Bob": [30.0]},
        times={"Top": [80], "Ana": [100], "Bob": [120]},
        active_holds_count=100,
        prev_resolved_decisions={fp: "yes"},
        prev_ranks_by_fingerprint={fp: {"Ana": 1, "Bob": 2}},
    )
    resolved_by_name = {row["name"]: row for row in resolved["overall_rows"]}
    assert resolved_by_name["Ana"]["rank"] == 2
    assert resolved_by_name["Bob"]["rank"] == 3

    moved = _ctx(
        scores={
            "Top": [40.0],
            "Cara": [35.0],
            "Dan": [34.0],
            "Ana": [30.0],
            "Bob": [30.0],
        },
        times={
            "Top": [80],
            "Cara": [90],
            "Dan": [95],
            "Ana": [100],
            "Bob": [120],
        },
        active_holds_count=100,
        prev_resolved_decisions={fp: "yes"},
        prev_ranks_by_fingerprint={fp: {"Ana": 1, "Bob": 2}},
        prev_resolved_fingerprint=fp,
        prev_resolved_decision="yes",
    )
    moved_by_name = {row["name"]: row for row in moved["overall_rows"]}
    assert moved_by_name["Ana"]["rank"] == 4
    assert moved_by_name["Bob"]["rank"] == 4


def test_tail_below_podium_collapses_when_tie_group_spans_3_4_5():
    initial = _ctx(
        scores={"Top": [40.0], "Second": [39.0], "Ana": [30.0], "Bob": [30.0], "Cara": [30.0]},
        times={"Top": [80], "Second": [85], "Ana": [100], "Bob": [110], "Cara": [120]},
        active_holds_count=100,
    )
    fp = initial["eligible_groups"][0]["fingerprint"]
    resolved = _ctx(
        scores={"Top": [40.0], "Second": [39.0], "Ana": [30.0], "Bob": [30.0], "Cara": [30.0]},
        times={"Top": [80], "Second": [85], "Ana": [100], "Bob": [110], "Cara": [120]},
        active_holds_count=100,
        prev_resolved_decisions={fp: "yes"},
        prev_ranks_by_fingerprint={fp: {"Ana": 1, "Bob": 2, "Cara": 3}},
        prev_resolved_fingerprint=fp,
        prev_resolved_decision="yes",
    )
    rows = {row["name"]: row for row in resolved["overall_rows"]}
    assert rows["Ana"]["rank"] == 3
    assert rows["Bob"]["rank"] == 4
    assert rows["Cara"]["rank"] == 4


def test_incremental_prev_rounds_memory_keeps_existing_split_when_tie_expands():
    initial = _ctx(
        scores={"Ana": [10.0], "Bob": [10.0], "Dan": [8.0]},
        times={"Ana": [100], "Bob": [120], "Dan": [200]},
    )
    fp_prev = initial["eligible_groups"][0]["fingerprint"]
    lineage_key = initial["eligible_groups"][0]["lineage_key"]
    resolved_two = _ctx(
        scores={"Ana": [10.0], "Bob": [10.0], "Dan": [8.0]},
        times={"Ana": [100], "Bob": [120], "Dan": [200]},
        prev_resolved_decisions={fp_prev: "yes"},
        prev_ranks_by_fingerprint={fp_prev: {"Ana": 1, "Bob": 2}},
        prev_lineage_ranks_by_key={lineage_key: {"Ana": 1, "Bob": 2}},
    )
    rows_two = {row["name"]: row for row in resolved_two["overall_rows"]}
    assert rows_two["Ana"]["rank"] == 1
    assert rows_two["Bob"]["rank"] == 2

    expanded = _ctx(
        scores={"Ana": [10.0], "Bob": [10.0], "Cris": [10.0], "Dan": [8.0]},
        times={"Ana": [100], "Bob": [120], "Cris": [140], "Dan": [200]},
        prev_lineage_ranks_by_key={lineage_key: {"Ana": 1, "Bob": 2}},
    )
    rows_expanded = {row["name"]: row for row in expanded["overall_rows"]}
    assert rows_expanded["Ana"]["rank"] == 1
    assert rows_expanded["Bob"]["rank"] == 2
    assert rows_expanded["Cris"]["rank"] == 3
    pending_prev = [ev for ev in expanded["eligible_groups"] if ev["stage"] == "previous_rounds"]
    assert pending_prev
    assert pending_prev[0]["lineage_key"] == lineage_key
    assert pending_prev[0]["known_prev_ranks_by_name"] == {"Ana": 1, "Bob": 2}
    assert pending_prev[0]["missing_prev_rounds_members"] == ["Cris"]
    assert pending_prev[0]["requires_prev_rounds_input"] is True
