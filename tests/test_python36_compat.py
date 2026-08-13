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

#: `clients/feeder.py` was exempted here through WP-29 (single-file
#: client engine, distributed into ANOTHER product's repository, where
#: "python" vs "python3" is exactly as uncontrolled as it is on this
#: project's own RHEL 8 target). That is no longer true: the file now
#: targets Python 3.6+ only, uses real inline annotations and f-strings
#: like the rest of testboard, and fails at PARSE time (not a graceful
#: in-file message) on anything older - there is no in-file check that
#: can pre-empt a SyntaxError. It is therefore held to the FULL
#: standard, same as every other .py file under the repo root: no
#: exemption from test_entry_scripts_carry_no_inline_annotations below,
#: swept unexempted by SourceCompatibilityTest above (grammar, builtin
#: generics, PEP 604, __future__ annotations, the 3.6 typing whitelist),
#: and its own annotations are forced to evaluate by
#: ClientFeederAnnotationsTest below - it cannot join the package sweep
#: in AnnotationsEvaluateTest, because "clients" has no __init__.py and
#: is deliberately not a package.

#: ``static`` is JS/HTML/CSS and holds no Python. Nothing else is
#: excluded: a stray .py under docs/ or tools/ must be gated too, and an
#: empty match costs nothing since the walk already filters to .py.
_SKIP_DIRS = frozenset(["__pycache__", "node_modules", "static"])

#: Vendored third-party source (see ``third_party/README.md``). Held to a
#: DIFFERENT standard, deliberately, and :class:`VendoredCodeTest` is where
#: that standard lives.
#:
#: The rules in this file are of two kinds, and only one of them applies to
#: code we did not write. ``list[str]`` is a *runtime TypeError on 3.6* —
#: that is correctness, and vendored code must pass it. "Use typing.List,
#: annotate everything, no f-strings in the entry scripts" are *our
#: conventions* — holding upstream to them would mean either editing
#: upstream (so it can never be updated by replacing the directory) or
#: permanently excusing the failures (so the gate rots).
#:
#: So the split is: vendored code must PARSE as 3.6 and must not use
#: constructs that fail at import on 3.6. It need not look like ours.
VENDORED_DIRS = frozenset(["third_party"])

#: The 3.6 parser produces ast.Str for string literals; 3.8+ produces
#: ast.Constant; 3.12 removed ast.Str altogether.
_AST_STR = getattr(ast, "Str", None)


def _walk_python(skip: frozenset, only: Optional[frozenset] = None
                 ) -> List[str]:
    """Every .py file under the repo, filtered by top-level directory.

    Dot-prefixed directories are skipped, which conveniently excludes
    ``.git``, ``.github``, ``.idea`` and any local ``.venv`` — a virtual
    environment is full of third-party code that has no obligation to
    run on 3.6.
    """
    found = []  # type: List[str]
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        if only is not None and dirpath == REPO_ROOT:
            dirnames[:] = sorted(name for name in dirnames if name in only)
        else:
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".") and name not in skip
            )
        for name in sorted(filenames):
            if name.endswith(".py"):
                if only is not None and dirpath == REPO_ROOT:
                    continue
                found.append(os.path.join(dirpath, name))
    return found


def python_files() -> List[str]:
    """Every .py file WE wrote, as absolute paths.

    Vendored third-party source is excluded here and checked separately
    (see :data:`VENDORED_DIRS`).
    """
    return _walk_python(_SKIP_DIRS | VENDORED_DIRS)


def vendored_files() -> List[str]:
    """Every .py file under a vendored third-party directory."""
    return _walk_python(_SKIP_DIRS, only=VENDORED_DIRS)


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


def pep604_unions(tree: ast.AST,
                  module_assignments: bool = True) -> List[Tuple[int, str]]:
    """``X | Y`` where it would be evaluated as a type on 3.6.

    Restricted to annotations, ``cast()`` type arguments and top-level
    assignments; a blanket search would flag ordinary bitwise-or and
    set-union code.

    *module_assignments* covers the module-level type alias
    (``Number = int | float``), which is a real ``TypeError`` on 3.6. It
    is a heuristic and not a sound one: it cannot distinguish a type
    alias from integer flag arithmetic at module scope, because telling
    them apart needs to know what the names are bound to.

    That over-approximation is safe for code we write — we do not do
    module-level bitwise-or on integers, and if we started, narrowing the
    rule would be the right response. It is NOT safe for vendored code,
    where ``CAPABILITIES = LONG_PASSWORD | LONG_FLAG | ...`` is ordinary
    and correct. Vendored callers pass False; see :class:`VendoredCodeTest`
    for what covers the gap that leaves.
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
    if module_assignments:
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

    ``feature_version`` is accepted from 3.8 but only *enforced* by the
    PEG parser, 3.9+ — 3.8's pgen parser takes the argument and then
    accepts the walrus operator anyway (CI's 3.8 leg proved it, via
    PlantedRegressionTest). On an interpreter that cannot answer the
    question this returns None rather than lying; callers skip below
    3.9. On 3.6 itself the import simply succeeding is the answer.
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

    def test_vendored_code_is_not_in_the_project_scan(self) -> None:
        """The exclusion must exclude vendored code and nothing else.

        An exclusion that accidentally matched everything would leave
        every test in this class passing over an empty set.
        """
        leaked = sorted(
            relative(path) for path in self.sources
            if relative(path).split("/")[0] in VENDORED_DIRS
        )
        self.assertEqual(
            leaked, [],
            "vendored files reached the project-style scan; they are "
            "checked by VendoredCodeTest instead")

    def test_grammar_accepts_every_file_as_python_36(self) -> None:
        """The exhaustive syntax gate.

        Covers the walrus operator, positional-only parameters, ``match``,
        ``except*`` and every other grammar change since 3.6 without this
        file having to enumerate them.
        """
        if sys.version_info < (3, 9):
            self.skipTest("feature_version is only enforced by the PEG "
                          "parser (3.9+); 3.8 accepts the walrus operator")
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

        These must parse under 2.7 so they can print a readable version
        error instead of a SyntaxError, which means type comments only.
        ``clients/feeder.py`` used to be listed alongside these
        (WP-29's predecessor decision) but is not any more - see the
        comment where it was removed, above ENTRY_SCRIPTS' definition.
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
        what some other test happened to load first.

        ``tests`` is swept too, deliberately: a test module's annotation
        that fails to evaluate is an ImportError under discovery on 3.6,
        which silently drops every test in the module from the run —
        exactly how test_storage.py lost its 200+ tests on the CI legs
        for three days while the local 3.14 suite stayed green.
        """
        import importlib
        import pkgutil

        names = []  # type: List[str]
        for package in ("testboard", "feeder", "tools", "tests"):
            names.append(package)
            path = os.path.join(REPO_ROOT, package)
            for _finder, name, _pkg in pkgutil.iter_modules([path]):
                names.append(package + "." + name)
        return [(name, importlib.import_module(name))
                for name in sorted(names)]

    def test_every_annotation_resolves_to_a_real_object(self) -> None:
        checked = 0
        failures = []  # type: List[str]
        for name, module in self.modules():
            module_checked, module_failures = _annotation_failures(name, module)
            checked += module_checked
            failures.extend(module_failures)
        self.assertEqual(failures, [], "annotations fail to evaluate")
        self.assertGreater(checked, 200,
                           "annotation sweep covered almost nothing")


def _annotation_failures(name: str, module: object) -> Tuple[int, List[str]]:
    """Force every module/class-level function's ``__annotations__`` to
    evaluate; return (checked count, failure messages).

    Shared by :class:`AnnotationsEvaluateTest`-style sweeps: the package
    one above and :class:`ClientFeederAnnotationsTest` below, which
    cannot join that sweep because its module is not part of a package.
    """
    import inspect

    checked = 0
    failures = []  # type: List[str]
    targets = []
    for attr, obj in sorted(vars(module).items(), key=lambda kv: kv[0]):
        if getattr(obj, "__module__", None) != name:
            continue
        if inspect.isfunction(obj):
            targets.append((name + "." + attr, obj))
        elif inspect.isclass(obj):
            for sub, member in sorted(vars(obj).items(), key=lambda kv: kv[0]):
                if inspect.isfunction(member):
                    targets.append(("%s.%s.%s" % (name, attr, sub), member))
    for label, obj in targets:
        checked += 1
        try:
            dict(getattr(obj, "__annotations__", {}) or {})
        except Exception as exc:  # pragma: no cover - the bug case
            failures.append("%s: %r" % (label, exc))
    return checked, failures


class ClientFeederAnnotationsTest(unittest.TestCase):
    """The ``clients/`` engines are not part of any package - "clients"
    has no ``__init__.py`` (see tests/test_feeder_client_engine.py's
    module docstring) - so :class:`AnnotationsEvaluateTest`'s
    ``pkgutil``-based sweep never loads them, and the files would
    otherwise get the static 3.6-grammar checks above but NOT the
    runtime "annotations actually evaluate" proof every other shipped
    module gets. This closes that gap the same way: load each file,
    force every function's and method's ``__annotations__``, on the
    interpreter actually running the suite.
    """

    #: (filename, floor on annotated defs) - the floor is what catches
    #: a sweep that silently loaded nothing. The micro engine's is
    #: lower because the whole file is deliberately smaller (WP-30).
    _ENGINES = (("feeder.py", 15), ("feeder_micro.py", 8))

    def test_client_feeder_annotations_evaluate(self) -> None:
        import importlib.util

        for filename, floor in self._ENGINES:
            path = os.path.join(REPO_ROOT, "clients", filename)
            module_name = "client_annotations_" + filename.replace(".", "_")
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            checked, failures = _annotation_failures(module_name, module)
            self.assertEqual(
                failures, [],
                "annotations fail to evaluate in clients/" + filename,
            )
            self.assertGreater(
                checked, floor,
                "annotation sweep of clients/{0} covered almost nothing "
                "- is the module loading correctly?".format(filename),
            )


#: Everything the interpreter provides without an import. An annotation
#: whose root name is neither here, nor bound in some enclosing scope of
#: the ``def``, is a NameError at def time on 3.6.
_BUILTIN_NAMES = frozenset(dir(__import__("builtins")))


def _scope_bound_names(body: List[ast.stmt]) -> set:
    """Names bound by the statements of ONE scope.

    Descends into compound statements (``if``/``for``/``try``/``with``)
    because they share the enclosing scope, but never into a nested
    ``def``/``class`` — those are new scopes and bind only their name
    here. Ordering is deliberately ignored: a name bound anywhere in the
    scope counts, which can under-report (name used before binding) but
    never false-positives, and a NameError detector must not cry wolf.
    """
    bound = set()  # type: set

    def collect_target(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                collect_target(elt)
        elif isinstance(target, ast.Starred):
            collect_target(target.value)

    def walk_stmts(stmts: List[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                bound.add(stmt.name)
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    collect_target(target)
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                collect_target(stmt.target)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                collect_target(stmt.target)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    if item.optional_vars is not None:
                        collect_target(item.optional_vars)
            elif isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    if handler.name:
                        bound.add(handler.name)
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(stmt, field, None)
                if sub:
                    walk_stmts(sub)
            for handler in getattr(stmt, "handlers", None) or []:
                walk_stmts(handler.body)

    walk_stmts(body)
    return bound


def _scope_child_defs(body: List[ast.stmt]) -> List[ast.stmt]:
    """Every ``def``/``class`` belonging directly to this scope, however
    deeply buried in ``if``/``for``/``try`` it is."""
    children = []  # type: List[ast.stmt]

    def walk_stmts(stmts: List[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                children.append(stmt)
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(stmt, field, None)
                if sub:
                    walk_stmts(sub)
            for handler in getattr(stmt, "handlers", None) or []:
                walk_stmts(handler.body)

    walk_stmts(body)
    return children


def annotation_name_gaps(source: str, filename: str
                         ) -> List[Tuple[int, str]]:
    """``(lineno, name)`` for every annotation name that resolves NOWHERE.

    The runtime sweep in :class:`AnnotationsEvaluateTest` forces the
    annotations of every module-level function and method — but a
    function nested inside another only comes into existence when its
    enclosing function RUNS, so on 3.6 its annotations are a NameError
    at test runtime, which the sweep cannot reach and a 3.14
    interpreter (PEP 649, lazy annotations) never evaluates at all.
    That exact gap put a NameError onto three CI legs on 2026-08-08
    (``tests.test_storage.CompareCountsManyTest``) while the local 3.14
    suite stayed green — this detector is the widening, static so it
    sees every ``def`` at every depth.

    A name resolves if it is a builtin or bound in ANY enclosing scope
    of the ``def`` (annotations evaluate where the ``def`` executes:
    module scope, an outer function's scope including its parameters, or
    an enclosing class body). String annotations are skipped — they are
    forward references, never evaluated at def time on any version.
    """
    tree = ast.parse(source, filename=filename)
    gaps = []  # type: List[Tuple[int, str]]

    def check_function(func: ast.stmt, visible: set) -> None:
        args = func.args  # type: ignore[attr-defined]
        annotated = []  # type: List[ast.expr]
        arg_lists = [args.args, args.kwonlyargs]
        arg_lists.append(getattr(args, "posonlyargs", None) or [])
        for arg_list in arg_lists:
            for arg in arg_list:
                if arg.annotation is not None:
                    annotated.append(arg.annotation)
        for special in (args.vararg, args.kwarg):
            if special is not None and special.annotation is not None:
                annotated.append(special.annotation)
        returns = getattr(func, "returns", None)
        if returns is not None:
            annotated.append(returns)
        for expr in annotated:
            for node in ast.walk(expr):
                if isinstance(node, ast.Name) and node.id not in visible:
                    gaps.append((node.lineno, node.id))

    def visit_scope(body: List[ast.stmt], scopes: List[set]) -> None:
        visible = set(_BUILTIN_NAMES)
        for scope in scopes:
            visible |= scope
        for child in _scope_child_defs(body):
            if isinstance(child, ast.ClassDef):
                visit_scope(child.body,
                            scopes + [_scope_bound_names(child.body)])
                continue
            check_function(child, visible)
            args = child.args  # type: ignore[attr-defined]
            params = set()  # type: set
            for arg_list in (args.args, args.kwonlyargs,
                             getattr(args, "posonlyargs", None) or []):
                for arg in arg_list:
                    params.add(arg.arg)
            for special in (args.vararg, args.kwarg):
                if special is not None:
                    params.add(special.arg)
            visit_scope(child.body,
                        scopes + [_scope_bound_names(child.body) | params])

    visit_scope(tree.body, [_scope_bound_names(tree.body)])
    return gaps


class NestedAnnotationNamesTest(unittest.TestCase):
    """Static widening of :class:`AnnotationsEvaluateTest` — see
    :func:`annotation_name_gaps` for why the runtime sweep is not enough.
    """

    def test_the_detector_catches_an_unimported_nested_annotation(self) -> None:
        planted = (
            "def outer():\n"
            "    def inner(x: Any) -> int:\n"
            "        return 0\n"
            "    return inner\n"
        )
        gaps = annotation_name_gaps(planted, "<planted>")
        self.assertEqual([name for _lineno, name in gaps], ["Any"],
                         "the detector must catch the exact shape that "
                         "reached CI on 2026-08-08")

    def test_enclosing_scope_names_are_not_flagged(self) -> None:
        fine = (
            "import typing\n"
            "def outer(width: int):\n"
            "    Row = dict\n"
            "    def inner(x: Row, y: typing.Any, z: 'Forward',\n"
            "              w: int = 0) -> None:\n"
            "        pass\n"
        )
        self.assertEqual(annotation_name_gaps(fine, "<fine>"), [],
                         "outer locals, parameters, attribute roots and "
                         "string forward references all resolve")

    def test_every_annotation_name_in_the_repo_resolves(self) -> None:
        failures = []  # type: List[str]
        for path in python_files():
            for lineno, name in annotation_name_gaps(read(path), path):
                failures.append(
                    "%s:%d: %r" % (relative(path), lineno, name))
        self.assertEqual(
            failures, [],
            "these annotation names are a def-time NameError on 3.6")


class VendoredCodeTest(unittest.TestCase):
    """Vendored third-party source: correctness gates only.

    The project vendors pure-Python dependencies rather than installing
    them, so that nothing has to be set up on the deployment host (see
    ``third_party/README.md``). That makes their 3.6 compatibility OUR
    problem — nobody else is checking it, and the failure mode is an
    ImportError on the production server and a green suite here.

    What is checked is exactly what would break at import on 3.6. What is
    NOT checked is anything that only expresses this project's style: a
    missing annotation upstream is not a defect, and treating it as one
    would force us either to edit vendored code (making it un-updatable)
    or to carry a growing list of excuses (making the gate meaningless).

    One gap is accepted knowingly. The PEP 604 scan drops its
    module-level-assignment arm here, because that arm cannot tell a type
    alias from integer flag arithmetic and PyMySQL's constants modules are
    full of the latter (``CAPABILITIES = LONG_PASSWORD | LONG_FLAG | ...``).
    A vendored module-level type alias written with ``|`` would therefore
    slip past. What catches it instead is the ubi8/python-36 CI job, where
    such an alias is a TypeError at import and the suite does not start.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {}  # type: Dict[str, str]
        cls.trees = {}  # type: Dict[str, ast.Module]
        for path in vendored_files():
            source = read(path)
            cls.sources[path] = source
            cls.trees[path] = ast.parse(source, filename=path)

    def test_the_vendor_scan_finds_what_is_on_disk(self) -> None:
        """A walk that matches nothing would pass every test below."""
        for name in sorted(VENDORED_DIRS):
            directory = os.path.join(REPO_ROOT, name)
            if not os.path.isdir(directory):
                continue
            found = [p for p in self.sources if relative(p).startswith(name)]
            self.assertTrue(
                found, name + "/ exists but the scan found no Python in it")

    def test_vendored_code_parses_as_python_36(self) -> None:
        """The whole reason a specific version is pinned.

        "Whatever is current" is how you end up shipping a driver that
        uses the walrus operator to a RHEL 8 box.
        """
        if sys.version_info < (3, 9):
            self.skipTest("feature_version is only enforced by the PEG "
                          "parser (3.9+); 3.8 accepts the walrus operator")
        bad = []  # type: List[str]
        for path, source in sorted(self.sources.items()):
            problem = parses_as_python36(source, path)
            if problem is not None:
                bad.append(relative(path) + " " + problem)
        self.assertEqual(bad, [], "vendored code is not 3.6-parseable")

    def test_vendored_code_has_no_import_time_36_failures(self) -> None:
        """PEP 585/604 and PEP 563 all fail at import on 3.6.

        Unlike the style rules, these are not opinions: each one is a
        TypeError or SyntaxError on the target interpreter.
        """
        bad = []  # type: List[str]
        for path, tree in sorted(self.trees.items()):
            for lineno, text in builtin_generics(tree):
                bad.append("%s:%d PEP 585 `%s`"
                           % (relative(path), lineno, text))
            for lineno, text in pep604_unions(tree, module_assignments=False):
                bad.append("%s:%d PEP 604 `%s`"
                           % (relative(path), lineno, text))
            for lineno in future_annotations(tree):
                bad.append("%s:%d `from __future__ import annotations`"
                           % (relative(path), lineno))
            for lineno, name in typing_imports(tree):
                if name not in TYPING_36:
                    bad.append("%s:%d typing.%s does not exist in 3.6"
                               % (relative(path), lineno, name))
        self.assertEqual(
            bad, [],
            "vendored code would fail at import on Python 3.6. Pin an "
            "older release of the package; do not edit the vendored files")


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
        if sys.version_info < (3, 9):
            self.skipTest("feature_version is only enforced by the PEG "
                          "parser (3.9+); 3.8 accepts the walrus operator")
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
