"""A scripted (not LLM) /new-class agent: plain Python following .claude/skills/new-class/SKILL.md over a real stdio
MCP session. It only interprets structured results, never free text, which is what the skill demands."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ALLOWED = frozenset(
    {
        "capabilities",
        "get_profile",
        "list_characters",
        "get_character",
        "check_class_request",
        "register_character",
        "campaign_create",
        "campaign_status",
        "campaign_submit",
        "campaign_decide",
        "campaign_escalate",
        "campaign_report",
        "job_status",
        "job_result",
        "view_artifact",
        "contact_sheet",
        "validate",
        "compare",
        "godot_check",
        "smoke_render",
        "compose_sheet",
        "build_delivery",
        "delivery_status",
    }
)
GPU_WAIT, CPU_WAIT, MAX_POLLS = 60, 10, 40
TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}
GAME_ORDER = ("idle", "run")


class Stop(Exception):
    def __init__(self, reason: str, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


@dataclass
class Agent:
    session: object
    wait: object  # async callable(seconds)
    project: str
    request: dict
    reference_id: str | None = None
    reference_dir: str | None = None
    campaign_id: str | None = None
    budget: dict = field(default_factory=dict)
    delivery: dict | None = None  # {composed, target_rev, base_manifest, import_template}
    calls: list = field(default_factory=list)
    images: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    composed: dict | None = None

    @property
    def character(self) -> str:
        return self.request["character"]

    async def tool(self, name: str, **params) -> dict:
        assert name in ALLOWED, name
        self.calls.append(name)
        response = await self.session.call_tool(name, params)
        envelope = response.structured_content
        if name == "view_artifact" and envelope["ok"]:
            self.images.append((envelope["result"], [c for c in response.content if c.type == "image"]))
        if not envelope["ok"]:
            self.errors.append((name, envelope["error"]))
        return envelope

    async def ok(self, name: str, **params) -> dict:
        envelope = await self.tool(name, **params)
        if not envelope["ok"]:
            if envelope["error"]["message"].startswith("campaign_busy:"):
                raise Stop("campaign_busy", error=envelope["error"])  # another orchestrator: report, never wait
            raise Stop(f"{name}: {envelope['error']['code']}", error=envelope["error"])
        return envelope["result"]

    async def run(self) -> dict:
        try:
            await self._run()
            stop = Stop("done")
        except Stop as exc:
            stop = exc
        return await self.handoff(stop)

    async def _run(self) -> None:
        self.caps = await self.ok("capabilities")
        await self.ok("get_profile", project=self.project)
        character = await self.tool("get_character", project=self.project, character=self.character)
        if not character["ok"]:
            raise Stop("AWAITING_PROFILE_ROW", error=character["error"])
        if self.campaign_id is None:
            await self.admission(character["result"])
        await self.loop()

    async def admission(self, character: dict) -> None:
        if self.reference_id is None:
            if self.reference_dir is None:
                check = await self.ok("check_class_request", project=self.project, request=self.request)
                raise Stop(check["status"], findings=check["findings"])
            registered = await self.ok(
                "register_character",
                project=self.project,
                character=self.character,
                reference_dir=self.reference_dir,
                notes=self.request.get("palette_notes", ""),
            )
            self.reference_id = registered["ref_set_id"]
            character = await self.ok("get_character", project=self.project, character=self.character)
        check = await self.ok(
            "check_class_request", project=self.project, request=self.request, stills_ref=self.reference_id
        )
        if check["status"] != "ready":
            raise Stop(check["status"].upper(), findings=check["findings"])
        refs = next((r for r in character["reference_sets"] if r["ref_set_id"] == self.reference_id), None)
        if refs is None or refs["approval"]["state"] != "approved":
            for direction in ("south", "east", "west"):
                await self.ok("view_artifact", ref=f"{self.reference_id}:still_{direction}.png", zoom=4)
            raise Stop("AWAITING_REFERENCE_APPROVAL", owner="sonoranlab registry approve-references ...")
        created = await self.tool(
            "campaign_create", project=self.project, request=self.request, reference_id=self.reference_id, **self.budget
        )
        if not created["ok"]:
            match = re.search(r"camp-\d{8}T\d{6}Z-[0-9a-f]{8}", created["error"]["message"])
            if created["error"]["code"] == "conflict" and match and "campaign_busy" not in created["error"]["message"]:
                self.campaign_id = match.group(0)  # resume, never revise blindly
                return
            raise Stop(f"campaign_create: {created['error']['code']}", error=created["error"])
        if not created["result"]["created"]:
            raise Stop(created["result"]["state"], findings=created["result"]["findings"])
        self.campaign_id = created["result"]["campaign_id"]

    async def status(self) -> dict:
        return await self.ok("campaign_status", campaign_id=self.campaign_id)

    async def loop(self) -> None:
        for _ in range(20):  # bounded: every pass submits, polls, decides or stops
            view = await self.status()
            if view["gate"]["state"] != "READY":
                raise Stop(view["gate"]["state"], detail=view["gate"]["detail"])
            if view["state"] == "BLOCKED":
                raise Stop("BLOCKED", next_actions=view["next_actions"])
            flying = [
                a["job_id"]
                for c in view["clips"].values()
                for a in c["attempts"]
                if a["class"] in {"active", "submitting"} and a["job_id"]
            ]
            if flying:
                await self.poll(flying[0], GPU_WAIT)
                continue
            plan = [c for c, v in view["clips"].items() if v["in_plan"]]
            for clip in plan:  # request priority order
                state = view["clips"][clip]["state"]
                if state == "HOLD_BUDGET":
                    await self.ok("campaign_escalate", campaign_id=self.campaign_id, clip=clip, reason="budget spent")
                    raise Stop("HOLD_BUDGET", clip=clip)
                if state == "TECHNICAL_PASS":
                    await self.inspect(view["clips"][clip])
                    break
                if state == "READY":
                    submitted = await self.tool("campaign_submit", campaign_id=self.campaign_id, clip=clip)
                    if not submitted["ok"]:
                        raise Stop(submitted["error"]["message"].split(":")[0], error=submitted["error"])
                    await self.poll(submitted["result"]["job_id"], GPU_WAIT)
                    break
            else:
                if all(view["clips"][c]["state"] in {"AWAITING_OWNER_REVIEW", "ARTIFACT_APPROVED"} for c in plan):
                    selected = {c: view["clips"][c]["selected"] for c in plan}
                    if self.delivery:
                        await self.deliver()
                    await self.final(selected)
                raise Stop(view["state"])
        raise Stop("loop bound reached")

    async def finished(self, job_id: str) -> None:
        """CPU/engine jobs must succeed; a failure is reported (with its error), never retried."""
        status = await self.poll(job_id, CPU_WAIT)
        if status["state"] != "succeeded":
            raise Stop(f"job {status['state']}", job_id=job_id, error=status["error"])

    async def poll(self, job_id: str, interval: int) -> dict:
        for _ in range(MAX_POLLS):
            status = await self.ok("job_status", job_id=job_id)
            if status["state"] in TERMINAL:
                return status
            await self.wait(interval)
        raise Stop("poll cap reached", job_id=job_id)

    async def inspect(self, clip: dict) -> None:
        candidate = next(c for c in clip["candidates"] if c["decision"] is None)
        result = await self.ok("job_result", job_id=candidate["job_id"])
        verdict = result["result"]["animate"]["verdict"]  # numbers first
        refs = [a["ref"] for a in result["artifacts"] if "/sweep/final/job/" in a["ref"]]  # the validated final
        for wanted, zoom in (("contact.png", 1), (f"export/{self.character}/{self.character}.png", 2)):
            ref = next((r for r in refs if r.endswith(wanted)), None)
            if ref:
                await self.ok("view_artifact", ref=ref, zoom=zoom)
        await self.siblings()
        await self.ok(
            "campaign_decide",
            campaign_id=self.campaign_id,
            job_id=candidate["job_id"],
            decision="selected" if verdict["pass"] else "rejected",
            reason="technical PASS (validator); recommendation: reads as the same figure as the stills",
        )

    async def siblings(self) -> list[str]:
        listed = await self.ok("list_characters", project=self.project)
        return [
            c["id"]
            for c in listed["characters"]
            if c["id"] != self.character and any(r["approval"]["state"] == "approved" for r in c["reference_sets"])
        ][:2]

    async def final(self, selected: dict) -> None:
        if self.reference_id is None:
            character = await self.ok("get_character", project=self.project, character=self.character)
            self.reference_id = next(
                r["ref_set_id"] for r in character["reference_sets"] if r["approval"]["state"] == "approved"
            )
        clips = [{"name": c, "job": selected[c]} for c in GAME_ORDER if c in selected]
        job = await self.ok(
            "compose_sheet", project=self.project, character=self.character, clips=clips, reference_id=self.reference_id
        )
        await self.finished(job["job_id"])
        composed = (await self.ok("job_result", job_id=job["job_id"]))["result"]
        self.composed = {"job": job["job_id"], **composed}
        if not composed.get("pass"):
            raise Stop("compose FAIL", failures=composed.get("failures"), error=composed.get("error"))
        for clip in clips:
            result = await self.ok("job_result", job_id=clip["job"])
            report = next(a["ref"] for a in result["artifacts"] if a["ref"].endswith("sweep/final/job/report.json"))
            check = await self.ok(
                "godot_check", project=self.project, character=self.character, anim=clip["name"], source=report
            )
            await self.finished(check["job_id"])
        smoke = await self.ok(
            "smoke_render",
            project=self.project,
            character=self.character,
            candidate=composed["artifact_id"],
            siblings=await self.sibling_exports(),
        )
        await self.finished(smoke["job_id"])
        rendered = await self.ok("job_result", job_id=smoke["job_id"])
        for ref in [a["ref"] for a in rendered["artifacts"] if a["ref"].endswith(".png")]:
            await self.ok("view_artifact", ref=ref)
        await self.ok("view_artifact", ref=composed["export"], zoom=1)
        raise Stop("AWAITING_OWNER_REVIEW", sheet=composed["export"], summary=composed["summary"])

    async def sibling_exports(self) -> list[str]:
        return []  # the toy profile has no approved sibling with an imported export; reported in the handoff

    async def deliver(self) -> None:
        existing = await self.ok("delivery_status", character=self.character)
        if existing["bundles"]:
            bundle = existing["bundles"][-1]
            raise Stop(bundle["state"], bundle=bundle["bundle"])
        built = await self.tool("build_delivery", project=self.project, campaign_id=self.campaign_id, **self.delivery)
        if not built["ok"]:
            reason = "AWAITING_OWNER_REVIEW" if built["error"]["code"] == "not_ready" else "delivery refused"
            raise Stop(reason, error=built["error"])
        done = await self.poll(built["result"]["job_id"], CPU_WAIT)
        if done["state"] != "succeeded":
            raise Stop("delivery job " + done["state"], error=done["error"])
        status = await self.ok("delivery_status", bundle=built["result"]["job_id"])
        raise Stop(status["bundles"][0]["state"], bundle=built["result"]["job_id"])

    async def handoff(self, stop: Stop) -> dict:
        report = None
        if self.campaign_id:
            envelope = await self.tool("campaign_report", campaign_id=self.campaign_id)
            report = envelope.get("result")
        return {"campaign_id": self.campaign_id, "stop": stop.reason, "detail": stop.detail, "report": report}
