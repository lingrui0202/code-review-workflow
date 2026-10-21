"""工作流图定义：节点 + 带条件的边。

图是"配置"而不是"代码"：换顺序、加节点、改分支条件都在这一层完成，
引擎和 Agent 都不用动。边上的 ``when`` 是一个以状态为入参的谓词，
因此分支和循环都是**数据驱动**的——同一份图，不同输入可以走不同路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .node import Node
from .state import WorkflowState

Predicate = Callable[[WorkflowState], bool]


@dataclass
class Edge:
    """一条有向边。``when=None`` 表示无条件。"""

    source: str
    target: str
    when: Predicate | None = None
    label: str = ""

    def matches(self, state: WorkflowState) -> bool:
        if self.when is None:
            return True
        try:
            return bool(self.when(state))
        except Exception:  # 条件写错不应该炸掉整个流程
            return False

    def describe(self) -> str:
        if self.when is None:
            return self.label
        return self.label or getattr(self.when, "__name__", "条件")

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "label": self.describe()}


class GraphError(ValueError):
    """图的静态检查没通过。"""


class Graph:
    """一张有向图，支持条件分支和回边（循环）。"""

    def __init__(self, name: str = "workflow", description: str = "") -> None:
        self.name = name
        self.description = description
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._start: str | None = None

    # ------------------------------------------------------------------ 构建
    def add_node(self, node: Node, start: bool = False) -> "Graph":
        if node.name in self._nodes:
            raise GraphError(f"节点名重复：{node.name}")
        self._nodes[node.name] = node
        if start or self._start is None:
            self._start = node.name
        return self

    def add_edge(
        self,
        source: str,
        target: str,
        when: Predicate | None = None,
        label: str = "",
    ) -> "Graph":
        self._edges.append(Edge(source=source, target=target, when=when, label=label))
        return self

    def chain(self, *names: str) -> "Graph":
        """把一串节点按顺序连起来。"""
        for left, right in zip(names, names[1:]):
            self.add_edge(left, right)
        return self

    # ------------------------------------------------------------------ 查询
    @property
    def start(self) -> str | None:
        return self._start

    def node(self, name: str) -> Node:
        try:
            return self._nodes[name]
        except KeyError as exc:
            raise GraphError(f"图里没有节点 {name!r}") from exc

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def node_names(self) -> list[str]:
        return list(self._nodes)

    def edges(self) -> list[Edge]:
        return list(self._edges)

    def edges_of(self, name: str) -> list[Edge]:
        return [edge for edge in self._edges if edge.source == name]

    def resolve(self, state: WorkflowState, node: str) -> list[str]:
        """给定当前节点和状态，算出下一步能走哪儿（按声明顺序）。"""
        targets: list[str] = []
        for edge in self.edges_of(node):
            if edge.matches(state) and edge.target not in targets:
                targets.append(edge.target)
        return targets

    def is_terminal(self, node: str) -> bool:
        return not self.edges_of(node)

    # ------------------------------------------------------------------ 校验
    def validate(self) -> "Graph":
        """静态检查：起止点、悬空边、可达性、无条件分支歧义。"""
        if not self._nodes:
            raise GraphError("图里没有任何节点")
        if not self._start or self._start not in self._nodes:
            raise GraphError(f"起始节点未定义或不存在：{self._start!r}")
        for edge in self._edges:
            for endpoint in (edge.source, edge.target):
                if endpoint not in self._nodes:
                    raise GraphError(f"边 {edge.source}→{edge.target} 引用了不存在的节点 {endpoint!r}")
        # 无条件出边只能有一条，否则执行顺序不确定
        for name in self._nodes:
            unconditional = [e for e in self.edges_of(name) if e.when is None]
            if len(unconditional) > 1:
                raise GraphError(f"节点 {name} 有 {len(unconditional)} 条无条件出边，请改成条件边")
        # 可达性
        reachable = self._reachable()
        unreachable = set(self._nodes) - reachable
        if unreachable:
            raise GraphError(f"以下节点从起点不可达：{sorted(unreachable)}")
        return self

    def _reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [self._start] if self._start else []
        while stack:
            current = stack.pop()
            if current in seen or current not in self._nodes:
                continue
            seen.add(current)
            stack.extend(edge.target for edge in self.edges_of(current))
        return seen

    def has_cycle(self) -> bool:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._nodes}

        def visit(name: str) -> bool:
            color[name] = GRAY
            for edge in self.edges_of(name):
                if color.get(edge.target) == GRAY:
                    return True
                if color.get(edge.target) == WHITE and visit(edge.target):
                    return True
            color[name] = BLACK
            return False

        return any(color[name] == WHITE and visit(name) for name in self._nodes)

    # ------------------------------------------------------------------ 展示
    def to_ascii(self) -> str:
        """把图摊成一张字符画，CLI 与报告里共用。"""
        lines = []
        for name in self._nodes:
            node = self._nodes[name]
            marker = "▶" if name == self._start else " "
            lines.append(f"{marker} [{node.kind:9s}] {name}  —  {node.description}")
            for edge in self.edges_of(name):
                cond = edge.describe()
                arrow = f"    ├─▶ {edge.target}" + (f"   （{cond}）" if cond else "")
                lines.append(arrow)
            if self.is_terminal(name):
                lines.append("    └─▶ <结束>")
        return "\n".join(lines)

    def to_mermaid(self) -> str:
        lines = ["graph TD"]
        for name in self._nodes:
            label = name.replace('"', "'")
            lines.append(f'    {_mid(name)}["{label}"]')
        for edge in self._edges:
            label = edge.describe().replace('"', "'")
            arrow = f"-->|{label}|" if label else "-->"
            lines.append(f"    {_mid(edge.source)} {arrow} {_mid(edge.target)}")
        return "\n".join(lines)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "start": self.start,
            "nodes": [
                {"name": n.name, "kind": n.kind, "description": n.description}
                for n in self.nodes()
            ],
            "edges": [edge.to_dict() for edge in self._edges],
            "has_cycle": self.has_cycle(),
        }

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def __iter__(self) -> Iterable[Node]:
        return iter(self._nodes.values())


def _mid(name: str) -> str:
    """mermaid 节点 id 只能安全地用字母数字下划线。"""
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
