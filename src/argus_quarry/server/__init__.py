"""Read-only FastAPI server package for argus-quarry (DESIGN.md section 9)."""

from argus_quarry.server.app import create_app

__all__ = ["create_app"]
