"""Undirected grouping graph over hunks (section 9.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable

from .schemas import canonical_pair, hunk_sort_key

__all__ = ["DependencyGraph", "GraphEdge"]


@dataclass
class GraphEdge:
    """One dependency rendered as a grouping edge."""

    hunk_a: str
    hunk_b: str
    strength: str
    category: str
    dependency_id: str
    record: dict = field(default_factory=dict)

    @property
    def pair(self) -> tuple[str, str]:
        return canonical_pair(self.hunk_a, self.hunk_b)

    def to_dict(self) -> dict:
        return {
            "dependency_id": self.dependency_id,
            "pair": list(self.pair),
            "strength": self.strength,
            "category": self.category,
        }


class DependencyGraph:
    """Nodes are hunks; edges are dependencies; only chosen strengths couple."""

    def __init__(
        self,
        hunk_ids: Iterable[str],
        dependencies: Iterable[dict] | None = None,
        *,
        edge_strengths: Iterable[str] = ("STRONG",),
        strength_resolver=None,
    ) -> None:
        self.edge_strengths = tuple(edge_strengths)
        self._nodes: list[str] = []
        self._node_set: set[str] = set()
        self.edges: list[GraphEdge] = []
        self.ignored_edges: list[dict] = []
        self._adjacency: dict[str, set[str]] = {}
        self._resolver = strength_resolver
        for hunk_id in hunk_ids:
            self.add_node(str(hunk_id))
        for record in dependencies or []:
            self.add_dependency(record)

    # -- construction ------------------------------------------------------ #

    def add_node(self, hunk_id: str) -> None:
        if hunk_id in self._node_set:
            return
        self._node_set.add(hunk_id)
        self._nodes.append(hunk_id)
        self._adjacency[hunk_id] = set()

    @property
    def nodes(self) -> list[str]:
        return sorted(self._nodes, key=hunk_sort_key)

    def add_dependency(self, record: dict) -> GraphEdge | None:
        strength = (
            self._resolver(record) if self._resolver is not None else record.get("strength")
        )
        if strength not in self.edge_strengths:
            return None
        source = str(record.get("source_hunk"))
        target = str(record.get("target_hunk"))
        if source not in self._node_set or target not in self._node_set:
            self.ignored_edges.append(
                {
                    "reason": "unknown hunk id",
                    "record": {
                        "dependency_id": record.get("dependency_id"),
                        "source_hunk": source,
                        "target_hunk": target,
                    },
                }
            )
            return None
        if source == target:
            self.ignored_edges.append(
                {
                    "reason": "self edge",
                    "record": {"dependency_id": record.get("dependency_id"), "hunk_id": source},
                }
            )
            return None
        edge = GraphEdge(
            hunk_a=source,
            hunk_b=target,
            strength=str(strength),
            category=str(record.get("category")),
            dependency_id=str(record.get("dependency_id")),
            record=record,
        )
        pair = edge.pair
        for existing in self.edges:
            if existing.pair == pair and existing.dependency_id == edge.dependency_id:
                return None
        self.edges.append(edge)
        self._adjacency[source].add(target)
        self._adjacency[target].add(source)
        return edge

    # -- queries ----------------------------------------------------------- #

    def neighbors(self, hunk_id: str) -> list[str]:
        return sorted(self._adjacency.get(hunk_id, set()), key=hunk_sort_key)

    def degree(self, hunk_id: str) -> int:
        return len(self._adjacency.get(hunk_id, set()))

    def is_isolated(self, hunk_id: str) -> bool:
        return self.degree(hunk_id) == 0

    def edges_for(self, hunk_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if hunk_id in edge.pair]

    def components(self) -> list[list[str]]:
        """Connected components in a deterministic order."""
        remaining = set(self.nodes)
        components: list[list[str]] = []
        for start in self.nodes:
            if start not in remaining:
                continue
            stack = [start]
            component: list[str] = []
            while stack:
                current = stack.pop()
                if current not in remaining:
                    continue
                remaining.discard(current)
                component.append(current)
                for neighbor in reversed(self.neighbors(current)):
                    if neighbor in remaining:
                        stack.append(neighbor)
            components.append(sorted(component, key=hunk_sort_key))
        components.sort(key=lambda group: [hunk_sort_key(hunk) for hunk in group])
        return components

    def cross_group_edges(self, group_of: dict[str, int]) -> list[GraphEdge]:
        """Strong edges whose endpoints ended up in different groups."""
        return [
            edge
            for edge in self.edges
            if group_of.get(edge.pair[0]) != group_of.get(edge.pair[1])
        ]

    def to_dict(self) -> dict:
        return {
            "node_count": len(self._nodes),
            "edge_count": len(self.edges),
            "components": len(self.components()),
            "edge_strengths": list(self.edge_strengths),
            "isolated_hunks": [hunk for hunk in self.nodes if self.is_isolated(hunk)],
            "ignored_edges": self.ignored_edges,
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def degree_stats(self) -> dict[str, Any]:
        degrees = {hunk: self.degree(hunk) for hunk in self.nodes}
        values = list(degrees.values()) or [0]
        return {
            "max_degree": max(values),
            "isolated_count": sum(1 for value in values if value == 0),
            "degrees": degrees,
        }
