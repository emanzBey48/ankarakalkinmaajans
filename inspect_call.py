import freya, json
try:
    res = freya.api('GET', '/api/v2/call/da15b85c-a433-46a8-b955-e4c8c6024e8e')
    call = res.get("call", {})
    msgs = call.get("messages", [])
    print(f"Total messages: {len(msgs)}")
    for m in msgs:
        role = m.get("role")
        msg = m.get("message")
        node = m.get("nodeId")
        sec = m.get("secondsFromStart")
        print(f"[{sec:.1f}s] {role} ({node}): {str(msg)[:160]}")
except Exception as e:
    print("Exception:", e)
