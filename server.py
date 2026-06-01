# SPDX-License-Identifier: MIT
"""Docker Engine MCP Server — exposes Docker data via Portainer API to Claude."""

import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

PORTAINER_URL = os.getenv("PORTAINER_URL", "https://localhost:9443").rstrip("/")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
MCP_PORT = int(os.getenv("MCP_PORT", "3003"))

SENSITIVE_PATTERNS = ("PASSWORD", "SECRET", "KEY", "TOKEN", "PASS")

mcp = FastMCP("docker-engine")

_environments: Optional[list[dict]] = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=PORTAINER_URL,
        headers={"X-API-Key": PORTAINER_API_KEY},
        verify=False,
        timeout=30.0,
    )


async def _get_environments() -> list[dict]:
    global _environments
    if _environments is not None:
        return _environments
    async with _client() as c:
        r = await c.get("/api/endpoints")
        r.raise_for_status()
        _environments = r.json()
    return _environments


async def _resolve_env_id(host: str) -> tuple[int, str]:
    envs = await _get_environments()
    if not host or host.lower() in ("local", "default"):
        return envs[0]["Id"], envs[0]["Name"]
    lower = host.lower()
    for e in envs:
        if lower in e.get("Name", "").lower():
            return e["Id"], e["Name"]
    names = ", ".join(e.get("Name", str(e["Id"])) for e in envs)
    raise ValueError(f"Unknown host '{host}'. Available environments: {names}")


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_age(created_ts: int) -> str:
    dt = datetime.fromtimestamp(created_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    delta = now - dt
    days = delta.days
    if days == 0:
        hours = delta.seconds // 3600
        return f"{hours}h ago" if hours else "just now"
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def _is_sensitive(var_name: str) -> bool:
    upper = var_name.upper()
    return any(p in upper for p in SENSITIVE_PATTERNS)


@mcp.tool()
async def list_environments() -> str:
    """List all Docker environments registered in Portainer with their IDs and names."""
    try:
        envs = await _get_environments()
    except Exception as e:
        return f"Error fetching environments: {e}"
    if not envs:
        return "No environments found in Portainer."
    lines = [f"Portainer environments ({len(envs)} total):"]
    for e in envs:
        status = "up" if e.get("Status") == 1 else "down"
        lines.append(f"  [{e['Id']}] {e.get('Name', 'unknown')} — {status}")
    return "\n".join(lines)


@mcp.tool()
async def list_containers(host: str = "local", all: bool = False) -> str:
    """
    List containers on a Docker host.

    Args:
        host: Environment name from list_environments. Defaults to first environment.
        all: Include stopped containers. Defaults to False (running only).
    """
    try:
        env_id, env_name = await _resolve_env_id(host)
    except ValueError as e:
        return str(e)
    params = {"all": "true" if all else "false"}
    try:
        async with _client() as c:
            r = await c.get(f"/api/endpoints/{env_id}/docker/containers/json", params=params)
            r.raise_for_status()
            containers = r.json()
    except Exception as e:
        return f"Error fetching containers from {env_name}: {e}"
    if not containers:
        label = "containers" if all else "running containers"
        return f"No {label} found on {env_name}."
    lines = [f"Containers on {env_name} ({len(containers)} {'total' if all else 'running'}):"]
    for c in sorted(containers, key=lambda x: x.get("Names", [""])[0]):
        name = (c.get("Names") or ["?"])[0].lstrip("/")
        image = c.get("Image", "?")
        state = c.get("State", "?")
        status = c.get("Status", "")
        ports = c.get("Ports", [])
        port_strs = []
        for p in ports:
            if p.get("PublicPort"):
                port_strs.append(f"{p['PublicPort']}->{p['PrivatePort']}/{p.get('Type','tcp')}")
        port_info = ", ".join(port_strs) if port_strs else "no ports"
        lines.append(f"  {name}")
        lines.append(f"    image: {image}")
        lines.append(f"    state: {state} | {status}")
        lines.append(f"    ports: {port_info}")
    return "\n".join(lines)


@mcp.tool()
async def get_container_logs(container_name: str, host: str = "local", lines: int = 50) -> str:
    """
    Fetch recent logs from a container.

    Args:
        container_name: Container name (without leading slash).
        host: Environment name. Defaults to first environment.
        lines: Number of log lines to return. Defaults to 50.
    """
    try:
        env_id, env_name = await _resolve_env_id(host)
    except ValueError as e:
        return str(e)
    params = {"tail": str(lines), "stdout": "1", "stderr": "1", "timestamps": "1"}
    try:
        async with _client() as c:
            r = await c.get(
                f"/api/endpoints/{env_id}/docker/containers/{container_name}/logs",
                params=params,
            )
            r.raise_for_status()
            raw = r.content
            log_lines = []
            i = 0
            while i < len(raw):
                if i + 8 > len(raw):
                    break
                frame_size = int.from_bytes(raw[i + 4: i + 8], "big")
                payload = raw[i + 8: i + 8 + frame_size]
                log_lines.append(payload.decode("utf-8", errors="replace"))
                i += 8 + frame_size
            text = "".join(log_lines).rstrip()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Container '{container_name}' not found on {env_name}."
        return f"Error fetching logs: {e}"
    except Exception as e:
        return f"Error fetching logs: {e}"
    if not text:
        return f"No logs available for '{container_name}' on {env_name}."
    return f"=== Logs: {container_name} on {env_name} (last {lines} lines) ===\n{text}"


@mcp.tool()
async def get_container_stats(container_name: str, host: str = "local") -> str:
    """
    Get CPU, memory, and network stats for a running container.

    Args:
        container_name: Container name.
        host: Environment name. Defaults to first environment.
    """
    try:
        env_id, env_name = await _resolve_env_id(host)
    except ValueError as e:
        return str(e)
    try:
        async with _client() as c:
            r = await c.get(
                f"/api/endpoints/{env_id}/docker/containers/{container_name}/stats",
                params={"stream": "false"},
            )
            r.raise_for_status()
            s = r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Container '{container_name}' not found on {env_name}."
        return f"Error fetching stats: {e}"
    except Exception as e:
        return f"Error fetching stats: {e}"
    cpu_delta = (
        s.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        - s.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
    )
    system_delta = s.get("cpu_stats", {}).get("system_cpu_usage", 0) - s.get(
        "precpu_stats", {}
    ).get("system_cpu_usage", 0)
    num_cpus = len(s.get("cpu_stats", {}).get("cpu_usage", {}).get("percpu_usage") or [1])
    cpu_pct = round(cpu_delta / system_delta * num_cpus * 100, 2) if system_delta > 0 else 0.0
    mem = s.get("memory_stats", {})
    mem_used = mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)
    mem_limit = mem.get("limit", 0)
    mem_pct = round(mem_used / mem_limit * 100, 2) if mem_limit > 0 else 0.0
    nets = s.get("networks", {})
    rx_total = sum(v.get("rx_bytes", 0) for v in nets.values())
    tx_total = sum(v.get("tx_bytes", 0) for v in nets.values())
    lines = [
        f"=== Stats: {container_name} on {env_name} ===",
        f"CPU:    {cpu_pct}%",
        f"Memory: {_fmt_bytes(mem_used)} / {_fmt_bytes(mem_limit)} ({mem_pct}%)",
        f"Net RX: {_fmt_bytes(rx_total)}",
        f"Net TX: {_fmt_bytes(tx_total)}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def list_images(host: str = "local") -> str:
    """
    List Docker images on a host.

    Args:
        host: Environment name. Defaults to first environment.
    """
    try:
        env_id, env_name = await _resolve_env_id(host)
    except ValueError as e:
        return str(e)
    try:
        async with _client() as c:
            r = await c.get(f"/api/endpoints/{env_id}/docker/images/json")
            r.raise_for_status()
            images = r.json()
    except Exception as e:
        return f"Error fetching images from {env_name}: {e}"
    if not images:
        return f"No images found on {env_name}."
    images.sort(key=lambda x: x.get("Created", 0), reverse=True)
    lines = [f"Images on {env_name} ({len(images)} total):"]
    for img in images:
        tags = img.get("RepoTags") or ["<none>:<none>"]
        size = _fmt_bytes(img.get("Size", 0))
        created = _fmt_age(img.get("Created", 0))
        short_id = (img.get("Id", "")[:19]).replace("sha256:", "")
        for tag in tags:
            lines.append(f"  {tag}")
            lines.append(f"    id: {short_id} | size: {size} | created: {created}")
    return "\n".join(lines)


@mcp.tool()
async def get_container_detail(container_name: str, host: str = "local") -> str:
    """
    Get full inspect data for a container — mounts, network, environment (sensitive vars redacted).

    Args:
        container_name: Container name.
        host: Environment name. Defaults to first environment.
    """
    try:
        env_id, env_name = await _resolve_env_id(host)
    except ValueError as e:
        return str(e)
    try:
        async with _client() as c:
            r = await c.get(f"/api/endpoints/{env_id}/docker/containers/{container_name}/json")
            r.raise_for_status()
            d = r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Container '{container_name}' not found on {env_name}."
        return f"Error fetching container detail: {e}"
    except Exception as e:
        return f"Error fetching container detail: {e}"
    cfg = d.get("Config", {})
    hcfg = d.get("HostConfig", {})
    state = d.get("State", {})
    net_cfg = d.get("NetworkSettings", {})
    lines = [f"=== Container Detail: {container_name} on {env_name} ==="]
    lines.append(f"ID:      {d.get('Id', '?')[:12]}")
    lines.append(f"Image:   {cfg.get('Image', '?')}")
    lines.append(f"Created: {d.get('Created', '?')}")
    lines.append(f"\nState:   {state.get('Status', '?')}")
    if state.get("StartedAt"):
        lines.append(f"Started: {state['StartedAt']}")
    rp = hcfg.get("RestartPolicy", {})
    if rp:
        max_retry = rp.get("MaximumRetryCount", 0)
        lines.append(f"Restart: {rp.get('Name', 'none')}" + (f" (max {max_retry})" if max_retry else ""))
    env_vars = cfg.get("Env") or []
    if env_vars:
        lines.append("\nEnvironment:")
        for var in env_vars:
            if "=" in var:
                k, _, v = var.partition("=")
                lines.append(f"  {k}=<redacted>" if _is_sensitive(k) else f"  {k}={v}")
            else:
                lines.append(f"  {var}")
    mounts = d.get("Mounts") or []
    if mounts:
        lines.append("\nMounts:")
        for m in mounts:
            src = m.get("Source", "?")
            dst = m.get("Destination", "?")
            mode = m.get("Mode") or ("rw" if m.get("RW") else "ro")
            lines.append(f"  {src} -> {dst} ({mode})")
    ports = net_cfg.get("Ports", {})
    exposed = [(k, v) for k, v in ports.items() if v]
    if exposed:
        lines.append("\nPorts:")
        for container_port, bindings in exposed:
            for b in bindings:
                lines.append(f"  {b.get('HostIp', '0.0.0.0')}:{b['HostPort']} -> {container_port}")
    nets = net_cfg.get("Networks", {})
    if nets:
        lines.append("\nNetworks:")
        for net_name, net_info in nets.items():
            ip = net_info.get("IPAddress", "")
            lines.append(f"  {net_name}" + (f" ({ip})" if ip else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    import uvicorn
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
