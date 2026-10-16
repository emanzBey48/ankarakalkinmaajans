import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from builder.brief_parser import _closes_from_prose

TEXT = """Every call ends with exactly one close_call.

Close as not_reached / callback after booking the slot.
Close as `not_reached` with reason `no_callback` when nothing is agreed.
Close as `not_verified` / `failed` after three wrong answers.
Close as `no_promise` / dispute when the debt is not theirs.
Close as `transferred` / restructuring for a plan request.
Close as `promise` when a promise was recorded.
Close as `paid`.
"""

ENUM = ["promise", "paid", "transferred", "no_promise", "not_verified", "not_reached"]

for c in _closes_from_prose(TEXT, ENUM):
    print(f"  {c.outcome:<14} / {c.reason or '-':<14} -> {c.role}")