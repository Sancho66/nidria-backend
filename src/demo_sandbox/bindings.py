"""Additional public entry, registered only in the isolated sandbox database."""

from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding

BINDINGS = [RouteBinding("POST", "/demo/session", Audience.PUBLIC)]
