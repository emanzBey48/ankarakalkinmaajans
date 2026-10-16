"""Remove `on_enter: silent` from conversation nodes.

A conversation step entered with a silent on_enter never produces a turn: the
agent says nothing, the customer waits and hangs up. Steps entered before the
customer speaks use a fixed tts line; steps entered after the customer speaks
use the default, which lets the model write the reply.
"""
import pathlib
import re

p = pathlib.Path("builder/workflow_builder.py")
s = p.read_text(encoding="utf-8")
before = s.count('"on_enter": {"type": "silent"},')
s = re.sub(r'\n(\s*)"on_enter": \{"type": "silent"\},', '', s)
p.write_text(s, encoding="utf-8")
print("removed", before, "silent on_enter entries;", s.count('"type": "silent"'), "left")