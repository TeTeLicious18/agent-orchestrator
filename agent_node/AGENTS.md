# Agent node runtime

Installed on every machine that joins the fleet. Read [../AGENTS.md](../AGENTS.md) first for
repository-wide conventions.

## Modules

| File | Responsibility |
| --- | --- |
| `runner.py` | Entry point: loads config, builds adapters, registers, long-polls, executes |
| `client.py` | Signed HTTP client. Owns the HMAC canonicalisation on the node side |
| `foundry.py` | Model inference with `DefaultAzureCredential`. Falls back to simulation |
| `adapters/base.py` | The `Adapter` protocol and `AdapterError` |
| `adapters/builtin.py` | Sandboxed files, guarded HTTP, allow-listed shell, direct chat |
| `adapters/framework.py` | Bridge to an external runtime over `POST {endpoint}/run` |
| `adapters/scout.py` · `clawdbot.py` | Namespace and capability declarations over that bridge |
| `adapters/autonomous.py` | Goal-driven loop; owns the tool schema and the sandbox helper |
| `adapters/browser.py` | Playwright against a visible Edge window |
| `adapters/desktop.py` | Windows keyboard, windows, applications, screenshots |
| `adapters/office.py` | `.xlsx` through openpyxl |
| `adapters/code_runner.py` | Executes a program under an allow-listed runtime |

## The adapter contract

Three methods, nothing else assumed:

```python
def handles(self, action: str) -> bool: ...
def capabilities(self) -> list[str]: ...
async def execute(self, task: dict, emit: Emit) -> dict: ...
```

`capabilities()` is what the orchestrator routes on, so it must reflect what is actually
configured. Advertising `desktop.control` on a node without PyAutoGUI installed means work
gets routed there and then fails.

`emit(percent, message, telemetry)` streams progress upstream. Call it around anything slow;
it is the only visibility an operator has mid-task.

## Optional dependencies

Each capability sits behind its own requirements file and its own config flag. An adapter
whose library is missing must raise `AdapterError` with the install command, never crash the
runner at import time — that is why the imports are inside the methods.

| Extra | Enables | Config |
| --- | --- | --- |
| `requirements-foundry.txt` | model access, catalog reads | `foundry.enabled` |
| `requirements-browser.txt` | Edge automation | `autonomous.browser.enabled` |
| `requirements-desktop.txt` | keyboard and windows | `autonomous.desktop_control.enabled` |
| `requirements-office.txt` | spreadsheets | `autonomous.excel.enabled` |

Without `requirements-foundry.txt` the node silently runs in simulation mode. Results come
back with `"mode": "simulated"` — check that field before concluding a model answered.

## Tool guards

Everything the autonomous adapter exposes is constrained in code, not by prompting:

* `_resolve()` is the single sandbox entry point. Every path argument goes through it.
* Applications, shell commands, runtimes and key combinations come from allow-lists that map
  a name to a fixed argv. The model never supplies command text, and `shell=False` always.
* `open_file` checks an extension allow-list before handing anything to a file association.
* Browser and HTTP navigation reject loopback, link-local and metadata addresses.

`tests/test_adapters.py` asserts each of these. Adding a tool means adding its guard test in
the same change — the suite is the only thing preventing a quiet regression here.

## Adding a capability

1. Controller class in its own module, with an `AdapterError` if the library is missing.
2. Constructor parameter on `AutonomousAdapter`, defaulting to `None`.
3. Entry in `_tool_schema()`, gated on that parameter being set.
4. Branch in the dispatch method, resolving any path through `self._resolve`.
5. Capability string in `capabilities()`.
6. Config wiring in `runner.py`, gated on an `enabled` flag that defaults to off.
7. Guard test.

## Environment traps

* `python` on Windows often resolves to the Microsoft Store stub. Use the venv interpreter
  by full path.
* Desktop automation needs an interactive session. As a Windows service the node runs in
  session 0 and windows open invisibly.
* `DefaultAzureCredential` reaches IMDS at `169.254.169.254`; a proxy that intercepts it
  breaks managed identity. Set `NO_PROXY` accordingly.
