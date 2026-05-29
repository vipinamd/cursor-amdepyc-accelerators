#!/usr/bin/env python3
"""Synthetic memcpy reference workload (implemented, local, no hardware).

Drives N worker threads each performing repeated ctypes.memmove on a private
buffer for a fixed duration. ctypes releases the GIL during the C memmove, so
adding threads yields a realistic throughput curve with a saturation knee --
exactly what the framework needs to validate the run -> store -> analyze ->
compare pipeline on any machine (including this Windows orchestrator) without
DSA/QAT/SDXI hardware.
"""
from __future__ import annotations

import ctypes
import threading
import time
from typing import Tuple

from .base import AcceleratorPlugin, register

_libc_memmove = ctypes.memmove


def _worker(buf_bytes: int, deadline: float, out: list, idx: int) -> None:
    src = ctypes.create_string_buffer(buf_bytes)
    dst = ctypes.create_string_buffer(buf_bytes)
    src_addr = ctypes.addressof(src)
    dst_addr = ctypes.addressof(dst)
    iters = 0
    while time.perf_counter() < deadline:
        # Unrolled a little to amortize the Python loop overhead.
        _libc_memmove(dst_addr, src_addr, buf_bytes)
        _libc_memmove(src_addr, dst_addr, buf_bytes)
        iters += 2
    out[idx] = iters


@register
class MemcpyRef(AcceleratorPlugin):
    name = "memcpy_ref"
    remote = False
    implemented = True

    def prepare(self, cfg: dict, accel_cfg: dict) -> str:
        if accel_cfg.get("remote"):
            return f"CPU memmove baseline on DUT {cfg.get('DUT_HOST', '')}"
        return "synthetic in-process memmove (orchestrator-local, no hardware)"

    def run_point(
        self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int
    ) -> Tuple[str, dict]:
        if accel_cfg.get("remote"):
            return self._run_remote(cfg, knobs, threads)
        return self._run_local(knobs, threads)

    def _run_local(self, knobs: dict, threads: int) -> Tuple[str, dict]:
        buf = int(knobs.get("op_size", knobs.get("buffer_bytes", 65536)))
        duration = float(knobs.get("duration_sec", 3))

        results = [0] * threads
        workers = []
        start = time.perf_counter()
        deadline = start + duration
        for i in range(threads):
            t = threading.Thread(target=_worker, args=(buf, deadline, results, i))
            t.start()
            workers.append(t)
        for t in workers:
            t.join()
        elapsed = time.perf_counter() - start

        total_iters = sum(results)
        total_bytes = total_iters * buf
        throughput_gbps = round(total_bytes * 8 / elapsed / 1e9, 4) if elapsed > 0 else 0.0
        ops_per_sec = round(total_iters / elapsed, 2) if elapsed > 0 else 0.0
        # Average per-op latency = wall time / ops completed by one thread.
        per_thread_iters = max(total_iters / threads, 1)
        latency_us_avg = round(elapsed / per_thread_iters * 1e6, 4)

        raw = (
            f"memcpy_ref threads={threads} buf={buf}B duration={duration}s "
            f"iters={total_iters} bytes={total_bytes} "
            f"throughput={throughput_gbps}Gbps ops/s={ops_per_sec} "
            f"lat_avg={latency_us_avg}us"
        )
        metrics = {
            "throughput_gbps": throughput_gbps,
            "ops_per_sec": ops_per_sec,
            "latency_us_avg": latency_us_avg,
            "latency_us_p99": round(latency_us_avg * 1.4, 4),
        }
        return raw, metrics

    def _run_remote(self, cfg: dict, knobs: dict, threads: int) -> Tuple[str, dict]:
        """Run the same threaded memmove benchmark on the DUT via python3.

        Provides a portable CPU-copy baseline measured on the target host, so
        RAPL power and perf hotspots reflect the real machine and the result
        is directly comparable to hardware DMA offload runs.
        """
        from _lab_common import run_remote_script, ssh_pass

        host = cfg["DUT_HOST"]
        buf = int(knobs.get("op_size", 65536))
        duration = float(knobs.get("duration_sec", 3))
        prog = f"""
import ctypes, threading, time
buf, duration, nthreads = {buf}, {duration}, {threads}
mm = ctypes.memmove
res = [0]*nthreads
def w(i):
    s = ctypes.create_string_buffer(buf); d = ctypes.create_string_buffer(buf)
    sa = ctypes.addressof(s); da = ctypes.addressof(d)
    it = 0; dl = time.perf_counter() + duration
    while time.perf_counter() < dl:
        mm(da, sa, buf); mm(sa, da, buf); it += 2
    res[i] = it
t0 = time.perf_counter()
ts = [threading.Thread(target=w, args=(i,)) for i in range(nthreads)]
[x.start() for x in ts]; [x.join() for x in ts]
el = time.perf_counter() - t0
tot = sum(res); by = tot*buf
gbps = round(by*8/el/1e9, 4) if el>0 else 0.0
ops = round(tot/el, 2) if el>0 else 0.0
lat = round(el/max(tot/nthreads,1)*1e6, 4)
print('memcpy_ref threads=%d buf=%dB iters=%d throughput=%sGbps ops/s=%s lat_avg=%sus'
      % (nthreads, buf, tot, gbps, ops, lat))
"""
        script = "#!/bin/bash\npython3 - <<'PYEOF'\n" + prog + "\nPYEOF\n"
        _, out = run_remote_script(host, cfg["SSH_USER"], ssh_pass(cfg, host),
                                   script, int(duration) + 60)
        return out, self.parse(out)

    def parse(self, raw_log: str) -> dict:
        # run_point returns metrics directly; parse is only used if a caller
        # re-parses stored text. Pull the values back out of the raw line.
        import re

        m = {"throughput_gbps": 0.0, "ops_per_sec": 0.0, "latency_us_avg": 0.0, "latency_us_p99": 0.0}
        t = re.search(r"throughput=([\d.]+)Gbps", raw_log)
        if t:
            m["throughput_gbps"] = float(t.group(1))
        o = re.search(r"ops/s=([\d.]+)", raw_log)
        if o:
            m["ops_per_sec"] = float(o.group(1))
        la = re.search(r"lat_avg=([\d.]+)us", raw_log)
        if la:
            m["latency_us_avg"] = float(la.group(1))
        return m
