"""Main Build-the-Builder entrypoint.

    python builder/main.py briefs/brief-a.md
    python builder/main.py briefs/brief-a.md briefs/brief-b.md --test
    python builder/main.py hidden-brief.md --slot hidden --out out/hidden.workflow.json

Reads the brief, compiles a Freya workflow, checks it locally, publishes it and
optionally runs the practice customers against it. One command, no manual step.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import freya  # noqa: E402
from builder.brief_parser import parse_brief  # noqa: E402
from builder.workflow_builder import build_workflow  # noqa: E402


PLACEHOLDER = re.compile(r"\{\{[A-Za-z_][\w.:\-]*\}\}")


def _bad_placeholder(text: str) -> str:
    """A prompt must spell a variable {{like_this}}, never {like_this} or text}."""
    stripped = PLACEHOLDER.sub("", text or "")
    index = min((i for i in (stripped.find("{"), stripped.find("}")) if i >= 0), default=-1)
    if index < 0:
        return ""
    return stripped[index:index + 20]


def check_workflow(workflow):
    """Structural checks that catch the mistakes the platform only reports later."""
    problems = []
    nodes = workflow.get("nodes") or {}
    if workflow.get("initial_node") not in nodes:
        problems.append(f"initial_node {workflow.get('initial_node')!r} is not a node")

    bad = _bad_placeholder(workflow.get("system_prompt", ""))
    if bad:
        problems.append(f"system_prompt has a broken placeholder near {bad!r}")

    def walk(value, path):
        if value is None:
            problems.append(f"null value at {path}")
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")

    walk(workflow, "workflow")

    for name, node in nodes.items():
        node_type = node.get("node_type")
        if node.get("name") != name:
            problems.append(f"node {name} has name {node.get('name')!r}")
        for field in ("task_message", "role_message", "routing_prompt"):
            bad = _bad_placeholder(node.get(field, "") or "")
            if bad:
                problems.append(f"{name}: {field} has a broken placeholder near {bad!r}")
        enter = node.get("on_enter") or {}
        if isinstance(enter, dict) and enter.get("text"):
            bad = _bad_placeholder(enter["text"])
            if bad:
                problems.append(f"{name}: on_enter text has a broken placeholder near {bad!r}")
        if node_type == "conversation" and node.get("pre_actions"):
            problems.append(f"{name} is a conversation node and must not carry pre_actions")
        if node_type == "conversation" and (node.get("on_enter") or {}).get("type") == "silent":
            problems.append(f"{name} enters silently, so the agent never says anything there "
                            "and the customer hangs up waiting")
        if node.get("passthrough") and (node.get("on_enter") or {}).get("type") != "silent":
            problems.append(f"{name} is a passthrough node and needs on_enter silent")
        if node_type != "conversation" and node.get("tools"):
            problems.append(f"{name} is a {node_type} node and must not carry tools")
        if node.get("passthrough"):
            last = (node.get("transitions") or [{}])[-1]
            if last.get("condition", {}).get("operator") != "always":
                problems.append(f"{name} is a passthrough node without a final 'always' transition")
        for tool in node.get("tools") or []:
            fn = tool.get("function") or {}
            bad = _bad_placeholder(fn.get("description", ""))
            if bad:
                problems.append(f"{name}: tool {fn.get('name')} description has a broken placeholder near {bad!r}")
            props = (((fn.get("parameters") or {}).get("properties")) or {})
            body = (props.get("body") or {}).get("properties") or {}
            for field, spec in body.items():
                code = (tool.get("pre_process_code") or "") + (tool.get("post_process_code") or "")
                if field in code and field.startswith("{{"):
                    problems.append(f"{name}: hook reads the placeholder {{{{{field}}}}} literally")
            for field, spec in props.items():
                if not spec.get("default") and field in ("url", "method"):
                    problems.append(f"{name}: {field} of {fn.get('name')} has no default")
        for action in node.get("pre_actions") or []:
            fn = action.get("function") or {}
            if fn.get("handler") == "__function__:code_exec":
                code = ((fn.get("parameters") or {}).get("properties") or {}).get("code", {}).get("default", "")
                if "{{" in code or "}}" in code:
                    problems.append(f"{name}: the code snippet contains a template placeholder")
        for t in node.get("transitions") or []:
            target = t.get("target_node")
            if target not in nodes and target != "__previous__":
                problems.append(f"{name}: transition to unknown node {target!r}")
        for key in ("max_turn_transition", "passthrough_default_transition"):
            target = node.get(key)
            if target and target not in nodes:
                problems.append(f"{name}: {key} points at unknown node {target!r}")
    return problems


def main():
    parser = argparse.ArgumentParser(description="Build a phone agent from brief(s).")
    parser.add_argument("briefs", nargs="+", help="Path(s) to brief markdown/text file(s)")
    parser.add_argument("--openapi", default=str(ROOT / "tools" / "openapi.json"))
    parser.add_argument("--slot", help="Slot name for the workflow (default: derived from the first brief)")
    parser.add_argument("--out", help="Output path for the compiled workflow JSON")
    parser.add_argument("--test", action="store_true", help="Run the practice customers against the new agent")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--style", choices=["pieces", "corrects", "talkative", "asks_back", "in_words"])
    parser.add_argument("--only", help="Case ids, comma separated")
    parser.add_argument("--no-publish", action="store_true", help="Compile and check, but do not publish")
    args = parser.parse_args()

    start_ts = time.time()
    trace_dir = ROOT / "traces"
    trace_dir.mkdir(exist_ok=True)
    trace_file = trace_dir / (time.strftime("%Y%m%d-%H%M%S") + ".txt")
    trace = []

    def log(msg):
        print(msg, flush=True)
        trace.append("[" + time.strftime("%H:%M:%S") + "] " + msg)

    log("=== BUILD STARTED ===")
    log("briefs: " + ", ".join(args.briefs))
    log("openapi: " + args.openapi)
    log("model calls: none. This builder is a compiler: it reads the brief and the tool API "
        "and writes the workflow itself. No language model is used at build time.")

    slot = args.slot or pathlib.Path(args.briefs[0]).stem

    log("step 1: reading the brief")
    policy = parse_brief(args.briefs, args.openapi)
    log("  bank: " + policy.bank_name)
    log("  holidays: " + str(policy.holidays))
    log("  bands: " + ", ".join(f"{b.min_days_late}-{b.max_days_late} {b.band_name}: +{b.business_days} business "
                                f"days, {b.persuasion_attempts} attempts" for b in policy.deadline_bands))
    log("  identity: " + ", ".join(q.field for q in policy.identity_questions)
        + f" (max {policy.max_wrong_answers} wrong, {policy.max_declines} declines)")
    log("  callback: " + policy.callback.window_text() + " on " + policy.callback.days_rule)
    log("  products: " + ", ".join(f"{p.type_name} floor={p.floor_field}" for p in policy.product_types))
    log("  products per customer: " + str(policy.max_products))
    log("  notes for unpromised products: " + str(policy.note_unpromised))
    log("  closing rows: " + ", ".join(f"{c.outcome}/{c.reason or '-'}" for c in policy.closes))
    log("  queues: " + ", ".join(f"{q.role}={q.queue}" for q in policy.queues))

    log("  brief-driven component plan:")
    for component in policy.components:
        rendered = json.dumps(component.value, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > 260:
            rendered = rendered[:257] + "..."
        log(f"    {component.state.upper():10} {component.name}: {rendered or component.reason}")
        if component.evidence:
            log("      evidence: " + component.evidence)

    if policy.diagnostics:
        log("  POLICY EXTRACTION WARNINGS:")
        for diagnostic in policy.diagnostics:
            log("  - " + diagnostic)
        log("step 2: refusing to compile or publish because a critical brief rule was not extracted")
        log("=== BUILD FAILED ===")
        pathlib.Path(trace_file).write_text("\n".join(trace) + "\n", encoding="utf-8")
        sys.exit(1)
    log("  policy extraction check passed: no critical fallbacks detected")

    log("step 2: compiling the workflow")
    workflow = build_workflow(policy, args.openapi)
    for note in workflow.pop("_builder_notes", []):
        log("  note: " + note)
    log("  nodes: " + ", ".join(workflow["nodes"]))
    conversation = [n for n, v in workflow["nodes"].items() if v.get("node_type") == "conversation"]
    code = [n for n, v in workflow["nodes"].items() if v.get("node_type") == "code"]
    log(f"  {len(conversation)} conversation steps, {len(code)} code steps, "
        f"{sum(len(v.get('transitions') or []) for v in workflow['nodes'].values())} routes")
    log("  every close_call outcome and reason is baked into its own tool, so the model cannot "
        "choose the wrong ending")

    problems = check_workflow(workflow)
    if problems:
        log("step 3: LOCAL CHECK FAILED")
        for p in problems:
            log("  - " + p)
        log("=== BUILD FAILED ===")
        pathlib.Path(trace_file).write_text("\n".join(trace) + "\n", encoding="utf-8")
        sys.exit(1)
    log("  local check passed")

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    out_path = pathlib.Path(args.out) if args.out else out_dir / (slot + ".workflow.json")
    out_path.write_text(json.dumps(workflow, indent=1, ensure_ascii=False), encoding="utf-8")
    log("  written to " + str(out_path))

    if args.no_publish or not os.environ.get("FREYA_API_KEY"):
        log("  not published (no key or --no-publish)")
        log("=== BUILD FINISHED in %.1fs ===" % (time.time() - start_ts))
        pathlib.Path(trace_file).write_text("\n".join(trace) + "\n", encoding="utf-8")
        return

    log("step 3: publishing on Freya")
    saved = freya.save_workflow(slot, workflow)
    workflow_id, agent_id = saved["workflow_id"], saved["agent_id"]
    log("  workflow_id=" + workflow_id + " agent_id=" + agent_id + " version=" + str(saved.get("version")))
    export_path = ROOT / (slot + ".exported.workflow.json")
    freya.export_workflow(workflow_id, str(export_path))
    log("  exported to " + str(export_path))

    if args.test:
        brief_letter = "B" if policy.multi_product else "A"
        cmd = [sys.executable, str(ROOT / "tools" / "run_cases.py"), agent_id,
               "--brief", brief_letter, "--out", str(ROOT / "results" / ("run-" + slot + ".json"))]
        if args.reps > 1:
            cmd += ["--reps", str(args.reps)]
        if args.style:
            cmd += ["--style", args.style]
        if args.only:
            cmd += ["--only", args.only]
        log("step 4: testing the phone agent on the practice customers")
        log("  " + " ".join(cmd))
        try:
            code = subprocess.call(cmd, cwd=str(ROOT))
            log("  test run exit code: " + str(code))
        except Exception as exc:  # a failed test must not lose the trace
            log("  test run failed: " + str(exc))

    log("=== BUILD FINISHED in %.1fs ===" % (time.time() - start_ts))
    pathlib.Path(trace_file).write_text("\n".join(trace) + "\n", encoding="utf-8")
    print("trace: " + str(trace_file), flush=True)


if __name__ == "__main__":
    main()
