"""The account result reaches the code node in three different shapes depending on
which engine path ran. The snippet must find the products in all of them.

    python tests/test_product_shapes.py
"""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from check_code import run
from builder.brief_parser import parse_brief
from builder.date_calculator import product_code, setup_code

POLICY = parse_brief(["briefs/brief-a.md"], "tools/openapi.json")
FIELDS = [
    ("product_id", "p1"), ("type", "credit_card"), ("card_last4", "4821"),
    ("past_due", 420.0), ("days_late", 6), ("minimum_payment", 126.0),
]

SHAPES = {
    "response_mapping gave a list": {
        "products": [dict(FIELDS)],
    },
    "response_mapping gave a json string": {
        "products": '[{"product_id":"p1","type":"credit_card","card_last4":"4821",'
                    '"past_due":420.0,"days_late":6,"minimum_payment":126.0}]',
    },
    "engine envelope held in one variable": {
        "fetch_account_response": {
            "status": "success",
            "data": {"ok": True, "products": [dict(FIELDS)]},
        },
    },
    "flattened keys only": {
        "fetch_account_response.status": "success",
        "fetch_account_response.data.ok": "true",
        "fetch_account_response.data.products.0.product_id": "p1",
        "fetch_account_response.data.products.0.type": "credit_card",
        "fetch_account_response.data.products.0.card_last4": "4821",
        "fetch_account_response.data.products.0.past_due": "420.0",
        "fetch_account_response.data.products.0.days_late": "6",
        "fetch_account_response.data.products.0.minimum_payment": "126.0",
    },
}

bad = 0
for label, variables in SHAPES.items():
    start = run(setup_code(POLICY), {"now": "2026-10-14T11:00"})
    out = run(product_code(POLICY, "Can you make this payment today?"),
              {"now": "2026-10-14T11:00", **variables, **start})
    ok = out.get("facts_status") == "ok" and "4821" in out.get("opening_line", "")
    bad += 0 if ok else 1
    print(("ok   " if ok else "FAIL "), label)
    if not ok:
        print("      facts_status:", out.get("facts_status"))
        print("      opening:", out.get("opening_line"))
        print("      facts  :", out.get("product_facts", "")[:200])
    else:
        print("      ", out["opening_line"])

# A customer with no late products at all must not crash the snippet.
start = run(setup_code(POLICY), {"now": "2026-10-14T11:00"})
out = run(product_code(POLICY, "x"), {"now": "2026-10-14T11:00", "products": [], **start})
print("empty account ->", out.get("facts_status"), "|", out.get("opening_line"))

print("product shape test:", "ok" if not bad else f"{bad} failures")
sys.exit(1 if bad else 0)