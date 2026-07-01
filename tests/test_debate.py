"""Structured debate: propose -> critique -> revise -> judge, plus votes/scoring."""

import team_coordinator as tc


def _open_debate():
    tc.start_debate("Which auth approach?", options="JWT, sessions",
                    max_rounds=2, judge_role="PM", by_role="PM")


def test_start_debate_sets_phase(team):
    out = tc.start_debate("Which auth?", options="JWT, sessions", by_role="PM")
    assert "PROPOSING" in out
    d = tc._load()["debate"]
    assert d["phase"] == "proposing"
    assert d["options"] == ["JWT", "sessions"]


def test_proposal_requires_active_debate(team):
    assert "No active debate" in tc.submit_proposal("Backend", "JWT", "stateless")


def test_proposal_moves_to_critiquing(team):
    _open_debate()
    out = tc.submit_proposal("Backend", "Use JWT", "stateless and scalable")
    assert "CRITIQUING" in out
    assert tc._load()["debate"]["phase"] == "critiquing"


def test_critique_recorded_with_agreement_flag(team):
    _open_debate()
    tc.submit_proposal("Backend", "Use JWT", "stateless and scalable")
    out = tc.submit_critique("QA", "Backend", False, "revocation is hard with JWT")
    assert "disagrees with" in out
    crit = tc._load()["debate"]["critiques"][0]
    assert crit["agree"] is False
    assert crit["target_role"] == "Backend"


def test_revise_proposal_appends_new_stance(team):
    _open_debate()
    tc.submit_proposal("Backend", "Use JWT", "stateless")
    tc.submit_critique("QA", "Backend", False, "revocation is hard")
    tc.revise_proposal("Backend", "JWT with short expiry + refresh tokens",
                       "QA's revocation point is sound")
    d = tc._load()["debate"]
    assert d["phase"] == "revising"
    assert len(d["proposals"]) == 2
    assert "(revised)" in d["proposals"][-1]["argument"]


def test_next_round_caps_at_max(team):
    _open_debate()  # max_rounds=2
    tc.submit_proposal("Backend", "JWT", "stateless")
    out1 = tc.next_round(by_role="PM")
    assert "round" in out1.lower()
    out2 = tc.next_round(by_role="PM")
    assert "judge_debate" in out2


def test_get_debate_renders_everything(team):
    _open_debate()
    tc.submit_proposal("Backend", "Use JWT", "stateless")
    tc.submit_critique("QA", "Backend", True, "agreed, fits our stack")
    out = tc.get_debate()
    assert "Which auth approach?" in out
    assert "Use JWT" in out
    assert "agrees with" in out


def test_judge_debate_archives_and_creates_task(team):
    _open_debate()
    tc.submit_proposal("Backend", "Use JWT", "stateless")
    out = tc.judge_debate("PM", "Use JWT", "strongest reasoning", create_tasks=True)
    assert "Debate decided" in out
    state = tc._load()
    assert state["debate"] is None
    # Decision is archived as a tagged note.
    assert any("decision" in n.get("tags", []) for n in state["notes"])
    # And a follow-up execution task exists.
    assert any(t["title"].startswith("Execute decision") for t in state["tasks"])


def test_votes_and_tally(team):
    _open_debate()
    tc.submit_proposal("Backend", "Use JWT", "stateless")
    tc.cast_vote("Backend", "JWT", "scales")
    tc.cast_vote("QA", "JWT")
    tc.cast_vote("PM", "sessions")
    out = tc.vote_tally()
    assert "JWT: 2" in out
    assert "sessions: 1" in out


def test_vote_without_debate(team):
    assert "No active debate" in tc.cast_vote("QA", "JWT")


def test_score_debate_ranks_contributors(team):
    _open_debate()
    tc.submit_proposal("Backend", "Use JWT",
                       "stateless, scales horizontally, no server session store needed")
    tc.submit_critique("QA", "Backend", False,
                       "token revocation before expiry is genuinely hard")
    out = tc.score_debate()
    assert "Backend" in out and "QA" in out
    assert "depth" in out


def test_score_debate_without_debate(team):
    assert "No active debate" in tc.score_debate()
