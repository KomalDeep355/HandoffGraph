"""Expose the graph tools over MCP so any MCP client (Claude Desktop, Cursor...) can use them.
   Run: python -m busfactor.mcp_server"""
from mcp.server.fastmcp import FastMCP
from .graph_tools import GraphTools
from .actions import plan_handoff

mcp = FastMCP("busfactor-graph")
_t = None
def T():
    global _t
    _t = _t or GraphTools()
    return _t

@mcp.tool()
def module_risk() -> dict:
    """Rank modules by key-person risk."""
    return T().module_risk()["data"]

@mcp.tool()
def leave_impact(person: str) -> dict:
    """Files/modules orphaned if this contributor leaves."""
    return T().leave_impact(person)["data"]

@mcp.tool()
def find_successors(person: str, module: str) -> dict:
    """Best active contributors to inherit a module."""
    return T().find_successors(person, module)["data"]

@mcp.tool()
def explain_path(person: str, module: str) -> dict:
    """Shortest graph path from a person to a module."""
    return T().explain_path(person, module)["data"]

@mcp.tool()
def key_people() -> dict:
    """PageRank of the collaboration graph."""
    return T().key_people()["data"]

@mcp.tool()
def make_handoff_plan(person: str) -> dict:
    """Write a hand-off plan (markdown) for a person who may leave."""
    return plan_handoff(T(), person)["data"]

if __name__ == "__main__":
    mcp.run()
