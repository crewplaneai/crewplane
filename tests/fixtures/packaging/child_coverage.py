import importlib.metadata as metadata
import inspect
import json
import sys

from crewplane.runtime.agent.usage_costs import estimate_token_count

lines, start = inspect.getsourcelines(estimate_token_count)
assert estimate_token_count(165) == 42
print(
    json.dumps(
        {
            "source": inspect.getsourcefile(estimate_token_count),
            "line": start
            + next(i for i, line in enumerate(lines) if "return ceil(" in line),
            "prefix": sys.prefix,
            "interpreter": sys.executable,
            "dependencies": {
                name: metadata.version(name) for name in ("crewplane", "coverage")
            },
        }
    )
)
