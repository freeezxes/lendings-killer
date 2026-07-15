# The `services` package supersedes the old top-level `services.py` module.
# Its business logic now lives in `services/_impl.py`; re-export it here so
# existing `import services; services.X` call sites keep working, and expose
# the `ai_service` submodule alongside it.
from services._impl import *  # noqa: F401,F403
from services import ai_service  # noqa: F401
