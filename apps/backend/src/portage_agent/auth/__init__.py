"""Auth (Phase 7, rev-C): GitHub OAuth as the sole identity provider.

Browser sessions: 15-min access JWT (kept in frontend memory) + 14-day rotating refresh
token (httpOnly cookie). Rotation-on-use with family reuse-detection — replaying an
already-rotated token revokes the whole chain. Machines (CLI/MCP/CI) use API keys
(hashed at rest, revocable). `AUTH_MODE=disabled` (local default) short-circuits
everything to a synthetic local admin so dev flows and DoD scripts stay frictionless.
"""

from .deps import current_user, require_admin
from .routes import router as auth_router

__all__ = ["auth_router", "current_user", "require_admin"]
