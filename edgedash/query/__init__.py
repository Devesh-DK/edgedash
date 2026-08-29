"""
edgedash/query — read-only, parameterised query tool registry.

Public surface:
    TOOLS   dict[str, ToolSpec]   the full registry; router reads this
    call(name, **kwargs)          validate, clamp, run, return ToolResult
"""

from edgedash.query.tools import TOOLS, call

__all__ = ["TOOLS", "call"]
