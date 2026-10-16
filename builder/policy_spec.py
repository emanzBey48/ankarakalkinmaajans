"""Data models for normalized bank policies extracted from briefs.

Everything in this module is filled in by `brief_parser` from the text of the
brief files and from the tool API description. Nothing here holds a value that
belongs to one particular brief: a builder that hardcoded a holiday or an
outcome name would not survive the hidden briefs.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class IdentityQuestion:
    """One security question the phone agent has to ask."""

    field: str                  # value the tool API expects, e.g. "date_of_birth"
    question: str               # the question to say, written from the brief
    answer_hint: str = ""       # how to format the answer for the tool
    words_retry: str = ""       # what to ask when the service reports a hint


@dataclass
class DeadlineBand:
    """How late the customer is -> latest promise date and how many persuasion attempts."""

    min_days_late: int
    max_days_late: int
    band_name: str
    business_days: int = 0
    calendar_days: int = 0
    persuasion_attempts: int = 1


@dataclass
class CallbackRule:
    days_rule: str = "today_or_next_business_day"
    start_hour: int = 10
    end_hour: int = 19

    def window_text(self) -> str:
        return f"{self.start_hour:02d}:00 to {self.end_hour:02d}:00"


@dataclass
class OutcomeClose:
    """One row of the brief's closing table."""

    outcome: str
    reason: str
    role: str = ""              # semantic slot: refused, hardship, callback, ...


@dataclass
class ProductTypeRule:
    """How one product type from the tool API is described and how its floor is found."""

    type_name: str
    display_name: str = ""
    last4_suffix: str = ""      # e.g. " ending {last4}" when the API returns a card tail
    floor_field: str = ""


@dataclass
class QueueRule:
    """What to pass to the transfer tool for one situation."""

    role: str                   # restructuring, support, human
    queue: str                  # the queue value the tool API accepts


@dataclass
class PolicyComponent:
    """One brief-controlled workflow capability and the evidence for its state."""

    name: str
    state: str                  # enabled, disabled, or unresolved
    value: object = None
    evidence: str = ""
    reason: str = ""


@dataclass
class PolicySpec:
    bank_name: str = "Freya Bank"

    # opening
    recording_disclosure: bool = True
    reveal_before_identity: bool = False

    # identity
    identity_questions: List[IdentityQuestion] = field(default_factory=list)
    max_wrong_answers: int = 3
    max_declines: int = 2
    failed_advice: str = "use the mobile app or a branch"
    declined_advice: str = ""

    # calendar
    holidays: List[str] = field(default_factory=list)
    deadline_bands: List[DeadlineBand] = field(default_factory=list)
    weekday_convention: str = ""      # e.g. '"next Friday" means ...'

    # callback
    callback: CallbackRule = field(default_factory=CallbackRule)

    # amounts
    product_types: List[ProductTypeRule] = field(default_factory=list)

    # persuasion
    first_question: str = "Can you make this payment today?"
    vague_period_rule: str = ""

    # closing
    closes: List[OutcomeClose] = field(default_factory=list)
    queues: List[QueueRule] = field(default_factory=list)

    # scope
    multi_product: bool = False
    max_products: int = 1
    note_unpromised: bool = False
    note_reasons: List[str] = field(default_factory=list)

    # exact sentences the brief requires
    sms_sentence: str = ""
    condolence: str = ""
    already_paid_sentence: str = ""
    refused_sentence: str = ""

    # raw text, kept so the compiler can quote the brief where it must
    raw: str = ""
    sources: List[str] = field(default_factory=list)
    # Extraction gaps are reported in the builder trace instead of being hidden.
    diagnostics: List[str] = field(default_factory=list)
    components: List[PolicyComponent] = field(default_factory=list)

    # ---------------------------------------------------------------- helpers

    def close_for(self, role: str) -> Optional[OutcomeClose]:
        for c in self.closes:
            if c.role == role:
                return c
        return None

    def component_enabled(self, name: str, fallback: bool = False) -> bool:
        """Read the compiled applicability plan; pre-plan callers retain compatibility."""
        for component in self.components:
            if component.name == name:
                return component.state == "enabled"
        return fallback

    def queue_for(self, role: str) -> str:
        for q in self.queues:
            if q.role == role:
                return q.queue
        return role

    def band_for(self, days_late: int) -> DeadlineBand:
        for b in self.deadline_bands:
            if b.min_days_late <= days_late <= b.max_days_late:
                return b
        if self.deadline_bands:
            if days_late < self.deadline_bands[0].min_days_late:
                return self.deadline_bands[0]
            return max(self.deadline_bands, key=lambda b: b.max_days_late)
        return DeadlineBand(0, 999, "default", business_days=7, persuasion_attempts=1)

    def summary(self) -> str:
        return (
            f"bank={self.bank_name} multi_product={self.multi_product} "
            f"holidays={len(self.holidays)} bands={len(self.deadline_bands)} "
            f"questions={[q.field for q in self.identity_questions]} "
            f"closes={[(c.outcome, c.reason, c.role) for c in self.closes]}"
        )
