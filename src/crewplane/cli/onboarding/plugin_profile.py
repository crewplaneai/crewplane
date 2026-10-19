"""Explicit recipe for the first official optional CLI provider plugin."""

from crewplane.architecture.contracts import JsonObject


def ollama_provider_options() -> JsonObject:
    return {
        "providers": {
            "ollama": {
                "implementation": "crewplane_ollama:OLLAMA",
                "distribution": "crewplane-ollama",
            }
        }
    }
