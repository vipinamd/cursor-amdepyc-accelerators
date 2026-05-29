#!/usr/bin/env python3
"""Profiler factory: pick the backend from config, fall back gracefully.

Local (non-remote) workloads use NoProfiler -- there is no DUT to sample.
For remote workloads we honor PROFILER (perf|uprof|vtune|none); if the
requested profiler is not installed on the DUT we fall back to perf, then
to NoProfiler, so a run never fails just because a profiler is missing.
"""
from __future__ import annotations

from _lab_common import log
from .base import Profiler, NoProfiler
from .perf import PerfProfiler
from .uprof import UprofProfiler
from .vtune import VtuneProfiler

_BACKENDS = {
    "perf": PerfProfiler,
    "uprof": UprofProfiler,
    "vtune": VtuneProfiler,
}


def make_profiler(cfg: dict, remote: bool) -> Profiler:
    if not remote:
        return NoProfiler(cfg)
    want = (cfg.get("PROFILER", "perf") or "perf").lower()
    if want == "none":
        return NoProfiler(cfg)

    cls = _BACKENDS.get(want, PerfProfiler)
    prof = cls(cfg)
    if prof.available():
        return prof
    log(f"profiler '{want}' not found on DUT; trying perf")
    perf = PerfProfiler(cfg)
    if perf.available():
        return perf
    log("no profiler available on DUT; continuing without profiling")
    return NoProfiler(cfg)
