"""Guard tests for the node adapters.

Every assertion here corresponds to a boundary an autonomous model is expected to push
against: escaping the workspace, opening an executable, launching an unlisted program,
running an unapproved interpreter, or browsing to the instance metadata endpoint. They
exist so those guards cannot be removed silently.
"""

from __future__ import annotations

import json

import pytest

from agent_node.adapters import AutonomousAdapter
from agent_node.adapters.browser import BrowserController
from agent_node.adapters.code_runner import CodeRunner
from agent_node.adapters.desktop import DesktopController
from agent_node.adapters.office import ExcelController


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path / "ws")


@pytest.fixture()
def agent(workspace):
    return AutonomousAdapter(
        workspace_root=workspace,
        foundry_client=object(),
        allow_desktop=True,
        excel=ExcelController(),
        desktop=DesktopController(workspace_root=workspace),
        code_runner=CodeRunner(workspace_root=workspace, timeout_seconds=30),
    )


async def call(agent: AutonomousAdapter, tool: str, **arguments):
    return await agent._run_tool(tool, json.dumps(arguments))


# --------------------------------------------------------------- filesystem
@pytest.mark.parametrize(
    "path",
    ["../escape.txt", "../../escape.txt", "sub/../../escape.txt", "C:/Windows/evil.txt", "\\\\host\\share\\x.txt"],
)
async def test_write_file_rejects_paths_outside_the_workspace(agent, path):
    result = await call(agent, "write_file", path=path, content="x")
    assert result["ok"] is False
    assert "sandbox" in result["error"] or "absolute" in result["error"]


async def test_write_file_accepts_a_relative_path_and_creates_parents(agent):
    result = await call(agent, "write_file", path="notes/deep/a.txt", content="hello")
    assert result["ok"] is True
    assert result["bytes_written"] == 5


async def test_write_file_refuses_to_fake_a_spreadsheet(agent):
    result = await call(agent, "write_file", path="report.xlsx", content="Region,Q1")
    assert result["ok"] is False
    assert "excel_write" in result["error"]


async def test_read_file_reports_a_missing_file_without_raising(agent):
    result = await call(agent, "read_file", path="nope.txt")
    assert result["ok"] is False


async def test_unknown_tool_is_reported_not_raised(agent):
    result = await call(agent, "definitely_not_a_tool")
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


# ------------------------------------------------------------------ desktop
async def test_open_file_refuses_executable_extensions(agent):
    await call(agent, "write_file", path="payload.exe", content="x")
    result = await call(agent, "open_file", path="payload.exe")
    assert result["ok"] is False
    assert ".exe" in result["error"]


async def test_open_file_refuses_script_extensions(agent):
    await call(agent, "write_file", path="s.ps1", content="x")
    result = await call(agent, "open_file", path="s.ps1")
    assert result["ok"] is False


async def test_desktop_launch_enforces_the_application_allow_list(agent):
    result = await call(agent, "desktop_launch", app="powershell")
    assert result["ok"] is False
    assert "allow-list" in result["error"]


async def test_desktop_open_with_enforces_the_application_allow_list(agent):
    await call(agent, "write_file", path="a.txt", content="x")
    result = await call(agent, "desktop_open_with", app="cmd", path="a.txt")
    assert result["ok"] is False
    assert "allow-list" in result["error"]


async def test_desktop_open_with_requires_an_existing_file(agent):
    result = await call(agent, "desktop_open_with", app="notepad", path="missing.txt")
    assert result["ok"] is False


async def test_desktop_hotkey_rejects_keys_outside_the_allow_list(agent):
    result = await call(agent, "desktop_hotkey", keys=["ctrl", "printscreen"])
    assert result["ok"] is False
    assert "printscreen" in result["error"]


# ------------------------------------------------------------ code execution
async def test_run_code_enforces_the_runtime_allow_list(agent):
    await call(agent, "write_file", path="code/x.py", content="print(1)")
    result = await call(agent, "run_code", runtime="bash", path="code/x.py")
    assert result["ok"] is False
    assert "allowed runtime" in result["error"]


async def test_run_code_rejects_a_program_outside_the_workspace(agent):
    result = await call(agent, "run_code", runtime="python", path="../../evil.py")
    assert result["ok"] is False
    assert "sandbox" in result["error"]


async def test_run_code_returns_a_failing_exit_code_as_an_observation(agent):
    await call(agent, "write_file", path="code/bad.py", content="raise SystemExit(3)")
    result = await call(agent, "run_code", runtime="python", path="code/bad.py")
    assert result["ok"] is False
    assert result["exit_code"] == 3


async def test_run_code_reports_stderr_so_the_model_can_repair_itself(agent):
    await call(agent, "write_file", path="code/boom.py", content="print(1 / 0)")
    result = await call(agent, "run_code", runtime="python", path="code/boom.py")
    assert "ZeroDivisionError" in result["stderr"]


# ------------------------------------------------------------------- excel
async def test_excel_round_trip_preserves_values_and_formulas(agent):
    written = await call(
        agent,
        "excel_write",
        path="reports/sales.xlsx",
        rows=[["Region", "Q1"], ["North", 100], ["Total", "=SUM(B2:B2)"]],
    )
    assert written["ok"] is True

    read = await call(agent, "excel_read", path="reports/sales.xlsx")
    assert read["rows"][0] == ["Region", "Q1"]
    assert read["rows"][2][1] == "=SUM(B2:B2)"


async def test_excel_read_does_not_pad_with_blank_rows(agent):
    await call(agent, "excel_write", path="r.xlsx", rows=[["a"], ["b"]])
    read = await call(agent, "excel_read", path="r.xlsx")
    assert len(read["rows"]) == 2


async def test_excel_write_rejects_a_flat_row_list(agent):
    result = await call(agent, "excel_write", path="r.xlsx", rows=["Region", "Q1"])
    assert result["ok"] is False


# ------------------------------------------------------------------ browser
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api",
        "http://localhost:8000/api",
        "http://169.254.169.254/metadata/identity/oauth2/token",
        "file:///C:/Windows/win.ini",
    ],
)
async def test_browser_blocks_local_and_metadata_targets(workspace, url):
    agent = AutonomousAdapter(
        workspace_root=workspace,
        foundry_client=object(),
        browser=BrowserController(workspace_root=workspace),
    )
    result = await call(agent, "browser_open", url=url)
    assert result["ok"] is False


async def test_browser_enforces_the_domain_allow_list(workspace):
    agent = AutonomousAdapter(
        workspace_root=workspace,
        foundry_client=object(),
        browser=BrowserController(workspace_root=workspace, allowed_domains=["bing.com"]),
    )
    result = await call(agent, "browser_open", url="https://example.com")
    assert result["ok"] is False
    assert "allow-list" in result["error"]


# ------------------------------------------------------- advertised surface
def test_tools_are_only_offered_when_their_backend_is_configured(workspace):
    bare = AutonomousAdapter(workspace_root=workspace, foundry_client=object())
    names = {tool["function"]["name"] for tool in bare._tool_schema()}
    assert names == {"write_file", "read_file", "list_files"}
    assert "desktop.control" not in bare.capabilities()


def test_capabilities_reflect_the_enabled_backends(agent):
    capabilities = set(agent.capabilities())
    assert {"autonomy", "desktop.control", "spreadsheets", "code.execution"} <= capabilities


def test_the_adapter_only_claims_the_agent_namespace(agent):
    assert agent.handles("agent.do") is True
    assert agent.handles("fs.write") is False
