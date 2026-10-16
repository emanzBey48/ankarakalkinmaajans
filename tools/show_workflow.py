import sys, json, pathlib
path = sys.argv[1] if len(sys.argv) > 1 else "out/brief-a.workflow.json"
w = json.load(open(path, encoding="utf-8"))
print(f"{'node':<22} {'type':<14} on_enter")
for name, node in w["nodes"].items():
    enter = json.dumps(node.get("on_enter"))[:58]
    print(f"{name:<22} {str(node.get('node_type')):<14} {enter}")
print()
print("silent conversation nodes:",
      [n for n, v in w["nodes"].items()
       if v.get("node_type") == "conversation" and (v.get("on_enter") or {}).get("type") == "silent"])
print("passthrough nodes without silent on_enter:",
      [n for n, v in w["nodes"].items()
       if v.get("passthrough") and (v.get("on_enter") or {}).get("type") != "silent"])