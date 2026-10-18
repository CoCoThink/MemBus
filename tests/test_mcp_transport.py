from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import Implementation

from membus import MemBus
from membus.adapters.mcp_server import DESCRIPTIONS, INSTRUCTIONS

ROOT = Path(__file__).resolve().parents[1]
COMMAND = [sys.executable, "-m", "membus.adapters.mcp_server"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if not key.startswith("MEMBUS_")}


@pytest.fixture(params=["cli", "environment", "cli_overrides_environment"])
def http_server(request, tmp_path: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    db = tmp_path / "memory.db"
    env = _environment()
    args = []
    path = "/mcp" if request.param == "cli" else "/memory/mcp"
    if request.param == "environment":
        env.update(MEMBUS_TRANSPORT="streamable-http", MEMBUS_DB=str(db),
                   MEMBUS_HOST="127.0.0.1", MEMBUS_PORT=str(port), MEMBUS_HTTP_PATH=path)
    else:
        args = ["--transport", "streamable-http", "--db", str(db), "--port", str(port)]
        if request.param == "cli_overrides_environment":
            env.update(MEMBUS_TRANSPORT="stdio", MEMBUS_DB=str(tmp_path / "ignored.db"),
                       MEMBUS_HOST="invalid.invalid", MEMBUS_PORT="invalid", MEMBUS_HTTP_PATH="invalid")
            args.extend(["--host", "127.0.0.1", "--http-path", path])

    log = tmp_path / "http-server.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(COMMAND + args, cwd=ROOT, env=env,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"MCP HTTP server exited: {log.read_text(encoding='utf-8')}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail(f"MCP HTTP server did not start: {log.read_text(encoding='utf-8')}")
            yield f"http://127.0.0.1:{port}{path}", db
            assert not (tmp_path / "ignored.db").exists()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.anyio
async def test_http_process_shares_memories_across_clients(http_server) -> None:
    url, db = http_server
    info = Implementation(name="http-writer", version="test")
    async with Client(url, client_info=info, read_timeout_seconds=10) as writer:
        tools = await writer.list_tools()
        assert {tool.name for tool in tools.tools} == set(DESCRIPTIONS)
        assert writer.instructions == INSTRUCTIONS
        created = await writer.call_tool("memory_put", {
            "content": "http-roundtrip-marker 中文记忆", "memory_type": "note",
            "project": "http-smoke",
        })
        assert not created.is_error
        memory = created.structured_content["memory"]
        assert memory["source"] == "http-writer"

        async with Client(url, read_timeout_seconds=10) as reader:
            found = await reader.call_tool("memory_search", {
                "query": "http-roundtrip-marker", "project": "http-smoke", "strict": True,
            })
            assert [item["id"] for item in found.structured_content["results"]] == [memory["id"]]
            updated = await reader.call_tool("memory_update", {
                "memory_id": memory["id"], "expected_revision": memory["revision"],
                "title": "Updated over HTTP",
            })
            assert not updated.is_error

        fetched = await writer.call_tool("memory_get", {"memory_id": memory["id"]})
        assert fetched.structured_content["title"] == "Updated over HTTP"
        assert MemBus(db).get(memory["id"]).title == "Updated over HTTP"


@pytest.mark.anyio
async def test_default_transport_is_stdio(tmp_path: Path) -> None:
    db = tmp_path / "stdio.db"
    env = {**_environment(), "MEMBUS_DB": str(db)}
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:], env=env, cwd=ROOT)
    async with Client(params, read_timeout_seconds=10) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == set(DESCRIPTIONS)
        created = await client.call_tool("memory_put", {
            "content": "stdio process remains available", "memory_type": "note",
        })
        assert not created.is_error
    assert MemBus(db).get(created.structured_content["memory"]["id"]) is not None


@pytest.mark.parametrize(("args", "settings", "message"), [
    ([], {"MEMBUS_TRANSPORT": "invalid"}, "transport must be"),
    ([], {"MEMBUS_PORT": "65536"}, "port must be"),
    (["--http-path", "relative"], {}, "HTTP path must start with /"),
    (["--http-path", "/mcp?query=1"], {}, "HTTP path must start with /"),
])
def test_invalid_startup_config_does_not_create_database(
    tmp_path: Path, args: list[str], settings: dict[str, str], message: str,
) -> None:
    db = tmp_path / "invalid.db"
    env = {**_environment(), "MEMBUS_DB": str(db), **settings}
    result = subprocess.run(COMMAND + args, cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert message in result.stderr
    assert not db.exists()


def test_mcp_help_does_not_create_database(tmp_path: Path) -> None:
    db = tmp_path / "help.db"
    result = subprocess.run(COMMAND + ["--help"], cwd=ROOT,
                            env={**_environment(), "MEMBUS_DB": str(db)},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert "--transport" in result.stdout and "--http-path" in result.stdout
    assert not db.exists()
