import pathlib
import re

p = pathlib.Path("builder/brief_parser.py")
s = p.read_text(encoding="utf-8")

start = s.index("def find_quoted(")
end = s.index("def detect_bank_name(")
new = '''def find_quoted(text: str, *keywords: str) -> str:
    """A sentence the brief puts in quotes, or writes as a standalone sentence."""
    flat = flatten(text)
    for q in re.findall(r"\\"([^\\"]{6,200})\\"", flat):
        if all(re.search(k, q, re.IGNORECASE) for k in keywords):
            return q.strip()
    for q in re.findall(r"'([^']{6,200})'", flat):
        if all(re.search(k, q, re.IGNORECASE) for k in keywords):
            return q.strip()
    for raw in sentences(flat):
        if all(re.search(k, raw, re.IGNORECASE) for k in keywords) and 8 < len(raw) < 220:
            return raw
    return ""


def said_clause(text: str, *keywords: str) -> str:
    """The sentence the brief tells the agent to say, without the surrounding rule text."""
    flat = flatten(text)
    quoted = find_quoted(flat, *keywords)
    if quoted and len(quoted) < 200 and not re.search(r"\\bsay\\b|\\bclose\\b|\\btake\\b", quoted, re.IGNORECASE):
        return quoted
    for raw in sentences(flat):
        if not all(re.search(k, raw, re.IGNORECASE) for k in keywords):
            continue
        m = re.search(r"\\bsay(?: that)?\\s+(.{5,180}?\\.)\\s", raw + " ", re.IGNORECASE)
        if m and m.group(1):
            clause = m.group(1).strip()
            clause = re.sub(r"\\s*,?\\s*(?:take no promise|close as|and close).*$", "", clause, flags=re.IGNORECASE).strip()
            clause = clause[0].upper() + clause[1:] if clause else clause
            if clause and not clause.endswith("."):
                clause += "."
            return clause
    return quoted


def clause_after(text: str, trigger: str) -> str:
    """The clause that follows a trigger phrase such as 'explain once that'."""
    for raw in sentences(flatten(text)):
        m = re.search(trigger + r"\\s*(?:that\\s+)?([^.]{10,170}\\.)", raw, re.IGNORECASE)
        if m and m.group(1):
            clause = m.group(1).strip()
            clause = clause[0].upper() + clause[1:] if clause else clause
            return clause
    return ""


# --------------------------------------------------------------------- entry

'''
s = s[:start] + new + s[end:]
p.write_text(s, encoding="utf-8")
print("patched", p)