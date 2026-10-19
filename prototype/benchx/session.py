"""Session loop: run two prepared targets side by side, then compare them.

This is the workbench's part (system-decomposition.md §3.4; prototype-design.md
§5, measure mode). The runner takes one order per invocation and no harness can
interleave two separate binaries, so something above the runner has to write one
work order per side and round, run them alternately, and hand the run to the
comparator. That loop lives here, and the CLI, a CI script, or a front end such
as `spin bench --compare` calls it.

The session builds nothing itself. Both targets must exist when it looks; a
target provider it calls may build them first (`prepare`), and plays the
target-provider role.
"""

import json
import os
from pathlib import Path

from . import adapters, harnesses, orders, runner, snapshot, target as target_mod
from . import compare as compare_mod
from .adapters.base import Unsupported
from .errors import SessionError
from .identity import normalize_remote

PROFILES = ("revisions", "environments")
HARNESS = harnesses.GOOGLE_BENCHMARK  # the default; `adapter` picks another


def parse_target(text: str) -> tuple[str, str | None]:
    """`BUILD_DIR[:SOURCE_DIR]`."""
    build, _, source = text.partition(":")
    if not build:
        raise SessionError(f"empty build directory in target {text!r}")
    return build, source or None


def parse_label(text: str) -> tuple[str, str, str]:
    """`NAME=BASE,CONTENDER`, the two label values that name the sides."""
    name, _, values = text.partition("=")
    base, comma, contender = values.partition(",")
    if not (name and base and comma and contender) or "," in contender:
        raise SessionError(f"--label must be NAME=BASE,CONTENDER in measure mode, not {text!r}")
    return name, base, contender


KINDS = ("build", "working_tree")


def _side(spec: str, suite: str, operation: str, harness=None, environment_variables=None) -> dict:
    """Locate and identify one side before anything runs.

    `spec` is `@CONFIG[=SOURCE_REF]` (ask the provider to `operation`) or a
    bare path, which the harness reads: Google Benchmark's is
    `BUILD_DIR[:SOURCE_DIR]` (read the build directory's sidecar, if any), and
    pyperf's is a working tree, its own source. A provider's description says
    which kind it is. The harness's adapter checks that the suite is there and
    usable, as the runner will, so a side it would refuse is refused before
    any round runs.
    """
    harness = harness or HARNESS
    try:
        description = target_mod.resolve(spec, operation=operation)
    except target_mod.TargetError as e:
        raise SessionError(str(e)) from None
    source = None
    if description:
        kind = description["target"]["kind"]
        if kind not in KINDS:
            raise SessionError(f"{spec}: only build and working_tree targets are supported; {kind!r} is deferred")
        if "build" in description:
            raise SessionError(f"{spec}: a declared build is deferred in the prototype "
                               "(the runner reads CMakeCache.txt)")
        path = description["target"]["path"]
        # A working tree is its own source, as for the runner.
        source = description["target"].get("source_dir") or (path if kind == "working_tree" else None)
        uri = description["target"]["source"]["uri"]
    else:
        kind = harness.BARE_PATH_KIND
        path, source = parse_target(spec) if kind == "build" else (spec, spec)  # a working tree is its own source
        uri = None
    path = Path(path).resolve()
    if not path.is_dir():
        raise SessionError(f"{'build directory' if kind == 'build' else 'working tree'} not found: {path}")
    try:
        # The environment the runner will give the harness: inherited, plus the order's variables.
        adapters.get(harness.name).locate(path, suite, {**os.environ, **(environment_variables or {})})
    except Unsupported as e:
        raise SessionError("; ".join(str(problem) for problem in e.problems)) from None
    if kind == "build" and ":" in spec and not spec.startswith("@"):
        source = parse_target(spec)[1] or source
    if kind == "build":
        source = source or snapshot.cmake_source_dir(path)
        identity = source and snapshot.git_identity(source)
        if not identity:
            raise SessionError(f"cannot locate the checkout {path} was built from; give BUILD_DIR:SOURCE_DIR")
    else:
        identity = snapshot.git_identity(source) or snapshot.directory_identity(source)
        if not identity:
            raise SessionError(f"cannot identify the working tree {source}")
    return {"kind": kind, "path": path, "source": identity["path"], "identity": identity, "uri": uri,
            "text": target_mod.text(description), "labels": dict((description or {}).get("labels", {}))}


def _order(side: dict, *, harness, suite, filter, quantity, precision, environment_variables, project, run_key,
           source_uri, round_, slot, labels) -> dict:
    """One side's order for one round."""
    target = {"kind": side["kind"], "path": str(side["path"]), "source_dir": side["source"],
              "source": {"uri": source_uri, "type": "git"}, **side["text"]}
    try:
        return orders.make_order(suite=harness.suite_entry(suite, filter), target=target, precision=precision,
                                 quantity=quantity, run_key=run_key, project=project,
                                 labels={**side["labels"], **(labels or {})},
                                 environment_variables=environment_variables, round_=round_, slot=slot)
    except orders.OrderError as e:
        raise SessionError(f"work order for round {round_}: {e}") from None


def compare_targets(baseline: str, contender: str, *, profile: str, suite: str, rounds: int, store,
                    adapter=None, flags=None, environment_variables=None, filter=None, quantity="wall-time",
                    project=None, label=None, run_key=None, out=None, source_uri=None, k=3.0, min_rounds=3,
                    progress=None, build=True, cwd=".") -> dict:
    """Alternate baseline and contender over `rounds` rounds, deliver every
    result to `store`, and return the run-mode comparison document.

    `baseline` and `contender` are `BUILD_DIR[:SOURCE_DIR]` or `@CONFIG[=SOURCE_REF]`;
    a provider is asked to `prepare` (it may build) unless `build` is false, when
    it is only asked to `describe`. For `environments`,
    `label` is (name, baseline value, contender value).

    `adapter` names the harness (default Google Benchmark); `flags` are its
    precision flags by keyword (`harnesses.Harness.FLAGS`), over its defaults
    and, for pyperf, the config file found from `cwd`. `environment_variables`
    are set for both sides alike, and so become workload parameters of both.
    Raises SessionError when the session cannot start, and runner.Refused when
    the runner refuses an order.
    """
    if profile not in PROFILES:
        raise SessionError(f"profile {profile!r} is not in the prototype ({', '.join(PROFILES)})")
    if rounds < 1:
        raise SessionError("--rounds must be at least 1")
    if profile == "environments" and label is None:
        raise SessionError("the environments profile needs --label NAME=BASE,CONTENDER")
    if profile == "revisions" and label is not None:
        raise SessionError("--label names the sides of an environments comparison; revisions are named by their trees")
    harness = harnesses.get(adapter) if adapter else HARNESS
    precision, _ = harness.precision(harness.flags_to_precision(**(flags or {})), start=cwd)
    operation = "prepare" if build else "describe"
    sides = [_side(spec, suite, operation, harness, environment_variables) for spec in (baseline, contender)]
    if profile == "revisions":
        # UC-03 §10: refuse before any measurement, not after R rounds.
        for name, side in zip(("baseline", "contender"), sides):
            if side["identity"]["dirty"] != "clean":
                raise SessionError(f"{name} tree {side['source']} is {side['identity']['dirty']}; "
                                   "a revisions comparison needs clean trees (UC-03 §10)")
    if source_uri:
        uris = {normalize_remote(source_uri)}
    else:
        uris = {normalize_remote(side["uri"], base=side["source"]) if side["uri"] else snapshot.source_uri(side["source"])
                for side in sides}
    if len(uris) != 1:
        raise SessionError(f"the sides report different source URIs {sorted(uris)}; pass --source-uri")
    source_uri = uris.pop()

    run_key = run_key or orders.default_run_key("compare")
    out = Path(out) if out else Path("results") / run_key.replace("/", "_")
    (out / "orders").mkdir(parents=True, exist_ok=True)
    labels = [{label[0]: label[1]}, {label[0]: label[2]}] if label else [None, None]

    slot = 0
    for round_ in range(rounds):
        for name, side, side_labels in zip(("baseline", "contender"), sides, labels):
            order = _order(side, harness=harness, suite=suite, filter=filter, quantity=quantity,
                           precision=precision, environment_variables=environment_variables, project=project,
                           run_key=run_key, source_uri=source_uri, round_=round_, slot=slot, labels=side_labels)
            path = out / "orders" / f"order-{slot:04d}.json"
            path.write_text(json.dumps(order, indent=1) + "\n")
            summary = runner.run(path, out)
            outcome = store.ingest_paths(summary["files"])
            if outcome["rejected"]:
                codes = sorted({r["code"] for r in outcome["rejected"]})
                raise SessionError(f"the store rejected results of round {round_} ({name}): {', '.join(codes)}")
            if progress:
                progress(f"round {round_ + 1}/{rounds} {name}: {summary['results']} result(s)")
            slot += 1

    base_value = label[1] if profile == "environments" else sides[0]["identity"]["tree"]
    return compare_mod.compare(store.documents(run_key), run_key=run_key, profile=profile,
                               baseline=base_value, label=label[0] if label else None, k=k,
                               min_rounds=min_rounds)
