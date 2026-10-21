"""Absolute entry point, usable from arbitrary worktrees without installation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.mcp_server import main  # noqa: E402 - absolute script entrypoint

if __name__ == "__main__":
    main()
