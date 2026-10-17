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
