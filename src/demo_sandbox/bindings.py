"""Additional public entry, registered only in the isolated sandbox database."""

from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

BINDINGS = [
    RouteBinding("POST", "/demo/session", Audience.PUBLIC),
    RouteBinding("GET", "/demo/ai-preview", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
]
