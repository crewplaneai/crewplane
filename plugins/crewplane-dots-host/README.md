# Crewplane Dots host (experimental)

Run [Crewplane](../../README.md) workflows using native agents from an active
OpenAI Dots session.

`crewplane-dots-host` connects Crewplane to the Dots agent in your session,
called the **host**. Crewplane queues tasks, manages their dependencies and
retries, and saves results. The host uses its terminal and native-agent tools
to pick up each task, run it, and return the output.

## Who is it for?

Use this package if you already work in Dots and want repeatable, multi-step
agent workflows with prompts and results saved on disk. The included
[review-and-summary example](examples/review-and-summary/review.task.md) runs
a read-only code review, then passes the saved review to a summary task.

**The Dots host must stay active throughout the run.** Installing the package
does not start native agents or provide an unattended service. The host performs
the queue operations below using its tools; you do not need to relay tasks or
results manually.

## Try the example

You need a Crewplane checkout, Python 3.13+, `uv`, `make`, and a Dots agent with
terminal access and native-agent tools. See the
[development setup](../../DEVELOPMENT.md#setup) for repository setup details.
Use Linux, macOS, or WSL; native Windows support is pending.

### 1. Install the package

From the repository root:

```bash
make setup
uv pip install -e plugins/crewplane-dots-host
export PATH="$PWD/.venv/bin:$PATH"
```

This optional package must be installed in the same environment as Crewplane.
Ensure the checkout's `.venv/bin` is on `PATH` in each terminal session the host
uses.

### 2. Preview the workflow

```bash
cd plugins/crewplane-dots-host/examples/review-and-summary
crewplane validate -c config.yml review.task.md
crewplane run -c config.yml -t review.task.md --dry-run --no-live
```

Validation should succeed, and the preview should show `review` followed by
`summarize`. These commands do not dispatch native agents or verify the host's
tools. The [example configuration](examples/review-and-summary/config.yml)
requests `gpt-6.1-sol` with `xhigh` reasoning and allows 30 minutes per invocation;
the host must be able to use and confirm those settings before you run it.

### 3. Run with the Dots host

Ask the Dots agent to launch this command from the example directory in a
background or yielding terminal session, then follow the
[host instructions](#host-instructions) while the workflow runs:

```bash
crewplane run -c config.yml -t review.task.md --no-live
```

The host must service the queue concurrently with Crewplane. Running the command
without a host leaves it waiting until it times out.

On success, Crewplane saves the review and summary under
`.crewplane/execution-stages/` and `.crewplane/execution-results/` in the example
directory. The queue is at `.crewplane/dots-host` in that same directory. Use
that directory for the host commands too, so both sides address the same queue.

## Use it in your own workflow

Start from the example's [config](examples/review-and-summary/config.yml) and
[workflow](examples/review-and-summary/review.task.md). Change the task prompts
and dependencies, keeping the `dots_host` provider registration and
`crewplane_dots_host.adapter:DotsHostInvokerAdapter` invoker from the config.
Keep `settings.workspace.enabled: false`.

Select a model with the agent's `default_model` or a workflow `model` override.
Set reasoning with `--reasoning` in the agent's `extra_args`; the workflow-level
`reasoning` field is unsupported. The plugin passes model IDs through unchanged
and requires the host to confirm the requested settings; it has no fallback
model.

## Current limits

- **Managed workspaces are unsupported.** Tasks use the existing checkout. Use
  the example's Dots invoker adapter rather than the generic `cli` adapter.
- **Cancellation needs the host's cooperation.** Cancelling or expiring a job
  prevents its result from being accepted, but cannot stop a native agent owned
  by the host or undo its side effects. The host must monitor jobs, renew active
  leases, and stop its native work when required.
- **Results arrive on completion.** Native output is not streamed, and
  output-idle timeouts are disabled. Use `--timeout` or Crewplane's
  `invocation_timeout_seconds` to bound elapsed time. `--timeout` includes reading
  stdin; if the prompt stream stays open past the deadline, no job is submitted.
- **Use a trusted local filesystem.** Prompts and results are stored on disk.
  Keep the queue private and off network filesystems. The host is responsible
  for permissions, sandboxing, and checking that its tools can perform the task.
- **Native execution needs separate verification.** Local tests cover the
  bridge protocol. An end-to-end run requires an active Dots host; a successful
  dry run does not establish that native dispatch works.

## Host instructions

<details>
<summary>Claim tasks, run native agents, and return results</summary>

These steps are for the active Dots agent. Run these commands from the example
directory while keeping Crewplane running. Prefix every command below with
`crewplane-dots-host --queue .crewplane/dots-host`.

1. Run `list` to inspect requests and job states. For a pending job, check its
   prompt, working directory, requested model and reasoning, and expiration.
   Verify that the work is authorized and your native tools support it. If it
   cannot be performed as requested, use `cancel JOB_ID` and report the reason.
2. Run `claim JOB_ID --lease-seconds 300`, replacing `JOB_ID` with the ID from
   `list`. A successful claim returns a `token`; use it as `TOKEN` below. Only
   dispatch after the claim succeeds.
3. Run the prompt with your actual native-agent tool in the requested working
   directory. While it runs, renew the lease before it expires with
   `renew JOB_ID --token TOKEN --lease-seconds 300`. Run `reconcile` and `list`
   to check for expiration or cancellation, and stop your native agent if either
   occurs. Renewing a lease does not extend the request's deadline.
4. Save the actual result to `result.json` and run
   `complete JOB_ID --token TOKEN --receipt result.json`. Use this receipt shape,
   replacing the sample text and confirming settings from the actual dispatch:

   ```json
   {
     "output": "The native agent's actual result",
     "host_agent_id": "Actual native-agent ID",
     "confirmed": {"model": "gpt-6.1-sol", "reasoning": "xhigh"},
     "evidence": "Actual host tool dispatch/result reference"
   }
   ```

   Never fill `confirmed` merely by copying the request. Use `null` for unknown
   values; completion fails if a requested setting is unconfirmed or differs.
   The receipt records the host's report, not independent model or billing
   verification.
5. Continue servicing jobs until Crewplane exits. Completing `review` allows
   Crewplane to queue `summarize`.

Check every command's exit status. After a failure, inspect job state before
retrying. Repeating an identical successful completion is safe; redispatching
native work with an unresolved outcome is not. Expired jobs cannot be reclaimed.
Stop the original native work before handling any Crewplane retry. If recovery
cannot finish within the request and lease deadlines, stop your native work and
report failure. A failed `cancel` command does not confirm cancellation.

</details>

## Development

From the repository root, `make dots-host-check` installs the optional package
and runs its local protocol tests. It is also included in `make check`. See the
[development guide](../../DEVELOPMENT.md) for the full validation workflow.
