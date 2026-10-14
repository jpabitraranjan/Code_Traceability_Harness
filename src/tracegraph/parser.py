"""Tree-sitter extraction: symbols (with exact line ranges), call sites, imports."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import tree_sitter_python as _tsp
from tree_sitter import Language, Parser

_parser = Parser(Language(_tsp.language()))


@dataclass
class Symbol:
    qualname: str
    kind: str  # class | function | method
    name: str
    start: int
    end: int
    parent: str = ""
    decorators: list = field(default_factory=list)
    bases: list = field(default_factory=list)


@dataclass
class Call:
    caller: str  # qualname, or "<module>"
    callee: str  # dotted text as written, e.g. "self.save", "tax.compute"
    line: int


@dataclass
class Import:
    alias: str
    module: str
    name: str | None
    level: int
    line: int


@dataclass
class Parsed:
    symbols: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    imports: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: [asdict(x) for x in v] for k, v in
                (("symbols", self.symbols), ("calls", self.calls), ("imports", self.imports))}


def parse(src: bytes) -> Parsed:
    root = _parser.parse(src).root_node
    out = Parsed()
    kinds: dict[str, str] = {}

    def t(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def dotted(n):
        if n is None:
            return None
        if n.type == "identifier":
            return t(n)
        if n.type == "attribute":
            obj = dotted(n.child_by_field_name("object"))
            attr = t(n.child_by_field_name("attribute"))
            return f"{obj}.{attr}" if obj else f"?.{attr}"
        return None

    def add_import(alias, module, name, level, line):
        out.imports.append(Import(alias, module, name, level, line))

    def handle_import(n):
        line = n.start_point[0] + 1
        if n.type == "import_statement":
            for c in n.named_children:
                if c.type == "dotted_name":
                    add_import(t(c), t(c), None, 0, line)
                elif c.type == "aliased_import":
                    add_import(t(c.child_by_field_name("alias")), t(c.child_by_field_name("name")), None, 0, line)
            return
        mod = n.child_by_field_name("module_name")
        level, modname = 0, ""
        if mod is not None:
            if mod.type == "relative_import":
                for c in mod.children:
                    if c.type == "import_prefix":
                        level = t(c).count(".")
                    elif c.type == "dotted_name":
                        modname = t(c)
            else:
                modname = t(mod)
        for c in n.children_by_field_name("name"):
            if c.type == "dotted_name":
                add_import(t(c), modname, t(c), level, line)
            elif c.type == "aliased_import":
                add_import(t(c.child_by_field_name("alias")), modname, t(c.child_by_field_name("name")), level, line)

    def define(defn, outer, scope, caller, decos):
        name = t(defn.child_by_field_name("name"))
        qual = ".".join(scope + [name])
        is_cls = defn.type == "class_definition"
        in_class = bool(scope) and kinds.get(".".join(scope)) == "class"
        kind = "class" if is_cls else ("method" if in_class else "function")
        kinds[qual] = kind
        bases = []
        if is_cls:
            sc = defn.child_by_field_name("superclasses")
            if sc is not None:
                bases = [d for a in sc.named_children if (d := dotted(a))]
        out.symbols.append(Symbol(qual, kind, name, outer.start_point[0] + 1, outer.end_point[0] + 1,
                                  ".".join(scope), decos, bases))
        body = defn.child_by_field_name("body")
        for c in defn.children:  # defaults, superclass args: evaluated in the enclosing scope
            if body is None or c.id != body.id:
                visit(c, scope, caller)
        if body is not None:
            for c in body.children:
                visit(c, scope + [name], qual)

    def visit(node, scope, caller):
        ty = node.type
        if ty in ("function_definition", "class_definition"):
            define(node, node, scope, caller, [])
        elif ty == "decorated_definition":
            decos = []
            for c in node.children:
                if c.type == "decorator":
                    decos.append(t(c).lstrip("@").strip())
                    for cc in c.children:
                        visit(cc, scope, caller)
            define(node.child_by_field_name("definition"), node, scope, caller, decos)
        elif ty in ("import_statement", "import_from_statement"):
            handle_import(node)
        else:
            if ty == "call":
                d = dotted(node.child_by_field_name("function"))
                if d:
                    out.calls.append(Call(caller, d, node.start_point[0] + 1))
            for c in node.children:
                visit(c, scope, caller)

    for c in root.children:
        visit(c, [], "<module>")
    return out
