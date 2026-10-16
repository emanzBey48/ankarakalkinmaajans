import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from builder.main import _bad_placeholder

GOOD = [
    "ok {{title}} {{last_name}} fine",
    "ok {{product_facts}}",
    "ok {{system.conversation.last-10}} and {{now}}",
    "ok {{today_date}} ({{today_long}})",
    "no placeholders at all",
]
BAD = [
    "broken {title} {last_name}",
    'a json blob {"a": 1} in text',
    "trailing } oops",
    "a price of 100 {weird} should still be reported",
]

bad = 0
for text in GOOD:
    got = _bad_placeholder(text)
    if got:
        bad += 1
        print("FAIL", repr(text), "should be clean, reported", repr(got))
for text in BAD:
    got = _bad_placeholder(text)
    if not got:
        bad += 1
        print("FAIL", repr(text), "should have been reported")
print("placeholder check:", "ok" if not bad else f"{bad} failures")
sys.exit(1 if bad else 0)