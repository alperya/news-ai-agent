"""Code quality metric computation — the definitions, with no I/O.

Kept separate from `analyze.py` (which does file discovery, config and JSON
output) so the metric definitions can be unit-tested against small synthetic
sources without touching the filesystem or the repo.

WHY THESE DEFINITIONS ARE WRITTEN OUT RATHER THAN DELEGATED
-----------------------------------------------------------
Cyclomatic complexity and maintainability index come from `radon`, which is
mature and whose numbers are the ones people quote. Duplication, coupling,
depth-of-inheritance and LCOM4 are computed here instead of via pylint's
`symilar` / third-party tools because these numbers get replayed across the
whole commit history and compared over time: a definition that shifts under a
dependency upgrade silently invalidates every prior data point. Owning the
definition is the only way the history stays comparable.

A NOTE ON THE OO METRICS
------------------------
Class coupling, DIT and LCOM4 are Chidamber-Kemerer metrics designed for
object-oriented systems. This codebase is largely procedural — the biggest
module (`lambda_handler.py`) contains no classes at all — so DIT and LCOM4
cover only a small slice of it and will barely move commit to commit. Class
coupling is therefore ALSO measured at module level (efferent coupling: how
many first-party modules a module imports), which is the direct structural
analogue and is where this architecture's real risk lives.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field

from radon.complexity import cc_visit
from radon.metrics import mi_visit

# Minimum consecutive normalised lines that count as a duplicated block.
# 6 sits between PMD/CPD's token-based default and pylint's `min-similarity-lines`
# of 4; at 4 this flags ordinary import headers and boilerplate `except` blocks.
DUPLICATE_BLOCK_LINES = 6


@dataclass
class FunctionComplexity:
    module: str
    name: str
    lineno: int
    complexity: int


@dataclass
class ModuleCoupling:
    module: str
    efferent: int          # Ce — first-party modules this one imports
    afferent: int = 0      # Ca — first-party modules importing this one
    imports: set[str] = field(default_factory=set)

    @property
    def instability(self) -> float:
        """Martin's I = Ce / (Ce + Ca). 0 = maximally stable, 1 = unstable."""
        total = self.efferent + self.afferent
        return round(self.efferent / total, 3) if total else 0.0


@dataclass
class ClassInfo:
    module: str
    name: str
    bases: list[str]
    dit: int = 1
    lcom4: int = 1
    methods: int = 0


# ── Cyclomatic complexity + maintainability index (radon) ────────────────────

def complexity_for_source(module: str, source: str) -> list[FunctionComplexity]:
    """Cyclomatic complexity per function/method. Returns [] on unparseable source."""
    try:
        blocks = cc_visit(source)
    except (SyntaxError, ValueError):
        return []
    return [
        FunctionComplexity(module=module, name=b.fullname, lineno=b.lineno, complexity=b.complexity)
        for b in blocks
    ]


def maintainability_for_source(source: str) -> float | None:
    """Radon maintainability index, 0-100. Higher is better; >=20 is radon's A grade."""
    try:
        return round(float(mi_visit(source, multi=True)), 2)
    except (SyntaxError, ValueError, ZeroDivisionError):
        return None


# ── Duplication ─────────────────────────────────────────────────────────────

_COMMENT = re.compile(r"#.*$")


def _normalise(source: str) -> list[tuple[int, str]]:
    """Strip comments, blank lines and indentation.

    Indentation is dropped deliberately: the same block copied into a different
    nesting level is still duplication. Docstrings are NOT stripped — a repeated
    docstring is repeated text, and stripping them requires a full parse that
    would make this fail closed on syntactically-broken historical commits.
    """
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(source.splitlines(), start=1):
        line = _COMMENT.sub("", raw).strip()
        if line:
            out.append((i, line))
    return out


def duplication(sources: dict[str, str], block_lines: int = DUPLICATE_BLOCK_LINES) -> dict:
    """Percentage of normalised lines that appear inside a duplicated block.

    Hashes every window of `block_lines` consecutive normalised lines across all
    files. Any window whose content is seen more than once marks all of its lines
    as duplicated. Reported as a share of total normalised lines, matching how
    SonarQube expresses its `duplicated_lines_density` gate.

    Counting *lines* rather than *blocks* is what makes the number comparable as
    the codebase grows — a repo that doubles in size with the same duplication
    ratio should report the same figure.
    """
    normalised = {path: _normalise(src) for path, src in sources.items()}
    total_lines = sum(len(v) for v in normalised.values())
    if total_lines == 0:
        return {"duplicated_line_pct": 0.0, "duplicated_lines": 0, "total_lines": 0, "blocks": 0}

    windows: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for path, lines in normalised.items():
        texts = [t for _, t in lines]
        for start in range(len(texts) - block_lines + 1):
            key = hash("\n".join(texts[start:start + block_lines]))
            windows[key].append((path, start))

    duplicated: dict[str, set[int]] = defaultdict(set)
    block_count = 0
    for occurrences in windows.values():
        if len(occurrences) < 2:
            continue
        block_count += 1
        for path, start in occurrences:
            duplicated[path].update(range(start, start + block_lines))

    dup_lines = sum(len(v) for v in duplicated.values())
    return {
        "duplicated_line_pct": round(dup_lines / total_lines * 100, 2),
        "duplicated_lines": dup_lines,
        "total_lines": total_lines,
        "blocks": block_count,
    }


# ── Module coupling ─────────────────────────────────────────────────────────

def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # `from video.footage import x` couples to `video.footage`, but
                # the flat sys.path layout means `from footage import x` is the
                # same module. Record both the head and the dotted form and let
                # the caller intersect against known first-party module names.
                names.add(node.module)
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name)
    return names


def coupling(trees: dict[str, ast.AST], first_party: set[str]) -> dict[str, ModuleCoupling]:
    """Efferent/afferent coupling per module, counting first-party imports only.

    Third-party and stdlib imports are excluded: they say something about
    dependency weight, not about how tangled *this* codebase is with itself,
    and they would drown out the internal signal (every module imports `os`).
    """
    result: dict[str, ModuleCoupling] = {}
    for module, tree in trees.items():
        imported = _imported_names(tree)
        internal = {n for n in imported if n in first_party and n != module}
        result[module] = ModuleCoupling(module=module, efferent=len(internal), imports=internal)

    for mc in result.values():
        for target in mc.imports:
            if target in result:
                result[target].afferent += 1
    return result


# ── Depth of inheritance + LCOM4 ────────────────────────────────────────────

def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _lcom4(cls: ast.ClassDef) -> tuple[int, int]:
    """LCOM4 = number of connected components among a class's methods.

    Two methods are connected if they touch a common `self.<attr>` or one calls
    the other via `self.<method>()`. 1 component = one cohesive responsibility;
    higher means the class is really N classes sharing a name.

    Returns (lcom4, method_count). Classes with 0 or 1 methods score 1 by
    convention — a single method cannot be incohesive.
    """
    methods: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        attrs: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(item):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "self":
                    attrs.add(node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    called.add(node.func.attr)
        methods[item.name] = attrs
        calls[item.name] = called

    names = list(methods)
    if len(names) <= 1:
        return 1, len(names)

    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared_state = bool((methods[a] - set(names)) & (methods[b] - set(names)))
            mutual_call = b in calls[a] or a in calls[b]
            if shared_state or mutual_call:
                union(a, b)

    return len({find(n) for n in names}), len(names)


def classes(trees: dict[str, ast.AST]) -> list[ClassInfo]:
    """Per-class DIT and LCOM4 across the project.

    DIT counts only project-internal base classes: a class inheriting from a
    third-party or stdlib base (Exception, ABC, Protocol) is depth 1, because
    the depth we care about is the hierarchy *this* codebase maintains.
    """
    found: list[ClassInfo] = []
    by_name: dict[str, ClassInfo] = {}
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                lcom, n_methods = _lcom4(node)
                info = ClassInfo(
                    module=module,
                    name=node.name,
                    bases=[b for b in (_base_name(x) for x in node.bases) if b],
                    lcom4=lcom,
                    methods=n_methods,
                )
                found.append(info)
                by_name.setdefault(node.name, info)

    def depth(info: ClassInfo, seen: frozenset[str] = frozenset()) -> int:
        if info.name in seen:      # defensive: a cycle cannot happen in valid
            return 1               # Python, but a malformed historical commit
        best = 1                   # should not hang the backfill
        for base in info.bases:
            parent = by_name.get(base)
            if parent is not None:
                best = max(best, 1 + depth(parent, seen | {info.name}))
        return best

    for info in found:
        info.dit = depth(info)
    return found
