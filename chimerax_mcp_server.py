"""
chimerax_mcp_server.py - Lightweight ChimeraX FastMCP Bridge for AI-Forge
========================================================================
Runs a FastMCP server in WSL2 that connects to UCSF ChimeraX's REST API
running on the host (e.g. Windows 11) or locally.

HOW IT WORKS:
1. UCSF ChimeraX has a native REST remote control server built-in.
   In ChimeraX's GUI command bar, run:
       remotecontrol rest start port 21049
2. This bridge runs in WSL2 and exposes standardized MCP tools over SSE or stdio.
3. Commands sent by AI-Forge are relayed to ChimeraX via:
       http://<HOST_IP>:21049/run?command=<cmd>
4. Because any ChimeraX command (open, surface, color, matchmaker, alphafold,
   save, python) can be passed to /run, this bridge gives 100% full coverage
   of all ChimeraX features without tool-schema bloat.

STARTING THIS SERVER:
    pixi run python chimerax_mcp_server.py
    # or with custom ports/hosts:
    pixi run python chimerax_mcp_server.py --port 21050 --cx-port 21049

CONNECTING IN AI-FORGE:
    /mcp on chimerax
"""

import os
import sys
import argparse
import html
import re
from typing import Optional, List

try:
    import httpx
except ImportError:
    httpx = None

try:
    import urllib.request
    import urllib.parse
    import urllib.error
except ImportError:
    pass

from fastmcp import FastMCP

# Try importing host IP resolution from AI-Forge harness
try:
    from mcp_config import resolve_host_ip, HOST_IP
except ImportError:
    def resolve_host_ip() -> str:
        return os.environ.get("HOST_IP", "127.0.0.1")
    HOST_IP = resolve_host_ip()

# Initialize FastMCP Server
mcp = FastMCP(
    name="ChimeraX",
)

# Global connection parameters (populated at runtime or via CLI)
CX_PORT: int = int(os.environ.get("CHIMERAX_PORT", 21049))
CX_HOST_OVERRIDE: Optional[str] = os.environ.get("CHIMERAX_HOST", None)
_cached_active_host: Optional[str] = None


def get_candidate_hosts() -> List[str]:
    """Returns candidate IPs to probe for ChimeraX REST server."""
    if CX_HOST_OVERRIDE:
        return [CX_HOST_OVERRIDE]
    candidates = []
    # 1. In WSL2 mirrored networking mode or native Linux, 127.0.0.1 connects directly
    candidates.append("127.0.0.1")
    # 2. In WSL2 NAT mode, Windows host IP from default gateway / DNS
    if HOST_IP and HOST_IP not in candidates:
        candidates.append(HOST_IP)
    for alias in ["host.wsl.internal", "host.containers.internal", "host.docker.internal"]:
        if alias not in candidates:
            candidates.append(alias)
    return candidates


def _clean_response(content: str) -> str:
    """Cleans up raw HTML tags from ChimeraX response if present."""
    if not content:
        return "Command executed successfully (no output)."
    # If HTML markup is detected, strip tags while preserving linebreaks
    if "<html" in content.lower() or "<body" in content.lower() or "<pre" in content.lower():
        text = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
        text = re.sub(r'<p.*?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text).strip()
        return text if text else "Command executed successfully (no output)."
    return content.strip()


def send_chimerax_command(command: str, timeout: float = 60.0) -> str:
    """
    Sends an arbitrary command string to ChimeraX REST API.
    Probes candidate host addresses dynamically if not yet connected.
    """
    global _cached_active_host
    candidate_hosts = [_cached_active_host] if _cached_active_host else get_candidate_hosts()
    attempted_urls = []
    last_error = None

    for host in candidate_hosts:
        url = f"http://{host}:{CX_PORT}/run"
        attempted_urls.append(url)
        try:
            if httpx:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(url, params={"command": command})
                    if resp.status_code == 200:
                        _cached_active_host = host
                        return _clean_response(resp.text)
                    else:
                        return f"[ChimeraX Error {resp.status_code}]: {_clean_response(resp.text)}"
            else:
                encoded_cmd = urllib.parse.urlencode({"command": command})
                req_url = f"{url}?{encoded_cmd}"
                req = urllib.request.Request(req_url)
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    _cached_active_host = host
                    return _clean_response(raw)

        except Exception as e:
            last_error = str(e)
            if _cached_active_host == host:
                _cached_active_host = None  # Reset cache and try other candidates

    # If cached host failed, try remaining candidate hosts once
    if len(candidate_hosts) == 1 and not CX_HOST_OVERRIDE:
        for host in get_candidate_hosts():
            if host in candidate_hosts:
                continue
            url = f"http://{host}:{CX_PORT}/run"
            attempted_urls.append(url)
            try:
                if httpx:
                    with httpx.Client(timeout=timeout) as client:
                        resp = client.get(url, params={"command": command})
                        if resp.status_code == 200:
                            _cached_active_host = host
                            return _clean_response(resp.text)
                        else:
                            return f"[ChimeraX Error {resp.status_code}]: {_clean_response(resp.text)}"
                else:
                    encoded_cmd = urllib.parse.urlencode({"command": command})
                    req_url = f"{url}?{encoded_cmd}"
                    req = urllib.request.Request(req_url)
                    with urllib.request.urlopen(req, timeout=timeout) as response:
                        raw = response.read().decode("utf-8", errors="replace")
                        _cached_active_host = host
                        return _clean_response(raw)
            except Exception as e:
                last_error = str(e)

    return (
        f"[ChimeraX Connection Error]: Unable to reach ChimeraX REST server on port {CX_PORT}.\n"
        f"Attempted endpoints: {', '.join(attempted_urls)}\n"
        f"Last system error: {last_error}\n\n"
        f"Troubleshooting Checklist:\n"
        f"1. Is UCSF ChimeraX running on the host machine?\n"
        f"2. Have you started the REST server in ChimeraX? In the command bar run:\n"
        f"       remotecontrol rest start port {CX_PORT}\n"
        f"3. In WSL2 NAT mode, Windows binds 127.0.0.1 by default. Either:\n"
        f"   - Enable mirrored networking in %USERPROFILE%/.wslconfig (networkingMode=mirrored), OR\n"
        f"   - Forward port 21049 in Windows PowerShell (Admin):\n"
        f"       netsh interface portproxy add v4tov4 listenport={CX_PORT} listenaddress=0.0.0.0 connectport={CX_PORT} connectaddress=127.0.0.1\n"
        f"   - Or specify the exact host IP via CHIMERAX_HOST=<ip>.\n"
    )


# ==============================================================================
# FAST-MCP TOOLS EXPOSED TO THE OVERSEER BRAIN
# ==============================================================================

@mcp.tool()
def run_command(command: str) -> str:
    """
    Executes an arbitrary UCSF ChimeraX command with full syntax support.
    
    Examples:
    - Open structure: 'open 1a0m' or 'open /path/to/protein.pdb'
    - Visualization: 'surface #1', 'cartoon', 'color #1/A red'
    - Alignment: 'matchmaker #2 to #1'
    - Electrostatics: 'coulombic #1'
    - Camera/View: 'view orient', 'view #1'
    - AlphaFold: 'alphafold predict <sequence>'
    - Render image: 'save snapshot.png width 1920 height 1080 transparent true'
    - Close models: 'close #1'
    """
    return send_chimerax_command(command)


@mcp.tool()
def open_structure(specifier: str) -> str:
    """
    Opens a molecular structure by PDB ID, local file path, or URL.
    
    Examples:
    - PDB ID: '1ubq', '7krr'
    - Local file: '/home/agent/ai_workspace/model.pdb'
    """
    return send_chimerax_command(f"open {specifier}")


@mcp.tool()
def save_image(filepath: str, width: int = 1920, height: int = 1080, transparent: bool = True) -> str:
    """
    Renders and saves the current ChimeraX 3D viewport to an image file.
    
    Arguments:
    - filepath: Destination image path (e.g. 'render.png').
    - width: Width in pixels (default 1920).
    - height: Height in pixels (default 1080).
    - transparent: Whether background is transparent (default True).
    """
    t_flag = "true" if transparent else "false"
    return send_chimerax_command(f"save {filepath} width {width} height {height} transparent {t_flag}")


@mcp.tool()
def close_models(target: str = "all") -> str:
    """
    Closes open molecular models in the active ChimeraX session.
    
    Arguments:
    - target: Model specifier (e.g. '#1', '#1,2') or 'all' to clear the entire session.
    """
    cmd = "close" if target.strip().lower() == "all" else f"close {target}"
    return send_chimerax_command(cmd)


@mcp.tool()
def run_python(code: str) -> str:
    """
    Executes Python code directly inside UCSF ChimeraX's Python runtime.
    Useful for inspecting model coordinates, calculating distances, or custom automation.
    """
    return send_chimerax_command(f"python {code}")


@mcp.tool()
def get_status() -> str:
    """
    Checks connection health to UCSF ChimeraX and reports active status and responsiveness.
    """
    res = send_chimerax_command("view")
    if "[ChimeraX Connection Error]" in res:
        return res
    active_host = _cached_active_host or "host"
    return f"ChimeraX is online and responding at http://{active_host}:{CX_PORT}. (View refreshed successfully)"


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChimeraX FastMCP Bridge for AI-Forge")
    parser.add_argument("--transport", choices=["sse", "stdio"], default="sse", help="Transport mode (default: sse)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address for FastMCP server (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=21050, help="Port for FastMCP SSE server (default: 21050)")
    parser.add_argument("--cx-host", default=None, help="Explicit host address for ChimeraX REST (default: auto-detect)")
    parser.add_argument("--cx-port", type=int, default=21049, help="ChimeraX REST port (default: 21049)")

    args = parser.parse_args()

    CX_PORT = args.cx_port
    if args.cx_host:
        CX_HOST_OVERRIDE = args.cx_host

    if args.transport == "sse":
        print(f"============================================================")
        print(f" ChimeraX FastMCP SSE Bridge Server")
        print(f"============================================================")
        print(f" FastMCP SSE URL : http://{args.host}:{args.port}/sse")
        print(f" ChimeraX Target : http://{CX_HOST_OVERRIDE or 'auto-detect'}:{CX_PORT}/run")
        print(f" Candidate Hosts : {', '.join(get_candidate_hosts())}")
        print(f" Connect in Chat : /mcp on chimerax http://127.0.0.1:{args.port}/sse")
        print(f"============================================================")
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run()
