"""ASV adapter (harness-adapter.md §6.7), both halves, driving asv_runner.

Driving: the suite is an ASV benchmark directory inside the target. Nothing of
asv's own (environments, builds, results files) is used: the benchmarks run in
the interpreter first on the child's PATH, which must import `asv_runner`, the
package asv itself runs benchmarks with. A case is one benchmark and, for a
parameterized one, one parameter combination, named as asv names it
(`module.Class.time_x-3`). Each case runs `setup_cache` once when it has one,
then one asv_runner `run` per round, each in a fresh process, from a scratch
directory outside the tree (so an unbuilt source checkout cannot shadow the
installed project). Only `time_*` benchmarks are cases; `peakmem_*`, `mem_*`,
`track_*` and `timeraw_*` are listed as skipped, since only wall time is mapped.

Precision: the work order's levels override asv's per-benchmark attributes;
what it leaves out, asv decides per benchmark, as it would on its own. The
protocol each result reports is that case's own (`case_protocol`), so a
benchmark that declares `number = 1` is recorded as fixed calibration.

Translating: each round's samples are a group of observations; parameter
values are kept as asv's repr() strings; asv's `version` (a hash of the
benchmark's code) is part of comparison context, so editing a benchmark starts
a new series.
"""

import itertools
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import Unsupported

NAME = "asv"
CONTEXT_KEY = "asv"
WORKLOAD_PARAMETERS = False
PRODUCER = {"name": "benchx/asv-adapter", "version": "0.1.0", "mapping_version": "asv-to-benchx/v1"}

QUANTITIES = {
    "wall-time": ("samples", {"name": "wall-time", "unit": "s", "direction": "lower-is-better"}),
}

# asv's own default for `rounds`: each round is one process.
DEFAULT_ROUNDS = 2
CONF_NAMES = ("asv.conf.json", "asv.conf.jsonc")


def default_precision() -> dict:
    """Only the process level: values, calibration and warmup are left to each benchmark."""
    return {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": DEFAULT_ROUNDS}]}}


def protocol(precision: dict) -> tuple[dict, dict]:
    """The order-level protocol, and the overrides passed to asv_runner.

    `process` is the number of rounds (processes); `value` overrides `repeat`;
    fixed calibration overrides `number`, adaptive overrides `sample_time`;
    time warmup overrides `warmup_time`. Each result reports its own case's
    protocol (`case_protocol`), which adds what the benchmark itself declares.
    """
    problems, given = [], {}
    repetitions = precision["repetitions"]
    if isinstance(repetitions, int):
        problems.append("asv repeats at two levels; give repetitions as "
                        "{mode: fixed, levels: [{unit: process, n}, {unit: value, n}]}")
    else:
        for level in repetitions["levels"]:
            if level["unit"] not in ("process", "value") or level["unit"] in given:
                problems.append("asv repetition levels are 'process' (rounds) and 'value' (repeat), once each")
                break
            given[level["unit"]] = level["n"]
    overrides = {}
    if "value" in given:
        overrides["repeat"] = given["value"]
    calibration = precision.get("calibration")
    if calibration:
        if calibration["mode"] == "fixed":
            overrides["number"] = calibration["n_iterations"]
        else:
            overrides["sample_time"] = calibration["minimum_sample_seconds"]
    warmup = precision.get("warmup")
    if warmup:
        if warmup["mode"] == "count":
            problems.append("asv warmup is time-based only, not count-based")
        else:
            overrides["warmup_time"] = warmup.get("seconds", 0)
    if problems:
        raise Unsupported(*problems)
    rounds = given.get("process", DEFAULT_ROUNDS)
    levels = [{"unit": "process", "n": rounds}] + ([{"unit": "value", "n": given["value"]}] if "value" in given else [])
    applied = {"repetitions": {"mode": "fixed", "levels": levels}, "timer": "perf_counter", "gc": "disabled",
               "environment": "inherit-all"}
    if calibration:
        applied["calibration"] = calibration
    if warmup:
        applied["warmup"] = warmup
    return applied, {"rounds": rounds, "overrides": overrides}


def _which_python(env: dict) -> str | None:
    path = env.get("PATH", "")
    return shutil.which("python", path=path) or shutil.which("python3", path=path)


def _read_conf(path: Path) -> dict:
    """asv.conf.json allows // comments; drop whole-line ones, as asv does."""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("//")]
    try:
        return json.loads("\n".join(lines))
    except ValueError:
        return {}


def _project(benchmark_dir: Path, root: Path) -> str | None:
    """The `project` of the nearest asv.conf.json at or above the benchmark directory, within the tree."""
    for directory in [benchmark_dir, *benchmark_dir.parents]:
        for name in CONF_NAMES:
            if (directory / name).is_file():
                return _read_conf(directory / name).get("project")
        if directory == root:
            break
    return None


def locate(target_path, suite, env) -> dict:
    """The benchmark directory, the interpreter that will run it, and the project it measures."""
    root = Path(target_path).resolve()
    benchmark_dir = (root / suite).resolve()
    if not benchmark_dir.is_dir():
        raise Unsupported(f"suite is not an asv benchmark directory: {benchmark_dir}")
    python = _which_python(env)
    if python is None:
        raise Unsupported("no `python` on the PATH the benchmark would run with")
    probe = subprocess.run([python, "-c", "import asv_runner"], env=env, capture_output=True, text=True)
    if probe.returncode != 0:
        raise Unsupported(f"asv_runner is not importable by {python}: {probe.stderr.strip().splitlines()[-1:]}")
    return {"python": python, "benchmark_dir": benchmark_dir, "root": root,
            "project": _project(benchmark_dir, root), "benchmarks": {}}


LISTER = r"""
import contextlib, importlib, io, json, os, pkgutil, re, sys
from asv_runner.discovery import disc_benchmarks, update_sys_path
root, case_filter = sys.argv[1], sys.argv[2]
update_sys_path(root)
package = os.path.basename(root)
failed = []
def walk(name):
    try:
        module = importlib.import_module(name)
    except BaseException as e:
        failed.append(f"{name}: {type(e).__name__}: {e}")
        return
    for _, sub, _ in pkgutil.iter_modules(getattr(module, "__path__", None) or [], name + "."):
        walk(sub)
walk(package)
with contextlib.redirect_stderr(io.StringIO()):  # modules that failed above print again
    found = list(disc_benchmarks(root, ignore_import_errors=True))
out = []
for b in found:
    if case_filter and not re.search(case_filter, b.name):
        continue
    out.append({"name": b.name, "type": b.type, "params": b.params, "param_names": b.param_names,
                "version": getattr(b, "version", None),
                "setup_cache_key": b.setup_cache_key,
                "has_setup": bool(getattr(b, "_setups", None)),
                "setup_isolates": bool(getattr(b, "_setup_isolates_timed_calls", False)),
                **{k: getattr(b, k) for k in ("number", "repeat", "rounds", "sample_time", "warmup_time",
                                               "min_run_count", "timeout") if hasattr(b, k)}})
print(json.dumps({"benchmarks": out, "failed": failed}))
"""


def list_cases(runnable, case_filter, env) -> list[str]:
    """The `time_*` benchmarks whose name the filter (a regex, as asv's --bench)
    searches, one case per parameter combination. Modules that fail to import
    and benchmarks of other types are reported on stderr, never run."""
    with tempfile.TemporaryDirectory() as scratch:
        out = subprocess.run([runnable["python"], "-c", LISTER, str(runnable["benchmark_dir"]), case_filter or ""],
                             env=env, cwd=scratch, capture_output=True, text=True)
    if out.returncode != 0:
        raise Unsupported(f"cannot discover asv benchmarks in {runnable['benchmark_dir']}: "
                          f"{(out.stderr.strip().splitlines() or ['no output'])[-1]}")
    listing = json.loads(out.stdout.strip().splitlines()[-1])
    for failure in listing["failed"]:
        print(f"asv: skipped module {failure}", file=sys.stderr)
    cases, other = [], {}
    for info in listing["benchmarks"]:
        if info["type"] != "time":
            other[info["type"]] = other.get(info["type"], 0) + 1
            continue
        runnable["benchmarks"][info["name"]] = info
        if info["params"]:
            combos = len(list(itertools.product(*info["params"])))
            cases += [f"{info['name']}-{i}" for i in range(combos)]
        else:
            cases.append(info["name"])
    if other:
        print("asv: not run (only time benchmarks map to wall-time): "
              + ", ".join(f"{n} {t}" for t, n in sorted(other.items())), file=sys.stderr)
    return cases


def _split(case: str) -> tuple[str, int | None]:
    name, _, index = case.rpartition("-")
    return (name, int(index)) if name and index.isdigit() else (case, None)


def workload(case: str, runnable: dict) -> tuple[str, dict]:
    """The benchmark name, and its parameters as asv's repr() strings, by name."""
    name, index = _split(case)
    info = runnable["benchmarks"][name]
    if index is None:
        return name, {}
    combo = list(itertools.product(*info["params"]))[index]
    return name, dict(zip(info["param_names"], combo))


DRIVER = '''\
# Generated by benchx. Runs one asv benchmark step through asv_runner.
import importlib.metadata, json, math, platform, sys
from pathlib import Path

command, benchmark_dir, benchmark_id, extra, out, project = sys.argv[1:7]
if command == "setup_cache":
    from asv_runner.setup_cache import _setup_cache
    _setup_cache((benchmark_dir, benchmark_id, extra))
    sys.exit(0)

from asv_runner.run import _run
raw = Path(out + ".raw")
_run((benchmark_dir, benchmark_id, extra, "None", str(raw)))
result = json.loads(raw.read_text())
raw.unlink()

facts = {"python_executable": sys.executable, "python_version": platform.python_version(),
         "python_implementation": platform.python_implementation().lower(),
         "python_compiler": platform.python_compiler().strip()}
try:
    facts["asv_runner_version"] = importlib.metadata.version("asv_runner")
except importlib.metadata.PackageNotFoundError:
    pass
if project:
    installed = {}
    try:
        installed["version"] = importlib.metadata.version(project)
    except importlib.metadata.PackageNotFoundError:
        pass
    module = sys.modules.get(project)
    if module is not None:
        installed["path"] = getattr(module, "__file__", None)
        for attr in ("__version__", "__git_version__"):
            if isinstance(getattr(module, attr, None), str):
                installed[attr.strip("_")] = getattr(module, attr)
    facts["project"] = {"name": project, **installed}
Path(out).write_text(json.dumps({"result": result, "facts": facts}))
'''


def _step(args, env, cwd, timeout) -> dict:
    started = time.time()
    proc = subprocess.Popen(args, env=env, cwd=cwd, text=True, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        status = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        status = None
    return {"exit_status": status, "timed_out": status is None, "stdout": stdout or "", "stderr": stderr or "",
            "started": started, "ended": time.time()}


def run_case(runnable, case, invocation, env, timeout, native_path, env_names=None) -> dict:
    """setup_cache once if the benchmark has one, then one process per round.
    Each round may take the benchmark's own `timeout`; the case as a whole is
    cut off at the order's. Never raises for harness failures."""
    name, _ = _split(case)
    info = runnable["benchmarks"][name]
    stem = native_path.name.removesuffix(".native.json")
    driver = native_path.with_name(f"{stem}.driver.py")
    driver.write_text(DRIVER)
    native_path.unlink(missing_ok=True)
    extra = json.dumps(invocation["overrides"])
    base = [runnable["python"], str(driver.resolve())]
    tail = [str(runnable["benchmark_dir"]), case, extra]
    started, deadline = time.time(), time.time() + timeout
    native = {"benchmark": info, "case": case, "overrides": invocation["overrides"],
              "planned_rounds": invocation["rounds"], "rounds": []}
    out_parts, err_parts = [], []
    status, timed_out = 0, False
    with tempfile.TemporaryDirectory() as scratch:
        steps = []
        if info.get("setup_cache_key"):
            steps.append(("setup_cache", None))
        steps += [("run", r) for r in range(invocation["rounds"])]
        for command, round_ in steps:
            budget = deadline - time.time()
            if budget <= 0:
                timed_out, status = True, None
                break
            out_file = Path(scratch, f"round-{round_}.json")
            step = _step(base + [command, *tail, str(out_file), runnable["project"] or ""], env, scratch,
                         min(budget, info.get("timeout") or budget))
            label = "setup_cache" if round_ is None else f"round {round_}"
            out_parts.append(f"--- {label}\n{step['stdout']}")
            err_parts.append(f"--- {label}\n{step['stderr']}")
            if round_ is None:
                native["setup_cache"] = {k: step[k] for k in ("exit_status", "timed_out", "started", "ended")}
                if step["exit_status"] != 0:
                    status, timed_out = step["exit_status"], step["timed_out"]
                    break
                continue
            record = {"round": round_, **{k: step[k] for k in ("exit_status", "timed_out", "started", "ended")}}
            if out_file.exists():
                record.update(json.loads(out_file.read_text()))
            native["rounds"].append(record)
            if step["exit_status"] != 0:
                status, timed_out = step["exit_status"], step["timed_out"]
                break
    native_path.write_text(json.dumps(native, indent=1, allow_nan=True) + "\n")
    return {"exit_status": status, "timed_out": timed_out, "stdout": "".join(out_parts),
            "stderr": "".join(err_parts), "native": native, "started": started, "ended": time.time(),
            "artifacts": [("driver", "text/x-python", driver)]}


def _effective(info: dict, overrides: dict, key: str, default):
    return overrides.get(key, info.get(key, default))


def case_protocol(applied: dict, run: dict, case: str) -> dict:
    """The protocol this case ran under: the order's overrides, then what the
    benchmark declares, then asv's defaults, as asv_runner resolves them."""
    native = run["native"]
    info, overrides = native["benchmark"], native["overrides"]
    rounds = native["planned_rounds"]
    repeat = _effective(info, overrides, "repeat", 0)
    number = int(_effective(info, overrides, "number", 0))
    sample_time = _effective(info, overrides, "sample_time", 0.01)
    warmup_time = _effective(info, overrides, "warmup_time", -1)
    levels = [{"unit": "process", "n": rounds}]
    if isinstance(repeat, int) and repeat > 0:
        levels.append({"unit": "value", "n": repeat})
    if number > 0:
        calibration = {"mode": "fixed", "n_iterations": number}
    elif info.get("has_setup") and info.get("setup_isolates"):
        # asv#966: with setup hooks, asv times one call per sample, setup between samples.
        calibration = {"mode": "fixed", "n_iterations": 1}
    else:
        calibration = {"mode": "adaptive", "minimum_sample_seconds": sample_time}
    if warmup_time < 0:
        warmup_time = 0.1  # asv_runner's CPython default
    warmup = {"mode": "time", "seconds": warmup_time} if warmup_time > 0 else {"mode": "none"}
    asv = {"rounds": rounds, "repeat": repeat, "number": number, "sample_time": sample_time,
           "warmup_time": warmup_time, "min_run_count": _effective(info, overrides, "min_run_count", 2),
           "setup_per_sample": bool(info.get("has_setup"))}
    return {**applied, "repetitions": {"mode": "fixed", "levels": levels}, "calibration": calibration,
            "warmup": warmup, "asv": asv}


def attempted(applied: dict) -> int:
    """Order-level: rounds, times a fixed value count when the order gives one."""
    n = 1
    for level in applied["repetitions"]["levels"]:
        n *= level["n"]
    return n


def _value_level(native: dict) -> int | None:
    repeat = _effective(native["benchmark"], native["overrides"], "repeat", 0)
    return repeat if isinstance(repeat, int) and repeat > 0 else None


def repetition_levels(applied: dict, run: dict, case: str) -> list[dict]:
    native = run["native"]
    rounds = native["planned_rounds"]
    measured = [r for r in native["rounds"] if isinstance(r.get("result"), dict)]
    values = sum(len(r["result"].get("samples", [])) for r in measured)
    levels = [{"unit": "process", "attempted": rounds, "completed": len(measured)}]
    per_round = _value_level(native)
    # An adaptive repeat (asv's default, or a (min, max, seconds) tuple) stops by its own rule.
    levels.append({"unit": "value", "attempted": rounds * per_round if per_round else max(values, 1),
                   "completed": values})
    return levels


def harness(info: dict) -> dict:
    out = {"name": NAME}
    if info.get("asv_runner_version"):
        out["version"] = info["asv_runner_version"]
    return out


def comparison_extras(info: dict) -> dict:
    """Host runtime, and asv's `version` of the benchmark code (§6.7: benchmark identity)."""
    extras = {}
    runtime = {k: info[k] for k in ("python_version", "python_implementation", "python_compiler") if k in info}
    if runtime:
        extras["runtime"] = runtime
    if info.get("benchmark_version"):
        extras["benchmark_version"] = info["benchmark_version"]
    return extras


def _failure_message(stderr: str) -> str:
    lines = [l for l in stderr.splitlines() if l and not l[0].isspace() and not l.startswith(("Traceback", "---"))]
    return lines[-1] if lines else "no output"


def translate(case: str, quantities: list[str], run: dict, attempted_: int) -> list[dict]:
    native = run["native"]
    rounds = native["rounds"]
    outputs = []
    for name in quantities:
        _, quantity = QUANTITIES[name]
        observations, numbers, skipped = [], set(), False
        for r in rounds:
            result = r.get("result")
            if isinstance(result, float) and math.isnan(result):
                skipped = True
            elif isinstance(result, dict):
                numbers.add(result.get("number"))
                for value in result.get("samples", []):
                    observations.append({"value": value, "ordinal": len(observations), "group": str(r["round"])})
        per_round = _value_level(native)
        attempted = per_round * native["planned_rounds"] if per_round else max(len(observations), 1)
        procedure = {"attempted_repetitions": attempted, "completed_repetitions": len(observations)}
        info = {}
        if run["timed_out"]:
            measurement = {"status": "error", "reason": "timeout"}
        elif skipped and not observations:
            measurement = {"status": "skipped", "reason": "harness.skipped"}
        elif not observations:
            failed = run["exit_status"] not in (0, None)
            measurement = {"status": "error", "reason": "harness.error" if failed else "harness.no-output"}
            if failed:
                info["message"] = _failure_message(run["stderr"])
        else:
            measurement = {"status": "success", "observations": observations}
            if len([r for r in rounds if isinstance(r.get("result"), dict)]) < native["planned_rounds"]:
                measurement.update(status="partial", reason="harness.partial")
            if len(numbers) == 1:
                procedure["inner_iterations"] = numbers.pop()
            else:
                info["inner_iterations_per_round"] = [r["result"].get("number") for r in rounds
                                                      if isinstance(r.get("result"), dict)]
        if run["exit_status"] not in (0, None):
            info["exit_status"] = run["exit_status"]
        outputs.append({"quantity": quantity, "measurement": measurement, "procedure": procedure, "info": info})
    return outputs


def context_facts(native: dict | None) -> tuple[dict, dict]:
    """(observed context, provenance info): the interpreter and installed project
    each round reported, and the benchmark's declared attributes."""
    native = native or {}
    facts = next((r["facts"] for r in native.get("rounds", []) if r.get("facts")), {})
    info = {k: facts[k] for k in ("python_executable", "python_version", "python_implementation",
                                  "python_compiler", "asv_runner_version") if k in facts}
    observed = {}
    if facts.get("project"):
        observed["project"] = facts["project"]
    benchmark = native.get("benchmark")
    if benchmark:
        if benchmark.get("version"):
            info["benchmark_version"] = benchmark["version"]
        info["declared"] = {k: benchmark[k] for k in ("number", "repeat", "rounds", "sample_time", "warmup_time",
                                                      "min_run_count", "timeout", "setup_cache_key")
                            if benchmark.get(k) is not None}
    return observed, info
