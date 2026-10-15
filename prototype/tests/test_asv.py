"""`bx asv` and the asv adapter, on a small suite run through asv_runner."""

import json
import textwrap

import pytest

pytest.importorskip("asv_runner")

from benchx import bench, cli, core
from benchx.adapters import asv
from benchx.store import Store

SUITE = textwrap.dedent('''
    class Sort:
        params = [[10, 100], ["a", "b"]]
        param_names = ["n", "key"]
        number = 3

        def setup(self, n, key):
            self.data = list(range(n))[::-1]

        def time_sorted(self, n, key):
            sorted(self.data)

        def peakmem_sorted(self, n, key):
            sorted(self.data)

    class Cached:
        def setup_cache(self):
            return list(range(50))

        def time_sum(self, data):
            sum(data)

    class WithSetup:
        def setup(self):
            self.data = list(range(100))

        def time_sum(self):
            sum(self.data)

    def time_plain():
        sum(range(100))

    class Skipped:
        def setup(self):
            raise NotImplementedError  # asv: skip this benchmark

        def time_skipped(self):
            pass

    def time_broken():
        raise ValueError("boom")
''')
FAST = ["--rounds", "2", "--repeat", "2", "--warmup-time", "0"]


@pytest.fixture
def suite(tmp, monkeypatch):
    root = tmp / "proj"
    (root / "benchmarks").mkdir(parents=True)
    (root / "asv.conf.json").write_text('{\n  // a comment, as asv allows\n  "version": 1, "project": "json",\n'
                                        '  "benchmark_dir": "benchmarks"\n}\n')
    (root / "benchmarks" / "__init__.py").write_text("")
    (root / "benchmarks" / "bm.py").write_text(SUITE)
    (root / "benchmarks" / "needs_missing.py").write_text("import no_such_module_anywhere\n")
    monkeypatch.chdir(root)
    return root


def results(root, run_key):
    return {(d["coordinates"]["workload"]["name"], json.dumps(d["coordinates"]["workload"].get("parameters", {}),
                                                              sort_keys=True)): d
            for d in (json.loads(p.read_text()) for p in (root / "results" / run_key).glob("*.json"))
            if not d.get("schema_version") == "benchx/work-order/0.1.0"}


def test_flags_become_precision_and_overrides():
    precision = bench.asv_precision(rounds=3, repeat=4, number=5, warmup_time=0)
    applied, invocation = asv.protocol(precision)
    assert invocation == {"rounds": 3, "overrides": {"repeat": 4, "number": 5, "warmup_time": 0}}
    assert applied["repetitions"]["levels"] == [{"unit": "process", "n": 3}, {"unit": "value", "n": 4}]
    with pytest.raises(bench.SessionError):
        bench.asv_precision(number=1, sample_time=0.1)
    with pytest.raises(asv.Unsupported):
        asv.protocol({"repetitions": 2})
    with pytest.raises(asv.Unsupported):
        asv.protocol({"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 1}]},
                      "warmup": {"mode": "count", "n_warmup": 1}})


def test_asv_suite_reads_the_benchmark_dir(suite):
    assert bench.asv_suite(suite) == suite / "benchmarks"
    assert bench.asv_suite(suite / "benchmarks") == suite / "benchmarks"


def test_asv_command_runs_every_time_benchmark(suite, capsys):
    assert cli.main(["asv", "--run-key", "all", "--project", "p", *FAST]) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "skipped module benchmarks.needs_missing" in err
    assert "1 peakmem" in err
    found = results(suite, "all")
    for document in found.values():
        assert core.check(document, "measurement-result") == []
    # One case per parameter combination, parameters as asv's repr() strings.
    sort = {k[1]: d for k, d in found.items() if k[0] == "bm.Sort.time_sorted"}
    assert sorted(sort) == sorted(json.dumps({"n": n, "key": key}, sort_keys=True)
                                  for n in ("10", "100") for key in ("'a'", "'b'"))
    one = next(iter(sort.values()))
    assert one["measurement"]["status"] == "success"
    assert len(one["measurement"]["observations"]) == 4
    assert {o["group"] for o in one["measurement"]["observations"]} == {"0", "1"}
    protocol = one["coordinates"]["comparison_context"]["protocol"]
    # The order's --repeat wins; the benchmark's own `number = 3` applies.
    assert protocol["calibration"] == {"mode": "fixed", "n_iterations": 3}
    assert protocol["repetitions"]["levels"] == [{"unit": "process", "n": 2}, {"unit": "value", "n": 2}]
    assert protocol["warmup"] == {"mode": "none"}
    assert one["procedure"]["inner_iterations"] == 3
    assert one["coordinates"]["comparison_context"]["benchmark_version"]
    assert one["observed_context"]["asv"]["project"]["name"] == "json"
    # setup_cache ran, and its value reached the benchmark.
    assert found[("bm.Cached.time_sum", "{}")]["measurement"]["status"] == "success"
    assert found[("bm.time_plain", "{}")]["measurement"]["status"] == "success"
    assert found[("bm.Skipped.time_skipped", "{}")]["measurement"]["status"] == "skipped"
    broken = found[("bm.time_broken", "{}")]
    assert broken["measurement"] == {"status": "error", "reason": "harness.error"}
    assert "boom" in broken["provenance"]["info"]["message"]
    assert not any("peakmem" in name for name, _ in found)
    assert {d["coordinates"]["workload"]["name"] for d in Store().documents("all")} >= {"bm.time_plain"}


def test_filter_and_benchmark_defaults(suite, capsys):
    assert cli.main(["asv", ".", "-b", r"time_plain$", "--run-key", "one", "--no-ingest",
                     "--rounds", "1"]) == cli.EXIT_OK
    found = results(suite, "one")
    assert list(found) == [("bm.time_plain", "{}")]
    protocol = found[("bm.time_plain", "{}")]["coordinates"]["comparison_context"]["protocol"]
    # Nothing overridden: asv's own defaults, adaptive calibration and 0.1 s warmup.
    assert protocol["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.01}
    assert protocol["warmup"] == {"mode": "time", "seconds": 0.1}
    assert protocol["repetitions"]["levels"] == [{"unit": "process", "n": 1}]
    assert "1 rounds x per-benchmark repeat" in capsys.readouterr().err


def test_setup_means_one_call_per_sample(suite):
    # asv#966: a benchmark with a setup and no declared number is timed one call per sample.
    assert cli.main(["asv", "-b", r"WithSetup\.time_sum$", "--run-key", "c", "--no-ingest",
                     "--rounds", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "c").values()
    protocol = document["coordinates"]["comparison_context"]["protocol"]
    assert protocol["calibration"] == {"mode": "fixed", "n_iterations": 1}
    assert protocol["asv"]["setup_per_sample"] is True
    assert document["procedure"]["inner_iterations"] == 1


def test_missing_asv_runner_is_refused(suite, monkeypatch, tmp, capsys):
    bare = tmp / "bin"
    bare.mkdir()
    (bare / "python").symlink_to("/usr/bin/false")
    monkeypatch.setenv("PATH", str(bare))
    assert cli.main(["asv", "--no-ingest"]) == cli.EXIT_REFUSED
    assert "asv_runner is not importable" in capsys.readouterr().err
