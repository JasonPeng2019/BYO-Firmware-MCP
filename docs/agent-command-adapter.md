# Configurable agent command adapter

The BYO MCP server is already client-neutral: any MCP client that can launch a
stdio server can use the command shown in the README. No provider or model is
selected by the server.

The optional R11 benchmark launcher historically invoked only Codex. It now
also accepts `--agent-config <absolute-json-path>`. This is an operator-owned
command adapter, not a universal vendor API. MCP standardizes JSON-RPC between
client and server; it does not standardize an agent CLI's headless flags,
prompt delivery, MCP-registration format, permission flags, model selection,
or structured output. Therefore any CLI is usable directly—or through a thin
wrapper—when it can satisfy this contract:

1. It is launchable as an executable plus argv without a shell string.
2. It reads the supplied `{prompt_path}` and knows `{result_path}`.
3. It either consumes/translates `{mcp_manifest_path}` or is already configured
   for the BYO stdio server.
4. It writes the existing benchmark result JSON object before exiting.

## Configuration

```json
{
  "schema_version": 1,
  "name": "my-agent-wrapper",
  "command": [
    "my-agent-wrapper",
    "--workspace", "{workspace}",
    "--prompt-file", "{prompt_path}",
    "--result-file", "{result_path}",
    "--mcp-launch-manifest", "{mcp_manifest_path}"
  ],
  "version_command": ["my-agent-wrapper", "--version"],
  "registration_check": null,
  "mcp_mode": "launch_manifest",
  "result_transport": "file",
  "permission_profile": "operator-approved-full-access",
  "model": "operator-selected-model",
  "effort": null,
  "inherit_env": ["PATH", "MY_AGENT_API_TOKEN"],
  "env": {"MY_AGENT_NONSECRET_MODE": "benchmark"}
}
```

Allowed placeholders are `{workspace}`, `{prompt_path}`, `{result_path}`,
`{result_schema_path}`, `{mcp_manifest_path}`, and `{repo_root}`. For
`mcp_mode: "launch_manifest"`, the manifest describes the BYO server command,
stdio transport, cwd, and nonsecret environment. It is deliberately neutral;
the CLI or wrapper translates it into that client's own configuration. Use
`mcp_mode: "preconfigured"` when the client registration already exists; in
that mode the command need not mention `{mcp_manifest_path}`.

`result_transport: "stdout_json"` writes captured stdout to `{result_path}`;
otherwise the agent or wrapper must write the file. The benchmark's existing
strict parser and canonical MCP-session reconciliation still apply.
`version_command` and `registration_check` are optional; omit either one when
the CLI has no corresponding command.

The same placeholders may appear in nonsecret `env` values. This supports
run-scoped provider state such as an isolated configuration directory under
`{workspace}` without mutating a user's global agent registration. If a
provider exits unsuccessfully after producing stdout, stderr, or a result
file, the adapter preserves that completed-run evidence in its structured
error rather than discarding the diagnostics.

## Trust and evidence

Choosing an executable is equivalent to choosing trusted operator code. The
config is loaded only from the explicit command-line path before the agent
starts; it is never discovered from the agent-editable workspace. Commands are
argv arrays executed with `shell=False` semantics and finite timeouts.

Literal environment values with secret-like names are rejected. Authentication
variables should be named under `inherit_env`; values are never copied into run
metadata. Each run records the adapter name, resolved executable, sanitized
argv template, config digest, declared model/effort, permission-profile label,
bounded CLI version output, timestamps, exit code, prompt, stdout/stderr, and
neutral MCP manifest. Model and permission fields are operator declarations;
the generic harness cannot cryptographically verify vendor-specific behavior.

Run one case with:

```text
uv run --locked python -m tests.harness.r11_benchmark \
  --case-id <case-id> \
  --agent-config C:\absolute\path\agent.json \
  --agent-timeout-seconds 180
```

With no `--agent-config`, the backward-compatible registered Codex adapter is
still used. No adapter is installed, registered, or model-pinned automatically.

Provider permission behavior remains provider-specific. For example, an
operator may use an isolated Claude configuration, checkout-scoped MCP config,
and an exact bounded MCP-tool allowlist when that provider's auto-permission
mode is unavailable. Such settings belong in the explicit run configuration;
the product server does not infer or write them.
