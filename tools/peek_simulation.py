"""Look at what the newest simulation actually did, turn by turn.

    python tools/peek_simulation.py [simulation_id]

Prints the newest simulation's runs and, for one of them, the transcript events.
The transcript route is the one that fails while the platform is busy.
"""
import json
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import freya  # noqa: E402


def newest():
    r = freya.api("GET", "/api/v2/simulations", workspaceId=freya.workspace_id())
    items = r.get("simulations") or r.get("items") or r
    return items[0]


def main():
    sim = sys.argv[1] if len(sys.argv) > 1 else newest()
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    sim_id = sim["id"] if isinstance(sim, dict) else sim
    print("simulation", sim_id, (sim.get("status") if isinstance(sim, dict) else ""))
    its = freya.api("GET", "/api/v2/simulations/" + sim_id + "/iterations")
    it_id = (its.get("iterations") or its.get("items") or its)[0]["id"]
    runs = freya.get_iteration(sim_id, it_id)["runs"]
    for r in runs:
        print(" ", r.get("outcome"), "turns=", r.get("turnCount"), (r.get("personaName") or "")[:26])

    which = [r for r in runs if r.get("callId")]
    if only:
        which = [r for r in which if only in (r.get("personaName") or "")]
    if not which:
        return
    call_id = which[0]["callId"]
    print("\ntranscript of", which[0].get("personaName"))
    call = freya.get_call(call_id)
    print("ended:", call.get("endedReason"), "duration:", call.get("duration"))
    for m in call.get("messages", []):
        role, msg = m.get("role"), m.get("message")
        if role == "system":
            try:
                e = json.loads(msg)
                keep = {k: e.get(k) for k in ("event", "node", "from_node", "target", "reason",
                                             "function", "matched", "error", "message") if k in e}
                msg = json.dumps(keep)[:260]
            except Exception:
                msg = str(msg)[:200]
        print("  [" + str(role) + "]", str(msg)[:260])


if __name__ == "__main__":
    main()