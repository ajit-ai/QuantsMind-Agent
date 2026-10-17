"""Symbol-level navigation over Python source.

v0.2 could read files and grep them. That is enough to *find* text and not
enough to *understand* code: to change a function safely an agent has to know
where it starts, where it ends, what its signature is, and whether anything else
in the project calls it. Reading whole files to answer that wastes the context
window on a large module and fails outright when the file is bigger than the
read limit.

This module is the deterministic half of that capability: it turns source text
into structured facts using :mod:`ast`, with no index, no cache, no background
process and no model call. It is pure -- it never touches the filesystem, never
raises for malformed input, and the same source always yields the same answer.
The tools in :mod:`quantsmind_agent.code_tools` add path resolution and bounds.

Only Python is supported, deliberately and honestly: a wrong outline for a
language this cannot parse would be worse than no outline, so callers are told
plainly when a file is out of scope rather than being given a guess.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = [
    "Symbol",
    "find_symbols",
    "find_uses",
    "parse_symbols",
]

#: Upper bound on symbols reported for one file, so a generated module cannot
#: flood the context window.
MAX_SYMBOLS_PER_FILE = 400


@dataclass(frozen=True, slots=True)
class Symbol:
    """One class, function, method or property, with its exact source range.

    ``line``/``end_line`` are 1-based and inclusive, which is what
    ``edit_file``'s ``replace_lines`` operation addresses, so an outline can be
    turned into a precise edit without re-reading the file.
    """

    name: str
    qualified: str
    kind: str
    line: int
    end_line: int
    signature: str
    doc: str
    decorators: tuple[str, ...]
    parent: str

    @property
    def is_method(self) -> bool:
        return self.kind == "method"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "qualified": self.qualified,
            "kind": self.kind,
            "line": self.line,
            "end_line": self.end_line,
            "signature": self.signature,
            "decorators": list(self.decorators),
            "doc": self.doc,
            "parent": self.parent,
        }


def parse_symbols(source: str) -> list[Symbol]:
    """Return every class/function/method/property in ``source``, in file order.

    Raises:
        SyntaxError: the source does not parse. Callers turn this into a failed
            tool result that names the line, never into a crash.
    """

    tree = ast.parse(source)
    found: list[Symbol] = []

    def visit(node: ast.AST, parent: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qualified = f"{parent}.{child.name}" if parent else child.name
                found.append(_class_symbol(child, qualified, parent))
                visit(child, qualified, True)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{parent}.{child.name}" if parent else child.name
                found.append(_function_symbol(child, qualified, parent, in_class=in_class))
                # Recurse so nested and closure functions are not missed. Inside
                # a function body a def is a closure, not a method, so the class
                # context does not carry into the body.
                visit(child, qualified, False)
            else:
                visit(child, parent, in_class)

    visit(tree, "", False)
    return found


def find_symbols(source: str, name: str) -> list[Symbol]:
    """Definitions of ``name``, matched on the simple or the qualified name."""

    wanted = name.strip()
    return [
        symbol
        for symbol in parse_symbols(source)
        if symbol.name == wanted or symbol.qualified == wanted
    ]


def find_uses(source: str, name: str) -> list[int]:
    """1-based lines where ``name`` is read, used, imported or defined.

    Three kinds of reference matter to a reader of code:

    * plain reads -- only *loads* count, because ``NAME = 1`` stores the name
      rather than reading it and pointing the model at it would be noise;
    * attribute access (``obj.name``), because call sites overwhelmingly look
      like that;
    * imports, which are the line where a reader first learns the name is used
      elsewhere at all.

    Matching is by name, so an unrelated attribute that happens to share the name
    is reported too. Over-reporting is deliberate: a missed call site costs a
    broken refactor, while a spurious one costs one look.
    """

    tree = ast.parse(source)
    lines = {line for node in ast.walk(tree) if (line := _reference_line(node, name)) is not None}
    return sorted(lines)


def _reference_line(node: ast.AST, name: str) -> int | None:
    """The line this node references ``name`` on, or ``None`` if it does not."""

    if isinstance(node, ast.Name):
        return node.lineno if node.id == name and isinstance(node.ctx, ast.Load) else None
    if isinstance(node, ast.Attribute):
        return node.lineno if node.attr == name else None
    if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
        (alias.asname or _imported_root(alias)) == name for alias in node.names
    ):
        return node.lineno
    return None


def _imported_root(alias: ast.alias) -> str:
    """The name an import binds: ``pkg.mod.name`` binds ``name``."""

    return alias.name.split(".")[-1] if alias.asname is None else alias.asname


def _class_symbol(node: ast.ClassDef, qualified: str, parent: str) -> Symbol:
    return Symbol(
        name=node.name,
        qualified=qualified,
        kind="class",
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        signature=_render_class_header(node),
        doc=_doc_summary(node),
        decorators=tuple(ast.unparse(item) for item in node.decorator_list),
        parent=parent,
    )


def _function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified: str,
    parent: str,
    *,
    in_class: bool,
) -> Symbol:
    return Symbol(
        name=node.name,
        qualified=qualified,
        kind=_function_kind(node, in_class),
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        signature=_render_function_header(node),
        doc=_doc_summary(node),
        decorators=tuple(ast.unparse(item) for item in node.decorator_list),
        parent=parent,
    )


def _function_kind(node: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool) -> str:
    decorators = {ast.unparse(item).split("(")[0] for item in node.decorator_list}
    if decorators & {"property", "cached_property"} or any(
        item.endswith(".setter") or item.endswith(".getter") for item in decorators
    ):
        return "property"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async method" if in_class else "async function"
    return "method" if in_class else "function"


def _doc_summary(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """The first line of a docstring, trimmed to something displayable."""

    doc = ast.get_docstring(node) or ""
    first = doc.strip().splitlines()
    return first[0].strip() if first else ""


def _render_class_header(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    joined = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    return joined[:300]


def _render_function_header(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({_render_args(node.args)}){returns}"[:300]


def _render_args(args: ast.arguments) -> str:
    """Rebuild a signature's parameter list from the AST.

    Reconstructed rather than sliced out of the source so the result does not
    depend on how the file happens to wrap its line breaks.
    """

    parts: list[str] = []
    positional = [*args.posonlyargs, *args.args]
    defaults = list(args.defaults)
    first_default = len(positional) - len(defaults)

    for index, arg in enumerate(positional):
        text = arg.arg
        if arg.annotation is not None:
            text = f"{text}: {ast.unparse(arg.annotation)}"
        if index >= first_default:
            text = f"{text} = {ast.unparse(defaults[index - first_default])}"
        parts.append(text)
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            parts.append("/")

    if args.vararg is not None:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        text = arg.arg
        if arg.annotation is not None:
            text = f"{text}: {ast.unparse(arg.annotation)}"
        if default is not None:
            text = f"{text} = {ast.unparse(default)}"
        parts.append(text)

    if args.kwarg is not None:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)
