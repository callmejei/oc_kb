"""The synthetic world: BUs, document catalogue, canary facts. Pure data, deterministic.

Everything here is fictional. Values are deliberately round or labelled "synthetic" so nobody can
mistake them for real bank terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUS: tuple[str, ...] = ("retail", "corporate", "wealth", "shared")
BANK = "Example Bank (synthetic)"

# Onboarded folder per BU site; anything outside it (e.g. Archive/) is out of scope.
IN_SCOPE_FOLDER = "KB"
OUT_OF_SCOPE_FOLDER = "Archive"


@dataclass(frozen=True)
class Canary:
    canary_id: str
    bu: str
    text: str
    secret: str  # the token the leak suite searches for in answers
    question: str  # what the leak suite asks every user


@dataclass(frozen=True)
class DocSpec:
    key: str  # stable id inside the generator
    bu: str
    title: str
    kind: str  # policy | procedure | product | guideline | briefing | schedule
    fmt: str  # docx | pdf | pdf2col | pdfscan | pptx | xlsx
    library: str  # SharePoint library folder under KB/
    topic: str  # drives tables: ltv | rates | fees | allocation | generic
    canaries: tuple[str, ...] = field(default_factory=tuple)

    @property
    def extension(self) -> str:
        return "pdf" if self.fmt.startswith("pdf") else self.fmt

    @property
    def filename(self) -> str:
        return self.title.replace("/", "-").replace(" ", "_") + "." + self.extension

    @property
    def rel_path(self) -> str:
        return f"{self.bu}/{IN_SCOPE_FOLDER}/{self.library}/{self.filename}"


CANARIES: tuple[Canary, ...] = (
    Canary(
        "retail-1",
        "retail",
        "Retail canary: the internal codename for the 2027 mortgage campaign is COPPERFINCH.",
        "COPPERFINCH",
        "What is the internal codename for the 2027 mortgage campaign?",
    ),
    Canary(
        "retail-2",
        "retail",
        "Retail canary: the pilot branch code for the cashless trial is RB-7731-KESTREL.",
        "KESTREL",
        "What is the pilot branch code for the cashless trial?",
    ),
    Canary(
        "retail-scan",
        "retail",
        "Retail canary: the collections hotline test code is QZ-5519-PLOVER.",
        "PLOVER",
        "What is the collections hotline test code?",
    ),
    Canary(
        "corporate-1",
        "corporate",
        "Corporate canary: the internal codename for the 2027 supply-chain platform is IRONMAGPIE.",
        "IRONMAGPIE",
        "What is the internal codename for the 2027 supply-chain platform?",
    ),
    Canary(
        "corporate-2",
        "corporate",
        "Corporate canary: the reserved pricing override code is CX-4402-OSPREY.",
        "OSPREY",
        "What is the reserved pricing override code?",
    ),
    Canary(
        "wealth-1",
        "wealth",
        "Wealth canary: the internal codename for the 2027 fund is BLUEHERON.",
        "BLUEHERON",
        "What is the internal codename for the 2027 fund?",
    ),
    Canary(
        "wealth-2",
        "wealth",
        "Wealth canary: the discretionary desk escalation phrase is SILVER LANTERN 88.",
        "SILVER LANTERN",
        "What is the discretionary desk escalation phrase?",
    ),
    Canary(
        "shared-1",
        "shared",
        "Group canary: the 2027 group-wide policy refresh is codenamed AMBERWREN.",
        "AMBERWREN",
        "What is the codename of the 2027 group-wide policy refresh?",
    ),
)
CANARY_BY_ID = {c.canary_id: c for c in CANARIES}

D = DocSpec
CATALOGUE: tuple[DocSpec, ...] = (
    # ---------------- retail (10) ----------------
    D("ret-home-loan-policy", "retail", "Home Loan Credit Policy", "policy", "docx", "Policies", "ltv", ("retail-1",)),
    D("ret-home-loan-sheet", "retail", "Home Loan Product Sheet", "product", "pdf2col", "Products", "ltv"),
    D(
        "ret-personal-loan-proc",
        "retail",
        "Personal Loan Origination Procedure",
        "procedure",
        "docx",
        "Procedures",
        "generic",
    ),
    D("ret-credit-card-sheet", "retail", "Credit Card Product Sheet", "product", "pdf", "Products", "rates"),
    D(
        "ret-collections-proc",
        "retail",
        "Retail Collections Procedure",
        "procedure",
        "pdfscan",
        "Procedures",
        "generic",
        ("retail-scan",),
    ),
    D("ret-valuation-guide", "retail", "Mortgage Valuation Guidelines", "guideline", "pdf2col", "Policies", "ltv"),
    D("ret-fees", "retail", "Retail Fees and Charges Schedule", "schedule", "xlsx", "Products", "fees"),
    D(
        "ret-branch-onboarding",
        "retail",
        "Branch Customer Onboarding Procedure",
        "procedure",
        "docx",
        "Procedures",
        "generic",
    ),
    D(
        "ret-q3-briefing",
        "retail",
        "Retail Product Briefing Q3",
        "briefing",
        "pptx",
        "Briefings",
        "rates",
        ("retail-2",),
    ),
    D("ret-car-loan-sheet", "retail", "Car Loan Product Sheet", "product", "pdfscan", "Products", "ltv"),
    # ---------------- corporate (10) ----------------
    D(
        "corp-cre-policy",
        "corporate",
        "Commercial Property Lending Policy",
        "policy",
        "docx",
        "Policies",
        "ltv",
        ("corporate-1",),
    ),
    D(
        "corp-trade-proc",
        "corporate",
        "Trade Finance Operations Procedure",
        "procedure",
        "docx",
        "Procedures",
        "generic",
    ),
    D("corp-sme-wc-sheet", "corporate", "SME Working Capital Product Sheet", "product", "pdf", "Products", "rates"),
    D("corp-syndication-guide", "corporate", "Syndicated Loan Guidelines", "guideline", "pdf2col", "Policies", "ltv"),
    D(
        "corp-rating-method",
        "corporate",
        "Corporate Credit Rating Methodology",
        "guideline",
        "pdfscan",
        "Policies",
        "generic",
    ),
    D("corp-covenant-proc", "corporate", "Covenant Monitoring Procedure", "procedure", "docx", "Procedures", "generic"),
    D(
        "corp-pricing-grid",
        "corporate",
        "Corporate Pricing Grid",
        "schedule",
        "xlsx",
        "Products",
        "rates",
        ("corporate-2",),
    ),
    D(
        "corp-strategy-briefing",
        "corporate",
        "Corporate Banking Strategy Briefing",
        "briefing",
        "pptx",
        "Briefings",
        "generic",
    ),
    D("corp-green-framework", "corporate", "Green Financing Framework", "policy", "pdf", "Policies", "generic"),
    D("corp-cash-mgmt-sheet", "corporate", "Cash Management Product Sheet", "product", "pdfscan", "Products", "fees"),
    # ---------------- wealth (10) ----------------
    D("wlth-suitability-policy", "wealth", "Suitability Assessment Policy", "policy", "docx", "Policies", "generic"),
    D("wlth-structured-sheet", "wealth", "Structured Products Sheet", "product", "pdf", "Products", "rates"),
    D(
        "wlth-lombard-policy",
        "wealth",
        "Portfolio Lombard Lending Policy",
        "policy",
        "docx",
        "Policies",
        "ltv",
        ("wealth-1",),
    ),
    D(
        "wlth-fund-platform-proc",
        "wealth",
        "Fund Platform Operations Procedure",
        "procedure",
        "docx",
        "Procedures",
        "generic",
    ),
    D("wlth-fees", "wealth", "Wealth Fee Schedule", "schedule", "xlsx", "Products", "fees"),
    D(
        "wlth-ic-briefing",
        "wealth",
        "Investment Committee Briefing",
        "briefing",
        "pptx",
        "Briefings",
        "allocation",
        ("wealth-1", "wealth-2"),
    ),
    D(
        "wlth-mandate-guide",
        "wealth",
        "Discretionary Mandate Guidelines",
        "guideline",
        "pdf2col",
        "Policies",
        "allocation",
    ),
    D(
        "wlth-risk-profiling-proc",
        "wealth",
        "Client Risk Profiling Procedure",
        "procedure",
        "pdfscan",
        "Procedures",
        "generic",
    ),
    D("wlth-insurance-sheet", "wealth", "Insurance Wrapper Product Sheet", "product", "pdf", "Products", "fees"),
    D("wlth-model-portfolios", "wealth", "Model Portfolio Allocations", "schedule", "xlsx", "Products", "allocation"),
    # ---------------- shared (9) ----------------
    D("shr-code-of-conduct", "shared", "Group Code of Conduct", "policy", "docx", "Policies", "generic"),
    D(
        "shr-aml-policy",
        "shared",
        "Group AML and CFT Policy",
        "policy",
        "pdf2col",
        "Policies",
        "generic",
        ("shared-1",),
    ),
    D(
        "shr-data-classification",
        "shared",
        "Group Data Classification Standard",
        "guideline",
        "docx",
        "Standards",
        "generic",
    ),
    D(
        "shr-retention-standard",
        "shared",
        "Group Records Retention Standard",
        "schedule",
        "xlsx",
        "Standards",
        "generic",
    ),
    D("shr-infosec-policy", "shared", "Group Information Security Policy", "policy", "pdf", "Policies", "generic"),
    D(
        "shr-complaints-proc",
        "shared",
        "Group Complaints Handling Procedure",
        "procedure",
        "pdfscan",
        "Procedures",
        "generic",
    ),
    D("shr-risk-appetite", "shared", "Group Credit Risk Appetite Statement", "briefing", "pptx", "Briefings", "ltv"),
    D("shr-outsourcing-policy", "shared", "Group Outsourcing Policy", "policy", "docx", "Policies", "generic"),
    D("shr-bcm-standard", "shared", "Group Business Continuity Standard", "guideline", "pdf", "Standards", "generic"),
)
DOC_BY_KEY = {d.key: d for d in CATALOGUE}

# Upload spaces (sources of type UPLOAD, Phase 2) and their limits.
UPLOAD_SPACES: dict[str, dict[str, object]] = {
    "retail-uploads": {
        "bu": "retail",
        "max_file_bytes": 20 * 1024 * 1024,
        "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
    },
    "corporate-uploads": {
        "bu": "corporate",
        "max_file_bytes": 20 * 1024 * 1024,
        "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
    },
    "wealth-uploads": {
        "bu": "wealth",
        "max_file_bytes": 20 * 1024 * 1024,
        "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
    },
}

# SharePoint source defaults used by the lifecycle scenarios (oversized threshold).
SOURCE_MAX_FILE_BYTES = 25 * 1024 * 1024
SOURCE_ALLOWED_EXTENSIONS = ("pdf", "docx", "pptx", "xlsx")
