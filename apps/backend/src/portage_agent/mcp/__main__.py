"""Entry point: `python -m portage_agent.mcp` — serve the Portage tools over stdio."""

from .server import mcp

if __name__ == "__main__":
    mcp.run()
