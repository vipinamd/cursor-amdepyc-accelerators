#!/usr/bin/env python3
"""Shared cross-platform helpers for the accelerator benchmark framework.

Provides config loading (config/lab.secrets, config/lab.hosts,
config/report-email.conf) and paramiko-based SSH helpers so the pipeline
runs identically on Windows and Linux without sshpass.

Ported from the brcm-ptp lab framework.
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - guidance for the user
    sys.exit(
        "paramiko is required. Install it with:\n"
        "  pip install -r requirements.txt"
    )

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config"
RESULTS = REPO / "results"
RUNS = RESULTS / "runs"
REPORTS = RESULTS / "reports"
BUNDLES = RESULTS / "bundles"

# Defaults mirror config/lab.hosts.example so behaviour is well-defined even
# before the user copies the example files into place.
DEFAULTS = {
    "BMC_USER": "root",
    "BMC_PASS": "0penBmc",
    "SSH_USER": "amd",
    "SSH_PASS": "amd123",
    "DUT_SSH_PASS": "",
    "PKTGEN_SSH_PASS": "",
    "DUT_HOST": "",
    "DUT_BMC_IP": "",
    "PKTGEN_HOST": "",
    "CPU_SOC": "turin",
    "DPDK_VERSION": "26.03",
    "DPDK_URL": "https://fast.dpdk.org/rel/dpdk-26.03.tar.xz",
    "ACCEL_BASE": "~/accel-bench",
    "DPDK_DIR": "~/accel-bench/dpdk-26.03",
    "DPDK_BUILD": "build",
    "CTRL_LCORE": "1",
    "MAX_WORKER_LCORES": "8",
    "HUGEPAGE_MODE": "2m",
    "TUNE_HUGEPAGES": "64",
    "TUNE_ISOLCPUS": "",
    "POWER_SOURCE": "both",
    "POWER_INTERVAL": "1",
    "PROFILER": "perf",
    "PROFILER_FREQ": "997",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config() -> dict[str, str]:
    """Load lab.secrets + lab.hosts + report-email.conf into one dict.

    Files are simple KEY=value with '#' comments. Later files override
    earlier ones; DEFAULTS fill the gaps.
    """
    cfg: dict[str, str] = {}
    for name in ("lab.secrets", "lab.hosts", "report-email.conf"):
        path = CONFIG / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip("'\"")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    if not cfg.get("DUT_SSH_PASS"):
        cfg["DUT_SSH_PASS"] = cfg["SSH_PASS"]
    if not cfg.get("PKTGEN_SSH_PASS"):
        cfg["PKTGEN_SSH_PASS"] = cfg["SSH_PASS"]
    return cfg


def ssh_pass(cfg: dict[str, str], host: str) -> str:
    if host and host == cfg.get("DUT_HOST"):
        return cfg["DUT_SSH_PASS"]
    if host and host == cfg.get("PKTGEN_HOST"):
        return cfg["PKTGEN_SSH_PASS"]
    return cfg["SSH_PASS"]


def expand_home(path: str) -> str:
    """Remote '~' is not expanded under sudo; use $HOME instead."""
    if path.startswith("~/"):
        return "$HOME/" + path[2:]
    return path


def sudo(pw: str, cmd: str) -> str:
    return f"echo '{pw}' | sudo -S {cmd}"


def ssh_client(host: str, user: str, pw: str, timeout: int = 20) -> "paramiko.SSHClient":
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=pw,
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def run_remote(host: str, user: str, pw: str, cmd: str, timeout: int = 600) -> tuple[int, str]:
    """Run a single command; return (exit_code, combined stdout+stderr)."""
    client = ssh_client(host, user, pw)
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=True)
        out = (stdout.read() + stderr.read()).decode(errors="replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    return code, out


def run_remote_script(host: str, user: str, pw: str, script: str, timeout: int = 600) -> tuple[int, str]:
    """Upload a bash script via SFTP and execute it; return (code, output)."""
    client = ssh_client(host, user, pw)
    try:
        sftp = client.open_sftp()
        path = "/tmp/accel_bench_run.sh"
        sftp.putfo(io.BytesIO(script.encode()), path)
        sftp.chmod(path, 0o755)
        sftp.close()
        _, stdout, stderr = client.exec_command(f"bash {path}", timeout=timeout, get_pty=True)
        out = (stdout.read() + stderr.read()).decode(errors="replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    return code, out


def get_remote(host: str, user: str, pw: str, remote: str, local: str | Path) -> bool:
    """SFTP-fetch a remote file to a local path. Returns True on success."""
    client = ssh_client(host, user, pw)
    try:
        sftp = client.open_sftp()
        sftp.get(remote, str(local))
        sftp.close()
        return True
    except Exception:  # noqa: BLE001 - missing file is non-fatal
        return False
    finally:
        client.close()


def verify_ssh(cfg: dict[str, str], host: str) -> tuple[bool, str]:
    """Return (ok, hostname-or-error) for a quick connectivity check."""
    try:
        code, out = run_remote(host, cfg["SSH_USER"], ssh_pass(cfg, host), "hostname", 15)
        return code == 0, out.strip()
    except Exception as exc:  # noqa: BLE001 - report any connection failure
        return False, str(exc)


def reboot_host(host: str, user: str, pw: str) -> None:
    """Issue a reboot; the SSH connection drops, which is expected."""
    try:
        run_remote(host, user, pw, sudo(pw, "shutdown -r now") + " || true", 20)
    except Exception as exc:  # noqa: BLE001 - connection drops on reboot
        log(f"  {host}: reboot issued (connection dropped: {type(exc).__name__})")


def wait_for_ssh(host: str, user: str, pw: str, timeout_s: int = 420,
                 settle_s: int = 30) -> bool:
    """Wait for a host to come back after reboot. Returns True when SSH works."""
    deadline = time.time() + timeout_s
    time.sleep(settle_s)  # give the box a moment to actually go down first
    while time.time() < deadline:
        try:
            code, out = run_remote(host, user, pw, "uptime", 12)
            if code == 0:
                tail = out.strip().splitlines()[-1] if out.strip() else "ok"
                log(f"  {host}: back up - {tail}")
                return True
        except Exception:  # noqa: BLE001 - host still down
            pass
        time.sleep(10)
    return False
