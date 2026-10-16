"""Local regression tests for the builder. No network, no Freya, no simulation.

    python tools/check_builder.py

Every fixture is a brief the builder has never seen, written with different
values, different words and a different tool API, plus the values a reader would
take out of it by hand. If the builder ever stops reading a brief correctly,
this fails before a phone agent is published.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from builder.brief_parser import parse_brief  # noqa: E402
from builder.date_calculator import product_code, setup_code  # noqa: E402
from builder.main import check_workflow  # noqa: E402
from builder.workflow_builder import build_workflow  # noqa: E402

FIXTURES = [
    {
        "name": "brief A (public)",
        "briefs": ["briefs/brief-a.md"],
        "openapi": "tools/openapi.json",
        "expect": {
            "bank_name": "Freya Bank",
            "holidays": ["2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01"],
            "identity": ["date_of_birth", "phone_last4"],
            "callback": (10, 19),
            "floors": {"credit_card": "minimum_payment", "personal_loan": "instalment_amount",
                       "car_loan": "instalment_amount"},
            "queues": {"restructuring": "restructuring", "human": "general", "support": "support"},
            "bands": [(1, 7, 12, 1), (8, 45, 7, 2)],
            "multi_product": False,
            "closes": {("promise", ""): "promise", ("paid", ""): "paid",
                       ("transferred", "restructuring"): "restructuring",
                       ("transferred", "human"): "human",
                       ("transferred", "support"): "support",
                       ("no_promise", "refused"): "refused",
                       ("no_promise", "hardship"): "hardship",
                       ("no_promise", "dispute"): "dispute",
                       ("no_promise", "service_error"): "service_error",
                       ("not_verified", "failed"): "failed",
                       ("not_verified", "declined"): "declined",
                       ("not_reached", "callback"): "callback",
                       ("not_reached", "no_callback"): "no_callback",
                       ("not_reached", "wrong_number"): "wrong_number",
                       ("not_reached", "deceased"): "deceased",
                       ("not_reached", "unclear"): "unclear"},
            "deadlines": {"2026-10-14": {"p1": "2026-11-02", "p2": "2026-10-23"}},
        },
    },
    {
        "name": "brief B on top of A (public, multi product)",
        "briefs": ["briefs/brief-a.md", "briefs/brief-b.md"],
        "openapi": "tools/openapi.json",
        "expect": {
            "multi_product": True,
            "max_products": 3,
            "note_unpromised": True,
            "identity": ["date_of_birth", "phone_last4"],
            "deadlines": {"2026-10-14": {"p1": "2026-11-02", "p2": "2026-10-23", "p3": "2026-11-02"}},
        },
    },
    {
        "name": "prose fixture (a brief the builder has never seen)",
        "briefs": ["tests/brief-prose.md"],
        "openapi": "tests/openapi-prose.json",
        "expect": {
            "bank_name": "Meridian Mutual",
            "holidays": ["2027-01-02", "2027-01-18", "2027-02-15", "2027-05-31", "2027-07-05",
                         "2027-09-06"],
            "identity": ["postcode", "birth_year"],
            "callback": (9, 18),
            "floors": {"credit_card": "minimum_payment", "personal_loan": "instalment_amount",
                       "car_loan": "instalment_amount"},
            "queues": {"restructuring": "restructuring", "human": "frontline", "support": "welfare"},
            "bands": [(1, 14, 9, 2), (15, 60, 5, 3)],
            "multi_product": False,
            "closes": {("promise", ""): "promise", ("settled", ""): "paid",
                       ("handed_over", "plan_change"): "restructuring",
                       ("handed_over", "person_requested"): "human",
                       ("handed_over", "hardship_transfer"): "support",
                       ("no_agreement", "declined"): "refused",
                       ("no_agreement", "hardship"): "hardship",
                       ("no_agreement", "contested"): "dispute",
                       ("no_agreement", "system_error"): "service_error",
                       ("identity_not_met", "attempts"): "failed",
                       ("identity_not_met", "withheld"): "declined",
                       ("identity_not_met", "scheduled"): "callback",
                       ("identity_not_met", "no_slot"): "no_callback",
                       ("identity_not_met", "wrong_person"): "wrong_number",
                       ("identity_not_met", "bereaved"): "deceased",
                       ("identity_not_met", "no_answer"): "unclear"},
        },
    },
]

SAMPLE_PRODUCTS = [
    {"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 420.0,
     "days_late": 6, "minimum_payment": 126.0},
    {"product_id": "p2", "type": "personal_loan", "past_due": 640.0, "days_late": 20,
     "instalment_amount": 213.0},
    {"product_id": "p3", "type": "car_loan", "past_due": 75.0, "days_late": 3, "instalment_amount": 25.0},
]


def run_snippet(code, variables):
    scope = {"vars": dict(variables)}
    exec(compile(code, "<generated>", "exec"), scope)  # noqa: S102 - our own snippet
    return scope["vars"]


def facts_for(policy, now):
    out = run_snippet(setup_code(policy), {"now": now})
    out = run_snippet(product_code(policy), {"now": now, "products": SAMPLE_PRODUCTS, **out})
    return out


def main():
    failures = []
    for fixture in FIXTURES:
        name, expect = fixture["name"], fixture["expect"]
        policy = parse_brief(fixture["briefs"], fixture["openapi"])

        def check(label, got, want):
            if got != want:
                failures.append(f"{name}: {label}\n     got  {got}\n     want {want}")

        if "bank_name" in expect:
            check("bank name", policy.bank_name, expect["bank_name"])
        if "holidays" in expect:
            check("holidays", policy.holidays, expect["holidays"])
        if "identity" in expect:
            check("identity questions", [q.field for q in policy.identity_questions], expect["identity"])
        if "callback" in expect:
            check("callback window", (policy.callback.start_hour, policy.callback.end_hour),
                  expect["callback"])
        if "floors" in expect:
            check("floors", {p.type_name: p.floor_field for p in policy.product_types}, expect["floors"])
        if "queues" in expect:
            check("queues", {q.role: q.queue for q in policy.queues}, expect["queues"])
        if "bands" in expect:
            check("bands", [(b.min_days_late, b.max_days_late, b.business_days, b.persuasion_attempts)
                            for b in policy.deadline_bands], expect["bands"])
        if "multi_product" in expect:
            check("multi product", policy.multi_product, expect["multi_product"])
        if "max_products" in expect:
            check("max products", policy.max_products, expect["max_products"])
        if "note_unpromised" in expect:
            check("notes for unpromised products", policy.note_unpromised, expect["note_unpromised"])
        if "closes" in expect:
            got = {(c.outcome, c.reason): c.role for c in policy.closes}
            check("closing table", got, expect["closes"])

        for now, wanted in expect.get("deadlines", {}).items():
            out = facts_for(policy, now)
            if out.get("facts_status") != "ok":
                failures.append(f"{name}: the code node produced no facts on {now}")
                continue
            for pid, deadline in wanted.items():
                import datetime
                got = datetime.date.fromisoformat(deadline)
                rule = json.loads(out["promise_policy_json"])[pid]["max"]
                if rule != got.isoformat():
                    failures.append(f"{name}: deadline for {pid} on {now} is {rule}, expected {deadline}")

        workflow = build_workflow(policy, fixture["openapi"])
        workflow.pop("_builder_notes", None)
        problems = check_workflow(workflow)
        if problems:
            failures.extend(f"{name}: workflow check: {p}" for p in problems)

        # Each brief-selected close route records exactly once, then reaches a
        # terminal that speaks the farewell and gives a short goodbye window.
        for node_name, node in workflow["nodes"].items():
            if not node_name.startswith("close_") or node.get("node_type") != "api_request":
                continue
            transitions = node.get("transitions") or []
            targets = [t.get("target_node") for t in transitions]
            if node_name in targets:
                failures.append(f"{name}: {node_name} loops and can repeat close_call")
            if len(node.get("pre_actions") or []) != 1:
                failures.append(f"{name}: {node_name} must make exactly one close_call request")
            if (not transitions or transitions[-1].get("condition", {}).get("operator") != "always"
                    or transitions[-1].get("target_node") != "end_close_error"):
                failures.append(f"{name}: {node_name} must end safely if close_call does not confirm success")
            mapping = {m.get("variable") for m in node.get("response_mapping") or []}
            if "close_result_ok" not in mapping:
                failures.append(f"{name}: {node_name} does not map the close_call result")
            role = node_name.removeprefix("close_")
            terminal = workflow["nodes"].get("end_" + role, {})
            props = (((terminal.get("terminal") or {}).get("function") or {})
                     .get("parameters", {}).get("properties", {}))
            if terminal.get("node_type") != "end_call" or props.get("message_mode", {}).get("default") != "custom":
                failures.append(f"{name}: {node_name} has no fixed terminal farewell")
            if (props.get("interruptible", {}).get("default") is not True
                    or props.get("trigger_timeout", {}).get("default") != 2):
                failures.append(f"{name}: {node_name} does not allow the two-second goodbye window")
            if not props.get("spoken_message", {}).get("default"):
                failures.append(f"{name}: {node_name} has an empty terminal farewell")
        if "I am " not in workflow.get("system_prompt", "") or "digital assistant" not in workflow.get("system_prompt", ""):
            failures.append(f"{name}: system prompt does not directly answer the AI/robot question")
        identity_schema = workflow["nodes"].get("identity", {}).get("extraction_schema") or []
        if not any(x.get("name") == "preverify_hardship" for x in identity_schema):
            failures.append(f"{name}: identity step does not retain a hardship disclosed before collection")
        if not any(x.get("name") == "preverify_ai_question" for x in identity_schema):
            failures.append(f"{name}: identity step does not retain a pre-verification AI question")
        product_code_text = " ".join(
            str(action.get("function", {}).get("parameters", {}).get("properties", {}).get("code", {}).get("default", ""))
            for action in workflow["nodes"].get("product_facts", {}).get("pre_actions", [])
        )
        if "preverify_ai_question" not in product_code_text:
            failures.append(f"{name}: post-verification opening does not answer a previously asked AI question")
        elif not policy.multi_product:
            product_routes = workflow["nodes"].get("product_facts", {}).get("transitions") or []
            if not any(t.get("target_node") == "hardship_step" for t in product_routes):
                failures.append(f"{name}: pre-collection hardship does not bypass the payment ask")
        if policy.multi_product:
            negotiate_text = workflow["nodes"].get("negotiate", {}).get("task_message", "")
            if "Never offer amounts for multiple products in one turn" not in negotiate_text:
                failures.append(f"{name}: multi-product flow does not enforce one product per turn")
            if "{{collection_plan_json}}" not in negotiate_text:
                failures.append(f"{name}: multi-product flow does not consume the precomputed collection plan")
            if "exactly one confirmed create_promise OR the brief-required negative record" not in negotiate_text:
                failures.append(f"{name}: multi-product flow does not require a final ledger record per product")
            confirm = workflow["nodes"].get("confirm_promise", {})
            confirm_tools = [t.get("function", {}).get("name") for t in confirm.get("tools", [])]
            if any(name.startswith("close_") for name in confirm_tools):
                failures.append(f"{name}: multi-product confirmation exposes close_call before the readiness gate")
            if not any(t.get("target_node", "").startswith("close_")
                       and {c.get("variable") for c in t.get("condition", {}).get("conditions", [])}
                       >= {"collection_close_ready", "collection_close_role"}
                       for t in confirm.get("transitions", [])):
                failures.append(f"{name}: multi-product confirmation has no deterministic close-readiness routes")
            if confirm.get("max_turn_transition") != "end_close_error":
                failures.append(f"{name}: exhausted multi-product confirmation can bypass the close-readiness gate")
            if any(x.get("name") == "collection_close_ready"
                   for x in confirm.get("extraction_schema", [])):
                failures.append(f"{name}: the close gate is still controlled by model extraction")
            for tool in confirm.get("tools", []):
                if tool.get("function", {}).get("name") in ("create_promise", "add_note"):
                    if not tool.get("pre_process_code") or not tool.get("post_process_code"):
                        failures.append(f"{name}: {tool['function']['name']} lacks ledger tracking hooks")
        else:
            for node_name in ("negotiate", "confirm_promise"):
                node = workflow["nodes"].get(node_name, {})
                schema = node.get("extraction_schema") or []
                if any(x.get("name") in ("collection_close_ready", "confirmed_promises_json")
                       for x in schema):
                    failures.append(f"{name}: single-product flow unexpectedly enables a multi-product gate")

        closes = {}
        for node in workflow["nodes"].values():
            carried = list(node.get("tools") or [])
            for action in node.get("pre_actions") or []:
                carried.append(action)
            for tool in carried:
                fn = tool["function"]
                if fn["name"].startswith("close_"):
                    body = fn["parameters"]["properties"]["body"]["properties"]
                    closes[fn["name"]] = (body.get("outcome", {}).get("default"),
                                          body.get("reason", {}).get("default"))
        for role in ("close_promise", "close_refused", "close_callback", "close_deceased"):
            if role not in closes:
                failures.append(f"{name}: no {role} tool was generated")
        for tool_name, (_, reason) in closes.items():
            if reason and reason.startswith("{{"):
                failures.append(f"{name}: {tool_name} would ask the model for its reason: {reason}")

        # every conversation step must be able to speak when the router enters it
        for node_name, node in workflow["nodes"].items():
            if node.get("node_type") == "conversation" and (node.get("on_enter") or {}).get("type") == "silent":
                failures.append(f"{name}: {node_name} enters silently")
            if node.get("node_type") == "conversation" and (node.get("on_enter") or {}).get("type") == "silent":
                failures.append(f"{name}: {node_name} enters silently")
            if node.get("node_type") == "conversation" and (node.get("on_enter") or {}).get("type") == "tts" \
                    and not node.get("on_enter_first_entry_only"):
                failures.append(f"{name}: {node_name} has a fixed opening line but could repeat it")

        print(("PASS  " if not failures else "CHECK "), name,
              f"({len(workflow['nodes'])} nodes, {len(policy.closes)} closing rows)")

    print()
    if failures:
        print("FAILURES")
        for f in failures:
            print(" -", f)
        return 1
    print("all builder checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
