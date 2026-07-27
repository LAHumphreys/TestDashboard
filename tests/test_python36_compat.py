"""Python 3.6.8 is the deployment target, and dev machines cannot run it.

The CI job in ``.github/workflows`` runs the suite inside RHEL's
``ubi8/python-36`` container, and that remains the authoritative gate.
But it only speaks after a push. This module is the gate that speaks on
every ``python -m unittest discover``, whatever interpreter you happen
to be holding.

It exists because the dangerous 3.6 mistakes are *invisible on a modern
interpreter*:

- ``list[str]`` (PEP 585) is perfectly good syntax everywhere and a
  ``TypeError`` at import on 3.6. No test on 3.9+ can see it.
- ``int | None`` (PEP 604) is likewise fine on 3.10+ and fatal on 3.6.
- 3.6 evaluates annotations eagerly at ``def`` time; 3.14 (PEP 649)
  defers them. So an unresolvable annotation is an ImportError on the
  target and completely silent in a green suite here.
- ``typing.Protocol`` / ``Literal`` / ``TypedDict`` import cleanly on
  3.8+ and do not exist on 3.6.

A checker that cannot fail proves nothing, so
:class:`PlantedRegressionTest` feeds deliberately broken source through
the very same detectors and requires them to complain.

This file must itself stay 3.6-clean: the 3.6 parser emits ``ast.Str``
rather than ``ast.Constant`` (and ``ast.Str`` was removed in 3.12), and
``ast.unparse`` / ``feature_version`` are newer than the target. Each is
accessed defensively below.

Python 3.6 compatible; standard library only.
"""

import ast
import os
import sys
import tempfile
import unittest
from typing import Dict, Iterator, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Subscripting these is PEP 585 — a TypeError until 3.9.
BUILTIN_GENERICS = frozenset([
    "list", "dict", "set", "frozenset", "tuple", "type",
])

#: Everything ``typing`` exported in 3.6.0. Deliberately the *oldest* 3.6:
#: a few names (Deque, ChainMap, NoReturn) only appeared in 3.6.1/3.6.2,
#: and there is no reason to depend on a micro version.
TYPING_36 = frozenset("""
AbstractSet Any AnyStr AsyncIterable AsyncIterator Awaitable ByteString
Callable ClassVar Collection Container ContextManager Coroutine Counter
DefaultDict Dict FrozenSet Generator Generic Hashable IO ItemsView Iterable
Iterator KeysView List Mapping MappingView Match MutableMapping
MutableSequence MutableSet NamedTuple NewType Optional Pattern Reversible
Sequence Set Sized SupportsAbs SupportsBytes SupportsComplex SupportsFloat
SupportsInt SupportsRound TYPE_CHECKING Text TextIO Tuple Type TypeVar Union
ValuesView cast get_type_hints no_type_check no_type_check_decorator overload
""".split())

#: These two are launched as bare ``python`` by someone on RHEL 8 sooner
#: or later, so they must PARSE under 2.7 and print a civil error. Their
#: own docstrings state the rule; this is the enforcement.
ENTRY_SCRIPTS = ("run_server.py", "run_feeder.py")

#: ``static`` is JS/HTML/CSS and holds no Python. Nothing else is
#: excluded: a stray .py under docs/ or tools/ must be gated too, and an
#: empty match costs nothing since the walk already filters to .py.
_SKIP_DIRS = frozenset(["__pycache__", "node_modules", "static"])

#: The 3.6 parser produces ast.Str for string literals; 3.8+ produces
#: ast.Constant; 3.12 removed ast.Str altogether.
_AST_STR = getattr(ast, "Str", None)


def python_files() -> List[str]:
    """Every .py file that ships, as absolute paths.

    Dot-prefixed directories are skipped, which conveniently excludes
    ``.git``, ``.github``, ``.idea`` and any local ``.venv`` — a virtual
    environment is full of third-party code that has no obligation to
    run on 3.6.
    """
    found = []  # type: List[str]
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = sorted(
            name for name in dirnames
            if not name.startswith(".") and name not in _SKIP_DIRS
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(os.path.join(dirpath, name))
    return found


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def relative(path: str) -> str:
    return os.path.relpath(path, REPO_ROOT).replace("\\", "/")


def describe(node: ast.AST) -> str:
    """Render a node for an error message, on any interpreter."""
    unparse = getattr(ast, "unparse", None)
    if unparse is not None:
        try:
            return unparse(node)
        except Exception:
            pass
    return type(node).__name__


def dotted_name(node: ast.AST) -> Optional[str]:
    """``a.b.c`` for Name/Attribute chains, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return None if prefix is None else prefix + "." + node.attr
    return None


def string_value(node: ast.AST) -> Optional[str]:
    """The text of a string literal, across ast.Str and ast.Constant."""
    if _AST_STR is not None and isinstance(node, _AST_STR):
        return node.s
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def annotation_nodes(tree: ast.AST) -> Iterator[Tuple[ast.AST, ast.AST]]:
    """Yield (owner, annotation-expression) for every annotation."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            every = list(args.args) + list(args.kwonlyargs)
            every += list(getattr(args, "posonlyargs", []) or [])
            if args.vararg is not None:
                every.append(args.vararg)
            if args.kwarg is not None:
                every.append(args.kwarg)
            for arg in every:
                if arg.annotation is not None:
                    yield node, arg.annotation
            if node.returns is not None:
                yield node, node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node, node.annotation


def builtin_generics(tree: ast.AST) -> List[Tuple[int, str]]:
    """Every ``list[...]``-style subscript ANYWHERE in the tree.

    Not just annotation slots: ``cast(list[str], x)``, a module-level
    alias ``Rows = list[tuple[str, int]]`` and ``TypeVar(bound=list[str])``
    all evaluate at import on 3.6 too. The base has to be literally the
    builtin name, so false positives are not a practical concern.
    """
    found = []  # type: List[Tuple[int, str]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            name = dotted_name(node.value)
            if name in BUILTIN_GENERICS:
                found.append((getattr(node, "lineno", 0), describe(node)))
    return found


def pep604_unions(tree: ast.AST) -> List[Tuple[int, str]]:
    """``X | Y`` where it would be evaluated as a type on 3.6.

    Restricted to annotations, ``cast()`` type arguments and top-level
    assignments; a blanket search would flag ordinary bitwise-or and
    set-union code.
    """
    found = []  # type: List[Tuple[int, str]]

    def scan(expr: ast.AST) -> None:
        for node in ast.walk(expr):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                found.append((getattr(node, "lineno", 0), describe(node)))

    for _owner, annotation in annotation_nodes(tree):
        scan(annotation)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted_name(node.func)
            if name in ("cast", "typing.cast") and node.args:
                scan(node.args[0])
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            scan(node.value)
    return found


def typing_imports(tree: ast.AST) -> List[Tuple[int, str]]:
    """Names pulled from ``typing``, with their line numbers."""
    found = []  # type: List[Tuple[int, str]]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "typing":
                found.append((getattr(node, "lineno", 0), node.attr))
    return found


def future_annotations(tree: ast.AST) -> List[int]:
    """Lines importing PEP 563, which is a SyntaxError on 3.6."""
    found = []  # type: List[int]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    found.append(node.lineno)
    return found


def parses_as_python36(source: str, path: str) -> Optional[str]:
    """None if the 3.6 grammar accepts the source, else the error.

    ``feature_version`` is 3.8+. On an older interpreter this returns
    None rather than lying, because a 3.6/3.7 run cannot answer the
    question — and on 3.6 itself the import simply succeeding is the
    answer.
    """
    try:
        ast.parse(source, filename=path, feature_version=(3, 6))
    except SyntaxError as exc:
        return "line %s: %s" % (exc.lineno, exc.msg)
    except TypeError:
        return None  # interpreter predates feature_version
    return None


class SourceCompatibilityTest(unittest.TestCase):
    """Static properties every shipped file must have."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {}  # type: Dict[str, str]
        cls.trees = {}  # type: Dict[str, ast.Module]
        for path in python_files():
            source = read(path)
            cls.sources[path] = source
            cls.trees[path] = ast.parse(source, filename=path)

    def test_the_scan_actually_covers_the_repository(self) -> None:
        """Guard against a walk that silently matches nothing."""
        names = set(relative(path) for path in self.sources)
        self.assertGreaterEqual(len(names), 25, "file discovery is broken")
        for expected in ("testboard/storage.py", "testboard/api.py",
                         "feeder/submitter.py", "run_server.py"):
            self.assertIn(expected, names)

    def test_grammar_accepts_every_file_as_python_36(self) -> None:
        """The exhaustive syntax gate.

        Covers the walrus operator, positional-only parameters, ``match``,
        ``except*`` and every other grammar change since 3.6 without this
        file having to enumerate them.
        """
        if sys.version_info < (3, 8):
            self.skipTest("feature_version needs Python 3.8+")
        bad = []  # type: List[str]
        for path, source in sorted(self.sources.items()):
            problem = parses_as_python36(source, path)
            if problem is not None:
                bad.append(relative(path) + " " + problem)
        self.assertEqual(bad, [], "not parseable as Python 3.6")

    def test_no_builtin_generics(self) -> None:
        """``list[str]`` and friends: a TypeError at import on 3.6."""
        bad = []  # type: List[str]
        for path, tree in sorted(self.trees.items()):
            for lineno, text in builtin_generics(tree):
                bad.append("%s:%d `%s` -- use the typing equivalent"
                           % (relative(path), lineno, text))
        self.assertEqual(bad, [], "PEP 585 builtin generics need 3.9+")

    def test_no_pep604_unions(self) -> None:
        """``int | None`` needs 3.10; use Optional/Union."""
        bad = []  # type: List[str]
        for path, tree in sorted(self.trees.items()):
            for lineno, text in pep604_unions(tree):
                bad.append("%s:%d `%s` -- use Optional/Union"
                           % (relative(path), lineno, text))
        self.assertEqual(bad, [], "PEP 604 unions need 3.10+")

    def test_no_future_annotations_import(self) -> None:
        """PEP 563 is a SyntaxError on 3.6, not a graceful no-op."""
        bad = []  # type: List[str]
        for path, tree in sorted(self.trees.items()):
            for lineno in future_annotations(tree):
                bad.append("%s:%d" % (relative(path), lineno))
        self.assertEqual(bad, [], "`from __future__ import annotations`")

    def test_typing_names_all_exist_in_python_36(self) -> None:
        """Protocol/Literal/TypedDict/OrderedDict are all too new."""
        bad = []  # type: List[str]
        for path, tree in sorted(self.trees.items()):
            for lineno, name in typing_imports(tree):
                if name not in TYPING_36:
                    bad.append("%s:%d typing.%s"
                               % (relative(path), lineno, name))
        self.assertEqual(bad, [], "typing names absent from Python 3.6")

    def test_entry_scripts_carry_no_inline_annotations(self) -> None:
        """The bare-``python`` trap on RHEL 8.

        These two must parse under 2.7 so they can print a readable
        version error instead of a SyntaxError, which means type
        comments only.
        """
        for name in ENTRY_SCRIPTS:
            path = os.path.join(REPO_ROOT, name)
            tree = self.trees[path]
            annotated = [describe(annotation)
                         for _owner, annotation in annotation_nodes(tree)]
            self.assertEqual(
                annotated, [],
                name + " must use type comments, not inline annotations, "
                "so it still parses under Python 2",
            )
            fstrings = [node.lineno for node in ast.walk(tree)
                        if isinstance(node, ast.JoinedStr)]
            self.assertEqual(
                fstrings, [],
                name + " must not use f-strings; Python 2 cannot parse "
                "them, so the version check would never be reached",
            )


class AnnotationsEvaluateTest(unittest.TestCase):
    """3.6 evaluates annotations at def time. Prove they all can.

    On 3.14 (PEP 649) annotations are lazy, so a green suite is no
    evidence that they even resolve. Touching ``__annotations__`` forces
    exactly the work 3.6 does at import.
    """

    def modules(self) -> List[Tuple[str, object]]:
        """Import every shipped module, so the sweep never depends on
        what some other test happened to load first."""
        import importlib
        import pkgutil

        names = []  # type: List[str]
        for package in ("testboard", "feeder", "tools"):
            names.append(package)
            path = os.path.join(REPO_ROOT, package)
            for _finder, name, _pkg in pkgutil.iter_modules([path]):
                names.append(package + "." + name)
        return [(name, importlib.import_module(name))
                for name in sorted(names)]

    def test_every_annotation_resolves_to_a_real_object(self) -> None:
        import inspect

        checked = 0
        failures = []  # type: List[str]
        for name, module in self.modules():
            targets = []
            for attr, obj in sorted(vars(module).items(), key=lambda kv: kv[0]):
                if getattr(obj, "__module__", None) != name:
                    continue
                if inspect.isfunction(obj):
                    targets.append((name + "." + attr, obj))
                elif inspect.isclass(obj):
                    for sub, member in sorted(vars(obj).items(),
                                              key=lambda kv: kv[0]):
                        if inspect.isfunction(member):
                            targets.append(
                                ("%s.%s.%s" % (name, attr, sub), member))
            for label, obj in targets:
                checked += 1
                try:
                    dict(getattr(obj, "__annotations__", {}) or {})
                except Exception as exc:  # pragma: no cover - the bug case
                    failures.append("%s: %r" % (label, exc))
        self.assertEqual(failures, [], "annotations fail to evaluate")
        self.assertGreater(checked, 200,
                           "annotation sweep covered almost nothing")


class PlantedRegressionTest(unittest.TestCase):
    """A gate that cannot fail is decoration. Make it fail on purpose."""

    def detectors(self, source: str) -> Dict[str, int]:
        tree = ast.parse(source)
        return {
            "builtin_generics": len(builtin_generics(tree)),
            "pep604": len(pep604_unions(tree)),
            "future": len(future_annotations(tree)),
            "typing": len([name for _line, name in typing_imports(tree)
                           if name not in TYPING_36]),
        }

    def test_detects_builtin_generic_in_an_annotation(self) -> None:
        counts = self.detectors("def f(x: list[str]) -> dict[str, int]: ...")
        self.assertEqual(counts["builtin_generics"], 2)

    def test_detects_builtin_generic_outside_an_annotation(self) -> None:
        """The case a naive annotation-only scan misses."""
        for source in ("y = cast(list[str], x)",
                       "Rows = tuple[str, int]",
                       "T = TypeVar('T', bound=set[str])"):
            self.assertGreaterEqual(
                self.detectors(source)["builtin_generics"], 1, source)

    def test_detects_pep604_union(self) -> None:
        self.assertEqual(
            self.detectors("def f(x: int | None) -> None: ...")["pep604"], 1)

    def test_ordinary_bitwise_or_is_not_flagged(self) -> None:
        """Guard the other way: no false positive on real ``|`` code."""
        source = "def f(a: int, b: int) -> int:\n    return a | b\n"
        self.assertEqual(self.detectors(source)["pep604"], 0)

    def test_detects_future_annotations_and_new_typing_names(self) -> None:
        counts = self.detectors(
            "from __future__ import annotations\n"
            "from typing import Protocol, Literal, List\n")
        self.assertEqual(counts["future"], 1)
        self.assertEqual(counts["typing"], 2)

    def test_detects_f_strings_in_either_quote_style(self) -> None:
        """The entry-script rule; a substring match would miss f'...'."""
        for source in ("x = f\"{a}\"", "x = f'{a}'", "x = rf'{a}'"):
            found = [node for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.JoinedStr)]
            self.assertEqual(len(found), 1, source)
        plain = [node for node in ast.walk(ast.parse("x = 'conf'"))
                 if isinstance(node, ast.JoinedStr)]
        self.assertEqual(plain, [], "plain strings must not be flagged")

    def test_grammar_gate_rejects_post_36_syntax(self) -> None:
        if sys.version_info < (3, 8):
            self.skipTest("feature_version needs Python 3.8+")
        handle, path = tempfile.mkstemp(suffix=".py")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        self.assertIsNotNone(
            parses_as_python36("if (n := 10) > 5:\n    pass\n", path),
            "the walrus operator must be rejected as non-3.6",
        )
        self.assertIsNone(
            parses_as_python36("def f(a, b=1):\n    return a + b\n", path),
            "plain 3.6 code must be accepted",
        )


if __name__ == "__main__":
    unittest.main()
