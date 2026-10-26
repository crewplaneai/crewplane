# Crewplane Ollama

Run local Ollama models in Crewplane workflows to inspect and edit files, run
checks, and follow explicitly selected filesystem skills. The plugin also
supports text tasks and returns a final answer through Crewplane's normal
artifacts.

Standard Crewplane installs include this plugin and its `ollama-agent`
executable. It requires Python 3.13+ and Crewplane 0.3.8+.

## Get started

Prepare a reachable Ollama server with an installed local model that supports
tools, then select Ollama during onboarding:

```bash
crewplane onboarding
```

Use `providers: [local]` on workflow nodes to select the saved profile.

Model-selected commands run on your host with your user permissions. Repository
content and tool results go to the selected server; workspaces are not sandboxes.

## Documentation

- [Setup guide](docs/setup.md): server preparation, configuration, migration, and WSL.
- [Execution reference](docs/reference.md): command tool, protocol, resource limits, and lifecycle.
