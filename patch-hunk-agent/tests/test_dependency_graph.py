"""Graph behaviour: deterministic components and honest handling of bad ids."""

from __future__ import annotations

from helpers import dependency
from patch_hunk_agent.dependency_graph import DependencyGraph


def test_components_follow_strong_edges_only():
    records = [
        dependency("H1", "H2"),
        dependency("H3", "H4", strength="WEAK", dependency_id="D2"),
    ]
    graph = DependencyGraph(["H1", "H2", "H3", "H4"], records)
    assert graph.components() == [["H1", "H2"], ["H3"], ["H4"]]
    assert graph.degree("H1") == 1
    assert graph.is_isolated("H3") is True


def test_component_order_is_stable_and_numeric():
    records = [dependency("H2", "H10")]
    graph = DependencyGraph(["H10", "H2", "H1"], records)
    assert graph.components() == [["H1"], ["H2", "H10"]]
    assert graph.nodes == ["H1", "H2", "H10"]


def test_unknown_endpoints_are_recorded_not_crashed():
    graph = DependencyGraph(["H1"], [dependency("H1", "H7")])
    assert graph.edges == []
    assert graph.ignored_edges[0]["reason"] == "unknown hunk id"


def test_self_edges_are_ignored():
    graph = DependencyGraph(["H1"], [dependency("H1", "H1")])
    assert graph.edges == []
    assert graph.ignored_edges[0]["reason"] == "self edge"


def test_cross_group_edges_are_detected():
    records = [dependency("H1", "H2")]
    graph = DependencyGraph(["H1", "H2"], records)
    group_of = {"H1": 1, "H2": 2}
    crossing = graph.cross_group_edges(group_of)
    assert len(crossing) == 1
    assert crossing[0].to_dict()["pair"] == ["H1", "H2"]


def test_degree_stats_and_serialisation():
    records = [dependency("H1", "H2"), dependency("H1", "H3", dependency_id="D2")]
    graph = DependencyGraph(["H1", "H2", "H3", "H4"], records)
    stats = graph.degree_stats()
    assert stats["max_degree"] == 2
    assert stats["isolated_count"] == 1
    payload = graph.to_dict()
    assert payload["node_count"] == 4 and payload["edge_count"] == 2
