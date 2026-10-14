import importlib.metadata as metadata
import json
import sys

import crewplane
from crewplane.cli import templates
from crewplane.version import SCHEMA_VERSION

print(
    json.dumps(
        {
            "origin": crewplane.__file__,
            "prefix": sys.prefix,
            "version": metadata.version("crewplane"),
            "interpreter": sys.executable,
            "python": sys.version,
            "schema_version": SCHEMA_VERSION,
            "dependencies": {
                d.metadata["Name"]: d.version for d in metadata.distributions()
            },
            "config_resource": templates.CONFIG_TEMPLATE.is_file(),
            "default_workflow_resource": templates.DEFAULT_WORKFLOW_TEMPLATE.is_file(),
            "library_resources": len(templates.discover_workflow_library_templates()),
        }
    )
)
