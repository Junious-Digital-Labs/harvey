"""Research-and-draft mode: email channel off => drafts are exported, never sent."""

import json
import os
import tempfile

import pytest
import pytest_asyncio

from harvey.export import collect_leads, leads_to_json
from harvey.main import decide_next_action
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class _Cfg:
    class channels:
        class email:
            enabled = False
            provider = "instantly"
            max_daily_sends = 50
            send_to_risky = False


class _Env:
    instantly_api_key = ""


async def _campaign(state, pids):
    return await state.add_campaign(Campaign(
        id="", name="hvac-outreach", channel="email",
        sequence=[EmailStep(step=1, subject="hi {{first_name}}", body="re {{company}}", delay_days=0)],
        prospect_ids=pids, status="draft",
    ))


@pytest.mark.asyncio
async def test_sender_marks_drafts_exported_without_outbox(state):
    from harvey.agents.sender import Sender

    pid = await state.add_prospect(Prospect(
        first_name="Ann", last_name="Lee", title="Owner", email="ann@acme.com",
        email_status="verified", company="Acme"))
    cid = await _campaign(state, [pid])
    sender = Sender(brain=None, state=state, config=_Cfg(), env=_Env())
    assert sender.is_native is False

    await sender.run()

    assert (await state.get_campaigns_by_status("draft")) == []
    assert len(await state.get_campaigns_by_status("exported")) == 1
    summary = await state.get_state_summary()
    assert summary["draft_campaigns"] == 0 and summary["exported_campaigns"] == 1
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM outbox") as cur:
            assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_mail_provider_is_none_when_email_disabled():
    from harvey.integrations.mail_provider import get_mail_provider

    class C:
        class channels:
            class email:
                enabled = False
                provider = "gmail"
    assert get_mail_provider(C(), _Env()) is None


@pytest.mark.asyncio
async def test_decide_next_action_ignores_unwritable_prospects(state):
    # Guessed addresses are never written for; they must not pin write_campaign.
    for i in range(3):
        await state.add_prospect(Prospect(
            first_name=f"G{i}", last_name="X", title="Owner",
            email=f"g{i}@acme.com", email_status="guess"))
    summary = await state.get_state_summary()
    assert summary["prospects"]["new"] == 3
    assert summary["writable_prospects"] == 0
    assert await decide_next_action(None, state, None, summary=summary) == "prospect"

    await state.add_prospect(Prospect(
        first_name="V", last_name="X", title="Owner",
        email="v@acme.com", email_status="verified"))
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, None, summary=summary) == "write_campaign"


@pytest.mark.asyncio
async def test_export_leads_shape_with_personal_and_template_drafts(state):
    cid = await state.add_company(Company(
        name="Acme HVAC", domain="acme.com", website="https://acme.com",
        description="Heating and cooling", tech_stack=["WordPress"]))
    p1 = await state.add_prospect(Prospect(
        company_id=cid, first_name="Ann", last_name="Lee", title="Owner",
        email="ann@acme.com", email_status="verified", email_verified=True,
        company="Acme HVAC"))
    p2 = await state.add_prospect(Prospect(
        company_id=cid, first_name="Bob", last_name="Ray", title="Manager",
        email="bob@acme.com", email_status="risky", company="Acme HVAC"))
    await state.add_prospect(Prospect(
        first_name="Gus", last_name="Guess", title="Owner",
        email="gus@acme.com", email_status="guess"))
    camp = await _campaign(state, [p1, p2])
    await state.add_draft(p1, "your cooling page", "Saw your site...", campaign_id=camp)

    leads = await collect_leads(state)
    assert {l["email"] for l in leads} == {"ann@acme.com", "bob@acme.com"}
    by = {l["email"]: l for l in leads}
    assert by["ann@acme.com"]["draft"]["source"] == "personalized"
    assert by["ann@acme.com"]["draft"]["subject"] == "your cooling page"
    assert by["bob@acme.com"]["draft"]["source"] == "template"
    assert by["bob@acme.com"]["draft"]["subject"] == "hi Bob"
    assert by["ann@acme.com"]["company"]["domain"] == "acme.com"
    assert by["ann@acme.com"]["email_verified"] is True

    assert len(await collect_leads(state, require_draft=True)) == 2
    assert len(await collect_leads(state, include_all=True)) == 3
    assert await collect_leads(state, since="2999-01-01T00:00:00") == []
    doc = json.loads(leads_to_json(leads))
    assert doc["version"] == 1 and doc["count"] == 2


@pytest.mark.asyncio
async def test_add_draft_replaces_same_step(state):
    pid = await state.add_prospect(Prospect(first_name="A", last_name="B", title="Owner"))
    await state.add_draft(pid, "one", "first")
    await state.add_draft(pid, "two", "second")
    d = await state.get_draft(pid, 1)
    assert d["subject"] == "two"


# ── Research mode: guessed addresses are drafted for; scout reads the site ──


class _StubBrain:
    def __init__(self, scout_people=None):
        self.scout_people = scout_people or []
        self.calls = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return ""

    async def think_json(self, prompt, session_id=None, agent="", task=""):
        self.calls.append(task)
        if task == "write_sequence":
            return [
                {"step": 1, "subject": "s1 {{first_name}}", "body": "b1 {{company}}", "delay_days": 0},
                {"step": 2, "subject": "s2 {{first_name}}", "body": "b2 {{company}}", "delay_days": 3},
            ]
        if task == "personal_email":
            return {"subject": "your front desk", "body": "Saw your site. Quick question?"}
        if task == "site_team":
            return self.scout_people
        return None


class _WriterCfg:
    class channels:
        class email:
            enabled = False
            provider = "instantly"

    class product:
        name = "p"
        description = "d"
        key_benefits = ["b"]
        pricing = "x"

    class persona:
        name = "Juni"
        company = "JDL"
        role = "BD"
        tone = "plain"


@pytest.mark.asyncio
async def test_count_writable_includes_guess_only_when_asked(state):
    await state.add_prospect(Prospect(first_name="A", last_name="B", title="Owner",
                                      email="a@x.com", email_status="guess"))
    assert await state.count_writable_prospects() == 0
    assert await state.count_writable_prospects(include_guess=True) == 1
    s = await state.get_state_summary(draft_unverified=True)
    assert s["writable_prospects"] == 1


@pytest.mark.asyncio
async def test_writer_drafts_guess_in_research_mode_and_stores_in_drafts_table(state):
    from harvey.agents.writer import Writer

    pid = await state.add_prospect(Prospect(
        first_name="Ann", last_name="Lee", title="Owner", email="ann@acme.com",
        email_status="guess", company="Acme", industry="Dentist"))
    junk = await state.add_prospect(Prospect(
        first_name="Bad", last_name="Addr", title="Owner", email="bad@acme.com",
        email_status="invalid", company="Acme", industry="Dentist"))
    w = Writer(_StubBrain(), state, _WriterCfg(), env=None)
    await w.run()

    d = await state.get_draft(pid, 1)
    assert d and d["subject"] == "your front desk"
    assert await state.get_draft(junk, 1) is None
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM outbox") as cur:
            assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_scout_ai_team_is_grounded_in_page_text(state, monkeypatch):
    from harvey.agents.scout import Scout

    class Resp:
        text = ("<html><body><p>" + "Welcome to Acme Dental. " * 20 +
                "Dr. Jane Smith founded the practice. Reach us at jane@acme.com. "
                "Front desk run by Sam Cole.</p></body></html>")

    class Cfg:
        class icp:
            titles = ["Owner", "Office Manager"]
            industries = ["Dentist"]
        class channels:
            class linkedin:
                enabled = False

    people = [
        {"first_name": "Jane", "last_name": "Smith", "role": "owner",
         "title_on_site": "Founder", "email": "jane@acme.com"},
        {"first_name": "Sam", "last_name": "Cole", "role": "other", "email": ""},
        {"first_name": "Made", "last_name": "Up", "role": "owner", "email": ""},
    ]
    scout = Scout(_StubBrain(people), state, Cfg(), env=None)

    async def fake_base(domain, retries=1):
        return "https://acme.com", None

    async def fake_fetch(url, **kw):
        return Resp() if url.endswith("/about") else None

    monkeypatch.setattr(scout, "_resolve_base_url", fake_base)
    monkeypatch.setattr(scout, "_fetch", fake_fetch)

    members = await scout._ai_team_from_site("acme.com", "Acme Dental")
    assert [(m["first_name"], m["last_name"], m["title"]) for m in members] == [("Jane", "Smith", "Owner")]
    assert members[0]["published_email"] == "jane@acme.com"


@pytest.mark.asyncio
async def test_directory_strategy_rotates_mappable_queries(state, monkeypatch):
    from harvey.agents.scout import Scout
    import harvey.pipeline as D

    class Cfg:
        class icp:
            industries = ["Med Spa", "Dentist", "Veterinarian"]  # Med Spa has no OSM mapping
            geography = ["Spokane, WA", "Denver, CO"]
            geo_coordinates = {"Spokane, WA": "47.6,-117.4,40", "Denver, CO": "39.7,-104.9,50"}
            web_discovery = False
            titles = ["Owner"]
        class product:
            name = "p"
        class channels:
            class linkedin:
                enabled = False

    seen = []

    class Report:
        errors = []
        def summary_line(self):
            return "stub"

    async def fake_run(state_, cfg, provider, queries, **kw):
        seen.append((provider, queries[0].term, queries[0].location, kw.get("max_spend")))
        return Report()

    monkeypatch.setattr(D, "run_prospecting", fake_run)
    scout = Scout(_StubBrain(), state, Cfg(), env=None)
    for _ in range(4):
        await scout._prospect_via_directory()
    # 2 mappable industries x 2 metros, rotating; never the unmappable one; free only
    assert [(t, l) for _, t, l, _ in seen] == [
        ("Dentist", "Spokane, WA"), ("Dentist", "Denver, CO"),
        ("Veterinarian", "Spokane, WA"), ("Veterinarian", "Denver, CO")]
    assert all(p == "osm" and spend == 0.0 for p, _, _, spend in seen)
