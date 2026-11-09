"""Structured document content (format independent): sections, paragraphs, tables."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from synthetic.world import BANK, CANARY_BY_ID, DocSpec


@dataclass
class Table:
    caption: str
    header: list[str]
    rows: list[list[str]]


@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    table: Table | None = None
    subsections: list[Section] = field(default_factory=list)


@dataclass
class DocContent:
    title: str
    subtitle: str
    version: int
    sections: list[Section]

    def all_text(self) -> str:
        out = [self.title, self.subtitle]

        def walk(s: Section) -> None:
            out.append(s.heading)
            out.extend(s.paragraphs)
            out.extend(s.bullets)
            if s.table:
                out.append(s.table.caption)
                out.extend(" | ".join(r) for r in [s.table.header, *s.table.rows])
            for sub in s.subsections:
                walk(sub)

        for s in self.sections:
            walk(s)
        return "\n".join(out)


BU_LABEL = {
    "retail": "Retail Banking",
    "corporate": "Corporate Banking",
    "wealth": "Wealth Management",
    "shared": "Group Functions",
}

PROPERTY_TYPES = [
    "Owner-occupied residential",
    "Investment residential",
    "Shophouse (mixed use)",
    "Commercial office",
    "Industrial (B1/B2)",
    "Land for development",
]


def _rng(spec: DocSpec, version: int) -> random.Random:
    return random.Random(f"{spec.key}:v{version}")  # noqa: S311 - synthetic content only


def ltv_table(spec: DocSpec, version: int) -> Table:
    r = _rng(spec, version)
    base = {"retail": 75, "corporate": 60, "wealth": 50, "shared": 70}[spec.bu]
    bump = 5 if version > 1 else 0  # a new version changes the numbers (lifecycle scenario)
    if spec.bu == "wealth":
        rows = [
            [a, f"{v + bump}%", f"{v - 10 + bump}%"]
            for a, v in [
                ("Cash and deposits", 90),
                ("Investment-grade bonds", 75),
                ("Blue-chip equities", 60),
                ("Diversified funds", 65),
                ("Structured notes", 40),
                ("Single unlisted shares", 0),
            ]
        ]
        return Table(
            "Table 1: Maximum advance ratio by collateral class (synthetic)",
            ["Collateral class", "Standard advance", "Concentrated holding advance"],
            rows,
        )
    rows = []
    for i, pt in enumerate(PROPERTY_TYPES[: 4 if spec.bu == "retail" else 6]):
        first = max(base - i * 5 + bump, 30)
        rows.append([pt, f"{first}%", f"{max(first - 30, 15)}%", str(r.choice([25, 30, 35]))])
    return Table(
        "Table 1: Maximum loan-to-value (LTV) by property type (synthetic)",
        ["Property type", "LTV - first facility", "LTV - third or more facilities", "Max tenure (years)"],
        rows,
    )


def rates_table(spec: DocSpec, version: int) -> Table:
    r = _rng(spec, version)
    tiers = ["Tier A", "Tier B", "Tier C", "Tier D"]
    rows = [
        [t, f"{2.0 + i * 0.75 + r.choice([0, 0.25]):.2f}%", f"{0.50 + i * 0.25:.2f}%", f"{(i + 1) * 10000:,}"]
        for i, t in enumerate(tiers)
    ]
    return Table(
        "Table 2: Indicative pricing by customer tier (synthetic)",
        ["Tier", "Indicative rate p.a.", "Spread over base", "Minimum amount (EXB$)"],
        rows,
    )


def fees_table(spec: DocSpec, version: int) -> Table:
    fees = [
        ("Account maintenance", "5.00", "Monthly"),
        ("Late payment", "75.00", "Per occurrence"),
        ("Early redemption", "1.50%", "Of amount redeemed"),
        ("Document retrieval", "20.00", "Per copy"),
        ("Telegraphic transfer", "25.00", "Per transfer"),
        ("Annual platform fee", "0.40%", "Of AUM"),
    ]
    return Table("Table 3: Fees and charges (synthetic, EXB$)", ["Fee", "Amount", "Basis"], [list(f) for f in fees])


def allocation_table(spec: DocSpec, version: int) -> Table:
    profiles = [
        ("Conservative", 20, 60, 20),
        ("Balanced", 45, 45, 10),
        ("Growth", 65, 30, 5),
        ("Aggressive", 80, 15, 5),
    ]
    return Table(
        "Table 4: Model portfolio allocation by risk profile (synthetic)",
        ["Risk profile", "Equities", "Bonds", "Cash"],
        [[p, f"{e}%", f"{b}%", f"{c}%"] for p, e, b, c in profiles],
    )


TABLES = {"ltv": ltv_table, "rates": rates_table, "fees": fees_table, "allocation": allocation_table}

KIND_SECTIONS = {
    "policy": [
        "Purpose",
        "Scope",
        "Definitions",
        "Policy Statements",
        "Roles and Responsibilities",
        "Exceptions and Dispensations",
        "Review and Approval",
    ],
    "procedure": [
        "Purpose",
        "Scope",
        "Pre-requisites",
        "Procedure Steps",
        "Controls and Checks",
        "Records",
        "Escalation",
    ],
    "product": ["Product Overview", "Eligibility", "Key Features", "Pricing", "Risks", "Important Notes"],
    "guideline": ["Introduction", "Principles", "Methodology", "Worked Example", "Governance"],
    "briefing": ["Highlights", "Performance", "Priorities", "Risks and Mitigations", "Asks"],
    "schedule": ["Schedule"],
}


def _paragraphs(spec: DocSpec, heading: str, r: random.Random) -> list[str]:
    unit = BU_LABEL[spec.bu]
    subj = spec.title.lower()
    templates = [
        f"This section of the {spec.title} sets out how {unit} applies the {heading.lower()} requirements. "
        f"It applies to all staff of {BANK} who originate, review or approve activity covered by the {subj}.",
        f"Staff must document every decision taken under this {spec.kind} in the system of record, including the "
        f"rationale, the approver and the date. Missing records are treated as control breaches.",
        f"Where local regulation is stricter than this {spec.kind}, the stricter requirement prevails. "
        f"Conflicts must be raised with the {unit} policy owner within five business days.",
        f"Thresholds in this document are synthetic and are reviewed annually by the {unit} risk committee. "
        f"Interim changes require approval by both the policy owner and an independent checker.",
        "Exceptions are granted for a maximum of twelve months and are recorded in the exception register "
        "with a named owner and an expiry date.",
    ]
    r.shuffle(templates)
    return templates[: r.choice([1, 2, 2, 3])]


def build_content(spec: DocSpec, version: int = 1) -> DocContent:
    r = _rng(spec, version)
    sections: list[Section] = []
    for i, heading in enumerate(KIND_SECTIONS[spec.kind], start=1):
        s = Section(heading=f"{i}. {heading}", paragraphs=_paragraphs(spec, heading, r))
        if heading in (
            "Policy Statements",
            "Key Features",
            "Methodology",
            "Procedure Steps",
            "Principles",
            "Highlights",
            "Priorities",
        ):
            s.bullets = [
                f"{heading} item {j}: {txt}"
                for j, txt in enumerate(
                    r.sample(
                        [
                            "obtain two independent valuations above the threshold",
                            "verify income documents against the latest statements",
                            "record the four-eyes approval before disbursement",
                            "re-assess the customer risk profile every twelve months",
                            "escalate any breach of limits to the BU risk committee",
                            "retain records for the period set in the retention standard",
                        ],
                        3,
                    ),
                    start=1,
                )
            ]
            s.subsections = [
                Section(
                    heading=f"{i}.1 Detailed requirements", paragraphs=_paragraphs(spec, "detailed requirements", r)
                )
            ]
        sections.append(s)
    # the topic table goes into the most relevant section
    if spec.topic in TABLES:
        target = next(
            (
                s
                for s in sections
                if any(
                    k in s.heading
                    for k in (
                        "Policy Statements",
                        "Pricing",
                        "Methodology",
                        "Key Features",
                        "Performance",
                        "Principles",
                        "Schedule",
                        "Procedure Steps",
                    )
                )
            ),
            sections[-1],
        )
        target.table = TABLES[spec.topic](spec, version)
    # canaries: a dedicated, unremarkable-looking paragraph
    for cid in spec.canaries:
        sections[min(2, len(sections) - 1)].paragraphs.append(CANARY_BY_ID[cid].text)
    subtitle = f"{BU_LABEL[spec.bu]} | {spec.kind.title()} | Version {version}.0 | {BANK}"
    return DocContent(title=spec.title, subtitle=subtitle, version=version, sections=sections)
