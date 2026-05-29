#!/usr/bin/env python3
"""CPU topology discovery + worker-lcore placement planning.

Probes a DUT for its SMT-sibling / L3-domain (CCX/CCD) / NUMA layout and turns
a placement strategy + worker count into an explicit list of logical CPUs to
pin DPDK worker lcores to. Used by run-accel-benchmark.py under --topology to
explore how placement affects accelerator throughput:

  single_core  one physical core, one thread
  smt_pair     one physical core + its SMT sibling
  same_ccd     N logical CPUs from a single L3 domain (shared cache)
  across_ccd   N logical CPUs spread one-per-L3 (maximum cache separation)

On AMD EPYC a CCD's compute complex (CCX) shares one L3, so the L3 instance id
from `lscpu -e=...,L3` is the CCD grouping signal.
"""
from __future__ import annotations

import re

import _accel_common as store
from _lab_common import run_remote_script

STRATEGIES = ("single_core", "smt_pair", "same_ccd", "across_ccd")


# --------------------------------------------------------------------------
# remote probe
# --------------------------------------------------------------------------
def probe_script() -> str:
    return r"""#!/bin/bash
echo '===LSCPU==='
lscpu -e=CPU,CORE,SOCKET,NODE,L3 2>/dev/null || echo NOLSCPU
echo '===ISOLATED==='
cat /sys/devices/system/cpu/isolated 2>/dev/null
echo '===SYSFS==='
for c in /sys/devices/system/cpu/cpu[0-9]*; do
  id=${c##*cpu}
  core=$(cat "$c/topology/core_id" 2>/dev/null || echo -1)
  pkg=$(cat "$c/topology/physical_package_id" 2>/dev/null || echo 0)
  l3=$(cat "$c/cache/index3/id" 2>/dev/null || echo -1)
  nd=$(ls -d "$c"/node* 2>/dev/null | head -1); nd=${nd##*node}
  echo "$id $core $pkg ${nd:-0} $l3"
done
echo '===END==='
"""


def _section(raw: str, name: str) -> str:
    m = re.search(rf"==={name}===\n(.*?)(?=\n===|\Z)", raw, re.S)
    return m.group(1).strip() if m else ""


def parse(raw: str) -> dict:
    """Return {cpus, siblings, l3_domains, sockets, nodes, smt}.

    cpus: list of {cpu, core, socket, node, l3} (one per logical CPU)
    siblings: (socket, core) -> [cpu, ...] sorted (SMT threads of a core)
    l3_domains: l3_id -> ordered list of (socket, core) keys (by primary cpu)
    """
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cpus = _parse_lscpu(_section(raw, "LSCPU"))
    if not cpus:
        cpus = _parse_sysfs(_section(raw, "SYSFS"))
    model = _build_model(cpus)
    model["isolated"] = parse_cpu_list(_section(raw, "ISOLATED"))
    return model


def parse_cpu_list(spec: str) -> set[int]:
    """Expand a sysfs cpu-range string like '8-15,20,30-31' into a set."""
    out: set[int] = set()
    for tok in (spec or "").strip().split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(tok))
            except ValueError:
                continue
    return out


def _parse_lscpu(blk: str) -> list[dict]:
    if not blk or "NOLSCPU" in blk:
        return []
    lines = [ln for ln in blk.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split()
    idx = {name: i for i, name in enumerate(header)}
    needed = ("CPU", "CORE", "SOCKET", "NODE", "L3")
    if not all(k in idx for k in needed):
        return []
    out: list[dict] = []
    for ln in lines[1:]:
        cols = ln.split()
        if len(cols) < len(header):
            continue
        try:
            out.append({
                "cpu": int(cols[idx["CPU"]]),
                "core": int(cols[idx["CORE"]]),
                "socket": int(cols[idx["SOCKET"]]),
                "node": int(re.sub(r"\D", "", cols[idx["NODE"]]) or "0"),
                "l3": int(re.sub(r"\D", "", cols[idx["L3"]]) or "0"),
            })
        except ValueError:
            continue
    return out


def _parse_sysfs(blk: str) -> list[dict]:
    out: list[dict] = []
    for ln in blk.splitlines():
        parts = ln.split()
        if len(parts) < 5:
            continue
        try:
            cpu, core, pkg, node, l3 = (int(re.sub(r"\D", "", p) or "0") for p in parts[:5])
        except ValueError:
            continue
        out.append({"cpu": cpu, "core": core, "socket": pkg, "node": node, "l3": l3})
    return out


def _build_model(cpus: list[dict]) -> dict:
    cpus = sorted(cpus, key=lambda c: c["cpu"])
    siblings: dict[tuple[int, int], list[int]] = {}
    core_l3: dict[tuple[int, int], int] = {}
    core_node: dict[tuple[int, int], int] = {}
    for c in cpus:
        key = (c["socket"], c["core"])
        siblings.setdefault(key, []).append(c["cpu"])
        core_l3[key] = c["l3"]
        core_node[key] = c["node"]
    for key in siblings:
        siblings[key].sort()

    # l3 -> ordered core keys (by the core's primary/lowest cpu id)
    l3_domains: dict[int, list[tuple[int, int]]] = {}
    for key in sorted(siblings, key=lambda k: siblings[k][0]):
        l3_domains.setdefault(core_l3[key], []).append(key)

    smt = any(len(v) >= 2 for v in siblings.values())
    return {
        "cpus": cpus,
        "siblings": siblings,
        "core_l3": core_l3,
        "core_node": core_node,
        "l3_domains": l3_domains,
        "sockets": sorted({c["socket"] for c in cpus}),
        "nodes": sorted({c["node"] for c in cpus}),
        "smt": smt,
    }


# --------------------------------------------------------------------------
# placement planning
# --------------------------------------------------------------------------
def _domains_on_node(model: dict, numa_node: int | None,
                     exclude: set[int]) -> list[tuple[int, list[dict]]]:
    """Ordered [(l3_id, [core,...])] where core = {key, cpus} on numa_node.

    Excluded cpus are removed; cores left with no usable cpu are dropped.
    When the host has isolated CPUs (isolcpus), placement is restricted to that
    set so workers never land on a non-isolated housekeeping core (which would
    stall a DPDK lcore barrier). Falls back to all nodes if nothing matches the
    requested node.
    """
    sib, core_node = model["siblings"], model["core_node"]
    isolated = model.get("isolated") or set()

    def usable(cpu_list: list[int]) -> list[int]:
        cpus = [c for c in cpu_list if c not in exclude]
        if isolated:
            cpus = [c for c in cpus if c in isolated]
        return cpus

    def build(node_filter):
        doms: list[tuple[int, list[dict]]] = []
        for l3, keys in model["l3_domains"].items():
            cores = []
            for key in keys:
                if node_filter is not None and core_node[key] != node_filter:
                    continue
                cpu_list = usable(sib[key])
                if cpu_list:
                    cores.append({"key": key, "cpus": cpu_list})
            if cores:
                doms.append((l3, cores))
        return doms

    doms = build(numa_node)
    if not doms:
        doms = build(None)
    # Largest domains first so same_ccd uses the fullest CCD.
    return sorted(doms, key=lambda d: -sum(len(c["cpus"]) for c in d[1]))


def _domain_cpu_order(cores: list[dict]) -> list[int]:
    """Primaries of every core first, then SMT siblings (2nd thread, 3rd, ...)."""
    order = [c["cpus"][0] for c in cores]
    maxt = max((len(c["cpus"]) for c in cores), default=1)
    for t in range(1, maxt):
        for c in cores:
            if len(c["cpus"]) > t:
                order.append(c["cpus"][t])
    return order


def _result(model: dict, lcores: list[int], note: str = "") -> dict:
    core_l3 = model["core_l3"]
    cpu_key = {c["cpu"]: (c["socket"], c["core"]) for c in model["cpus"]}
    keys = [cpu_key[l] for l in lcores if l in cpu_key]
    cores = sorted(set(keys))
    l3s = sorted({core_l3[k] for k in keys})
    # SMT used if any physical core contributes >1 lcore here.
    smt_used = len(lcores) > len(cores)
    return {
        "lcores": lcores,
        "cores": [f"{s}:{c}" for s, c in cores],
        "l3_domains": l3s,
        "smt_used": smt_used,
        "note": note,
    }


def plan_lcores(model: dict, strategy: str, count: int, numa_node: int | None,
                exclude: set[int] | None = None) -> dict:
    """Map (strategy, count) to an explicit worker-lcore list for this host."""
    exclude = set(exclude or ())
    doms = _domains_on_node(model, numa_node, exclude)
    if not doms:
        return _result(model, [], "no usable cores")

    if strategy == "single_core":
        first = doms[0][1][0]
        return _result(model, [first["cpus"][0]], "single physical core, 1 thread")

    if strategy == "smt_pair":
        for _l3, cores in doms:
            for c in cores:
                if len(c["cpus"]) >= 2:
                    return _result(model, c["cpus"][:2], "one core + SMT sibling")
        return _result(model, [doms[0][1][0]["cpus"][0]], "SMT off; only 1 thread available")

    if strategy == "same_ccd":
        # Fullest single L3 domain: primaries first, then siblings.
        order = _domain_cpu_order(doms[0][1])
        chosen = order[:count]
        note = f"L3 domain {doms[0][0]}"
        if count > len(order):
            note += f"; clamped {count}->{len(order)} (CCD capacity)"
        return _result(model, chosen, note)

    if strategy == "across_ccd":
        orders = [_domain_cpu_order(cores) for _l3, cores in doms]
        chosen: list[int] = []
        col = 0
        maxcol = max((len(o) for o in orders), default=0)
        while len(chosen) < count and col < maxcol:
            for o in orders:
                if col < len(o):
                    chosen.append(o[col])
                    if len(chosen) >= count:
                        break
            col += 1
        note = f"{len(doms)} L3 domain(s), round-robin"
        if count > len(chosen):
            note += f"; clamped {count}->{len(chosen)}"
        return _result(model, chosen, note)

    raise ValueError(f"unknown placement strategy '{strategy}'")


def count_list(strategy: str, count_sweep: list[int]) -> list[int]:
    """The worker-count sweep for a strategy (single/pair are fixed-size)."""
    if strategy == "single_core":
        return [1]
    if strategy == "smt_pair":
        return [2]
    return list(count_sweep)


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
def snapshot(cfg: dict, host: str, user: str, pw: str) -> dict:
    _, raw = run_remote_script(host, user, pw, probe_script(), 60)
    model = parse(raw)
    doms = model["l3_domains"]
    cores_per_l3 = ([len(v) for v in doms.values()] or [0])
    return {
        "host": host,
        "generated": store.now_iso(),
        "git_commit": store.git_commit(),
        "model": model,
        "l3_count": len(doms),
        "cores_per_l3": max(cores_per_l3),
        "sockets": len(model["sockets"]),
        "nodes": model["nodes"],
        "smt": model["smt"],
        "isolated_count": len(model.get("isolated") or set()),
    }
