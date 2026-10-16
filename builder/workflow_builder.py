"""Turn a PolicySpec into a Freya workflow.

Design, and why:

* The model is used for conversation, never for arithmetic. Every date, amount
  and deadline is computed in a code node and handed to the model as text.
* Every way of ending the call is a separate close node whose close_call tool
  has the outcome and the reason already baked in, and its own name. The model
  never picks an outcome, and the router can tell which ending happened.
* Transfers and hardship get their own step, so that the tool call itself is a
  step of the call and not something the model may forget to make.
* Every way of leaving a step is a deterministic condition on a variable or on
  a tool result. The model only decides what a rule cannot decide.
"""

from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from builder.date_calculator import (
    DATE_ANSWER_PRE_HOOK,
    close_hook,
    collection_record_pre_hook,
    create_promise_hook,
    create_promise_pre_hook,
    note_hook,
    product_code,
    schedule_callback_hooks,
    setup_code,
    transfer_hook,
    verify_identity_hook,
)
from builder.brief_parser import ROLE_FAMILY
from builder.openapi_parser import load_tools
from builder.policy_spec import OutcomeClose, PolicySpec

# Which situation maps to which row of the brief's closing table.
FAMILIES = ROLE_FAMILY

SITUATIONS = list(FAMILIES)

# The outcome a brief gives a successful payment, and the one it gives a payment
# the customer says they already made. Used only when the table carries no reason.
PROMISE_WORDS = ("promis", "agreement", "arrang", "settled_promise")
PAID_WORDS = ("paid", "settled", "resolved", "clear")

FAREWELLS = {
    "unclear": "I'm sorry for the confusion. Thank you for your time. Goodbye.",
    "wrong_number": "I'm sorry for the mistake. Thank you for your time. Goodbye.",
    "deceased": "I'm very sorry for your loss. Goodbye.",
    "no_callback": "Thank you for your help. Goodbye.",
    "callback": "Thank you for your help. Goodbye.",
    "failed": "You can check this in the mobile app or in a branch. Thank you for your time. Goodbye.",
    "declined": "The questions protect your account. Thank you for your time. Goodbye.",
    "refused": "Thank you for your time. Goodbye.",
    "hardship": "I'm sorry I couldn't help today. Thank you for your time. Goodbye.",
    "dispute": "Thank you for your time. Goodbye.",
    "paid": "Thank you for your time. Goodbye.",
    "service_error": "I'm sorry, I couldn't record your promise. Thank you for your time. Goodbye.",
    "restructuring": "I'm connecting you to the right team now.",
    "human": "I'm connecting you to a person now.",
    "support": "I'm connecting you to the support team now.",
    "promise": "Thank you for your time. Goodbye.",
}


# What the closing step actually says. These lines are the brief's own sentences
# where it supplies them, so they are spoken by the engine, not by the model.
FAREWELL_LINES = {
    "paid": lambda p: [p.already_paid_sentence, "Thank you for your time. Goodbye."],
    "deceased": lambda p: [p.condolence or "I am sorry for your loss.", "Goodbye."],
    "failed": lambda p: [((p.failed_advice or "You can check this in the mobile app or in a branch.")
                           .replace("Tell the customer to", "Please")
                           .replace("tell the customer to", "Please")),
                          "Thank you for your time. Goodbye."],
    "declined": lambda p: [p.declined_advice or "The questions protect their account.",
                           "Thank you for your time. Goodbye."],
    "promise": lambda p: [p.sms_sentence or "You will receive a text message confirming this promise.",
                          "Thank you for your time. Goodbye."],
}

CLOSE_WHEN = {
    "unclear": "CALL it when it is still not clear who is on the line.",
    "wrong_number": "CALL it when the person says this number does not belong to the customer.",
    "deceased": "CALL it when the person says the customer has died.",
    "no_callback": "CALL it when no slot inside the window was agreed.",
    "callback": "CALL it once the callback has been booked.",
    "failed": "CALL it when the security check ends because the answers were wrong.",
    "declined": "CALL it when the customer will not answer the security questions.",
    "refused": "CALL it when the persuasion attempts are used up and the customer will not pay.",
    "hardship": "CALL it when the customer has hardship and will not accept the support team.",
    "dispute": "CALL it when the customer says the debt is not theirs or that they never had the product.",
    "paid": "CALL it when the customer says they have already paid this amount.",
    "service_error": "CALL it when a promise could not be recorded after a second attempt.",
    "restructuring": "CALL it right after the restructuring transfer succeeded.",
    "human": "CALL it right after the transfer to a person succeeded.",
    "support": "CALL it right after the support transfer succeeded.",
    "promise": "CALL it once every promise is recorded.",
}


def _or(*conditions: Dict[str, Any]) -> Dict[str, Any]:
    return {"logic": "or", "conditions": list(conditions)}


def quote(sentence: str) -> str:
    """A sentence from the brief, without its final full stop, ready to be put in quotes."""
    return (sentence or "").strip().rstrip(".").strip()


def ph(name: str) -> str:
    """A workflow variable placeholder, spelled the way the engine expects it.

    Prompts are built by concatenation rather than str.format, because `format`
    eats the double braces a placeholder needs.
    """
    return "{{" + name + "}}"


def lower_first(sentence: str) -> str:
    s = (sentence or "").strip()
    return s[0].lower() + s[1:] if s else s


def _var(variable: str, operand: Any) -> Dict[str, Any]:
    return {"variable": variable, "operator": "equals", "operand": str(operand)}


def _gt(variable: str, operand: Any) -> Dict[str, Any]:
    return {"variable": variable, "operator": "greater_than", "operand": operand}


def _ok(tool: str) -> Dict[str, Any]:
    return _or(*_ok_conditions(tool))


def _close_done_conditions(role: str) -> List[Dict[str, Any]]:
    """Close tools expose a role-specific response variable via response_mapping."""
    variable = "close_" + role + "_ok"
    return [_var(variable, value) for value in ("true", "True", "false", "False")]


def _close_attempted(role: str) -> Dict[str, Any]:
    return _or(*_close_done_conditions(role))


def _ok_conditions(tool: str) -> List[Dict[str, Any]]:
    """Both spellings of a boolean result, because an API may write either."""
    return [_var(tool + "_response.data.ok", "true"),
            _var(tool + "_response.data.ok", "True")]


def _transition(target: str, description: str, condition: Dict[str, Any], **extra) -> Dict[str, Any]:
    t = {"target_node": target, "description": description, "condition": condition}
    t.update(extra)
    return t


def _extract(name: str, values: List[str], description: str, confidence: float = 0.55) -> Dict[str, Any]:
    return {"name": name, "type": "string", "enum_values": values,
            "description": description, "min_confidence": confidence}


def _collection_state_schema(policy: PolicySpec) -> List[Dict[str, Any]]:
    """Brief-sized state fields for the current product and every ledger row."""
    if not policy.component_enabled("multi_product_sequence", policy.multi_product) or policy.max_products <= 1:
        return []
    count = max(1, int(policy.max_products or 1))
    positions = [str(i) for i in range(1, count + 1)] + ["complete"]
    return [
        {
            "name": "collection_state_json",
            "type": "string",
            "description": (
                "Persistent, compact JSON array copied from the current COLLECTION STATE. Preserve every entry, "
                "sequence, product_id, attempts_allowed, attempts_used, floor_offer_made, offer_history, "
                "disposition and ledger_status. Update only what the latest customer turn or tool result changed. "
                "Never reset or omit prior history. Return valid JSON, not prose."
            ),
            "min_confidence": 0.55,
        },
        _extract(
            "collection_active_product", positions,
            "The one-based sequence number of the product currently being handled, in get_account order. "
            "Skip positions marked not_present. Move to the next present index only after the current product "
            "has a valid promise candidate or its brief-required disposition and all allowed attempts are complete. "
            "Return 'complete' only when "
            "every product is resolved or a brief-required transfer ends the sequence.", 0.55),
    ]


class Builder:
    def __init__(self, policy: PolicySpec, openapi_path: str):
        self.policy = policy
        self.tools = {t["function"]["name"]: t for t in load_tools(openapi_path)}
        self.notes: List[str] = []
        self.closes = self._resolve_closes()

    def warn(self, message: str) -> None:
        self.notes.append(message)

    def _resolve_closes(self) -> Dict[str, Optional[OutcomeClose]]:
        """One usable close per situation, falling back inside the right family."""
        out: Dict[str, Optional[OutcomeClose]] = {}
        for role in SITUATIONS:
            found = self.policy.close_for(role)
            if not found:
                family = [c for c in self.policy.closes if FAMILIES.get(role) == c.outcome]
                found = family[0] if family else None
            if not found and role in ("promise", "paid"):
                words = PROMISE_WORDS if role == "promise" else PAID_WORDS
                found = next((c for c in self.policy.closes if not c.reason
                              and any(w in c.outcome for w in words)), None)
            if not found and role in ("promise", "paid"):
                found = next((c for c in self.policy.closes if not c.reason), None)
            if found and not found.reason:
                found = OutcomeClose(outcome=found.outcome, reason="", role=role)
            if not found:
                self.warn(f"no closing row for '{role}'")
            out[role] = found
        return out

    def tool(self, name: str) -> Dict[str, Any]:
        if name not in self.tools:
            raise KeyError(f"the tool API has no tool named {name}")
        return deepcopy(self.tools[name])

    def bake(self, tool: Dict[str, Any], values: Dict[str, str], drop: Optional[List[str]] = None) -> Dict[str, Any]:
        """Put fixed values into a request body, hide them from the model, and drop fields
        the brief says do not exist for this call."""
        body = tool["function"]["parameters"]["properties"]["body"]
        props, required = body["properties"], body.get("required", [])
        for key in drop or []:
            if key in props:
                props.pop(key)
                required = [r for r in required if r != key]
        for key, value in values.items():
            if key in props:
                props[key]["default"] = value
                props[key].pop("enum", None)
                props[key].pop("description", None)
                required = [r for r in required if r != key]
        body["required"] = required
        return tool

    # ------------------------------------------------------------ close tool

    def close_tool(self, role: str, when: str) -> Optional[Dict[str, Any]]:
        close = self.closes.get(role)
        if not close:
            return None
        tool = self.tool("close_call")
        values = {"outcome": close.outcome}
        drop: List[str] = []
        if close.reason:
            values["reason"] = close.reason
        else:
            drop.append("reason")        # the brief's table lists no reason for this row
        self.bake(tool, values, drop)
        tool["function"]["name"] = "close_" + role
        tool["function"]["description"] = (
            "Record the outcome and reason for this call. The outcome and reason are fixed: outcome '"
            + close.outcome + "'" + (", reason '" + close.reason + "'." if close.reason else ".")
            + " " + when + " Take no argument. This tool is available only after the workflow has routed to "
            "this specific closing situation. Call it exactly once. Never use it just to end a turn, because "
            "the customer paused, or before the required product records and transfers are complete. The "
            "workflow will speak the final message and end the call after this result."
        )
        tool["config"]["maxRetries"] = 0
        tool["response_mapping"] = [{"path": "data.ok", "variable": "close_" + role + "_ok"}]
        tool["post_process_code"] = close_hook()
        return tool

    def close_step(self, role: str) -> List[Dict[str, Any]]:
        """Record one policy-selected outcome and finish through a fixed terminal."""
        tool = self.close_tool(role, CLOSE_WHEN.get(role, "CALL it to finish the call."))
        if not tool:
            self.warn(f"no closing tool for '{role}'")
            return []
        farewell = FAREWELLS.get(role, "Thank you for your time. Goodbye.")
        maker = FAREWELL_LINES.get(role)
        if maker:
            parts = [p for p in maker(self.policy) if p]
            farewell = " ".join(parts)
        close_node = {
            "name": "close_" + role,
            "node_type": "api_request",
            "passthrough": True,
            "passthrough_default_transition": "end_close_error",
            "on_enter": {"type": "silent"},
            "pre_actions": [tool],
            "response_mapping": [{"path": "data.ok", "variable": "close_result_ok"}],
            "transitions": [
                _transition("end_" + role, "the outcome was recorded",
                            _or(_var("close_result_ok", "true"), _var("close_result_ok", "True"))),
                _transition("end_close_error", "close_call did not confirm the outcome", {"operator": "always"}),
            ],
        }
        end_node = {
            "name": "end_" + role,
            "node_type": "end_call",
            "on_enter": {"type": "silent"},
            "disable_inactivity_monitor": True,
            "transitions": [],
            "terminal": {
                "type": "function",
                "function": {
                    "name": "call_closed_" + role,
                    "handler": "__function__:end_call",
                    "parameters": {"type": "object", "properties": {
                        "message_mode": {"type": "string", "default": "custom"},
                        "spoken_message": {"type": "string", "default": farewell},
                        "interruptible": {"type": "boolean", "default": True},
                        "trigger_timeout": {"type": "number", "default": 2},
                        "trigger_pattern_both": {"type": "string", "default":
                            "goodbye|good bye|bye|thanks|thank you|you too|take care|have a good day"},
                    }},
                },
            },
        }
        return [close_node, end_node]

    def close_error_terminal(self) -> Dict[str, Any]:
        return {
            "name": "end_close_error",
            "node_type": "end_call",
            "on_enter": {"type": "silent"},
            "disable_inactivity_monitor": True,
            "transitions": [],
            "terminal": {"type": "function", "function": {
                "name": "call_closed_recording_error",
                "handler": "__function__:end_call",
                "parameters": {"type": "object", "properties": {
                    "message_mode": {"type": "string", "default": "custom"},
                    "spoken_message": {"type": "string", "default":
                        "I'm sorry, I couldn't finish recording the call outcome. Thank you for your time. Goodbye."},
                    "interruptible": {"type": "boolean", "default": True},
                    "trigger_timeout": {"type": "number", "default": 2},
                    "trigger_pattern_both": {"type": "string", "default":
                        "goodbye|good bye|bye|thanks|thank you|you too|take care|have a good day"},
                }},
            }},
        }

    def transfer_tool(self, role: str) -> Dict[str, Any]:
        queue = self.policy.queue_for(role)
        tool = self.tool("transfer_to_agent")
        self.bake(tool, {"queue": queue})
        tool["function"]["name"] = "transfer_" + role
        tool["function"]["description"] = (
            "Hand the call to a person. The queue is fixed to " + queue + "."
        )
        tool["config"]["maxRetries"] = 0
        tool["post_process_code"] = transfer_hook(self.policy)
        return tool

    def transfer_step(self, role: str, lead: str) -> Optional[Dict[str, Any]]:
        """A step whose only job is to make the transfer call and then close."""
        tool = self.transfer_tool(role)
        if not self.close_tool(role, CLOSE_WHEN.get(role, "")):
            self.warn(f"no closing row after a '{role}' transfer")
            return None
        return {
            "name": "transfer_" + role,
            "node_type": "conversation",
            "task_message": (
                lead + " CALL " + tool["function"]["name"] + " now, without any argument. Do not describe any plan, "
                "discount or waiver before or after the call. After the result succeeds, tell the customer they "
                "are being transferred and let the next step close the call."
            ),
            "tools": [tool],
            "max_node_turns": 8,
            "max_turn_transition": "close_" + role,
            "transitions": [
                _transition("close_" + role, "the transfer was made", _ok(tool["function"]["name"])),
                _transition("transfer_" + role, "keep trying to transfer", {"operator": "always"}),
            ],
        }


# -------------------------------------------------------------- negotiation

def _negotiation_nodes(b: Builder, policy: PolicySpec, bank: str, first_question: str) -> Dict[str, Any]:
    multi = (policy.component_enabled("multi_product_sequence", policy.multi_product)
             and policy.max_products > 1)
    nodes: Dict[str, Any] = {}

    hardship_tool = b.transfer_tool("support")
    hardship_close = b.close_tool("hardship", CLOSE_WHEN["hardship"])
    hardship_tools = [hardship_tool] + ([hardship_close] if hardship_close else [])

    refused_close = b.close_tool("refused", CLOSE_WHEN["refused"])
    dispute_close = b.close_tool("dispute", CLOSE_WHEN["dispute"])
    paid_close = b.close_tool("paid", CLOSE_WHEN["paid"])

    rules = [
        "1. Start by telling the customer, one sentence per product and before you ask anything, what each late "
        "product is, how much is past due on it and how many days late it is. Only now may you talk about the debt.",
        "2. Your first question is exactly: \"" + first_question + "\"",
        "3. Never ask \"how much can you pay?\" or \"what can you afford?\". You propose every amount yourself.",
        "4. Offer the full past-due amount first. " + describe_floors(policy),
        "5. The floor offer rule: ONLY trigger it when the customer says the full past-due amount is too much "
        "or they cannot afford that specific dollar amount. A customer who names a different date, says 'not "
        "today', or says they cannot pay right now without rejecting the amount is making a DATE objection, "
        "not an amount refusal — handle it with the date rules (rules 7-11). "
        "When the floor IS triggered: offer the floor once and say that the rest stays overdue. Do NOT offer "
        "the floor a second time for the same product, even if the customer raises another objection. If after "
        "the floor offer the customer asks 'how much', refer back to the minimum you already stated; do not "
        "re-state it as a new offer. If they refuse the floor as well, follow the refusal-attempt rules.",
        "6. If the customer offers an amount between the floor and the full amount, both included, accept it. "
        "Below the floor it cannot be recorded: say so and offer the floor once.",
        "7. Dates: use only the dates listed under each product in ACCOUNT FACTS. A promise date must be today or "
        "later, a business day, and not after that product's deadline.",
        "8. If the customer names a weekend day or a bank holiday that is on or before the deadline, offer the "
        "next allowed business day from the list.",
        "9. If the customer names a date after the deadline, say that the latest date you can record is the "
        "deadline, say the date out loud, and offer it.",
        "10. If the customer gives a vague period such as \"next week\", \"end of the month\" or \"when my salary "
        "comes\", ask once for the exact day. If they still give no exact day, propose the deadline.",
        "11. " + (policy.vague_period_rule or
                   "\"next <weekday>\" means that weekday in the following calendar week; \"this <weekday>\" "
                   "means the one in the current week."),
        "12. A date or an amount the customer proposes is never a refusal. Handle it with the amount and date "
        "rules instead of using a persuasion attempt. 'I cannot pay today' paired with a proposed date is a "
        "date proposal, not a flat refusal.",
        "13. A flat refusal counts as one persuasion attempt: a plain 'no' to paying, with no date and no amount. "
        "HARD LIMIT: before making any offer for a product, read attempts_allowed and attempts_used from "
        "COLLECTION STATE. If attempts_used >= attempts_allowed, do NOT make another offer — record the negative "
        "disposition immediately. Never exceed attempts_allowed for any product. A customer saying they cannot "
        "pay the full amount is not yet refusing the floor. Do not transfer them just because they cannot pay "
        "today or the full amount.",
        "14. When the attempts are used up (attempts_used >= attempts_allowed), stop. "
        "Do not repeat an offer the customer has already refused.",
        "15. If the customer says they already paid this amount, say \"" + quote(policy.already_paid_sentence) + "\", "
        "take no promise, and CALL " + (paid_close["function"]["name"] if paid_close else "the paid close tool") + ".",
        "16. If the customer says the debt is not theirs or that they never had the product, do not argue, take "
        "no promise, and CALL "
        + (dispute_close["function"]["name"] if dispute_close else "the dispute close tool") + ".",
        "17. A request for a new payment plan, a different instalment amount or to pay less each month: do not "
        "describe any plan, do not name an amount, and follow the brief's transfer/close action. This means a "
        "request to change future recurring payments, spread the debt over time or lower monthly instalments. "
        "Accepting the offered full amount, floor, another allowed amount, or a payment date is a promise proposal, "
        "not restructuring. Do not transfer for an amount/date proposal.",
        "18. A request for a person: ask once whether you can help with the payment. The call moves on by itself "
        "from there.",
        "19. Illness, a death in the family, job loss or hardship comes before the attempt rules: say one short "
        "sentence of sympathy, make no persuasion attempt, and offer the support team once. If they accept, "
        "transfer to support; if they decline, close as hardship. If they themselves give a date or amount, "
        "handle it as a promise. Do not ask them to pay or make another offer after hardship is disclosed.",
        "20. A question you cannot answer from the tools: say you do not have that information.",
        "21. Never offer a discount, a waiver, an instalment plan or any term that is not in these facts.",
        "22. " + ("Only after every product has been discussed and all of its allowed attempts are used or it has "
                   "a clear customer-proposed promise, read back all promises together and ask one \"Do you "
                   "confirm?\" Do not ask for confirmation or record any promise early. Then wait for the answer." 
                   if multi else
                   "When you have a product, an amount and a date the customer agreed: read all three back and ask "
                   "\"Do you confirm?\" Then say nothing else and wait for the answer. Do not call a tool in that message."),
        "23. One tool call per turn. After every tool result, say something to the customer. Never end a turn in "
        "silence. Do not call a tool that this step does not offer.",
    ]
    if multi:
        rules += [
            "24. Before speaking about payment, silently read COLLECTION PLAN and use it as this call's checklist. "
            "It was computed from this brief and the current account response; do not replace its order, dates, "
            "amounts, deadlines or attempt counts with remembered values. The first question is about all products. "
            "After that answer, handle exactly ONE product at a time in the listed order. Never offer amounts for "
            "multiple products in one turn. State the current product's offer, wait for the answer, and resolve it "
            "before moving to the next product.",
            "25. Count attempts separately for each product using COLLECTION STATE. "
            "HARD LIMIT: before making any offer for a product, check attempts_allowed and attempts_used in its "
            "COLLECTION STATE entry. If attempts_used >= attempts_allowed, do NOT make another offer for that "
            "product — move immediately to its required negative disposition (note or close). This is absolute: "
            "no matter how the customer responds, never exceed the attempts_allowed for any product. "
            "After a refusal, only if attempts_used < attempts_allowed: make the next listed offer exactly once. "
            "Do not mark the product resolved or move to the next product until its listed attempts are used OR "
            "the customer gives a valid promise, or a brief-defined exception applies. Never let one product's "
            "refusal end collection for the others. A clear request to end the call or a required transfer is the "
            "only reason to stop the product sequence early. If one customer message promises one product and "
            "refuses another, resolve the promised product's terms, then continue with the refused product's next "
            "required attempt (only if attempts_used < attempts_allowed); do not jump to read-back or notes.",
            "26. If the customer names one date for every product and it is after one product's deadline, explain "
            "before confirmation that this product's latest allowed date is its own deadline. Keep the customer's "
            "date for products where it is allowed. In the final read-back, say each product's actual date "
            "separately; never group products under a date that is not valid for all of them.",
            "27. Read every promise back together and ask for one confirmation. A dispute or already-paid claim "
            "about one product is a negative disposition for that product only: do not close the call for it; retain "
            "the disposition for the required note and carry on with the remaining products.",
            "28. COLLECTION PLAN, precomputed from this brief and get_account: \n{{collection_plan_json}}\n"
            "Track the current product, its attempts and its disposition from the conversation. For every product, "
            "the final ledger needs exactly one confirmed create_promise OR the brief-required negative record. "
            "Before close_call, verify every product is recorded. Never claim a record succeeded unless its tool "
            "returned success. Honor a clear customer request to end the call immediately.",
        ]

    if multi:
        rules.append(
            "29. PRODUCT STATE tracks conversation offers; BANK LEDGER STATE tracks successful bank writes. "
            "Before deciding what to do next, read the product index and offer-state snapshot. Skip entries marked "
            "not_present. Preserve offer history and count only brief-defined persuasion attempts. Never claim a "
            "bank write succeeded based on the offer snapshot. Only the ledger snapshot is authoritative for "
            "recorded promises or notes. The workflow updates ledger state from successful tool responses and "
            "permits close_call only after every present product has a successful required record.\n"
            "CURRENT PRODUCT INDEX: {{collection_active_product}}\n"
            "OFFER STATE JSON: {{collection_state_json}}\n"
            "BANK LEDGER STATE JSON: {{collection_ledger_json}}\n"
            "CLOSE READY: {{collection_close_ready}}"
        )

    confirmed_schema = ([{
        "name": "confirmed_promises_json",
        "type": "string",
        "description": (
            "After the customer clearly confirms the immediately preceding final read-back, return a compact JSON "
            "array with each promised product exactly as read back: product_id (exact tool id from ACCOUNT FACTS), "
            "product_type, amount (number), and date (YYYY-MM-DD). Include only confirmed promises. Return [] if "
            "none; never invent a value. Preserve this array on later turns."
        ),
        "min_confidence": 0.6,
    }] if multi else [])

    # In a multi-product call, a refusal/paid/dispute is product-level state.
    # Notes are recorded per product. Put add_note in negotiate so the model can note immediately or before confirmation.
    note_tool = b.tool("add_note") if (multi and policy.note_unpromised) else None
    if note_tool:
        reasons = ", ".join(policy.note_reasons) if policy.note_reasons else ""
        nprops = note_tool["function"]["parameters"]["properties"]["body"]["properties"]
        if "product_id" in nprops:
            nprops["product_id"]["description"] = "The tool id of the product, copied exactly from ACCOUNT FACTS."
        if "reason" in nprops:
            nprops["reason"]["description"] = "Why this product has no promise." + ((" One of: " + reasons + ".") if reasons else "")
            if not nprops["reason"].get("enum") and policy.note_reasons:
                nprops["reason"]["enum"] = policy.note_reasons
        note_tool["function"]["description"] = (
            "Record why one product ended without a promise. CALL it for any product that the customer refuses, "
            "disputes, or says is already paid. Never call it for a product that has a promise."
        )
        note_tool["pre_process_code"] = collection_record_pre_hook("note")
        note_tool["post_process_code"] = note_hook(policy)
        note_tool["config"]["maxRetries"] = 0

    negotiate_tools = [note_tool] if note_tool else ([] if multi else [t for t in [paid_close, dispute_close, refused_close] if t])
    if not negotiate_tools and not multi:
        b.warn("the negotiating step has no closing tool at all")

    nodes["negotiate"] = negotiate = {
        "name": "negotiate",
        "node_type": "conversation",
        "on_enter": {"type": "tts", "text": ph("opening_line")},
        "on_enter_first_entry_only": True,
        "task_message": (
            "You are calling from the collections team about a late payment. The customer has passed the identity "
            "check, so you may now talk about the debt.\n"
            "If the customer's latest message asks whether you are a robot or AI, begin your reply exactly: \"I am "
            + bank + "'s digital assistant.\" Do this even if the message also contains a date, amount, refusal, "
            "or another request. Then handle all other information in that message without asking them to repeat it.\n"
            "A direct question about whether you are AI is not a payment decision: answer it first every time, then "
            "continue with any payment details from the same message. Never ignore that question.\n"
            "ACCOUNT FACTS (computed for you; trust them over anything you remember):\n{{product_facts}}\n"
            "FIRST TURN ONLY: if you have not yet told the customer about the products and asked your first "
            "question, do that now. After that, never repeat the products and never ask the first question again.\n"
            + "\n".join(rules)
        ),
        "extraction_schema": [
            _extract(
                "move",
                ["hardship", "dispute", "paid_claim", "restructuring", "human", "stop_talking", "continue"],
                "What the customer has just said, judged against the rules. 'hardship' only when they mention "
                "illness, a death in the family, job loss, or serious hardship; classify these as hardship even if "
                "the customer also says they cannot pay. Mere inability to pay the full amount or a request to "
                "lower monthly instalments is not hardship. Treat an explicit request for lower instalments as "
                "restructuring. 'dispute' only "
                "when they say the debt is not theirs or they never had the product. 'paid_claim' only when they "
                "say they have already made this payment. 'restructuring' only when they explicitly ask for a new "
                "payment plan, a different instalment amount, to spread the money over more months, or to pay less "
                "each month, including saying they need lower monthly instalments to make payments affordable. "
                "Accepting or proposing a one-time amount or date, including accepting the offered minimum/floor, "
                "is NOT restructuring. "
                "Do NOT classify inability to pay today, inability to pay the full balance, or a "
                "refusal of an offered amount as restructuring unless they explicitly request a new plan or "
                "lower monthly instalments. 'human' only when they ask to speak to a person or an adviser. "
                "'stop_talking' when "
                "they say they do not want to talk or to continue. 'continue' for everything else, including any "
                "date, any amount, 'you tell me', 'I will think about it', or any answer that is not one of the "
                "cases above.",
                0.6,
            ),
            _extract(
                "answer",
                ["agrees", "changes", "other"],
                "The customer's answer to the assistant's immediately preceding FINAL read-back, which listed every "
                "payment product, amount and date and ended with the exact question 'Do you confirm?'. 'agrees' only "
                "when that exact grouped read-back was just asked and the customer clearly confirms it. A yes to an "
                "offer such as 'Can you pay this amount on this date?' is NOT read-back confirmation; classify it "
                "as 'other' so the offer can be processed and, in multi-product calls, the remaining products can "
                "be handled first. 'changes' only after the final grouped read-back when they change a listed value. "
                "'other' otherwise.",
                0.6,
            ),
        ] + _collection_state_schema(policy) + confirmed_schema,
        "tools": negotiate_tools,
        "max_node_turns": 80,
        "max_turn_transition": "close_refused",
        "transitions": [
            _transition("hardship_step", "the customer has hardship",
                        _or(_var("move", "hardship"))),
            *([] if multi else [
                _transition("close_dispute", "the customer disputes the debt", _or(_var("move", "dispute"))),
                _transition("close_paid", "the customer says they already paid", _or(_var("move", "paid_claim"))),
            ]),
            _transition("transfer_restructuring", "the customer asks for another payment plan",
                        _or(_var("move", "restructuring"))),
            _transition("human_step", "the customer asks for a person", _or(_var("move", "human"))),
            _transition("stop_step", "the customer says they do not want to talk",
                        _or(_var("move", "stop_talking"))),
            _transition("confirm_promise", "the customer confirmed the read-back",
                        _or(_var("answer", "agrees"))),
            _transition("negotiate", "stay in the collection conversation", {"operator": "always"}),
        ],
    }
    # If a close tool was called from this step, it already recorded the outcome;
    # go straight to the terminal node instead of entering a second close step.
    for tool in (() if multi else (paid_close, dispute_close, refused_close)):
        if tool:
            negotiate["transitions"].insert(0, _transition(
                "end_call", "the outcome was recorded by the closing tool",
                _close_attempted(tool["function"]["name"][len("close_"):])))

    # hardship: sympathy, no attempt, offer the support team
    nodes["hardship_step"] = {
        "name": "hardship_step",
        "node_type": "conversation",
        "task_message": (
            "The customer has told you about illness, a death in the family, job loss or hardship.\n"
            "Say one short sentence of sympathy, once. Make no persuasion attempt and offer no amount or date.\n"
            "Then offer to connect them with the customer support team, once.\n"
            "- If they accept, CALL " + hardship_tool["function"]["name"] + " now.\n"
            "- If they decline, thank them and CALL "
            + (hardship_close["function"]["name"] if hardship_close else "the hardship close tool") + ".\n"
            "- If they then name a date or an amount themselves, treat that as a promise and let the call move "
            "back to the collection conversation.\n"
            "- Never offer a discount, a waiver, a plan or a lower instalment."
        ),
        "extraction_schema": [_extract(
            "hardship_answer",
            ["accept_support", "decline_support", "names_promise", "other"],
            "The customer's answer to the support offer. 'accept_support' when they agree to be transferred to "
            "the support team. 'decline_support' when they refuse it. 'names_promise' when they instead give a "
            "date or an amount they will pay. 'other' otherwise.",
            0.55,
        )],
        "tools": hardship_tools,
        "max_node_turns": 14,
        "max_turn_transition": "close_hardship",
        "transitions": [
            _transition("close_support", "the customer accepted the support team",
                        _ok(hardship_tool["function"]["name"])),
            _transition("end_call", "the hardship outcome was recorded",
                        _close_attempted("hardship")),
            _transition("close_hardship", "the customer declined the support team",
                        _or(_var("hardship_answer", "decline_support"),
                            *_close_done_conditions("hardship") if hardship_close else [])),
            _transition("negotiate", "the customer named a payment of their own",
                        _or(_var("hardship_answer", "names_promise"))),
            _transition("hardship_step", "keep the sympathy short", {"operator": "always"}),
        ],
    }

    # the customer wants to end the call
    stop_tools = [refused_close] if refused_close else []
    nodes["stop_step"] = {
        "name": "stop_step",
        "node_type": "conversation",
        "task_message": (
            "The customer has said they do not want to talk.\n"
            "Ask once, politely and in one short sentence, whether they would like to continue now.\n"
            "- If they say they still do not want to talk, thank them and CALL "
            + (refused_close["function"]["name"] if refused_close else "the refused close tool") + ".\n"
            "- If they agree to carry on, let the call move back to the collection conversation."
        ),
        "extraction_schema": [_extract(
            "stop_answer",
            ["still_no", "agrees", "other"],
            "'still_no' when they repeat that they do not want to talk. 'agrees' when they agree to continue. "
            "'other' otherwise.",
            0.55,
        )],
        "tools": stop_tools,
        "max_node_turns": 10,
        "max_turn_transition": "close_refused",
        "transitions": [
            _transition("end_call", "the refusal outcome was recorded",
                        _close_attempted("refused")),
            _transition("close_refused", "they repeated that they will not talk",
                        _or(_var("stop_answer", "still_no"),
                            *_close_done_conditions("refused") if refused_close else [])),
            _transition("negotiate", "they agreed to continue", _or(_var("stop_answer", "agrees"))),
            _transition("stop_step", "wait for their answer", {"operator": "always"}),
        ],
    }

    nodes["human_step"] = {
        "name": "human_step",
        "node_type": "conversation",
        "task_message": (
            "The customer is asking to speak to a person.\n"
            "Ask once, in one short sentence, whether you can help them here with the payment. Do not offer a "
            "plan, a discount or a waiver.\n"
            "- If they insist on a person, say you will put them through and let the call move on.\n"
            "- If they agree to deal with you, let the call move back to the collection conversation.\n"
            "- If they want the call to end, thank them and CALL "
            + (refused_close["function"]["name"] if refused_close else "the refused close tool") + "."
        ),
        "extraction_schema": [_extract(
            "human_answer",
            ["insist", "accepts_help", "wants_end"],
            "'insist' when they insist on speaking to a person. 'accepts_help' when they agree to deal with you. "
            "'wants_end' when they want the call to end.",
            0.55,
        )],
        "tools": [refused_close] if refused_close else [],
        "max_node_turns": 12,
        "max_turn_transition": "close_refused",
        "transitions": [
            _transition("end_call", "the refusal outcome was recorded",
                        _close_attempted("refused")),
            _transition("close_refused", "the customer wants the call to end",
                        _or(_var("human_answer", "wants_end"),
                            *_close_done_conditions("refused") if refused_close else [])),
            _transition("transfer_human", "the customer insists on a person", _or(_var("human_answer", "insist"))),
            _transition("negotiate", "the customer agreed to deal with me", _or(_var("human_answer", "accepts_help"))),
            _transition("human_step", "wait for their answer", {"operator": "always"}),
        ],
    }

    for role in ("restructuring", "human"):
        step = b.transfer_step(
            role,
            "The customer asked to be put through to a person for this."
            if role == "restructuring"
            else "The customer insisted on speaking to a person.")
        if step:
            nodes["transfer_" + role] = step

    return {k: v for k, v in nodes.items() if v}


# ----------------------------------------------------------------- recording

def _recording_nodes(b: Builder, policy: PolicySpec) -> Dict[str, Any]:
    create = b.tool("create_promise")
    cprops = create["function"]["parameters"]["properties"]["body"]["properties"]
    hints = {
        "product_id": "The tool id of the product, copied exactly from ACCOUNT FACTS, for example p1. "
                      "Never invent one.",
        "amount": "The amount the customer agreed in the read-back, as a number. It must be the full past-due "
                  "amount, or the floor, or a figure between them that the customer agreed to.",
        "date": "The payment date as YYYY-MM-DD, copied exactly from the allowed dates of that product in "
                "ACCOUNT FACTS. Never use another date.",
    }
    for key, desc in hints.items():
        if key in cprops:
            cprops[key]["description"] = desc
            cprops[key]["default"] = "{{" + key + "}}"
            cprops[key].pop("enum", None)
    create["function"]["description"] = (
        "Record one payment promise. CALL it only after the customer has clearly confirmed the read-back, once "
        "for each product that has a promise. Saying that it is recorded is not enough: you must make this call."
    )
    create["post_process_code"] = create_promise_hook(policy)
    create["pre_process_code"] = collection_record_pre_hook("promise") + create_promise_pre_hook()
    create["config"]["maxRetries"] = 0

    confirm_tools = [create]
    if policy.note_unpromised:
        note = b.tool("add_note")
        nprops = note["function"]["parameters"]["properties"]["body"]["properties"]
        reasons = ", ".join(policy.note_reasons) if policy.note_reasons else ""
        if "product_id" in nprops:
            nprops["product_id"]["description"] = (
                "The tool id of the product that has no promise, copied exactly from ACCOUNT FACTS.")
            nprops["product_id"]["default"] = "{{product_id}}"
        if "reason" in nprops:
            nprops["reason"]["description"] = "Why this product has no promise." + ((" One of: " + reasons + ".") if reasons else "")
            if not nprops["reason"].get("enum") and policy.note_reasons:
                nprops["reason"]["enum"] = policy.note_reasons
        note["function"]["description"] = (
            "Record why one product ended without a promise. CALL it once for every product that has no promise, "
            "after the read-back and before the call is closed. Never call it for a product that has a promise.")
        note["pre_process_code"] = collection_record_pre_hook("note")
        note["post_process_code"] = note_hook(policy)
        note["config"]["maxRetries"] = 0
        confirm_tools.append(note)

    promise_close = b.close_tool("promise", CLOSE_WHEN["promise"])
    service_close = b.close_tool("service_error", CLOSE_WHEN["service_error"])
    multi = (policy.component_enabled("multi_product_sequence", policy.multi_product)
             and policy.max_products > 1)
    confirmed_schema = ([{
        "name": "confirmed_promises_json",
        "type": "string",
        "description": (
            "After the customer clearly confirms the immediately preceding final read-back, return a compact JSON "
            "array with each promised product exactly as read back: product_id (exact tool id from ACCOUNT FACTS), "
            "product_type, amount (number), and date (YYYY-MM-DD). Include only confirmed promises. Return [] if "
            "none; never invent a value. Preserve this array on later turns."
        ),
        "min_confidence": 0.6,
    }] if multi else [])
    if not multi:
        # One product: the goodbye is the engine's job, so the model only records.
        confirm_tools += [t for t in [service_close] if t]

    notes_rule = ""
    if policy.note_unpromised:
        reasons = ", ".join(policy.note_reasons) if policy.note_reasons else ""
        notes_rule = (
            "- For every product that has no promise, CALL add_note once with that product's tool id and a reason "
            "from: " + reasons + ". Do that for every such product, even when no product got a promise.\n"
            "- On any transfer, add no notes at all.\n"
            "- If one create_promise fails twice, keep the promises that were recorded and CALL add_note with "
            "the service error reason for that product.\n"
        )

    sms = policy.sms_sentence or "You will receive a text message confirming this promise."
    confirm: Dict[str, Any] = {
        "name": "confirm_promise",
        "node_type": "conversation",
        "task_message": (
            "The customer has just said yes to the read-back of the product, the amount and the date.\n"
            + ("MULTI-PRODUCT EXECUTION ORDER (follow this exact sequence, one tool call per turn):\n"
               "The exact confirmed read-back values are in {{confirmed_promises_json}}. Use that list for each "
               "create_promise call; do not reconstruct amounts or dates from memory. The tool hook resolves product "
               "aliases to account ids and applies these confirmed values.\n"
               "STEP 1: For each product that has a confirmed promise, CALL create_promise once with its product_id, "
               "agreed amount and agreed date. Do NOT ask for confirmation again; the customer already confirmed.\n"
               "STEP 2: " + notes_rule.strip() + "\n"
               "STEP 3: Only after EVERY promised product has create_promise AND every unpromised product has add_note, "
               "say \"" + quote(sms) + "\" once and thank the customer. Do not call close_call yourself; the "
               "workflow closes only after its deterministic bank-ledger tracker confirms every required write.\n"
               if multi else
               "- CALL create_promise once with that product's tool id, the amount you read back and the date you read back.\n"
               + "- If create_promise comes back ok, do not repeat the SMS sentence or say goodbye in this step. "
               "The closing step says the SMS and goodbye exactly once.\n")
            + ("- If create_promise fails twice on one product: keep the promises that succeeded, CALL add_note with "
               "reason 'service_error' for that product, and continue with the remaining products. Do not call "
               "close_call yourself; the workflow selects the close only after all required records are done.\n"
               if multi else
               "- If create_promise fails, say nothing that was recorded and CALL it once more with the same values. "
               "If it fails a second time, say you could not record it and CALL "
               + (service_close["function"]["name"] if service_close else "the service error close tool") + ".\n")
            + "- If the customer changes the date or the amount, check the new value against the allowed dates and "
            "amounts in ACCOUNT FACTS, read it back again and ask \"Do you confirm?\" once more.\n"
            "- A recorded promise cannot be changed or cancelled in this same call. If they ask, say they can "
            "change it in the mobile app.\n"
            + "- One tool call per turn, and say something to the customer after every result.\n"
            + ("OFFER STATE JSON: {{collection_state_json}}\n"
               "BANK LEDGER STATE JSON: {{collection_ledger_json}}\n"
               "CLOSE READY (workflow-maintained): {{collection_close_ready}}\n"
               if multi else "")
            + "Never claim a record succeeded unless its tool returned success."
        ),
        "extraction_schema": [_extract(
            "answer",
            ["yes", "change", "no"],
            "The customer's answer to \"Do you confirm?\". 'yes' when they clearly agree to the product, the "
            "amount and the date you read back. 'change' when they want to change the date, the amount or the "
            "product. 'no' when they do not agree.",
            0.6,
        )] + _collection_state_schema(policy) + confirmed_schema,
        "tools": confirm_tools,
        "max_node_turns": 40,
        "max_turn_transition": "end_close_error" if multi else "close_service_error",
        "transitions": [],
    }

    nodes: Dict[str, Any] = {}
    if not multi:
        confirm["transitions"].append(
            _transition("close_promise", "the promise was recorded", _ok("create_promise")))
    else:
        for role in SITUATIONS:
            if b.closes.get(role):
                confirm["transitions"].insert(0, _transition(
                    "close_" + role,
                    "all required product writes succeeded and the brief-derived outcome was selected",
                    {"logic": "and", "conditions": [
                        _var("collection_close_ready", "yes"),
                        _var("collection_close_role", role),
                    ]},
                ))
    confirm["transitions"] += ([] if multi else [
        _transition("end_call", "the promise outcome was recorded by the closing tool",
                    _close_attempted("promise") if promise_close else _or(_var("never", "x"))),
        _transition("end_call", "the service-error outcome was recorded by the closing tool",
                    _close_attempted("service_error") if service_close else _or(_var("never", "x"))),
    ]) + [
        _transition("negotiate", "the customer wants to change the details",
                    _or(_var("answer", "change"), _var("answer", "no"))),
        _transition("confirm_promise", "stay here until the promise is recorded", {"operator": "always"}),
    ]
    nodes["confirm_promise"] = confirm

    return nodes


# ---------------------------------------------------------------------- main

def build_workflow(policy: PolicySpec, openapi_path: str = "tools/openapi.json") -> Dict[str, Any]:
    b = Builder(policy, openapi_path)
    bank = policy.bank_name

    recording = ", and we keep a recording of this call" if policy.recording_disclosure else ""
    greeting_line = ("Hello, this is the digital assistant calling from " + bank + recording + "."
                     + " Am I speaking with {{title}} {{last_name}}?")

    system_prompt = (
        "You are the digital assistant of " + bank + ". You are speaking to a customer on a phone call. "
        "Speak English. One or two short sentences per turn.\n"
        "If asked whether you are a robot or AI, your response must begin with this direct answer: \"I am " + bank
        + "'s digital assistant.\" Do this even when the same message also contains a payment date, amount or "
        "another request. Then continue from the exact point in the conversation; do not ignore the question or restart.\n"
        "The date of this call is {{today_date}} ({{today_long}}), taken from the call variable now = {{now}}. "
        "Every date rule uses that date. Never use the real calendar date.\n"
        "When a step tells you to call a tool, call the tool. Never describe a call in words instead of making it.\n"
        "After every tool result, say something to the customer or make the next tool call. Never end a turn in "
        "silence.\n"
        "Never say that something has been recorded, booked or done unless the tool result says it succeeded.\n"
        "If you do not know something, say you do not have that information. Never invent an amount, a date or a "
        "reason.\n"
        "Speak the numbers and dates the customer said. If they gave a number in two parts or corrected "
        "themselves, use the value they finally settled on.\n"
        "CALL ENDING CONDITIONS: A call may end only after a brief-defined ending condition has been reached. "
        "Examples are: identity cannot be verified or is declined after the allowed attempts; the number is "
        "wrong, the customer is deceased, or the allowed callback cannot be booked; the customer explicitly "
        "chooses to stop after the required check; an outcome such as promise, refusal, dispute, paid, hardship, "
        "or service error has been recorded as the brief requires; or a required transfer has succeeded. For a "
        "multi-product call, do not end while any required product still needs its promise or negative note, "
        "unless a brief rule says a transfer ends the call. Do not call close_call because of silence, confusion, "
        "or to finish a turn. Follow the workflow's selected close route and call its fixed close_call exactly "
        "once. The terminal then speaks the final farewell, opens a two-second window for a short customer "
        "goodbye, and hangs up. Do not speak after that terminal message or repeat a farewell. If close_call "
        "does not confirm success, never claim that the outcome was recorded."
    )
    nodes: Dict[str, Any] = {}

    # The brief's first question. The code node puts it at the end of the opening
    # sentence, so the first thing the customer hears is right without a model call.
    first_question = policy.first_question
    if policy.component_enabled("multi_product_sequence", policy.multi_product):
        first_question = first_question.replace("this payment", "all of them")

    nodes["setup"] = {
        "name": "setup",
        "node_type": "code",
        "passthrough": True,
        "passthrough_default_transition": "greeting",
        "on_enter": {"type": "silent"},
        "pre_actions": [code_exec(setup_code(policy))],
        "transitions": [_transition("greeting", "the date is known", {"operator": "always"})],
    }

    nodes["greeting"] = {
        "name": "greeting",
        "node_type": "conversation",
        "on_enter": {"type": "tts", "text": greeting_line},
        "on_enter_first_entry_only": True,
        "task_message": (
            "Your only job is the opening: say who you are and find out who is on the line.\n"
            "The customer you are looking for is " + ph("title") + " " + ph("last_name") + ".\n"
            "- Only a clear yes, \"that's me\", \"speaking\", or the customer giving their own name counts as "
            "confirmation. \"Hello\", \"go ahead\", \"who is this\" or a question do not.\n"
            "- If the answer is unclear or only polite, ask the very same question once more.\n"
            "- If the caller says they are a robot or an AI, say you are " + bank + "'s digital assistant and "
            "carry on.\n"
            "- If the caller asks whether this is a scam, say they can check by phoning " + bank + "'s customer "
            "service number, and carry on.\n"
            "- Do not say why you are calling. Do not use the words collections, debt, overdue, payment, balance "
            "or the name of a product before the identity check is done.\n"
            "- If it is somebody else on the line, say nothing beyond what you have already said, and stop "
            "there.\n"
            "- Do not call any tool in this step."
        ),
        "extraction_schema": [_extract(
            "who",
            ["customer", "third_party", "unclear"],
            "Who is on the line. 'customer' only when they clearly confirmed they are {{title}} {{last_name}}. "
            "'third_party' when it is a relative, a colleague, a neighbour or anybody else, or when the person "
            "says it is a wrong number or that the customer has died. 'unclear' otherwise.",
            0.55,
        )],
        "max_node_turns": 20,
        "max_turn_transition": "close_unclear",
        "transitions": [
            _transition("identity", "the customer confirmed who they are", _or(_var("who", "customer"))),
            _transition("third_party", "somebody else is on the line", _or(_var("who", "third_party"))),
            _transition("close_unclear", "still unclear after asking twice", _or(_gt("greeting.total_turns", 2))),
            _transition("greeting", "keep asking who is on the line", {"operator": "always"}),
        ],
    }

    cb_pre, cb_post = schedule_callback_hooks(policy)
    callback_tool = b.tool("schedule_callback")
    cprops = callback_tool["function"]["parameters"]["properties"]["body"]["properties"]
    for key, desc in {
        "date": "The day of the callback as YYYY-MM-DD. Copy exactly the date written in CALLBACK RULES.",
        "time": "The time of the callback as HH:MM, inside the window written in CALLBACK RULES.",
    }.items():
        if key in cprops:
            cprops[key]["description"] = desc
            cprops[key]["default"] = "{{" + key + "}}"
    callback_tool["post_process_code"] = cb_post
    callback_tool["pre_process_code"] = cb_pre
    callback_tool["function"]["description"] = (
        "Book a callback for the day and the time the person agreed. CALL it as soon as they give both, and only "
        "if both are inside the window. Never say the callback is booked before the result says ok.")
    callback_tool["config"]["maxRetries"] = 0

    third_party_tools = [callback_tool]
    third_party_closes: Dict[str, str] = {}
    for role in ("wrong_number", "deceased", "no_callback"):
        tool = b.close_tool(role, CLOSE_WHEN[role])
        if tool:
            third_party_tools.append(tool)
            third_party_closes[role] = tool["function"]["name"]

    nodes["third_party"] = {
        "name": "third_party",
        "node_type": "conversation",
        "on_enter": {"type": "tts",
                     "text": "Do you know when " + ph("title") + " " + ph("last_name") + " can be reached?"},
        "on_enter_first_entry_only": True,
        "task_message": (
            ph("title") + " " + ph("last_name") + " is not on the line. You must not say why you are calling.\n"
            "Do not name a product, an amount, a debt or a balance. Do not ask a security question. A relative may "
            "not answer the security questions and may not be told anything about the account, even if they offer "
            "to pay. Say you are not able to discuss the account with anybody else.\n"
            "Ask when " + ph("title") + " " + ph("last_name") + " can be reached.\n"
            "CALLBACK RULES:\n" + ph("date_facts") + "\n"
            "- If the person gives a day and a time inside that window, CALL " + callback_tool["function"]["name"]
            + " with that day and that time, then thank them.\n"
            "- If the time they offer is outside the window, ask once for a time inside it. If the next time they "
            "offer is outside it too, book nothing and CALL "
            + (third_party_closes.get("no_callback") or "the close tool") + ".\n"
            "- If the person says the number does not belong to the customer, thank them and CALL "
            + (third_party_closes.get("wrong_number") or "the close tool") + ".\n"
            "- If the person says the customer has died, say \"" + quote(policy.condolence or "I am sorry for your loss.")
            + "\", ask nothing else, and CALL "
            + (third_party_closes.get("deceased") or "the close tool") + ".\n"
            "Make at most one tool call per turn, and say what happened after the result."
        ),
        "extraction_schema": [_extract(
            "tp",
            ["callback", "no_callback", "wrong_number", "deceased", "other"],
            "What the person on the line told you. 'callback' when they agreed a day and a time inside the "
            "window. 'no_callback' when they will not agree a slot inside the window. 'wrong_number' when they "
            "say the number is not the customer's. 'deceased' when they say the customer has died. 'other' "
            "otherwise.",
            0.55,
        )],
        "tools": third_party_tools,
        "max_node_turns": 20,
        "max_turn_transition": "close_no_callback",
        "transitions": [
            _transition("close_callback", "the callback was booked", _ok("schedule_callback")),
            _transition("end_call", "the wrong-number close tool returned", _close_attempted("wrong_number")),
            _transition("end_call", "the deceased close tool returned", _close_attempted("deceased")),
            _transition("end_call", "the no-callback close tool returned", _close_attempted("no_callback")),
            _transition("close_wrong_number", "the person says it is a wrong number",
                        _or(_var("tp", "wrong_number"))),
            _transition("close_deceased", "the person says the customer has died",
                        _or(_var("tp", "deceased"))),
            _transition("close_no_callback", "no callback was agreed",
                        _or(_var("tp", "no_callback"))),
            _transition("third_party", "keep working on the callback", {"operator": "always"}),
        ],
    }

    verify_tool = b.tool("verify_identity")
    vprops = verify_tool["function"]["parameters"]["properties"]["body"]["properties"]
    field_names = [q.field for q in policy.identity_questions]
    if "field" in vprops:
        vprops["field"]["description"] = ("Which security answer this is. One of: "
                                          + (", ".join(field_names) if field_names else "the values listed above")
                                          + ". Copy it exactly.")
    if "answer" in vprops:
        hints = [q.answer_hint for q in policy.identity_questions if q.answer_hint]
        vprops["answer"]["description"] = "; ".join(hints) or "The customer's answer, written out."
        vprops["answer"]["default"] = "{{answer}}"
    verify_tool["post_process_code"] = verify_identity_hook(policy)
    verify_tool["pre_process_code"] = DATE_ANSWER_PRE_HOOK
    verify_tool["function"]["description"] = (
        "Check one security answer. CALL it once for every answer the customer gives, before asking the next "
        "question, and never for a question that has not been answered.")
    verify_tool["config"]["maxRetries"] = 0

    identity_tools = [verify_tool]
    failed_tool = b.close_tool("failed", CLOSE_WHEN["failed"])
    declined_tool = b.close_tool("declined", CLOSE_WHEN["declined"])
    identity_tools += [t for t in [failed_tool, declined_tool] if t]

    questions = "\n".join("{0}. {1}".format(i + 1, q.question) for i, q in enumerate(policy.identity_questions))
    words_retry = " ".join(q.words_retry for q in policy.identity_questions if q.words_retry)
    identity_transitions = [
        _transition("end_call", "the failed identity close tool returned", _close_attempted("failed")),
        _transition("end_call", "the declined identity close tool returned", _close_attempted("declined")),
        _transition("fetch_account", "every security answer was accepted",
                    _or(_var("verify_identity_response.data.all_verified", "true"),
                        _var("verify_identity_response.data.all_verified", "True"))),
        _transition("close_failed", "no wrong answers are left",
                    _or(_var("verify_identity_response.data.wrong_answers_left", "0"))),
    ]
    identity_transitions.append(_transition("identity", "keep checking the identity",
                                            {"operator": "always"}))

    identity_schema = [_extract(
        "preverify_hardship",
        ["yes", "no"],
        "Sticky call-level flag for the conversation so far: return 'yes' if the customer explicitly mentioned "
        "illness, a death in the family, job loss, or another serious life circumstance at any point, including "
        "while answering security questions or asking why the bank called. Mere inability to pay today, saying a "
        "balance is too much, declining an offer, or asking for lower instalments is NOT hardship unless an "
        "underlying life circumstance is stated. Once yes, keep returning yes on later turns. Return 'no' if no "
        "qualifying circumstance has been mentioned.",
        0.6,
    ), _extract(
        "preverify_ai_question",
        ["yes", "no"],
        "Sticky call-level flag: return 'yes' if the customer asks whether you are a robot, AI, an automated "
        "assistant, or a real person at any point before identity is verified. Preserve yes after later turns. "
        "Return no only if no such question has been asked.",
        0.6,
    )]

    nodes["identity"] = {
        "name": "identity",
        "node_type": "conversation",
        "on_enter": ({"type": "tts", "text": policy.identity_questions[0].question}
                     if policy.identity_questions else {"type": "llm"}),
        "on_enter_first_entry_only": True,
        "task_message": (
            "Your only job now is the security check. Nothing about the account may be said in this step: no "
            "amount, no product, no balance, no reason for the call.\n"
            "If the customer mentions illness, a death in the family, job loss or serious hardship, remember it "
            "without discussing the account. Once identity is verified and the account facts are available, handle "
            "that hardship before asking for any payment.\n"
            "Ask these questions one at a time, in this order:\n" + questions + "\n"
            "CALL " + verify_tool["function"]["name"] + " after every answer, with the field name and the answer, "
            "before you ask the next question. Read the what_to_do_next instruction in the result and do exactly "
            "that.\n"
            "- An accepted answer: ask the next question, or continue when there is none left.\n"
            "- A wrong answer: apologise in one short sentence and ask the same question again.\n"
            + (words_retry + "\n" if words_retry else "")
            + "- A customer who will not answer, or who asks what the call is about instead of answering: explain "
            "once that " + lower_first(policy.declined_advice or "the questions protect their account") + ". "
            "If they decline a second time, or ask that question a second time, CALL "
            + (declined_tool["function"]["name"] if declined_tool else "the close tool") + ".\n"
            "- After the last wrong answer the check is over. Say that they can check this in the mobile app or "
            "in a branch, then CALL " + (failed_tool["function"]["name"] if failed_tool else "the close tool")
            + ". Do not state the debt.\n"
            "- If they ask whether you are a robot, say you are " + bank + "'s digital assistant and carry on.\n"
            "- Make one tool call per turn, and speak to the customer after every result."
        ),
        "extraction_schema": identity_schema,
        "tools": identity_tools,
        "max_node_turns": 30,
        "max_turn_transition": "close_failed",
        "transitions": identity_transitions,
    }

    account_tool = b.tool("get_account")
    account_tool["function"]["description"] = "The late products on the account."
    account_tool["config"]["maxRetries"] = 2
    nodes["fetch_account"] = {
        "name": "fetch_account",
        "node_type": "api_request",
        "passthrough": True,
        "passthrough_default_transition": "product_facts",
        "on_enter": {"type": "silent"},
        "pre_actions": [account_tool],
        "response_mapping": [{"path": "data.products", "variable": "products"}],
        "transitions": [_transition("product_facts", "the account has been read", {"operator": "always"})],
    }

    nodes["product_facts"] = {
        "name": "product_facts",
        "node_type": "code",
        "passthrough": True,
        "passthrough_default_transition": "negotiate",
        "on_enter": {"type": "silent"},
        "pre_actions": [code_exec(product_code(policy, first_question))],
        "transitions": (
            ([_transition("hardship_step", "the customer already disclosed hardship before collection",
                          _var("preverify_hardship", "yes"))]
             if not policy.component_enabled("multi_product_sequence", policy.multi_product) else [])
            + [_transition("negotiate", "the account facts are ready", {"operator": "always"})]
        ),
    }

    nodes.update(_negotiation_nodes(b, policy, bank, first_question))
    nodes.update(_recording_nodes(b, policy))

    for role in SITUATIONS:
        for node in b.close_step(role):
            nodes[node["name"]] = node
    nodes["end_close_error"] = b.close_error_terminal()
    missing = [r for r in SITUATIONS if "close_" + r not in nodes]

    nodes["end_call"] = {
        "name": "end_call",
        "node_type": "end_call",
        "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True,
        "transitions": [],
        "terminal": {
            "type": "function",
            "function": {
                "name": "call_closed",
                "handler": "__function__:end_call",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message_mode": {"type": "string", "default": "custom"},
                        "spoken_message": {"type": "string", "default": "Thank you for your time. Goodbye."},
                        "interruptible": {"type": "boolean", "default": True},
                        "trigger_timeout": {"type": "number", "default": 2},
                        "trigger_pattern_both": {"type": "string", "default":
                            "goodbye|good bye|bye|thanks|thank you|you too|take care|have a good day"},
                    },
                },
            },
        },
    }

    workflow = {
        "initial_node": "setup",
        "system_prompt": system_prompt,
        "nodes": nodes,
        "global_nodes": {},
        "test_variables": {
            "customer_id": "c_test",
            "first_name": "Daniel",
            "last_name": "Reyes",
            "title": "Mr",
            "now": "2026-10-14T11:00",
        },
    }
    workflow["_builder_notes"] = b.notes + ["no closing row for: " + ", ".join(missing)] if missing else b.notes
    return workflow


# --------------------------------------------------------------------- parts

def code_exec(code: str, timeout_ms: int = 20000) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "code_exec",
            "handler": "__function__:code_exec",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "default": code},
                    "timeout_ms": {"type": "number", "default": timeout_ms},
                },
            },
        },
    }


def describe_floors(policy: PolicySpec) -> str:
    """The floor rule, in words the agent can say.

    The figure itself is in ACCOUNT FACTS; the field name the bank uses for it
    is a code name and must never be spoken to a customer.
    """
    if not any(p.floor_field for p in policy.product_types):
        return "Offer the full past-due amount first."
    return ("The lowest amount you may record, the floor, is the figure labelled \"lowest amount you may record\" "
            "under each product in ACCOUNT FACTS. Read it out as a number of dollars, never as a field name. "
            "When you offer the floor, say that the rest stays overdue.")


if __name__ == "__main__":
    from builder.brief_parser import parse_brief
    spec = parse_brief(["briefs/brief-a.md"], "tools/openapi.json")
    wf = build_workflow(spec, "tools/openapi.json")
    print(len(wf["nodes"]), "nodes:", list(wf["nodes"]))
    print("notes:", wf.get("_builder_notes"))
