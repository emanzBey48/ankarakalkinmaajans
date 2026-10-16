"""Run the generated sandbox snippets locally, so a syntax or logic error shows up
before the phone agent is published. Nothing here talks to Freya.

    python tools/check_code.py            # every brief in briefs/
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from builder.brief_parser import parse_brief  # noqa: E402
from builder.date_calculator import product_code, setup_code  # noqa: E402

SAMPLE = {
    "now": "2026-10-14T11:00",
    "products": [
        {"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 420.0,
         "days_late": 6, "minimum_payment": 126.0, "instalment_amount": None},
        {"product_id": "p2", "type": "personal_loan", "past_due": 640.0, "days_late": 20,
         "instalment_amount": 213.0},
        {"product_id": "p3", "type": "car_loan", "past_due": 75.0, "days_late": 3,
         "instalment_amount": 25.0},
    ],
    "get_account_response": {"status": "success", "data": {"ok": True}},
}


def run(code, variables):
    scope = {"vars": dict(variables)}
    exec(compile(code, "<generated>", "exec"), scope)  # noqa: S102 - this is our own snippet
    return scope["vars"]


def main():
    briefs = sys.argv[1:] or sorted(str(p) for p in (ROOT / "briefs").glob("*.md"))
    openapi = str(ROOT / "tools" / "openapi.json")
    failed = False
    for brief in briefs:
        policy = parse_brief([brief], openapi)
        print("=" * 70)
        print(brief)
        for now in ("2026-10-14T11:00", "2026-10-16T19:30", "2026-10-23T09:05", "2026-11-02T08:00",
                    "2026-12-24T12:00", "2027-01-02T10:00"):
            variables = dict(SAMPLE, now=now)
            out = run(setup_code(policy), variables)
            out = run(product_code(policy), out)
            if out.get("facts_status") != "ok":
                print("  FAIL on " + now)
                print(out.get("date_facts", ""))
                print(out.get("product_facts", ""))
                failed = True
                continue
            if now == "2026-10-14T11:00":
                print("  now=" + now)
                for line in out["date_facts"].splitlines():
                    print("   ", line)
                for line in out["product_facts"].splitlines():
                    print("   ", line)
                print("    promise_policy:", out["promise_policy_json"][:200])
        print("  all dates ok" if not failed else "  there were failures")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()