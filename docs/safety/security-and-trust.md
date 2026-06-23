# Security And Trust

Crewplane coordinates provider CLIs. It is not a sandbox.

Provider CLIs run with the permissions, approval settings, environment,
network access, filesystem access, and credentials configured for those provider
tools.

## Provider Permissions

Generated provider profiles may include provider-specific unattended approval
flags. Those flags belong to the provider tools. Review the generated
`.crewplane/config.yml` before running real providers.

Crewplane does not:

- install provider CLIs
- manage provider credentials
- restrict provider network access
- sandbox provider filesystem access
- guarantee that provider-generated content is safe to execute

## Template File Access

`{{file:path}}` template references are bounded to the project root by default.
External files must be explicitly allowlisted:

```yaml
settings:
  integrations:
    artifacts:
      implementation: "filesystem"
      options:
        allowed_template_paths:
          - /absolute/path/to/allowed/context
```

File contents must be UTF-8 text. Symlinks are resolved before access checks.

## Secrets And Fingerprints

Preflight fingerprints sensitive env, var, and config-derived values so the
workflow signature can account for secret changes without writing raw secret
values into public artifacts. When a persisted fingerprint key is unavailable,
dry-run skip/resume advisories may be non-binding.

Provider-emitted output is outside the secret-redaction boundary. If a provider
prints a secret, that output can be captured in logs and artifacts.

## Experimental Workspace Isolation

Experimental workspace isolation is source-tree isolation for ordinary Git
repositories. It does not sandbox provider execution. A provider can still
access anything its own process permissions allow.
