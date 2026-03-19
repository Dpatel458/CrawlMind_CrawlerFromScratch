"""
tracker.py — records the crawl tree as it is visited and saves to JSON.

The tracker is passed into crawl() and called at each node.
It builds a nested tree structure matching the crawl_tree.json format.

Usage
-----
from tracker import CrawlTracker

tracker = CrawlTracker(output_path="crawl_tree.json")
results = crawl(..., tracker=tracker)
tracker.save()          # writes JSON — or set auto_save=True to save automatically
"""

import json
from typing import Optional


class _Node:
    """One node in the crawl tree."""

    def __init__(self, url: str, depth: int):
        self.url      = url
        self.depth    = depth
        self.status   = "ok"
        self.error    = None
        self.sample   = None
        self.children = []

    def to_dict(self) -> dict:
        d = {
            "url":      self.url,
            "depth":    self.depth,
            "status":   self.status,
            "children": [c.to_dict() for c in self.children],
            "sample":   self.sample,
        }
        if self.error:
            d["error"] = self.error
        return d


class CrawlTracker:
    """
    Tracks the crawl tree in real time.

    Parameters
    ----------
    output_path : str  — path to write the JSON file. Default: "crawl_tree.json"
    auto_save   : bool — save automatically after crawl() returns. Default: True
    indent      : int  — JSON indentation. Default: 2
    """

    def __init__(self, output_path="crawl_tree.json", auto_save=True, indent=2):
        self.output_path = output_path
        self.auto_save   = auto_save
        self.indent      = indent

        self._root       = None          # _Node — set on first visit
        self._stack      = []            # tracks the current path through the tree
        self._node_map   = {}            # url → _Node, for fast lookup

    # ── called by crawler ─────────────────────────────────────────────────────

    def on_visit(self, url: str, depth: int):
        """Called when a URL is first visited, before fetching."""
        node = _Node(url=url, depth=depth)
        self._node_map[url] = node

        if depth == 0:
            self._root = node
        else:
            # attach to parent — the last node at depth-1 in the stack
            parent = self._find_parent(depth)
            if parent:
                parent.children.append(node)

        # maintain depth stack
        self._stack = [n for n in self._stack if n.depth < depth]
        self._stack.append(node)

    def on_success(self, url: str, sample: str):
        """Called after a page is successfully fetched and parsed."""
        node = self._node_map.get(url)
        if node:
            node.status = "ok"
            node.sample = sample[:200] if sample else None

    def on_error(self, url: str, error: str):
        """Called when a page fetch fails or is skipped."""
        node = self._node_map.get(url)
        if node:
            node.status = "error"
            node.error  = error

    # ── save ──────────────────────────────────────────────────────────────────

    def save(self):
        """Write the crawl tree to the JSON file."""
        if self._root is None:
            print("[tracker] nothing to save — crawl produced no nodes.")
            return
        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(self._root.to_dict(), f, indent=self.indent, ensure_ascii=False)
        total = len(self._node_map)
        errors = sum(1 for n in self._node_map.values() if n.status == "error")
        print(f"[tracker] saved {total} nodes ({errors} errors) → {self.output_path}")

    def get_tree(self) -> Optional[dict]:
        """Return the tree as a plain dict (without saving)."""
        return self._root.to_dict() if self._root else None

    # ── internal ──────────────────────────────────────────────────────────────

    def _find_parent(self, depth: int) -> Optional[_Node]:
        """Walk the stack backwards to find the nearest node at depth-1."""
        for node in reversed(self._stack):
            if node.depth == depth - 1:
                return node
        return None
