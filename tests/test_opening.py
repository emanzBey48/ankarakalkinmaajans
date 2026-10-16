import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from check_code import run, SAMPLE
from builder.brief_parser import parse_brief
from builder.date_calculator import product_code, setup_code

PRODUCTS = SAMPLE["products"]
for briefs, products, label in (
    (["briefs/brief-a.md"], PRODUCTS[:1], "single"),
    (["briefs/brief-a.md", "briefs/brief-b.md"], PRODUCTS, "multi "),
):
    p = parse_brief(briefs, "tools/openapi.json")
    fq = p.first_question
    if p.multi_product:
        fq = fq.replace("this payment", "all of them")
    v = run(setup_code(p), {"now": "2026-10-14T11:00"})
    v = run(product_code(p, fq), {"now": "2026-10-14T11:00", "products": products, **v})
    print(label, "->", v["opening_line"])