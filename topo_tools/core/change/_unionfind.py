"""Path-compressed disjoint-set union over string keys.

Nodes are "a:<fid>" / "b:<fid>"; edges are pair rows that pass tau_match or
the identity-claim guard in _03_classify.py. Ported from topo-tools-js's
polygon-changelog/unionFind.ts.
"""


class UnionFind:
    """Disjoint-set union with union-by-rank and path compression."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def add(self, x: str) -> None:
        """Register x as its own singleton component if not already present."""
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0

    def find(self, x: str) -> str:
        """Return x's component root, path-compressing along the way."""
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        """Merge a's and b's components."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        rank_a, rank_b = self._rank[ra], self._rank[rb]
        if rank_a < rank_b:
            self._parent[ra] = rb
        elif rank_a > rank_b:
            self._parent[rb] = ra
        else:
            self._parent[rb] = ra
            self._rank[ra] += 1

    def components(self) -> dict[str, list[str]]:
        """Return {root: [members]} for every component."""
        out: dict[str, list[str]] = {}
        for x in self._parent:
            out.setdefault(self.find(x), []).append(x)
        return out
