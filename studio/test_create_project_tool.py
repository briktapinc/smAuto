"""create_project MCP tool: schema, unique ids, and load after create."""

from __future__ import annotations

import asyncio
import json
import unittest


class CreateProjectToolTests(unittest.TestCase):
    def test_tool_is_listed_and_does_not_replace_an_existing_project(self):
        from fastmcp.exceptions import ToolError

        from studio.auth import MEMBERSHIP_GATED_MCP_TOOLS
        from studio.mcp_server import MCP_TOOL_NAMES, build_mcp
        from studio.projects import delete_project, list_projects, load_meta, project_payload

        self.assertIn("create_project", MCP_TOOL_NAMES)
        self.assertIn("create_project", MEMBERSHIP_GATED_MCP_TOOLS)

        mcp = build_mcp()
        tools = asyncio.run(mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertIn("create_project", by_name)
        schema = by_name["create_project"].parameters
        props = schema.get("properties") or {}
        self.assertIn("topic", props)
        self.assertIn("duration_seconds", props)
        self.assertIn("title", props)
        self.assertIn("aspect", props)

        before = {item["id"]: item.get("title") for item in list_projects()}
        title = "MCP create project smoke"
        created_ids: list[str] = []
        try:
            created = asyncio.run(
                mcp.call_tool(
                    "create_project",
                    {"topic": title, "duration_seconds": 60, "title": title},
                )
            )
            project_id = _tool_data(created)["id"]
            created_ids.append(project_id)
            self.assertTrue(project_id)
            self.assertNotIn(project_id, before)
            self.assertEqual(load_meta(project_id)["title"], title)
            self.assertEqual(project_payload(project_id)["id"], project_id)

            again = asyncio.run(
                mcp.call_tool(
                    "create_project",
                    {"topic": title, "duration_seconds": 60, "title": title},
                )
            )
            second_id = _tool_data(again)["id"]
            created_ids.append(second_id)
            self.assertNotEqual(second_id, project_id)
            self.assertEqual(load_meta(project_id)["title"], title)
            self.assertEqual(load_meta(second_id)["title"], title)

            with self.assertRaises(ToolError) as caught:
                asyncio.run(mcp.call_tool("create_project", {"topic": "  ", "duration_seconds": 60}))
            self.assertIn("topic is required", str(caught.exception))

            for pid, old_title in before.items():
                self.assertEqual(load_meta(pid).get("title"), old_title)
        finally:
            for pid in created_ids:
                delete_project(pid, delete_files=True)
        if created_ids:
            with self.assertRaises(FileNotFoundError) as missing:
                load_meta(created_ids[0])
            self.assertIn("Unknown project", str(missing.exception))


def _tool_data(result) -> dict:
    if isinstance(result, dict) and "id" in result:
        return result
    structured = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(structured, dict) and structured.get("id"):
        return structured
    text = ""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", "") or ""
        if text:
            break
    data = json.loads(text) if text else {}
    if isinstance(data, dict) and "id" in data:
        return data
    raise AssertionError("create_project did not return a project id")


if __name__ == "__main__":
    unittest.main()
