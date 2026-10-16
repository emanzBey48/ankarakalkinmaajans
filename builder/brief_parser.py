"""Read a bank's brief (markdown or plain text) into a PolicySpec.

The brief can arrive as a clean specification or as raw bank material, so every
extractor has several patterns and a safe fallback. Nothing in this file knows
what a holiday of this bank is: it reads them.
"""

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from builder.policy_spec import (
    CallbackRule,
    DeadlineBand,
    IdentityQuestion,
    OutcomeClose,
    PolicyComponent,
    PolicySpec,
    ProductTypeRule,
    QueueRule,
)

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def to_int(token: str) -> Optional[int]:
    """'3', 'three', '2nd' -> int."""
    if token is None:
        return None
    t = str(token).strip().lower().rstrip(".,;:")
    t = re.sub(r"(st|nd|rd|th)$", "", t)
    if t.isdigit():
        return int(t)
    return WORD_NUMBERS.get(t)


def flatten(text: str) -> str:
    """The same text on one line.

    A brief is wrapped for reading. Any extractor that looks at sentences,
    clauses or quoted sentences works on this copy, so a sentence a human reads
    as one sentence is one string for the program too.
    """
    text = text.replace("\u00a0", " ")
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)          # a word split across lines
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> List[str]:
    flat = flatten(text)
    parts = re.split(r"(?<=[.!?:])\s+(?=[A-Z0-9\"'`(])", flat)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------- date reading

def _iso(y: int, m: int, d: int) -> str:
    try:
        if not (1 <= m <= 12 and 1 <= d <= 31 and 2000 <= y <= 2100):
            return ""
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return ""


def find_dates(text: str) -> List[str]:
    """Every calendar date in the text, in ISO form, in the order written."""
    out: List[str] = []
    # 2026-10-26 / 2026/10/26
    for m in re.finditer(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            out.append(iso)
    # 26 October 2026 / October 26, 2026 / 26 Oct 26
    pattern = re.compile(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})\b"
        r"|\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b"
    )
    for m in pattern.finditer(text):
        if m.group(1):
            d, mon, y = m.group(1), m.group(2), m.group(3)
        else:
            mon, d, y = m.group(4), m.group(5), m.group(6)
        month = MONTHS.get(mon.strip().lower()[:9]) or MONTHS.get(mon.strip().lower()[:3])
        if month:
            iso = _iso(int(y), month, int(d))
            if iso:
                out.append(iso)
    # 10/26/2026 (month first, US style) - only when the first part cannot be a day > 12
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= a <= 12 and 1 <= b <= 31:
            iso = _iso(y, a, b)
            if iso:
                out.append(iso)
    return out


def _section_of(text: str, keyword_re: str) -> str:
    """The markdown section whose heading matches, plus the line it starts on."""
    pattern = re.compile(r"^(#{1,6})[^\n]*(" + keyword_re + r")[^\n]*$", re.IGNORECASE | re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return ""
    level = len(m.group(1))
    rest = text[m.end():]
    nxt = re.search(r"^#{1," + str(level) + r"}\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def parse_holidays(text: str) -> List[str]:
    """Bank holidays: the dates of any holiday/calendar section, plus dates near the word."""
    found: List[str] = []
    section = _section_of(text, r"holiday|calendar|non[- ]?banking|closed|banking days|business days")
    if section:
        found += find_dates(section)
        # a list of bare month names with a day and a year, or "October 26" without a year
        for m in re.finditer(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\b(?!\s*,?\s*20\d{2})", section):
            month = MONTHS.get(m.group(2).lower()[:9]) or MONTHS.get(m.group(2).lower()[:3])
            if month and re.search(r"20\d{2}", section):
                years = [int(y) for y in re.findall(r"\b(20\d{2})\b", section)]
                for y in sorted(set(years)):
                    iso = _iso(y, month, int(m.group(1)))
                    if iso:
                        found.append(iso)
    for m in re.finditer(r"(?:holiday|holidays|non[- ]?banking|closed|banking day|business day)", text, re.IGNORECASE):
        window = text[max(0, m.start() - 300): m.end() + 500]
        found += find_dates(window)
    return sorted(set(found))


# -------------------------------------------------------------------- tables

def parse_tables(text: str) -> List[Tuple[List[str], List[List[str]]]]:
    """Every markdown table as (header cells, data rows)."""
    tables: List[Tuple[List[str], List[List[str]]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1].strip()):
            header = [c.strip() for c in line.strip("|").split("|")]
            rows: List[List[str]] = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            tables.append((header, rows))
            i = j
        else:
            i += 1
    return tables


def _cells_match(header: List[str], *words: str) -> List[int]:
    idx = []
    for n, h in enumerate(header):
        low = h.lower()
        if any(w in low for w in words):
            idx.append(n)
    return idx


# --------------------------------------------------------------------- bands

def parse_deadline_bands(text: str) -> List[DeadlineBand]:
    """The days-late -> (latest date, persuasion attempts) table."""
    bands: List[DeadlineBand] = []
    for header, rows in parse_tables(text):
        late_cols = _cells_match(header, "days_late", "days late", "late for", "band")
        bus_cols = _cells_match(header, "deadline", "latest", "business day", "due")
        att_cols = _cells_match(header, "attempt", "persuasion")
        if not late_cols or not bus_cols:
            continue
        for row in rows:
            band = _band_from_row(row, late_cols, bus_cols, att_cols, header)
            if band:
                bands.append(band)
    if not bands:
        bands = _bands_from_prose(text)
    if not bands:
        bands = [DeadlineBand(0, 999, "default", business_days=7, persuasion_attempts=1)]
    bands.sort(key=lambda b: b.min_days_late)
    return bands


def _band_from_row(row, late_cols, bus_cols, att_cols, header) -> Optional[DeadlineBand]:
    def first(cols):
        for n in cols:
            if n < len(row) and row[n]:
                return row[n]
        return ""

    late_cell = first(late_cols)
    rng = re.search(r"(\d+)\s*(?:to|-|–|—|until|through)\s*(\d+)", late_cell)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
    else:
        one = re.search(r"(\d+)", late_cell)
        if not one:
            return None
        lo = hi = int(one.group(1))
    band = DeadlineBand(lo, hi, "")

    bus_cell = first(bus_cols)
    bd = re.search(r"(\d+)\s*(?:st|nd|rd|th)?\s*(?:business|working)\s*day", bus_cell, re.IGNORECASE)
    if bd:
        band.business_days = int(bd.group(1))
    else:
        cd = re.search(r"(\d+)\s*(?:st|nd|rd|th)?\s*(?:calendar\s+)?day", bus_cell, re.IGNORECASE)
        if cd:
            band.calendar_days = int(cd.group(1))
    if not band.business_days and not band.calendar_days:
        plain = re.match(r"^\s*(\d+)\s*$", bus_cell)
        if plain:
            band.business_days = int(plain.group(1))

    att_cell = first(att_cols) if att_cols else ""
    if not att_cell:
        for cell in row:                      # a lone small integer is the attempt count
            if re.match(r"^\s*(one|two|three|\d)\s*$", cell.strip(), re.IGNORECASE):
                att_cell = cell
                break
    attempts = to_int(re.sub(r"[^A-Za-z0-9]", "", att_cell or "")) or 0
    if not attempts:
        m = re.search(r"(?:up to|allow|make)\s+(\w+)\s+(?:persuasion\s+)?attempt", text_or_empty(bus_cell), re.IGNORECASE)
        attempts = to_int(m.group(1)) if m else 0
    band.persuasion_attempts = max(attempts, 1)

    band_name = ""
    name_cols = _cells_match(header, "band", "name")
    if name_cols:
        for n in name_cols:
            cell = (row[n] if n < len(row) else "").strip().strip("`*_ ")
            if cell and cell.isalpha():
                band_name = cell
                break
    if not band_name:
        for cell in row:
            words = [w for w in re.findall(r"[A-Za-z]+", cell) if w.lower() not in
                     {"to", "the", "a", "an", "of", "day", "days", "business", "after", "today",
                      "band", "persuasion", "attempts", "attempt", "and", "on", "th", "st", "nd", "rd"}]
            if len(words) == 1:
                band_name = words[0].lower()
                break
    if not band_name:
        band_name = "early" if band.min_days_late <= 7 else "late"
    band.band_name = band_name
    return band


def text_or_empty(value: str) -> str:
    return value or ""


def _bands_from_prose(text: str) -> List[DeadlineBand]:
    bands: List[DeadlineBand] = []
    pattern = re.compile(
        r"(\d+)\s*(?:to|-|–|—)\s*(\d+)\s*days?\s*late[^.\n]{0,160}?(\d+)\s*(?:st|nd|rd|th)?\s*(business|calendar)?\s*days?",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        band = DeadlineBand(int(m.group(1)), int(m.group(2)), "")
        if (m.group(4) or "business").lower().startswith("business"):
            band.business_days = int(m.group(3))
        else:
            band.calendar_days = int(m.group(3))
        tail = text[m.end(): m.end() + 120]
        a = re.search(r"(\w+)\s+(?:persuasion\s+)?attempt", tail, re.IGNORECASE)
        band.persuasion_attempts = to_int(a.group(1)) if a else 1
        band.band_name = "early" if band.min_days_late <= 7 else "late"
        bands.append(band)
    for b in bands:
        if not b.band_name:
            b.band_name = f"band_{b.min_days_late}_{b.max_days_late}"
    return bands


# ------------------------------------------------------------------ identity

IDENTITY_HINTS = {
    "date_of_birth": ["date of birth", "birth date", "dob", "day and month you were born",
                      "when you were born"],
    "birth_year": ["year of birth", "year you were born", "birth year"],
    "phone_last4": ["last four digits of the phone", "last four digits of their phone",
                    "last four digits of his phone", "last four digits of her phone",
                    "last four digits of the number", "last four digits of your phone",
                    "phone number on file", "last four digits of the mobile"],
    "card_last4": ["last four digits of the card", "last four digits of the credit card",
                   "last four digits on the card"],
    "postcode": ["postcode", "postal code", "zip code"],
    "company_number": ["company number", "registration number", "company registration"],
    "founded_year": ["year the company was founded", "year it was founded", "founded in"],
    "account_number": ["account number"],
    "surname": ["last name", "surname"],
    "national_insurance": ["national insurance", "ni number"],
    "passport_last4": ["last four digits of the passport", "passport number"],
}


def _type_words(value: str) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", value.lower()) if w]


def match_identity_questions(brief_text: str, field_enum: List[str]) -> List[IdentityQuestion]:
    """Line the brief's security questions up with the field values the API accepts."""
    low = brief_text.lower()
    scored: List[Tuple[int, str]] = []
    for field in field_enum:
        keys = IDENTITY_HINTS.get(field)
        if keys is None:
            words = _type_words(field)
            keys = [" ".join(words), field.replace("_", " ")]
            if len(words) > 1:
                keys += [words[0] + " " + words[-1]]
        best, best_at = None, -1
        for key in keys:
            at = low.find(key)
            if at >= 0 and (best_at < 0 or at < best_at):
                best, best_at = key, at
        if best is not None:
            scored.append((best_at, field))
    scored.sort()
    questions: List[IdentityQuestion] = []
    for _, field in scored:
        questions.append(build_identity_question(brief_text, field))
    if not questions:
        questions = [build_identity_question(brief_text, f) for f in field_enum[:2]]
    return questions


def build_identity_question(brief_text: str, field: str) -> IdentityQuestion:
    """Quote the brief where it names the question, otherwise write a plain one."""
    low = brief_text.lower()
    keys = IDENTITY_HINTS.get(field, [field.replace("_", " ")])
    question = ""
    for key in keys:
        m = re.search(r"(?:ask|what is|what was|confirm|verify|check)[^.\n]{0,90}?" + re.escape(key) + r"[^.?!\n]*\??",
                      low, re.IGNORECASE)
        if m:
            text = brief_text[m.start(): m.end()].strip()
            text = re.sub(r"^(?:then|first|next|also)\s*,?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"^(?:ask|what is|what was|confirm|verify|check)\s+(?:the\s+|them\s+|us\s+)?", "", text, flags=re.IGNORECASE)
            text = text.strip().strip('"').strip("'").strip()
            if text.endswith("?"):
                question = text
                break
    if not question:
        label = field.replace("_", " ")
        if field == "date_of_birth":
            question = "Could you tell me your date of birth?"
        elif field == "phone_last4":
            question = "Could you tell me the last four digits of the phone number on file?"
        else:
            question = f"Could you confirm your {label}?"
    hint = ""
    if "date" in field or "birth" in field:
        hint = "Send the date as YYYY-MM-DD however the customer said it. A date the service cannot read is not a wrong answer."
    elif "last4" in field or "digits" in field:
        hint = "Send the last four digits as digits, for example 1234."
    else:
        hint = f"Send the {field.replace('_', ' ')} exactly as the customer said it."
    words_retry = ""
    if "date" in field or "birth" in field:
        words_retry = "If the service returns hint day_month_swapped, ask the customer to say the month as a word and send it again. That retry is not a wrong answer."
    return IdentityQuestion(field=field, question=question, answer_hint=hint, words_retry=words_retry)


def parse_identity_limits(text: str) -> Tuple[int, int]:
    """(max wrong answers, max declines)."""
    max_wrong = 0
    for m in re.finditer(r"(\w+|\d+)\s+(?:wrong|incorrect)\s+answers?", text, re.IGNORECASE):
        v = to_int(m.group(1))
        if v:
            max_wrong = max(max_wrong, v)
    max_declines = 0
    for m in re.finditer(r"(?:second|twice|2)(?:nd|time|nd time)?[^.\n]{0,60}(?:refus|declin)|"
                         r"(?:refus|declin)[^.\n]{0,40}(?:second|twice)", text, re.IGNORECASE):
        seg = m.group(0).lower()
        v = 2 if re.search(r"\bsecond\b|\btwice\b|\b2\b", seg) else 1
        max_declines = max(max_declines, v)
    if not max_wrong:
        max_wrong = 3
    if not max_declines:
        max_declines = 2
    return max_wrong, max_declines


# ------------------------------------------------------------------ callback

def parse_callback(text: str) -> CallbackRule:
    rule = CallbackRule()
    scoped = _section_of(text, r"callback|callbacks") or ""
    for scope in (scoped, text):
        m = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})\s*(?:to|and|until|through|-|–)\s*(\d{1,2})\s*[:.]\s*(\d{2})", scope)
        if m:
            rule.start_hour, rule.end_hour = int(m.group(1)), int(m.group(3))
            break
    low = text.lower()
    if "next business day" in low and "today" in low:
        rule.days_rule = "today_or_next_business_day"
    elif "next business day" in low:
        rule.days_rule = "next_business_day"
    return rule


# ------------------------------------------------------------------ outcomes

ROLE_KEYWORDS = [
    ("no_callback", ("no callback", "without a callback", "no slot", "no_slot", "not scheduled",
                     "cannot schedule", "no time")),
    ("callback", ("callback", "schedul", "call back", "reach later")),
    ("wrong_number", ("wrong number", "wrong person", "not your number", "wrong contact")),
    ("deceased", ("deceas", "death", "passed away", "bereaved", "died")),
    ("unclear", ("unclear", "unknown", "ambiguous", "no answer", "not sure", "no reply")),
    ("failed", ("fail", "incorrect", "wrong answer", "attempt")),
    ("declined", ("declin", "refuse to answer", "will not answer", "withheld", "withhold")),
    ("support", ("support", "transfer", "welfare")),
    ("hardship", ("hardship", "illness", "hard times")),
    ("dispute", ("disput", "contest", "not mine", "never had")),
    ("service_error", ("service", "error", "technical", "system")),
    ("refused", ("refus", "no promise", "will not pay", "declined to pay", "not paying")),
    ("restructuring", ("restructur", "plan", "instalment", "rearrang", "reschedul")),
    ("human", ("human", "person", "representative", "advisor")),
]

ROLE_FAMILY = {
    "unclear": "not_reached", "wrong_number": "not_reached", "deceased": "not_reached",
    "callback": "not_reached", "no_callback": "not_reached",
    "failed": "not_verified", "declined": "not_verified",
    "refused": "no_promise", "hardship": "no_promise", "dispute": "no_promise",
    "service_error": "no_promise",
    "restructuring": "transferred", "human": "transferred", "support": "transferred",
    "promise": "promise", "paid": "paid",
}

# The groups an outcome falls into, used to read an ambiguous reason word.
FAMILY_ROLES = {
    "not_reached": ["unclear", "wrong_number", "deceased", "callback", "no_callback"],
    "not_verified": ["failed", "declined"],
    "no_promise": ["refused", "hardship", "dispute", "service_error"],
    "transferred": ["restructuring", "human", "support"],
    "promise": ["promise"],
    "paid": ["paid"],
}


def classify_reason(reason: str, family: str = "") -> str:
    """Which situation a row of the closing table stands for.

    A word can be read two ways: "declined" is a customer who will not answer
    the security questions under one kind of outcome and a customer who will not
    pay under another, and "hardship_transfer" is a transfer rather than a
    hardship. The family the brief puts the row under decides.
    """
    low = reason.lower().replace("-", " ").replace("_", " ")
    cands = [role for role, keys in ROLE_KEYWORDS if any(k.replace("_", " ") in low for k in keys)]
    if family:
        same = [r for r in cands if ROLE_FAMILY.get(r) == family]
        if same:
            return same[0]
    if "declined" in cands:
        if family == "not_verified":
            return "declined"
        if family == "no_promise":
            return "refused"
    return cands[0] if cands else ""


def family_of(roles: List[str]) -> str:
    """The group an outcome belongs to: the one most of its reasons fall into."""
    counts: Dict[str, int] = {}
    for role in roles:
        group = ROLE_FAMILY.get(role)
        if group:
            counts[group] = counts.get(group, 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda g: counts[g])


def parse_closes(text: str, outcome_enum: List[str]) -> List[OutcomeClose]:
    """The closing table: which outcome, which reason, and which situation it stands for.

    The outcome names come from the brief when the tool API disagrees, so a brief
    that renames them still produces a workflow that sends the brief's names.
    """
    closes: List[OutcomeClose] = []
    seen = set()
    for header, rows in parse_tables(text):
        out_cols = _cells_match(header, "outcome")
        if not out_cols:
            continue
        reason_cols = _cells_match(header, "reason")
        for row in rows:
            outcome = ""
            for n in out_cols:
                cell = (row[n] if n < len(row) else "").strip().strip("`*").lower()
                if cell and cell not in ("none", "-"):
                    if cell in outcome_enum:
                        outcome = cell
                    else:
                        for value in outcome_enum:          # a brief word for the same outcome
                            if value.replace("_", " ") in cell or cell in value:
                                outcome = value
                    if not outcome:
                        outcome = cell
                    break
            if not outcome:
                continue
            reasons = []
            for n in reason_cols:
                cell = (row[n] if n < len(row) else "").strip()
                if not cell or cell.lower() in ("none", "-", "n/a", "no", "not applicable"):
                    reasons = [""]
                    break
                for piece in re.split(r"[,;/]|\bor\b|\band\b", cell):
                    piece = piece.strip().strip("`*.").lower()
                    if piece and piece not in ("none", "-"):
                        reasons.append(piece)
            for reason in reasons or [""]:
                key = (outcome, reason)
                if key in seen:
                    continue
                seen.add(key)
                closes.append(OutcomeClose(outcome=outcome, reason=reason, role=classify_reason(reason)))
    if not closes:
        closes = _closes_from_prose(text, outcome_enum)
    if not closes:
        for outcome in outcome_enum:
            closes.append(OutcomeClose(outcome=outcome, reason="", role=""))
    for c in closes:
        if c.reason == "" and not c.role:
            c.role = "promise" if any(w in c.outcome for w in ("promis", "agreement", "arrang")) else (
                "paid" if any(w in c.outcome for w in ("paid", "settled", "resolved", "clear")) else c.outcome)
    # Second pass: an outcome whose reasons mostly belong to one group tells us how
    # to read the one or two reasons that could belong to two.
    groups = {outcome: family_of([c.role for c in closes if c.outcome == outcome and c.role])
              for outcome in {c.outcome for c in closes}}
    for c in closes:
        if c.reason:
            c.role = classify_reason(c.reason, groups.get(c.outcome, ""))
    return closes


def _closes_from_prose(text: str, outcome_enum: List[str]) -> List[OutcomeClose]:
    """Closing rules written as sentences instead of a table.

    `close as not_reached / callback`, `close as \`no_promise\` with reason
    \`dispute\``, `close the call as \`paid\``.
    """
    flat = flatten(text)
    found: List[OutcomeClose] = []
    seen = set()
    bare = set()
    # Work clause by clause: the reason of a closing rule stops at the next "when".
    clauses = re.split(r"[;,\n]|\.(?=\s)|\b(?:when|if|after|before|unless|otherwise|and then)\b", flat,
                       flags=re.IGNORECASE)
    head = re.compile(r"close(?:\s+the\s+call)?\s+as\s+(?:the\s+)?[`'\"]?([a-z][a-z_]{1,30})[`'\"]?", re.IGNORECASE)
    pair = re.compile(r"close(?:\s+the\s+call)?\s+as\s+(?:the\s+)?[`'\"]?([a-z][a-z_]{1,30})[`'\"]?"
                      r"\s*(?:/|,|with\s+reason|and\s+reason|:)\s*[`'\"]?([a-z][a-z_]{1,30})[`'\"]?",
                      re.IGNORECASE)
    for clause in clauses:
        for m in pair.finditer(clause):
            outcome, reason = m.group(1).lower(), m.group(2).lower()
            outcome = next((o for o in outcome_enum if o == outcome), outcome)
            if outcome not in outcome_enum:
                outcome = next((o for o in outcome_enum if o in outcome or outcome in o), outcome)
            if outcome in outcome_enum and (outcome, reason) not in seen:
                seen.add((outcome, reason))
                found.append(OutcomeClose(outcome=outcome, reason=reason, role=classify_reason(reason)))
        for m in head.finditer(clause):
            outcome = m.group(1).lower()
            bare.add(outcome)
    for outcome in sorted(bare):
        if outcome in outcome_enum and not any(c.outcome == outcome for c in found):
            found.append(OutcomeClose(outcome=outcome, reason="", role=classify_reason("")))
    return found


def parse_note_reasons(text: str, note_enum: List[str]) -> List[str]:
    """The values add_note may take, as the brief words them."""
    if not note_enum:
        return []
    found = []
    low = text.lower()
    for value in note_enum:
        if re.search(r"\b" + re.escape(value.lower().replace("_", "[ _-]")) + r"\b", low):
            found.append(value)
    return found or list(note_enum)


# ------------------------------------------------------------------- queues

def parse_queues(text: str, queue_enum: List[str]) -> List[QueueRule]:
    """Which queue value the brief sends each kind of transfer to.

    Read every sentence that names a queue, and decide what kind of transfer it
    is from that same sentence - the brief usually names the reason right after
    the queue. Backticked tool names are ignored so that `transfer_to_agent`
    cannot look like a request for a person.
    """
    found: Dict[str, str] = {}
    for sentence in sentences(flatten(text)):
        if "queue" not in sentence.lower():
            continue
        stripped = re.sub(r"`[^`]*`", " ", sentence)
        for m in re.finditer(r"queue[^.\n]{0,24}?[`'\"]?([a-z][a-z_]{2,24})[`'\"]?", sentence.lower()):
            value = m.group(1)
            if value not in queue_enum:
                continue
            role = classify_reason(stripped.lower())
            if role in ("restructuring", "human", "support"):
                found.setdefault(role, value)
    rules: List[QueueRule] = []
    for role in ("restructuring", "human", "support"):
        queue = found.get(role)
        if queue is None:
            queue = next((v for v in queue_enum if v.lower() == role), None)
        if queue is None and role == "human":
            queue = next((v for v in queue_enum if v.lower() in ("general", "frontline", "human", "advisor")), None)
        if queue is None:
            queue = next((v for v in queue_enum if role in v.lower()), None)
        if queue is None and queue_enum:
            queue = queue_enum[0]
        if queue:
            rules.append(QueueRule(role=role, queue=queue))
    return rules


# ------------------------------------------------------------------ amounts

def parse_floor_fields(text: str, product_type_rule: ProductTypeRule, api_fields: Dict[str, str]) -> str:
    """Which account field is the floor for one product type.

    The brief usually writes it as 'the floor is X for a credit card and Y for a
    personal loan or a car loan', so each candidate field is read together with
    the words that follow it up to the next field name.
    """
    type_words = set(re.findall(r"[a-z]+", product_type_rule.type_name.lower()))
    for sentence in sentences(text):
        hits = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"`([a-z_]{3,40})`", sentence)]
        for i, (start, end, field) in enumerate(hits):
            if field not in api_fields:
                continue
            stop = hits[i + 1][0] if i + 1 < len(hits) else len(sentence)
            window = sentence[end:stop][:110]
            if type_words & set(re.findall(r"[a-z]+", window.lower())):
                return field
    # fall back to the API description: "credit cards only" / "loans only"
    for field, desc in api_fields.items():
        dw = set(re.findall(r"[a-z]+", desc.lower()))
        if "card" in type_words and "card" in dw:
            return field
        if "loan" in type_words and "loan" in dw:
            return field
    return ""


# ---------------------------------------------------------------- sentences

def find_quoted(text: str, *keywords: str) -> str:
    """A sentence the brief puts in quotes, or writes as a standalone sentence."""
    flat = flatten(text)
    for q in re.findall(r"\"([^\"]{6,200})\"", flat):
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
    if quoted and len(quoted) < 200 and not re.search(r"\bsay\b|\bclose\b|\btake\b", quoted, re.IGNORECASE):
        return quoted
    for raw in sentences(flat):
        if not all(re.search(k, raw, re.IGNORECASE) for k in keywords):
            continue
        m = re.search(r"\bsay(?: that)?\s+(.{5,180}?\.)\s", raw + " ", re.IGNORECASE)
        if m and m.group(1):
            clause = m.group(1).strip()
            clause = re.sub(r"\s*,?\s*(?:take no promise|close as|and close).*$", "", clause, flags=re.IGNORECASE).strip()
            clause = clause[0].upper() + clause[1:] if clause else clause
            if clause and not clause.endswith("."):
                clause += "."
            return clause
    return quoted


def _first_ask(text: str) -> str:
    """The brief's own first question, e.g. `First ask: "Can you pay today?"`.

    A question the brief forbids is not a candidate, so `Never ask "how much can
    you pay?"` is skipped.
    """
    flat = flatten(text)
    for m in re.finditer(r"\"([^\"]{10,140}\?)\"", flat):
        question = m.group(1).strip()
        before = flat[max(0, m.start() - 90): m.start()].lower()
        if re.search(r"never|do not|don't|must not|not allowed|instead of", before):
            continue
        if re.search(r"confirm|date of birth|last four|postcode|year of birth|scam|robot|how much can", question, re.IGNORECASE):
            continue
        if re.search(r"first (?:ask|question)|open with|start with", before) or len(question.split()) <= 9:
            return question
    return ""


def _advice_from_apps(text: str) -> str:
    """The sentence that points a failed customer at the app or a branch."""
    for raw in sentences(flatten(text)):
        if re.search(r"mobile app|\bbranch\b|internet banking", raw, re.IGNORECASE):
            body = re.split(r"(?<=[.!?])\s", raw, maxsplit=1)[0]
            body = re.sub(r"^\s*\d+[.)]\s*", "", body)
            body = re.sub(r"^(?:after [^,]{0,60},?\s*)", "", body, flags=re.IGNORECASE)
            if 10 < len(body) < 200:
                return _tidy(body)
    return ""


def clause_after(text: str, trigger: str) -> str:
    """The clause that follows a trigger phrase such as 'explain once that'."""
    for raw in sentences(flatten(text)):
        m = re.search(trigger + r"\s*(?:that\s+)?([^.]{10,170}\.)", raw, re.IGNORECASE)
        if m and m.group(1):
            return _tidy(m.group(1))
    return ""


def _tidy(clause: str) -> str:
    """A clause without the rule's bookkeeping, and with one full stop."""
    clause = clause.strip().strip("`\"'")
    clause = re.split(r"\s*,?\s*(?:and (?:close|then|tell|ask|if|when)|then close|close as|or close|"
                      r"if they|when they)\b", clause, maxsplit=1, flags=re.IGNORECASE)[0]
    clause = clause.strip().rstrip(",;:")
    if clause:
        clause = clause[0].upper() + clause[1:]
    if clause and not clause.endswith("."):
        clause += "."
    return clause


# --------------------------------------------------------------------- entry

def detect_bank_name(text: str) -> str:
    """The bank's own name, from the title, from how the agent introduces itself, or from prose."""
    for pattern in (
        r"calling from ([A-Z][A-Za-z0-9&'\-]{2,24}(?:\s+[A-Z][A-Za-z0-9&'\-]{2,24})?)",
        r"\b([A-Z][A-Za-z0-9&'\-]{2,24}\s+(?:Bank|Banks|Building Society|Financial|Finance|Credit Union|"
        r"Mutual|Trust|Capital|Savings))\b",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    m = re.match(r"^#{1,3}\s*(.{3,60}?)\s*(?:[-–—:].*)?$", text.strip().splitlines()[0] if text.strip() else "")
    if m:
        title = re.sub(r"\s*[-–—:]\s.*$", "", m.group(1)).strip(" .*")
        title = re.sub(r"\b(brief|specification|policy|note|script|call)\b.*$", "", title, flags=re.IGNORECASE).strip()
        if 3 < len(title) < 46 and not title.lower().startswith(("brief", "the ")):
            return title
    return "the bank"


def detect_multi_product(text: str, max_products: int) -> Tuple[bool, int]:
    low = text.lower()
    plural = bool(re.search(r"each of the (?:up to )?products|each product\b|every product|"
                            r"products? one by one|go through the products|"
                            r"(?:up to|one to|as many as|at most|maximum of)\s+(?:two|three|four|\d+)\s+products|"
                            r"(?:two|three|\d+)\s+or more products|"
                            r"multiple products|several products", low))
    found = re.search(r"(?:up to|one to|as many as|max(?:imum of)?)\s+(two|three|four|\d+)\s+products?", low)
    n = 1
    if found:
        n = to_int(re.sub(r"\D", "", found.group(1))) or max_products
    elif re.search(r"\b(three|3)\s+products", low):
        n = 3
    elif re.search(r"\b(two|2)\s+products", low):
        n = 2
    if plural:
        n = max(n, 2)
    return (plural or n > 1), max(n, 1)


def parse_brief(brief_paths: List[str], openapi_path: Optional[str] = None) -> PolicySpec:
    """Read one or more brief files into a single PolicySpec."""
    from builder.openapi_parser import load_openapi

    texts: List[str] = []
    for path in brief_paths:
        p = Path(path)
        if p.exists():
            texts.append(p.read_text(encoding="utf-8", errors="ignore"))
    text = "\n\n".join(texts)
    text = unicodedata.normalize("NFKC", text)

    spec = load_openapi(openapi_path) if openapi_path else {"paths": {}}
    field_enum = _enum_of(spec, "verify_identity", "field")
    outcome_enum = _enum_of(spec, "close_call", "outcome")
    queue_enum = _enum_of(spec, "transfer_to_agent", "queue")
    note_enum = _enum_of(spec, "add_note", "reason")
    product_types = _enum_of(spec, "get_account", "products[].properties.type")
    api_product_fields = _product_field_descriptions(spec)

    policy = PolicySpec(raw=text)
    policy.sources = [str(p) for p in brief_paths]
    policy.bank_name = detect_bank_name(text)
    policy.recording_disclosure = bool(re.search(r"record(?:ed|ing)|recorded", text, re.IGNORECASE))
    policy.identity_questions = match_identity_questions(text, field_enum) if field_enum else []
    if field_enum and not any(
            any(key in text.lower() for key in IDENTITY_HINTS.get(field, [field.replace("_", " ")]))
            for field in field_enum):
        policy.diagnostics.append("Identity questions did not match the API enum; parser may have selected fallback fields.")
    policy.max_wrong_answers, policy.max_declines = parse_identity_limits(text)
    policy.holidays = parse_holidays(text)
    policy.deadline_bands = parse_deadline_bands(text)
    if any(b.band_name == "default" for b in policy.deadline_bands):
        policy.diagnostics.append("No deadline band parsed; runtime currently falls back to 7 business days and 1 attempt.")
    policy.callback = parse_callback(text)
    callback_mentioned = bool(re.search(r"callback|call back|reach(?:ed)? later|call again", text, re.IGNORECASE))
    callback_hours_found = bool(re.search(r"\b(?:from|between)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:to|and|-)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", text, re.IGNORECASE))
    if callback_mentioned and not callback_hours_found:
        policy.diagnostics.append("Callback hours were not recognized; runtime may use default hours.")
    policy.closes = parse_closes(text, outcome_enum)
    if outcome_enum and not policy.closes:
        policy.diagnostics.append("No close_call outcome rows parsed from the brief.")
    policy.queues = parse_queues(text, queue_enum)
    policy.note_reasons = parse_note_reasons(text, note_enum)
    policy.multi_product, policy.max_products = detect_multi_product(text, 3)
    policy.note_unpromised = bool(note_enum) and policy.multi_product and bool(
        re.search(r"`?add_note`?", text, re.IGNORECASE))

    policy.first_question = (find_quoted(text, r"pay.*today|today.*pay")
                         or _first_ask(text)
                         or "Can you make this payment today?")
    policy.vague_period_rule = ""
    m = re.search(r"[^\n]*next\s*<[a-z ]+>[^\n]*", text, re.IGNORECASE)
    if m:
        policy.vague_period_rule = re.sub(r"^\s*\d+[.)]\s*", "", m.group(0).strip()).strip()

    policy.sms_sentence = find_quoted(text, r"text message|sms")
    policy.condolence = find_quoted(text, r"sorry for your loss|condolence")
    policy.already_paid_sentence = said_clause(text, r"business day", r"appear")
    if not policy.already_paid_sentence:
        policy.already_paid_sentence = "Payments can take one business day to appear."
    policy.failed_advice = (clause_after(text, r"(?:can|could|should)\s+they|ask them to|tell (?:the )?customer to|"
                                            r"point (?:the )?customer at|direct (?:the )?customer to|"
                                            r"they can (?:check|use|see)")
                         or said_clause(text, r"mobile app", r"branch")
                         or _advice_from_apps(text)
                         or "use the mobile app or a branch")
    if len(policy.failed_advice) > 160 or not policy.failed_advice.endswith("."):
        policy.failed_advice = "use the mobile app or a branch"
    policy.declined_advice = clause_after(text, r"explain[^.\n]{0,50}?that") or ""

    # product types and their floors
    for tname in product_types:
        rule = ProductTypeRule(type_name=tname, display_name=display_name(text, tname))
        rule.last4_suffix = last4_suffix(text, tname, api_product_fields)
        rule.floor_field = parse_floor_fields(text, rule, api_product_fields)
        policy.product_types.append(rule)
        if not rule.floor_field:
            policy.diagnostics.append(f"No payment floor field parsed for API product type {tname!r}.")

    # Keep the existing rule/tool vocabulary, but make applicability explicit.
    # Absence is a disabled optional feature; a mentioned but unparsable rule is
    # unresolved and blocks publication through diagnostics above.
    def excerpt(pattern: str) -> str:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return ""
        start, end = max(0, match.start() - 100), min(len(text), match.end() + 180)
        return re.sub(r"\s+", " ", text[start:end]).strip()

    def component(name: str, enabled: bool, value: Any, pattern: str, reason_on_off: str = "") -> None:
        evidence = excerpt(pattern) if enabled else ""
        policy.components.append(PolicyComponent(
            name=name,
            state="enabled" if enabled else "disabled",
            value=value if enabled else None,
            evidence=evidence,
            reason="Extracted from brief" if enabled else (reason_on_off or "No applicable rule found in supplied brief"),
        ))

    component("identity_verification", bool(policy.identity_questions),
              [{"field": q.field, "question": q.question} for q in policy.identity_questions],
              r"identity|verify|security question")
    component("promise_calendar", bool(policy.deadline_bands),
              {"bands": [b.__dict__ for b in policy.deadline_bands], "holidays": policy.holidays},
              r"deadline|business day|calendar day|holiday")
    component("callback", callback_mentioned and callback_hours_found,
              {"days": policy.callback.days_rule, "start": policy.callback.start_hour, "end": policy.callback.end_hour},
              r"callback|call back|reach(?:ed)? later|call again")
    component("payment_amount_rules", bool(policy.product_types) and all(p.floor_field for p in policy.product_types),
              [{"type": p.type_name, "floor_field": p.floor_field} for p in policy.product_types],
              r"floor|minimum payment|instalment|installment")
    component("multi_product_sequence", policy.multi_product,
              {"max_products": policy.max_products, "notes_for_unpromised": policy.note_unpromised},
              r"multiple products|products? one by one|up to .* products?|each product")
    component("hardship_path", bool(re.search(r"hardship|illness|job loss|bereav", text, re.IGNORECASE)),
              {"support_queue": policy.queue_for("support"), "close": (policy.close_for("hardship").__dict__ if policy.close_for("hardship") else None)},
              r"hardship|illness|job loss|bereav")
    component("dispute_path", bool(re.search(r"disput|not (?:theirs|their debt)|never had", text, re.IGNORECASE)),
              policy.close_for("dispute").__dict__ if policy.close_for("dispute") else None,
              r"disput|not (?:theirs|their debt)|never had")
    component("recording_disclosure", policy.recording_disclosure, True,
              r"record(?:ed|ing)")
    component("close_outcomes", bool(policy.closes), [c.__dict__ for c in policy.closes],
              r"close_call|outcome|reason")
    return policy


def _enum_of(spec, tool: str, path: str) -> List[str]:
    """Read an enum the tool API exposes, in its request body or in its response."""
    for schema in (_body_schema(spec, tool), _response_schema(spec, tool)):
        found = _walk_enum(schema, path)
        if found:
            return found
    return []


def _walk_enum(schema: Dict[str, Any], path: str) -> List[str]:
    """Follow a dotted path such as 'products[].properties.type' and read its enum."""
    cur: Any = (schema or {}).get("properties") or {}
    for part in path.split("."):
        if not isinstance(cur, dict):
            return []
        if part.endswith("[]"):
            cur = cur.get(part[:-2]) or {}
            cur = (cur.get("items") or {}) if isinstance(cur, dict) else {}
        else:
            cur = cur.get(part) or {}
    if isinstance(cur, dict) and isinstance(cur.get("enum"), list):
        return [str(v) for v in cur["enum"]]
    return []


def _body_schema(spec, tool: str) -> Dict[str, Any]:
    """The request-body schema of one tool, found by operationId or by its path name."""
    return _schema_of(spec, tool, "request")


def _response_schema(spec, tool: str) -> Dict[str, Any]:
    """The JSON schema of what one tool returns."""
    return _schema_of(spec, tool, "response")


def _schema_of(spec, tool: str, where: str) -> Dict[str, Any]:
    if not spec or not spec.get("paths"):
        return {}
    for path, pitem in spec["paths"].items():
        stem = path.strip("/").replace("/", "_")
        for method, op in pitem.items():
            if not isinstance(op, dict):
                continue
            name = op.get("operationId") or stem
            if name != tool and stem != tool:
                continue
            if where == "request":
                holders = [h for h in ((op.get("requestBody") or {}).get("content") or {}).values()]
            else:
                holders = [h for r in (op.get("responses") or {}).values() if isinstance(r, dict)
                           for h in (r.get("content") or {}).values()]
            for holder in holders:
                if isinstance(holder, dict) and holder.get("schema"):
                    return holder["schema"]
    return {}


def _product_field_descriptions(spec) -> Dict[str, str]:
    """What the account result says about each product field, request and response together."""
    fields: Dict[str, str] = {}
    for schema in (_body_schema(spec, "get_account"), _response_schema(spec, "get_account")):
        products = ((schema or {}).get("properties") or {}).get("products") or {}
        props = (products.get("items") or {}).get("properties") or {}
        for n, p in props.items():
            fields.setdefault(n, str(p.get("description", "")))
    return fields


def display_name(text: str, type_name: str) -> str:
    words = type_name.replace("_", " ")
    for quoted in re.findall(r"\"([a-z][^\"]{2,40})\"", text):
        if quoted.replace("_", " ").lower() == words.lower():
            return quoted
    return words


def last4_suffix(text: str, type_name: str, api_fields: Dict[str, str]) -> str:
    """A product that carries a card tail is spoken with it, e.g. 'ending 4821'."""
    words = set(re.findall(r"[a-z]+", type_name.lower()))
    for field, desc in api_fields.items():
        if not (field.endswith("last4") or "last four" in desc.lower()):
            continue
        dw = set(re.findall(r"[a-z]+", desc.lower()))
        if (words & dw) or ("card" in words and "card" in dw) or re.search(r"\b" + re.escape(field) + r"\b", text):
            return " ending {" + field + "}"
    return ""


# ---------------------------------------------------------------------- main

def main() -> None:
    import sys
    from builder.openapi_parser import load_openapi  # noqa: F401
    paths = sys.argv[1:] or ["briefs/brief-a.md"]
    spec = parse_brief(paths, "tools/openapi.json")
    print(spec.summary())
    print("holidays:", spec.holidays)
    for b in spec.deadline_bands:
        print("band:", b)
    for q in spec.identity_questions:
        print("question:", q.field, "|", q.question)
    print("callback:", spec.callback)
    for p in spec.product_types:
        print("product:", p)
    print("queues:", spec.queues)
    print("notes:", spec.note_reasons, spec.multi_product, spec.max_products)
    for c in spec.closes:
        print("close:", c)
    print("sms:", spec.sms_sentence)
    print("paid:", spec.already_paid_sentence)


if __name__ == "__main__":
    main()
