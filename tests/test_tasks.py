"""Task board: CRUD, priorities, dependencies, sub-tasks, skills, templates."""

import team_coordinator as tc


def test_add_task_basic(team):
    out = tc.add_task("Build API", assignee="Backend", created_by="PM", priority="high")
    assert "Task #1" in out
    task = tc._load()["tasks"][0]
    assert task["priority"] == "high"
    assert task["assignee"] == "Backend"
    assert task["status"] == "todo"


def test_add_task_invalid_priority_defaults_to_medium(team):
    tc.add_task("Something", created_by="PM", priority="URGENT!!")
    assert tc._load()["tasks"][0]["priority"] == "medium"


def test_task_ids_increment(team):
    tc.add_task("first", created_by="PM")
    tc.add_task("second", created_by="PM")
    ids = [t["id"] for t in tc._load()["tasks"]]
    assert ids == [1, 2]


def test_update_task_not_found(team):
    assert "No task #99" in tc.update_task(99, status="done")


def test_dependency_blocks_start(team):
    tc.add_task("schema", created_by="PM")
    tc.add_task("api", created_by="PM", depends_on="1")
    out = tc.update_task(2, status="in_progress", by_role="Backend")
    assert "BLOCKED" in out
    assert tc._load()["tasks"][1]["status"] == "todo"


def test_dependency_unblocks_after_done(team):
    tc.add_task("schema", assignee="Backend", created_by="PM")
    tc.add_task("api", assignee="Backend", created_by="PM", depends_on="1")
    tc.update_task(1, status="done", by_role="Backend")
    # Completion should announce the dependent task is unblocked.
    assert any("UNBLOCKED" in m["text"] for m in tc._load()["messages"])
    out = tc.update_task(2, status="in_progress", by_role="Backend")
    assert "status=in_progress" in out


def test_done_flips_assignee_to_idle(team):
    tc.add_task("Build API", assignee="Backend", created_by="PM")
    tc.update_task(1, status="done", by_role="Backend")
    assert tc._load()["agents"]["Backend"]["status"] == "idle"


def test_subtask_links_to_parent(team):
    tc.add_task("Epic", created_by="PM")
    tc.add_task("Step 1", created_by="PM", parent_id=1)
    assert tc._load()["tasks"][1]["parent_id"] == 1


def test_view_board_shows_tasks(team):
    tc.add_task("Build API", assignee="Backend", created_by="PM", priority="high")
    out = tc.view_board()
    assert "Build API" in out


def test_set_skills_and_auto_assign(team):
    tc.set_skills("Backend", "api database python")
    tc.set_status("Backend", "idle")
    tc.set_status("QA", "idle")
    tc.add_task("build the api endpoints", created_by="PM")
    out = tc.auto_assign(1, by_role="PM")
    assert "auto-assigned to @Backend" in out
    state = tc._load()
    assert state["tasks"][0]["assignee"] == "Backend"
    assert state["agents"]["Backend"]["status"] == "busy"


def test_auto_assign_no_agents():
    tc.add_task("lonely task", created_by="PM")
    assert "No available agents" in tc.auto_assign(1)


def test_template_save_list_run(team):
    out = tc.save_template(
        "release",
        "Write code|backend|high; Test it|qa|medium; Announce|pm|low",
        by_role="PM")
    assert "3 steps" in out
    assert "release" in tc.list_templates()

    tc.set_skills("Backend", "backend")
    tc.set_skills("QA", "qa")
    run_out = tc.run_template("release", by_role="PM")
    assert "3 tasks" in run_out
    tasks = tc._load()["tasks"]
    assert len(tasks) == 3
    # Each step depends on the previous one.
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == [tasks[0]["id"]]
    assert tasks[2]["depends_on"] == [tasks[1]["id"]]
    # Skill matching assigned the first two steps.
    assert tasks[0]["assignee"] == "Backend"
    assert tasks[1]["assignee"] == "QA"


def test_run_template_unknown(team):
    assert "No template named" in tc.run_template("nope")


def test_save_template_rejects_empty():
    assert "No valid steps" in tc.save_template("empty", " ; ; ")


def test_git_link_attaches_branch(team):
    tc.add_task("Build API", assignee="Backend", created_by="PM")
    out = tc.git_link(1, branch="feature/api", by_role="Backend")
    assert "feature/api" in out
    task = tc._load()["tasks"][0]
    assert task.get("git", {}).get("branch") == "feature/api" or "git" in str(task)


def test_recover_tasks_releases_offline_agents_work(team):
    tc.add_task("Build API", assignee="Backend", created_by="PM")
    tc.update_task(1, status="in_progress", by_role="Backend")
    tc.set_status("Backend", "offline")
    out = tc.recover_tasks(by_role="PM")
    assert "Recovered 1 task" in out
    assert tc._load()["tasks"][0]["status"] == "todo"


def test_recover_tasks_nothing_to_do(team):
    assert "Nothing to recover" in tc.recover_tasks()
