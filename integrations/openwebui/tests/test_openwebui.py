"""
Unit and integration tests for OpenWebUI Integration & Board Artifact (Sub-Agent Eta).
"""

from pathlib import Path
import pytest
import yaml

from integrations.openwebui.functions.ptb_board_action import Action
from integrations.openwebui.tools.ptb_tools import Tools


def test_docker_compose_configuration():
    """Verify docker-compose.yml contains open-webui service, network, and volume."""
    compose_path = Path("/Volumes/WorkSpace/Project/Personal_Task_Board/docker-compose.yml")
    assert compose_path.exists(), "docker-compose.yml must exist"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "neo4j" in services, "neo4j service must be defined"
    assert "open-webui" in services, "open-webui service must be defined"

    owui = services["open-webui"]
    assert owui["image"] == "ghcr.io/open-webui/open-webui:main"
    assert owui["container_name"] == "ptb_openwebui"
    assert "3000:8080" in owui["ports"]
    assert "openwebui_data:/app/backend/data" in owui["volumes"]

    env = owui["environment"]
    assert "WEBUI_AUTH=False" in env
    assert "ENABLE_OLLAMA_API=True" in env

    # Networks
    assert "ptb_network" in compose.get("networks", {})
    assert "ptb_network" in owui.get("networks", [])
    assert "ptb_network" in services["neo4j"].get("networks", [])

    # Volumes
    assert "openwebui_data" in compose.get("volumes", {})


def test_ptb_board_html_structure_and_views():
    """Verify ptb_board.html has all 8 functional views and responsive/dark mode assets."""
    board_path = Path("/Volumes/WorkSpace/Project/Personal_Task_Board/integrations/openwebui/board/ptb_board.html")
    assert board_path.exists(), "ptb_board.html must exist"

    content = board_path.read_text(encoding="utf-8")
    assert len(content) > 5000, "HTML artifact should be complete and non-trivial"

    # 8 Required Views from docs/v1.md
    required_views = [
        "view-today",
        "view-review",
        "view-tasks",
        "view-waiting",
        "view-forgotten",
        "view-decisions",
        "view-lessons",
        "view-health",
    ]
    for view_id in required_views:
        assert f'id="{view_id}"' in content, f"ptb_board.html must include {view_id}"

    # Verify Dark/Light Mode support
    assert "prefers-color-scheme" in content
    assert "html.dark" in content
    assert "toggleTheme" in content

    # Verify REST API & Mock Data
    assert "MOCK_DATA" in content
    assert "apiFetch" in content
    assert "/api/today" in content
    assert "/api/review" in content


@pytest.mark.asyncio
async def test_ptb_board_action_function():
    """Verify OpenWebUI Action function can load board artifact and process shortcuts."""
    action = Action()
    
    # 1. Load HTML
    html = action.load_board_html()
    assert "<!DOCTYPE html>" in html
    assert "Personal Task Board" in html

    # 2. Format Artifact
    artifact = action.format_as_artifact(html)
    assert ':::artifact{type="text/html" title="Personal Task Board"}' in artifact

    # 3. Action button execution
    result = await action.action(body={"messages": []})
    assert result is not None
    assert "Personal Task Board" in result["content"]

    # 4. Prompt shortcut: /board
    emitted = []
    async def mock_emitter(event):
        emitted.append(event)

    body = {"messages": [{"role": "user", "content": "/board"}]}
    await action.inlet(body=body, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    assert ":::artifact" in emitted[0]["data"]["content"]

    # 5. Prompt shortcut: /board review
    emitted.clear()
    body_rev = {"messages": [{"role": "user", "content": "/board review"}]}
    await action.inlet(body=body_rev, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    assert "Hàng đợi duyệt" in emitted[0]["data"]["content"]

    # 6. Prompt shortcut: /board approve
    emitted.clear()
    body_app = {"messages": [{"role": "user", "content": "/board approve cand-101"}]}
    await action.inlet(body=body_app, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    assert "Đã phê duyệt" in emitted[0]["data"]["content"]


def test_ptb_tools():
    """Verify all 11 tool definitions in ptb_tools.py."""
    tools = Tools()

    # 1. Today tasks
    today_res = tools.get_today_tasks()
    assert "Kế hoạch hôm nay" in today_res
    assert "/100]" in today_res

    # 2. Get tasks
    tasks_res = tools.get_tasks()
    assert "Danh sách Task" in tasks_res

    # 3. Review queue
    review_res = tools.get_review_queue()
    assert "Hàng đợi duyệt" in review_res

    # 4. Approve task
    app_res = tools.approve_task("rev-cand-101")
    assert "Đã phê duyệt" in app_res

    # 5. Dismiss task
    dis_res = tools.dismiss_task("rev-cand-101")
    assert "Đã loại bỏ" in dis_res

    # 6. Update task status
    upd_res = tools.update_task_status("task-001", "IN_PROGRESS")
    assert "IN_PROGRESS" in upd_res

    invalid_res = tools.update_task_status("task-001", "INVALID_STATUS")
    assert "không hợp lệ" in invalid_res

    # 7. Waiting items
    wait_res = tools.get_waiting_items()
    assert "Waiting on Others" in wait_res

    # 8. Forgotten commitments
    forg_res = tools.get_forgotten_commitments()
    assert "Forgotten Commitments" in forg_res

    # 9. Search knowledge
    know_res = tools.search_knowledge("architecture")
    assert "Kết quả tra cứu" in know_res

    # 10. Source health
    health_res = tools.get_source_health()
    assert "Tình trạng Đồng bộ" in health_res

    # 11. Render board artifact
    art_res = tools.render_board_artifact()
    assert ":::artifact" in art_res
    assert "<!DOCTYPE html>" in art_res
