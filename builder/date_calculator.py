"""Python that runs inside the phone agent's own sandbox.

Two code nodes do the arithmetic the brief needs, so the model is never asked to
count business days:

  * `setup`       runs before the first word is spoken: today, the next business
                  day, the callback window, the two persuasion dates.
  * `product_facts` runs after the account lookup: for every late product the
                  floor, the band, the deadline and every date that may be
                  recorded.

The snippets read `vars`, write `vars`, never raise, and contain no `{{` or `}}`
because the engine may template the code field.
"""

import json
from typing import Any, Dict, List

from builder.policy_spec import PolicySpec
from builder.brief_parser import classify_reason

# Replaced by the generated code. Nothing here writes `{{` or `}}`.
HELPERS = '''
import datetime

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _is_bus_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def _next_bus_day(d):
    c = d + datetime.timedelta(days=1)
    for _ in range(400):
        if _is_bus_day(c):
            return c
        c = c + datetime.timedelta(days=1)
    return c


def _prev_bus_day(d):
    c = d - datetime.timedelta(days=1)
    for _ in range(400):
        if _is_bus_day(c):
            return c
        c = c - datetime.timedelta(days=1)
    return c


def _add_bus_days(d, n):
    c = d
    k = 0
    while k < n:
        c = c + datetime.timedelta(days=1)
        if _is_bus_day(c):
            k = k + 1
    return c


def _add_cal_days(d, n):
    return d + datetime.timedelta(days=n)


def _iso(d):
    return d.isoformat()


def _long(d):
    return DAY_NAMES[d.weekday()] + " " + str(d.day) + " " + MONTH_NAMES[d.month] + " " + str(d.year)


def _short(d):
    return DAY_SHORT[d.weekday()] + " " + str(d.day) + " " + MONTH_NAMES[d.month]


def _money(v):
    return ("%.2f" % float(v))


def _read_now():
    raw = str(vars.get("now") or "").strip()
    day = None
    hour = 0
    minute = 0
    if raw:
        try:
            day = datetime.date.fromisoformat(raw[:10])
        except Exception:
            day = None
        m = re.search(r"[T ](\\d{1,2}):(\\d{2})", raw)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
    if day is None:
        day = datetime.date.today()
        hour = 12
    return day, hour, minute


def _rebuild_products():
    # The engine also writes every tool result into the variable table, flattened
    # with dots: fetch_account_response.data.products.0.past_due. When the
    # response mapping did not run, put the list back together from those keys.
    try:
        keys = [str(k) for k in list(vars.keys())]
        prefixes = dict()
        for name in keys:
            if ".products." in name and name.split(".products.")[1][:1].isdigit():
                prefixes[name.split(".products.")[0]] = True
        for prefix in sorted(prefixes):
            marker = prefix + ".products."
            indexes = dict()
            for name in keys:
                if name.startswith(marker):
                    indexes[name[len(marker):].split(".")[0]] = True
            items = []
            for index in sorted(indexes, key=lambda x: int(x) if str(x).isdigit() else 0):
                start = marker + index + "."
                item = dict()
                for name in keys:
                    if name.startswith(start):
                        item[name[len(start):]] = vars.get(name)
                if item:
                    items.append(item)
            if items:
                return items
        return None
    except Exception:
        return None
'''

# NOTE: the helper above is a plain string, so `\\d` stays a backslash-d escape for
# the regex module. It is injected verbatim into the generated snippet.


def setup_code(policy: PolicySpec) -> str:
    """The snippet that runs before the phone agent says anything."""
    return _render(SETUP_TEMPLATE, {
        "HOLIDAYS": _literal(policy.holidays),
        "BANDS": _literal([
            [b.min_days_late, b.max_days_late, b.business_days, b.calendar_days, b.persuasion_attempts]
            for b in policy.deadline_bands
        ]),
        "START_HOUR": str(policy.callback.start_hour),
        "END_HOUR": str(policy.callback.end_hour),
        "DAYS_RULE": _literal(policy.callback.days_rule),
        "BANK": _literal(policy.bank_name),
    })


def product_code(policy: PolicySpec, first_question: str = "Can you make this payment today?") -> str:
    """The snippet that turns the account lookup into facts the model can quote."""
    display = {p.type_name: p.display_name for p in policy.product_types}
    floors = {p.type_name: p.floor_field for p in policy.product_types}
    return _render(PRODUCT_TEMPLATE, {
        "HOLIDAYS": _literal(policy.holidays),
        "BANDS": _literal([
            [b.min_days_late, b.max_days_late, b.business_days, b.calendar_days, b.persuasion_attempts]
            for b in policy.deadline_bands
        ]),
        "DISPLAY": _literal(display),
        "FLOORS": _literal(floors),
        "DEFAULT_FLOOR": _literal(sorted({f for f in floors.values() if f})[0] if any(floors.values()) else ""),
        "BANK": _literal(policy.bank_name),
        "FIRST_Q": _literal(first_question),
        "NOTE_UNPROMISED": "True" if policy.note_unpromised else "False",
        "MAX_PRODUCTS": str(max(1, policy.max_products)),
        "TRACK_STATE": "True" if policy.component_enabled("multi_product_sequence", policy.multi_product)
                       and policy.max_products > 1 else "False",
    })


SETUP_TEMPLATE = """
import re
import json
__HELPERS__
HOLIDAYS = __HOLIDAYS__
BANDS = __BANDS__
START_HOUR = __START_HOUR__
END_HOUR = __END_HOUR__
DAYS_RULE = __DAYS_RULE__
BANK = __BANK__
try:
    today, now_hour, now_min = _read_now()
    clock = str(now_hour).rjust(2, "0") + ":" + str(now_min).rjust(2, "0")

    vars["today_date"] = _iso(today)
    vars["today_long"] = _long(today)
    vars["today_weekday"] = DAY_NAMES[today.weekday()]
    vars["now_clock"] = clock

    nxt = _next_bus_day(today)
    vars["next_business_day"] = _iso(nxt)

    # last business day of this week, and of the next one
    to_friday = 4 - today.weekday()
    this_fri = today + datetime.timedelta(days=to_friday)
    while not _is_bus_day(this_fri):
        this_fri = this_fri - datetime.timedelta(days=1)
    nxt_to_friday = 4 - today.weekday() + 7
    next_fri = today + datetime.timedelta(days=nxt_to_friday)
    while not _is_bus_day(next_fri):
        next_fri = next_fri - datetime.timedelta(days=1)
    vars["last_business_day_this_week"] = _iso(this_fri)
    vars["last_business_day_next_week"] = _iso(next_fri)

    # persuasion date: this week's last business day, unless today already is it
    attempt1 = next_fri if today == this_fri else this_fri
    vars["attempt1_date"] = _iso(attempt1)

    # callback window: today, or the next business day, inside the hours, later than now
    close_min = END_HOUR * 60
    open_min = START_HOUR * 60
    now_minutes = now_hour * 60 + now_min
    today_ok = _is_bus_day(today) and now_minutes < close_min and END_HOUR > START_HOUR
    first_ok = today if today_ok else nxt
    last_ok = nxt
    vars["callback_date_first"] = _iso(first_ok)
    vars["callback_date_last"] = _iso(last_ok)
    vars["callback_date"] = _iso(last_ok)
    vars["callback_today"] = _iso(today) if today_ok else ""
    vars["callback_time_from"] = clock if today_ok else ("%02d:00" % START_HOUR)
    vars["callback_time_to"] = "%02d:00" % END_HOUR
    vars["callback_window"] = "%02d:00 to %02d:00" % (START_HOUR, END_HOUR)

    lines = []
    lines.append("CALL DATE FACTS")
    lines.append("- Today is " + _long(today) + " and the time is " + clock + ".")
    lines.append("- Today counts as a business day: " + ("yes" if _is_bus_day(today) else "no") + ".")
    lines.append("- Next business day: " + _iso(nxt) + " (" + _short(nxt) + ").")
    lines.append("- Last business day of this week: " + _iso(this_fri) + " (" + _short(this_fri) + ").")
    lines.append("- Last business day of next week: " + _iso(next_fri) + " (" + _short(next_fri) + ").")
    lines.append("- Persuasion date for the first attempt: " + _iso(attempt1) + " (" + _short(attempt1) + ").")
    lines.append("- Bank holidays, never a business day: " + (", ".join(sorted(HOLIDAYS)) if HOLIDAYS else "none") + ".")
    if today_ok:
        lines.append("- A callback may be booked for " + _iso(today) + " (" + _short(today) + ") at any time after "
                     + clock + " and up to " + vars["callback_time_to"] + ", or for " + _iso(last_ok) + " ("
                     + _short(last_ok) + ") between " + ("%02d:00" % START_HOUR) + " and "
                     + vars["callback_time_to"] + ". Anything else is outside the window.")
    else:
        lines.append("- A callback may be booked only for " + _iso(first_ok) + " (" + _short(first_ok)
                     + ") between " + vars["callback_time_from"] + " and " + vars["callback_time_to"] + ".")
        lines.append("- Later today is not possible: it is not a business day or the window has closed.")
    lines.append("- " + __BANK__ + " business days are Monday to Friday, except the holidays listed above.")
    vars["date_facts"] = chr(10).join(lines)
    vars["facts_status"] = "ok"
except Exception:
    vars["today_date"] = str(vars.get("now") or "")[:10]
    vars["date_facts"] = "DATE FACTS UNAVAILABLE. Use only dates the customer names, and never invent one."
    vars["facts_status"] = "error"
"""


PRODUCT_TEMPLATE = """
import re
import json
__HELPERS__
HOLIDAYS = __HOLIDAYS__
BANDS = __BANDS__
DISPLAY = __DISPLAY__
FLOORS = __FLOORS__
DEFAULT_FLOOR = __DEFAULT_FLOOR__
BANK = __BANK__
FIRST_Q = __FIRST_Q__
NOTE_UNPROMISED = __NOTE_UNPROMISED__
MAX_PRODUCTS = __MAX_PRODUCTS__
TRACK_STATE = __TRACK_STATE__
try:
    today, now_hour, now_min = _read_now()
    attempt1_date = str(vars.get("attempt1_date") or _iso(today))

    raw = vars.get("products")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, list) or not raw:
        # The envelope may also be kept whole in one variable.
        for holder in ("fetch_account_response", "get_account_response", "load_account_response"):
            env = vars.get(holder)
            if isinstance(env, dict):
                data = env.get("data")
                if isinstance(data, dict) and isinstance(data.get("products"), list):
                    raw = data.get("products")
                    break
                if isinstance(data, list) and data:
                    raw = data
                    break
            if isinstance(env, list) and env:
                raw = env
                break
    if not isinstance(raw, list) or not raw:
        # The engine also writes every tool result into the variable table,
        # flattened with dots. Rebuild the list from those keys.
        raw = _rebuild_products()

    lines = []
    policy_json = dict()
    products_out = []
    total = len(raw) if isinstance(raw, list) else 0
    index = 0
    for p in (raw if isinstance(raw, list) else []):
        if not isinstance(p, dict):
            continue
        index = index + 1
        pid = str(p.get("product_id") or p.get("id") or ("p" + str(index)))
        ptype = str(p.get("type") or p.get("product_type") or "")
        full = p.get("past_due")
        if full is None:
            full = p.get("past_due_amount")
        if full is None:
            full = p.get("amount_due")
        try:
            full = float(full or 0.0)
        except Exception:
            full = 0.0
        late = p.get("days_late")
        if late is None:
            late = p.get("days_overdue")
        try:
            late = int(late or 0)
        except Exception:
            late = 0
        field = FLOORS.get(ptype) or DEFAULT_FLOOR
        floor = p.get(field) if field else None
        try:
            floor = float(floor)
        except Exception:
            floor = full
        if not floor:
            floor = full

        band = None
        for b in BANDS:
            if b[0] <= late <= b[1]:
                band = b
                break
        if band is None:
            band = BANDS[0] if BANDS else [0, 999, 7, 0, 1]
            if late < band[0]:
                band = BANDS[0]
            else:
                band = max(BANDS, key=lambda b: b[1]) if BANDS else band
        bus_days, cal_days, attempts = int(band[2]), int(band[3]), int(band[4])
        if cal_days and not bus_days:
            deadline = _add_cal_days(today, cal_days)
        else:
            deadline = _add_bus_days(today, bus_days)
        if deadline < today:
            deadline = today

        allowed = []
        blocked = []
        walk = today
        for _ in range(400):
            if walk > deadline:
                break
            if _is_bus_day(walk):
                allowed.append(_iso(walk))
            else:
                why = "weekend" if walk.weekday() > 4 else "bank holiday"
                blocked.append(_iso(walk) + " " + DAY_SHORT[walk.weekday()] + " " + str(walk.day) + " " +
                               MONTH_NAMES[walk.month] + " (" + why + ")")
            walk = walk + datetime.timedelta(days=1)

        name = DISPLAY.get(ptype, ptype.replace("_", " "))
        for key in list(p.keys()):
            if key.endswith("last4") or str(key).endswith("last_4"):
                tail = str(p.get(key) or "").strip()
                if tail:
                    name = name + " ending " + tail
                break

        lines.append("PRODUCT " + str(index) + " of " + str(total) + "  (tool id: " + pid + ")")
        lines.append("  what to call it: " + name)
        lines.append("  past due: " + _money(full) + " dollars, " + str(late) + " days late")
        lines.append("  full past-due amount you may propose: " + _money(full) + " dollars")
        lines.append("  lowest amount you may record (the floor): " + _money(floor) + " dollars")
        lines.append("  latest date you may record (deadline): " + _iso(deadline) + " (" + _short(deadline) + ")")
        lines.append("  dates you MAY record, today to the deadline: " + ", ".join(allowed))
        if blocked:
            lines.append("  dates you may NOT record: " + "; ".join(blocked))
        lines.append("  if the customer names a blocked date that is on or before the deadline: offer the next")
        lines.append("    date from the allowed list after it.")
        lines.append("  if the customer names a date after the deadline: say the latest date you can record is")
        lines.append("    " + _iso(deadline) + " and offer that date.")
        lines.append("  persuasion attempts allowed for this product: " + str(attempts))
        lines.append("    attempt 1: offer the full past-due amount " + _money(full) + " dollars on " +
                     attempt1_date + "; if the customer has already said the full amount is too much, "
                     "offer the floor " + _money(floor) + " dollars on that day instead.")
        if attempts > 1:
            lines.append("    attempt 2: offer the floor " + _money(floor) + " dollars on the deadline " +
                         _iso(deadline) + ".")
        # A compiled, per-call checklist keeps product order and the rules that
        # vary by product visible to the conversation model. The values are
        # derived from the account response and the parsed brief at runtime.
        lines.append("  required handling order: finish this product's allowed attempts before moving to the next product.")
        lines.append("  final ledger requirement: exactly one create_promise after confirmed read-back, or one negative disposition note if no promise is recorded (when the brief/API requires notes).")
        lines.append("")

        policy_json[pid] = {
            "min": _iso(today), "max": _iso(deadline), "allowed": allowed,
            "floor": round(floor, 2), "full": round(full, 2)
        }
        products_out.append({
            "index": index, "product_id": pid, "type": ptype, "name": name,
            "past_due": round(full, 2), "days_late": late, "floor": round(floor, 2),
            "deadline": _iso(deadline), "attempts": attempts, "allowed_dates": allowed,
            "block_dates": blocked,
            "handling_plan": {
                "sequence": index,
                "attempts": attempts,
                "attempt_1": {"amount": round(full if full <= floor else full, 2), "date": attempt1_date},
                "attempt_2": ({"amount": round(floor, 2), "date": _iso(deadline)} if attempts > 1 else None),
                "deadline": _iso(deadline),
                "allowed_dates": allowed,
                "positive_record": "create_promise after the customer confirms the read-back",
                "negative_record": ("add_note once for this product after its attempts, if no promise is recorded"
                                    if NOTE_UNPROMISED else "use the brief's required final close outcome"),
            },
        })

    # Explicit per-product collection state. The conversation may update this
    # snapshot, while these initial values are always derived from the brief
    # and the current account response rather than hard-coded product examples.
    if TRACK_STATE:
        state = []
        by_sequence = {pr["index"]: pr for pr in products_out}
        for sequence in range(1, MAX_PRODUCTS + 1):
            pr = by_sequence.get(sequence)
            state.append({
                "sequence": sequence, "product_id": pr["product_id"] if pr else None,
                "attempts_allowed": pr["attempts"] if pr else 0, "attempts_used": 0,
                "floor_offer_made": False, "offer_history": [],
                "disposition": "pending" if pr else "not_present",
                "ledger_status": "pending" if pr else "not_required",
                "note_reason": None,
            })
        vars["collection_state_json"] = json.dumps(state, separators=(",", ":"))
        vars["collection_ledger_json"] = json.dumps([
            {"sequence": s["sequence"], "product_id": s["product_id"],
             "ledger_status": s["ledger_status"], "disposition": s["disposition"], "note_reason": None}
            for s in state
        ], separators=(",", ":"))
        vars["collection_active_product"] = "1" if products_out else "complete"
        vars["collection_close_ready"] = "no"
        vars["collection_has_promise"] = "no"
        vars["collection_close_role"] = ""

    if products_out:
        # The first thing the agent says is written here rather than left to the
        # model, so the debt is stated from the account and never from memory.
        opening = []
        for pr in products_out:
            opening.append("Your " + pr["name"] + " has " + ("%.2f" % pr["past_due"])
                           + " dollars past due and is " + str(pr["days_late"]) + " days late.")
        direct_ai_answer = ("I am " + BANK + "'s digital assistant. "
                            if str(vars.get("preverify_ai_question") or "").strip().lower() == "yes" else "")
        vars["opening_line"] = direct_ai_answer + "This is a call from " + BANK + " about your late payment. " + " ".join(opening) \
            + " " + FIRST_Q
        vars["product_facts"] = chr(10).join(lines)
        vars["products_json"] = json.dumps(products_out)
        vars["collection_plan_json"] = json.dumps([p["handling_plan"] | {
            "product_id": p["product_id"], "name": p["name"]
        } for p in products_out])
        vars["promise_policy_json"] = json.dumps(policy_json)
        vars["product_count"] = str(len(products_out))
        vars["facts_status"] = "ok"
    else:
        direct_ai_answer = ("I am " + BANK + "'s digital assistant. "
                            if str(vars.get("preverify_ai_question") or "").strip().lower() == "yes" else "")
        vars["opening_line"] = direct_ai_answer + "This is a call from " + BANK + ". I do not have the account details here."
        vars["product_facts"] = "NO ACCOUNT DETAILS AVAILABLE. Say you do not have that information."
        vars["products_json"] = "[]"
        vars["collection_plan_json"] = "[]"
        if TRACK_STATE:
            vars["collection_state_json"] = "[]"
            vars["collection_ledger_json"] = "[]"
            vars["collection_active_product"] = "complete"
            vars["collection_close_ready"] = "no"
            vars["collection_has_promise"] = "no"
            vars["collection_close_role"] = ""
        vars["promise_policy_json"] = "{}"
        vars["product_count"] = "0"
        vars["facts_status"] = "error"
except Exception:
    vars["product_facts"] = "ACCOUNT DETAILS UNAVAILABLE. Say you do not have that information and do not invent figures."
    vars["products_json"] = "[]"
    vars["collection_plan_json"] = "[]"
    if TRACK_STATE:
        vars["collection_state_json"] = "[]"
        vars["collection_ledger_json"] = "[]"
        vars["collection_active_product"] = "complete"
        vars["collection_close_ready"] = "no"
        vars["collection_has_promise"] = "no"
        vars["collection_close_role"] = ""
    vars["promise_policy_json"] = "{}"
    vars["product_count"] = "0"
    vars["facts_status"] = "error"
"""


def _render(template: str, values: Dict[str, str]) -> str:
    out = template.replace("__HELPERS__", HELPERS)
    for key, value in values.items():
        out = out.replace("__" + key + "__", value)
    out = out.strip() + "\n"
    # The engine may resolve placeholders in the code field; a stray {{ }} breaks it.
    out = out.replace("{{", "{ ").replace("}}", " }")
    return out


def _literal(value: Any) -> str:
    return json.dumps(value)


# --------------------------------------------------------------------- hooks

def _load_json(text: str, fallback: Any) -> Any:
    """A tiny helper prepended to the hook snippets."""
    return (
        "def _jload(text, fallback):\n"
        "    try:\n"
        "        import json as _json\n"
        "        v = _json.loads(text)\n"
        "        return v if isinstance(v, type(fallback)) else fallback\n"
        "    except Exception:\n"
        "        return fallback\n"
    )


DATE_ANSWER_PRE_HOOK = (
    "try:\n"
    "    import re as _re\n"
    "    b = config.get('body')\n"
    "    if isinstance(b, dict):\n"
    "        fld = str(b.get('field') or '').lower()\n"
    "        raw = str(b.get('answer') or '').strip()\n"
    "        if raw and ('date' in fld or 'birth' in fld or 'dob' in fld):\n"
    "            if not _re.match(r'^\\d{4}-\\d{2}-\\d{2}$', raw):\n"
    "                mn = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,\n"
    "                      'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}\n"
    "                got = None\n"
    "                m = _re.search(r'(\\d{1,2})\\s*(?:st|nd|rd|th)?\\s*(?:of\\s+)?([A-Za-z]{3,9})\\.?,?\\s*(\\d{2,4})', raw)\n"
    "                if m and m.group(2)[:3].lower() in mn:\n"
    "                    got = (int(m.group(3)), mn[m.group(2)[:3].lower()], int(m.group(1)))\n"
    "                if got is None:\n"
    "                    m = _re.search(r'([A-Za-z]{3,9})\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s*(\\d{2,4})', raw)\n"
    "                    if m and m.group(1)[:3].lower() in mn:\n"
    "                        got = (int(m.group(3)), mn[m.group(1)[:3].lower()], int(m.group(2)))\n"
    "                if got is None:\n"
    "                    m = _re.search(r'\\b(\\d{4})[-/.](\\d{1,2})[-/.](\\d{1,2})\\b', raw)\n"
    "                    if m:\n"
    "                        got = (int(m.group(1)), int(m.group(2)), int(m.group(3)))\n"
    "                if got is None:\n"
    "                    m = _re.search(r'\\b(\\d{1,2})[-/.](\\d{1,2})[-/.](\\d{2,4})\\b', raw)\n"
    "                    if m:\n"
    "                        got = (int(m.group(3)), int(m.group(2)), int(m.group(1)))\n"
    "                if got is not None:\n"
    "                    y, mo, dd = got\n"
    "                    if y < 100:\n"
    "                        y = 2000 + y if y < 30 else 1900 + y\n"
    "                    try:\n"
    "                        import datetime as _dt\n"
    "                        b['answer'] = _dt.date(y, mo, dd).isoformat()\n"
    "                    except Exception:\n"
    "                        pass\n"
    "        elif raw and not _re.match(r'^[\\w\\s-]+$', raw):\n"
    "            b['answer'] = _re.sub(r'[^0-9a-zA-Z\\- ]', '', raw).strip()\n"
    "except Exception:\n"
    "    pass\n"
)


def verify_identity_hook(policy: PolicySpec) -> str:
    """Read the service's answer and tell the model, in one line, what to do next."""
    return (
        "try:\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        d = result.get('data')\n"
        "        if isinstance(d, dict):\n"
        "            nxt = ''\n"
        "            if d.get('all_verified'):\n"
        "                nxt = 'All security questions passed. Move on to the account.'\n"
        "            elif str(d.get('hint') or '') == 'day_month_swapped':\n"
        "                nxt = 'The day and month came back the wrong way round. Ask the customer to say the month as a word, then CALL verify_identity again with the month as a word. Do not count this as a wrong answer.'\n"
        "            elif d.get('verified'):\n"
        "                nxt = 'That answer was accepted. Ask the next security question, or continue if there is none left.'\n"
        "            elif not d.get('ok'):\n"
        "                nxt = 'The check service could not read that answer. Do not count it as a wrong answer. Ask the customer to say it again, more slowly.'\n"
        "            else:\n"
        "                try:\n"
        "                    left = int(d.get('wrong_answers_left') or 0)\n"
        "                except Exception:\n"
        "                    left = 0\n"
        "                if left <= 0:\n"
        "                    nxt = 'That was the last wrong answer. The check is over: do not say the amount, do not say the product, do not read the account out. Tell the customer they can check this in the mobile app or in a branch, then CALL the tool that closes the call as the identity check failed.'\n"
        "                else:\n"
        "                    nxt = 'That answer is wrong. Apologise in one short sentence and ask the same question again. ' + str(left) + ' wrong answers are still allowed before the check ends.'\n"
        "            d['what_to_do_next'] = nxt\n"
        "except Exception:\n"
        "    pass\n"
    )


def create_promise_hook(policy: PolicySpec) -> str:
    """Say plainly what the write did, so the agent never claims a promise that failed."""
    sms = policy.sms_sentence or "You will receive a text message confirming this promise."
    multi = (policy.component_enabled("multi_product_sequence", policy.multi_product)
             and policy.max_products > 1)
    multi_next = (
        "The promise is recorded. Continue with each remaining required product record. Do not call close_call; "
        "the workflow closes only when its ledger tracker confirms every required write."
    ) if multi else (
        "The promise is recorded. Say: \\\"" + sms.replace("\"", "'")
        + "\\\" Then thank the customer and close the call."
    )
    base = (
        "try:\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        d = result.get('data')\n"
        "        if not isinstance(d, dict):\n"
        "            d = dict()\n"
        "            result['data'] = d\n"
        "        if d.get('ok'):\n"
        "            d['what_to_do_next'] = '" + multi_next + "'\n"
        "        else:\n"
        "            d['what_to_do_next'] = 'Recording failed and nothing was saved. Do not say anything was recorded. CALL create_promise once more with the same product, amount and date. If that fails too, say you could not record it and close the call as a service error.'\n"
        "except Exception:\n"
        "    pass\n"
    )
    return base + collection_record_post_hook("promise", _note_reason_roles(policy))


def create_promise_pre_hook() -> str:
    """Before the call goes out: move the date and the amount inside the allowed set.

    The model already read the details back, so this only fires when it slipped.
    Moving a weekend day to the next business day, or a late date to the
    deadline, is what the brief itself asks the agent to do.
    """
    return (
        "try:\n"
        "    import datetime as _dt\n"
        "    _load = vars.get('promise_policy_json')\n"
        "    try:\n"
        "        import json as _json\n"
        "        _pp = _json.loads(_load) if isinstance(_load, str) else dict()\n"
        "    except Exception:\n"
        "        _pp = dict()\n"
        "    b = config.get('body')\n"
        "    if isinstance(b, dict) and isinstance(_pp, dict) and _pp:\n"
        "        pid = str(b.get('product_id') or '').strip()\n"
        "        try:\n"
        "            _products = _json.loads(vars.get('products_json') or '[]')\n"
        "        except Exception:\n"
        "            _products = []\n"
        "        _exact = next((p for p in _products if str(p.get('product_id')) == pid), None)\n"
        "        if _exact is None:\n"
        "            _hint = pid.lower().replace('-', '_').replace(' ', '_')\n"
        "            _matches = [p for p in _products if str(p.get('type', '')).lower() in _hint or\n"
        "                        _hint in str(p.get('name', '')).lower().replace(' ', '_')]\n"
        "            if not _matches and len(_products) == 1:\n"
        "                _matches = _products\n"
        "            if _matches:\n"
        "                _exact = _matches[0]\n"
        "                pid = str(_exact.get('product_id') or pid)\n"
        "                b['product_id'] = pid\n"
        "        _confirmed = []\n"
        "        try:\n"
        "            _confirmed = _json.loads(vars.get('confirmed_promises_json') or '[]')\n"
        "            if not isinstance(_confirmed, list): _confirmed = []\n"
        "        except Exception:\n"
        "            _confirmed = []\n"
        "        _row = next((x for x in _confirmed if isinstance(x, dict) and str(x.get('product_id')) == pid), None)\n"
        "        if _row is None and _exact is not None:\n"
        "            _row = next((x for x in _confirmed if isinstance(x, dict) and\n"
        "                         str(x.get('product_type', '')).lower() == str(_exact.get('type', '')).lower()), None)\n"
        "        if _row is None and len(_confirmed) == 1:\n"
        "            _row = _confirmed[0]\n"
        "        if isinstance(_row, dict):\n"
        "            if _row.get('amount') is not None: b['amount'] = _row.get('amount')\n"
        "            if _row.get('date'): b['date'] = _row.get('date')\n"
        "        try:\n"
        "            vars['collection_pending_record'] = _json.dumps({'kind':'promise','product_id':pid})\n"
        "        except Exception:\n"
        "            pass\n"
        "        rules = _pp.get(pid) or list(_pp.values())[0] or dict()\n"
        "        allowed = list(rules.get('allowed') or [])\n"
        "        if allowed:\n"
        "            raw = str(b.get('date') or '').strip()\n"
        "            day = None\n"
        "            m = __import__('re').search(r'(\\d{4})-(\\d{2})-(\\d{2})', raw)\n"
        "            if m:\n"
        "                try:\n"
        "                    day = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))\n"
        "                except Exception:\n"
        "                    day = None\n"
        "            if day is None:\n"
        "                try:\n"
        "                    day = _dt.date.fromisoformat(raw[:10])\n"
        "                except Exception:\n"
        "                    day = None\n"
        "            if day is None:\n"
        "                day = _dt.date.fromisoformat(str(rules.get('max'))[:10])\n"
        "            else:\n"
        "                iso = day.isoformat()\n"
        "                if iso not in allowed:\n"
        "                    later = [a for a in allowed if a >= iso]\n"
        "                    day = _dt.date.fromisoformat(later[0] if later else allowed[-1])\n"
        "            b['date'] = day.isoformat()\n"
        "            try:\n"
        "                amt = float(b.get('amount'))\n"
        "            except Exception:\n"
        "                amt = None\n"
        "            if amt is None:\n"
        "                amt = rules.get('floor')\n"
        "            if amt is not None:\n"
        "                lo = float(rules.get('floor') or 0.0)\n"
        "                hi = float(rules.get('full') or 0.0)\n"
        "                if lo and amt < lo:\n"
        "                    amt = lo\n"
        "                if hi and amt > hi:\n"
        "                    amt = hi\n"
        "                b['amount'] = round(amt, 2)\n"
        "except Exception:\n"
        "    pass\n"
    )


def collection_record_pre_hook(kind: str) -> str:
    """Resolve model aliases to the real account product id and remember the pending write."""
    return (
        "try:\n"
        "    import json as _json\n"
        "    b = config.get('body')\n"
        "    if isinstance(b, dict):\n"
        "        try: _products = _json.loads(vars.get('products_json') or '[]')\n"
        "        except Exception: _products = []\n"
        "        pid = str(b.get('product_id') or '').strip()\n"
        "        product = next((p for p in _products if str(p.get('product_id')) == pid), None)\n"
        "        if product is None:\n"
        "            hint = pid.lower().replace('-', '_').replace(' ', '_')\n"
        "            matches = [p for p in _products if str(p.get('type','')).lower() in hint or\n"
        "                       hint in str(p.get('name','')).lower().replace(' ','_')]\n"
        "            if not matches and len(_products) == 1: matches = _products\n"
        "            if not matches:\n"
        "                try: _ledger = _json.loads(vars.get('collection_ledger_json') or '[]')\n"
        "                except Exception: _ledger = []\n"
        "                _pending = [x for x in _ledger if x.get('product_id') and x.get('ledger_status') != 'succeeded']\n"
        "                if _pending:\n"
        "                    product = next((p for p in _products if str(p.get('product_id')) == str(_pending[0].get('product_id'))), None)\n"
        "                    if product: matches = [product]\n"
        "            if matches:\n"
        "                product = matches[0]\n"
        "                pid = str(product.get('product_id') or pid)\n"
        "                b['product_id'] = pid\n"
        "        reason = str(b.get('reason') or '').strip().lower()\n"
        "        vars['collection_pending_record'] = _json.dumps({'kind':'" + kind + "','product_id':pid,'reason':reason})\n"
        "except Exception:\n"
        "    pass\n"
    )


def _note_reason_roles(policy: PolicySpec) -> Dict[str, str]:
    out = {}
    for reason in policy.note_reasons:
        role = classify_reason(str(reason), "no_promise")
        if role and policy.close_for(role):
            out[str(reason)] = role
    for role in ("refused", "dispute", "paid", "hardship", "service_error"):
        close = policy.close_for(role)
        if close and close.reason:
            out[str(close.reason)] = role
        if role in policy.note_reasons:
            out[role] = role
    return out


def collection_record_post_hook(kind: str, reason_roles: Dict[str, str] = None) -> str:
    """Update the per-product ledger tracker from successful tool results only."""
    reason_roles = reason_roles or {}
    return (
        "try:\n"
        "    import json as _json\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        data = result.get('data') if isinstance(result.get('data'), dict) else {}\n"
        "        raw = vars.get('collection_ledger_json') or '[]'\n"
        "        ledger = _json.loads(raw) if isinstance(raw, str) else raw\n"
        "        pending = _json.loads(vars.get('collection_pending_record') or '{}')\n"
        "        pid = str(pending.get('product_id') or '')\n"
        "        entry = next((x for x in ledger if str(x.get('product_id')) == pid), None)\n"
        "        ok = data.get('ok') is True or str(data.get('ok') or '').strip().lower() == 'true'\n"
        "        if entry is not None and ok:\n"
        "            entry['ledger_status'] = 'succeeded'\n"
        "            if '" + kind + "' == 'promise':\n"
        "                entry['disposition'] = 'promise'\n"
        "                entry['note_reason'] = None\n"
        "            else:\n"
        "                entry['disposition'] = 'negative'\n"
        "                entry['note_reason'] = str(pending.get('reason') or '')\n"
        "        vars['collection_ledger_json'] = _json.dumps(ledger, separators=(',', ':'))\n"
        "        present = [x for x in ledger if x.get('product_id')]\n"
        "        ready = bool(present) and all(x.get('ledger_status') == 'succeeded' for x in present)\n"
        "        vars['collection_close_ready'] = 'yes' if ready else 'no'\n"
        "        vars['collection_active_product'] = next((str(x.get('sequence')) for x in present\n"
        "            if x.get('ledger_status') != 'succeeded'), 'complete')\n"
        "        promises = any(x.get('disposition') == 'promise' and x.get('ledger_status') == 'succeeded'\n"
        "                       for x in present)\n"
        "        vars['collection_has_promise'] = 'yes' if promises else 'no'\n"
        "        reasons = {str(x.get('note_reason') or '') for x in present\n"
        "                   if x.get('ledger_status') == 'succeeded' and x.get('disposition') == 'negative'}\n"
        "        reason_roles = " + json.dumps(reason_roles, ensure_ascii=False) + "\n"
        "        if promises: role = 'promise'\n"
        "        elif 'service_error' in reasons: role = reason_roles.get('service_error', 'service_error')\n"
        "        elif len(reasons) == 1: role = reason_roles.get(next(iter(reasons)), 'refused')\n"
        "        else: role = 'refused'\n"
        "        vars['collection_close_role'] = role if ready else ''\n"
        "except Exception:\n"
        "    pass\n"
    )


def schedule_callback_hooks(policy: PolicySpec) -> str:
    """Keep the booked slot inside the brief's window, then say what to do next."""
    pre = (
        "try:\n"
        "    import datetime as _dt\n"
        "    import re as _re\n"
        "    b = config.get('body')\n"
        "    if isinstance(b, dict):\n"
        "        def _day(name, fallback):\n"
        "            try:\n"
        "                return _dt.date.fromisoformat(str(vars.get(name) or fallback)[:10])\n"
        "            except Exception:\n"
        "                return None\n"
        "        today = _day('today_date', None)\n"
        "        first = _day('callback_date_first', None) or today\n"
        "        last = _day('callback_date_last', None) or first\n"
        "        raw = str(b.get('date') or '')\n"
        "        m = _re.search(r'(\\d{4})-(\\d{2})-(\\d{2})', raw)\n"
        "        try:\n"
        "            day = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else _dt.date.fromisoformat(raw[:10])\n"
        "        except Exception:\n"
        "            day = first\n"
        "        if today is not None and first is not None:\n"
        "            if day < first:\n"
        "                day = first\n"
        "            if day > last:\n"
        "                day = last\n"
        "        b['date'] = day.isoformat()\n"
        "        t = str(b.get('time') or '').strip()\n"
        "        mt = _re.search(r'(\\d{1,2}):?(\\d{2})', t)\n"
        "        try:\n"
        "            hh, mm = int(mt.group(1)), int(mt.group(2)) if mt else (0, 0)\n"
        "        except Exception:\n"
        "            hh, mm = 0, 0\n"
        "        hi_h = int(str(vars.get('callback_time_to') or '19:00')[:2])\n"
        "        lo_h = int(str(vars.get('callback_window') or '10:00 to 19:00')[:2])\n"
        "        if today is not None and day == today:\n"
        "            nm = _re.search(r'(\\d{1,2}):(\\d{2})', str(vars.get('now_clock') or ''))\n"
        "            if nm:\n"
        "                nh = int(nm.group(1))\n"
        "                if hh < nh or (hh == nh and mm < int(nm.group(2))):\n"
        "                    hh, mm = nh + 1, 0\n"
        "        if hh < lo_h:\n"
        "            hh, mm = lo_h, 0\n"
        "        if hh > hi_h:\n"
        "            hh, mm = hi_h, 0\n"
        "        b['time'] = '%02d:%02d' % (hh, mm)\n"
        "except Exception:\n"
        "    pass\n"
    )
    post = (
        "try:\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        d = result.get('data')\n"
        "        if isinstance(d, dict):\n"
        "            if d.get('ok'):\n"
        "                d['what_to_do_next'] = 'The callback is booked for ' + str(d.get('date') or '') + ' at ' + str(d.get('time') or '') + '. Thank the person and close the call as not reached, reason callback. Say nothing about the account.'\n"
        "            else:\n"
        "                d['what_to_do_next'] = 'The callback was NOT booked. Do not tell the person it is booked. Try once more with the same slot, or close the call as not reached with reason no_callback.'\n"
        "except Exception:\n"
        "    pass\n"
    )
    return pre, post


def transfer_hook(policy: PolicySpec) -> str:
    return (
        "try:\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        d = result.get('data')\n"
        "        if isinstance(d, dict):\n"
        "            if d.get('ok'):\n"
        "                d['what_to_do_next'] = 'The transfer was made. Tell the customer they are being transferred, and close the call with the matching transferred close tool. Do not add any note.'\n"
        "            else:\n"
        "                d['what_to_do_next'] = 'The transfer did NOT happen. Do not say they were transferred. Say you could not connect them and offer to help them here instead.'\n"
        "except Exception:\n"
        "    pass\n"
    )


def note_hook(policy: PolicySpec = None) -> str:
    base = (
        "try:\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        d = result.get('data')\n"
        "        if isinstance(d, dict):\n"
        "            if d.get('ok'):\n"
        "                d['what_to_do_next'] = 'Note recorded. Carry on with the remaining products.'\n"
        "            else:\n"
        "                d['what_to_do_next'] = 'The note was NOT recorded. Try once more with the same product and reason.'\n"
        "except Exception:\n"
        "    pass\n"
    )
    return base + collection_record_post_hook("note", _note_reason_roles(policy) if policy else {})


def close_hook() -> str:
    return (
        "try:\n"
        "    if isinstance(result, dict):\n"
        "        result.pop('header', None)\n"
        "        d = result.get('data')\n"
        "        if isinstance(d, dict):\n"
        "            if d.get('ok'):\n"
        "                d['what_to_do_next'] = 'The call is closed and the outcome is recorded. Say nothing more.'\n"
        "            else:\n"
        "                d['what_to_do_next'] = 'The outcome was NOT recorded. Do not say that it was recorded. End the call now.'\n"
        "except Exception:\n"
        "    pass\n"
    )


def sanitize(code: str) -> str:
    code = code.replace("{{", "{ ").replace("}}", " }")
    return code


def facts_preview(policy: PolicySpec) -> List[str]:
    """A short description of the code the builder will ship, for the trace."""
    return [
        "setup code: " + str(len(setup_code(policy))) + " chars",
        "product code: " + str(len(product_code(policy))) + " chars",
    ]


if __name__ == "__main__":
    from builder.brief_parser import parse_brief
    spec = parse_brief(["briefs/brief-a.md"], "tools/openapi.json")
    print(setup_code(spec))
    print("=" * 60)
    print(product_code(spec))
