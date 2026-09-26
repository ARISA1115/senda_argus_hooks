from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("senda-test-mcp")


@mcp.tool()
def get_server_status() -> dict:
    """Return deterministic health data for MCP trace testing."""
    return {"status": "ok", "service": "senda-test-mcp", "version": "1.0"}


@mcp.tool()
def search_cve(cve_id: str) -> dict:
    """Return a small deterministic CVE record without external network access."""
    samples = {
        "CVE-2024-3094": {
            "product": "XZ Utils",
            "summary": "Test record used only to exercise MCP observability.",
            "severity": "critical",
        },
        "CVE-2021-44228": {
            "product": "Apache Log4j",
            "summary": "Test record used only to exercise MCP observability.",
            "severity": "critical",
        },
    }
    return {"cve_id": cve_id, "found": cve_id in samples, "record": samples.get(cve_id)}


@mcp.tool()
def lookup_ip(ip: str) -> dict:
    """Return deterministic enrichment data for a documentation-range IP."""
    return {
        "ip": ip,
        "classification": "documentation" if ip.startswith(("192.0.2.", "198.51.100.", "203.0.113.")) else "unknown",
        "source": "senda-test-mcp",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
