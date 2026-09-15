"""Names used at import time must be defined before that use.

app.py starts its poller threads and loads the database *at import*, so a
function or class defined further down the file than the line that first
runs it is a NameError - not at test time, where the whole module has
loaded by the time anything is called, but in the real process, where the
thread dies or the configuration step aborts. Found twice on one day:
PollBackoff (defined after the interface poller's thread started; that
poller was dead in production for hours) and _wire_event_bus (called from
_load_database, defined 2,000 lines later; no push, no webhooks).
"""
import ast
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

APP = Path(__file__).parent.parent / "app.py"


def _defs_and_module_level_calls():
    tree = ast.parse(APP.read_text())
    defs = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node.lineno
    return tree, defs


def _names_before_first_sleep(fn):
    """Names a thread body touches before it first blocks. Anything after
    the first time.sleep() runs long after import has finished and can
    safely refer to things defined further down the file; anything before
    it runs the instant the thread starts - mid-import - and cannot."""
    names = set()
    for stmt in fn.body:
        src_names = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        is_sleep = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                       and c.func.attr == "sleep" for c in ast.walk(stmt))
        if isinstance(stmt, ast.While):
            # a `while True:` loop: only its statements up to the first sleep count
            for inner in stmt.body:
                inner_names = {n.id for n in ast.walk(inner) if isinstance(n, ast.Name)}
                inner_sleep = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                                  and c.func.attr == "sleep" for c in ast.walk(inner))
                names |= inner_names
                if inner_sleep:
                    return names
            return names
        names |= src_names
        if is_sleep:
            return names
    return names


def test_import_time_thread_targets_are_defined_before_they_start():
    """Every `threading.Thread(target=f).start()` at module level: f must
    be defined above that line, and so must every name f touches before
    its first sleep - that code runs mid-import, when the rest of the file
    does not exist yet. (PollBackoff was exactly this: constructed on the
    loop's first line, defined 60 lines further down.)"""
    tree, defs = _defs_and_module_level_calls()
    fn_bodies = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    problems = []
    text = APP.read_text()
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        src = ast.get_source_segment(text, node) or ""
        if "threading.Thread(" not in src or ".start()" not in src:
            continue
        for kw in ast.walk(node):
            if isinstance(kw, ast.keyword) and kw.arg == "target" and isinstance(kw.value, ast.Name):
                target = kw.value.id
                if defs.get(target, 10**9) > node.lineno:
                    problems.append(f"{target} starts at line {node.lineno} but is defined at {defs.get(target)}")
                body = fn_bodies.get(target)
                if body is None:
                    continue
                for name in _names_before_first_sleep(body):
                    if name in defs and defs[name] > node.lineno:
                        problems.append(f"thread {target} (started line {node.lineno}) uses {name} before its first sleep; defined at line {defs[name]}")
    assert not problems, "\n".join(problems)


def test_functions_called_during_database_load_are_defined_first():
    tree, defs = _defs_and_module_level_calls()
    load = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_load_database")
    late = []
    for call in ast.walk(load):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            name = call.func.id
            if name in defs and defs[name] > load.lineno:
                late.append(f"{name} (line {defs[name]}) is called from _load_database (line {load.lineno})")
    assert not late, "\n".join(late)


def test_the_syslog_poller_threads_are_actually_alive():
    """The dynamic version of the same check: importing app starts them,
    and a NameError on their first line kills them within milliseconds."""
    import app  # noqa: F401
    names = {t.name for t in threading.enumerate()}
    for wanted in ("interface-alert-syslog-checker", "hardware-alert-syslog-checker", "topology-refresh"):
        assert wanted in names, f"thread {wanted} is not running"
