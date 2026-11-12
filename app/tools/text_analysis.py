"""Deterministic text utilities shared by the resume, JD and ATS tools.

Keeping this logic deterministic (no LLM) means the *same* resume + JD always
gets the *same* score, which is what makes before/after comparisons fair and
lets the eval suite catch regressions.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections import Counter

# Curated skills lexicon. Multi-word phrases are matched before single words.
# Extend freely; keys are canonical names, values are accepted aliases.
SKILL_ALIASES: dict[str, list[str]] = {
    # Languages
    "python": ["python", "python3"],
    "java": ["java"],
    "javascript": ["javascript", "js", "node.js", "nodejs"],
    "typescript": ["typescript"],
    "go": ["golang"],
    "scala": ["scala"],
    "sql": ["sql", "t-sql", "pl/sql"],
    "r": [],  # too ambiguous as a bare token; only matched via phrases below
    "c++": ["c++", "cpp"],
    "c#": ["c#", ".net"],
    "rust": ["rust"],
    "kotlin": ["kotlin"],
    # Data / ML
    "machine learning": ["machine learning", "ml"],
    "deep learning": ["deep learning"],
    "generative ai": [
        "generative ai",
        "genai",
        "gen ai",
        "llm",
        "llms",
        "large language models",
    ],
    "nlp": ["nlp", "natural language processing"],
    "pytorch": ["pytorch"],
    "tensorflow": ["tensorflow"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "pandas": ["pandas"],
    "spark": ["spark", "pyspark", "apache spark"],
    "airflow": ["airflow", "apache airflow"],
    "dbt": ["dbt"],
    "kafka": ["kafka", "apache kafka"],
    "etl": ["etl", "elt", "data pipelines", "data pipeline"],
    "data modeling": ["data modeling", "data modelling", "dimensional modeling"],
    "data warehousing": ["data warehouse", "data warehousing"],
    "bigquery": ["bigquery"],
    "snowflake": ["snowflake"],
    "databricks": ["databricks"],
    "redshift": ["redshift"],
    "tableau": ["tableau"],
    "looker": ["looker"],
    "power bi": ["power bi", "powerbi"],
    "statistics": ["statistics", "statistical"],
    "a/b testing": ["a/b testing", "ab testing", "experimentation"],
    # Cloud / infra
    "gcp": ["gcp", "google cloud", "google cloud platform"],
    "aws": ["aws", "amazon web services"],
    "azure": ["azure"],
    "kubernetes": ["kubernetes", "k8s", "gke", "eks", "aks"],
    "docker": ["docker", "containers"],
    "terraform": ["terraform"],
    "ci/cd": [
        "ci/cd",
        "continuous integration",
        "continuous delivery",
        "github actions",
    ],
    "linux": ["linux"],
    "microservices": ["microservices", "microservice"],
    "rest apis": ["rest", "restful", "rest api", "rest apis", "apis"],
    "graphql": ["graphql"],
    "postgresql": ["postgresql", "postgres"],
    "mysql": ["mysql"],
    "mongodb": ["mongodb"],
    "redis": ["redis"],
    "observability": ["observability", "monitoring", "opentelemetry"],
    "security": ["security", "iam"],
    # Web / mobile
    "react": ["react", "react.js", "reactjs"],
    "angular": ["angular"],
    "vue": ["vue", "vue.js"],
    "ios": ["ios", "swift"],
    "android": ["android"],
    "next.js": ["next.js", "nextjs"],
    "node.js": ["node.js", "nodejs", "nestjs", "express.js"],
    "fastapi": ["fastapi"],
    "websockets": ["websockets", "websocket", "webrtc"],
    "accessibility": ["accessibility", "wcag", "a11y"],
    "oauth": ["oauth", "oauth 2.0", "oidc", "openid connect"],
    "automated testing": [
        "jest",
        "playwright",
        "pytest",
        "unit testing",
        "test automation",
    ],
    # Healthcare
    "fhir": ["fhir", "fhir r4", "smart on fhir"],
    "hl7": ["hl7", "hl7 v2"],
    "hipaa": ["hipaa", "phi"],
    "ehr integration": ["ehr", "emr", "epic", "cerner", "oracle health"],
    "healthcare": ["healthcare", "clinical", "telehealth", "digital health"],
    "soc 2": ["soc 2", "soc2"],
    # Practices / business
    "agile": ["agile", "scrum", "kanban"],
    "product management": ["product management", "product roadmap", "roadmap"],
    "project management": ["project management", "pmp"],
    "stakeholder management": [
        "stakeholder management",
        "stakeholders",
        "cross-functional",
    ],
    "leadership": [
        "leadership",
        "led a team",
        "people management",
        "mentoring",
        "mentored",
    ],
    "communication": ["communication", "presentation skills"],
    "customer success": ["customer success"],
    "sales": ["sales", "pipeline management", "quota"],
    "salesforce": ["salesforce", "sfdc"],
    "excel": ["excel", "spreadsheets"],
    "financial modeling": ["financial modeling", "financial modelling"],
    "system design": ["system design", "distributed systems", "scalability"],
}

STANDARD_SECTIONS: dict[str, list[str]] = {
    "summary": ["summary", "professional summary", "profile", "objective", "about me"],
    "experience": [
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "work history",
    ],
    "education": ["education", "academic background", "qualifications"],
    "skills": ["skills", "technical skills", "core competencies", "key skills"],
    "certifications": ["certifications", "certificates", "licenses"],
    "projects": ["projects", "selected projects", "key projects"],
}
REQUIRED_SECTIONS = ("summary", "experience", "education", "skills")
# Keyword -> section for combined ALL-CAPS headings. Order matters: the first
# keyword found wins (EDUCATION & CERTIFICATIONS -> education).
HEADING_KEYWORDS: list[tuple[str, str]] = [
    ("experience", "experience"),
    ("employment", "experience"),
    ("education", "education"),
    ("skills", "skills"),
    ("competencies", "skills"),
    ("summary", "summary"),
    ("profile", "summary"),
    ("projects", "projects"),
    ("certifications", "certifications"),
]

STOPWORDS = set(
    """a an and are as at be been but by can for from has have in into is it its of on or
    our that the their this to was we will with you your they them who what when where which
    while would should could may might must about across after all also any both each more
    most other over such than then these those through under up very via well work working
    team teams role roles job position candidate candidates company opportunity experience
    years year including include includes etc new ability strong excellent skills skill using
    use used help build building make ensure across within based like requirements required
    preferred plus bonus responsibilities qualifications looking join us apply""".split()
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?", re.I
)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_-]+/?", re.I)
QUANTIFIED_RE = re.compile(
    r"(\d+(?:\.\d+)?\s?%|\$\s?\d[\d,.]*\s?[kmb]?|\b\d[\d,.]*\s?(?:k|m|b|x|\+)?\s?"
    r"(?:users|customers|clients|requests|people|engineers|hours|days|ms|tb|gb|pb|"
    r"million|billion|thousand)\b)",
    re.I,
)
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", re.I)
BULLET_RE = re.compile(r"^\s*(?:[-*\u2022\u25aa\u25cf]|\d+\.)\s+", re.M)


def normalise(text: str) -> str:
    """Lowercase and collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _phrase_present(phrase: str, text: str) -> bool:
    # Word-boundary aware match that tolerates symbols like '+', '#', '/', '.'.
    pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def extract_skills(text: str) -> list[str]:
    """Return canonical skills found in text, in lexicon order."""
    norm = normalise(text)
    found = []
    for canonical, aliases in SKILL_ALIASES.items():
        candidates = sorted({canonical, *aliases}, key=len, reverse=True)
        if canonical == "r":
            candidates = ["r programming", "rstudio"]
        if any(_phrase_present(c, norm) for c in candidates if c):
            found.append(canonical)
    return found


def top_keywords(text: str, limit: int = 20) -> list[str]:
    """Most frequent meaningful single words and bigrams (excluding stopwords)."""
    tokens = [
        t
        for t in re.findall(r"[a-z][a-z0-9+#./-]{1,}", normalise(text))
        if t not in STOPWORDS
    ]
    tokens = [t.strip(".-/") for t in tokens if len(t.strip(".-/")) > 2]
    bigrams = [f"{a} {b}" for a, b in itertools.pairwise(tokens)]
    counts = Counter(tokens) + Counter(
        {b: c for b, c in Counter(bigrams).items() if c > 1}
    )
    return [w for w, _ in counts.most_common(limit)]


def detect_sections(text: str) -> dict[str, str]:
    """Split resume text into standard sections using heading detection.

    A line is a heading if it exactly matches a known alias ("Work Experience:"),
    or if it is a short ALL-CAPS line containing a section keyword, which covers
    combined headings like "EDUCATION & CERTIFICATIONS" or "KEY PROJECTS".
    """
    alias_to_section = {
        alias: section
        for section, aliases in STANDARD_SECTIONS.items()
        for alias in aliases
    }
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.splitlines():
        stripped = line.strip().rstrip(":").strip()
        key = stripped.lower()
        section = alias_to_section.get(key) if len(stripped) <= 40 else None
        if section is None and stripped.isupper() and 1 <= len(stripped.split()) <= 6:
            section = next(
                (s for kw, s in HEADING_KEYWORDS if re.search(rf"\b{kw}\b", key)), None
            )
        if section:
            current = section
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {
        k: "\n".join(v).strip() for k, v in sections.items() if "\n".join(v).strip()
    }


def clean_extracted_text(text: str) -> str:
    """Normalise text extracted from PDFs/DOCX.

    NFKC folds typographic ligatures ('ﬁ' -> 'fi', 'ﬂ' -> 'fl') and full-width
    characters, so words like 'workﬂows' match the keyword 'workflows'.
    """
    return unicodedata.normalize("NFKC", text)


def mask_contact_details(text: str) -> tuple[str, list[str]]:
    """Mask email/phone/LinkedIn/GitHub and report which were present.

    This is a lightweight first line of defence so raw contact details never
    reach the model context; the full PII pipeline (Cloud DLP) is applied to
    logs and memory separately. Profile URLs are masked BEFORE emails because
    PDF extraction often glues them together ('me@mail.comlinkedin.com/in/me').
    """
    detected = []
    if LINKEDIN_RE.search(text):
        detected.append("linkedin")
        text = LINKEDIN_RE.sub(" [LINKEDIN] ", text)
    if GITHUB_RE.search(text):
        detected.append("github")
        text = GITHUB_RE.sub(" [GITHUB] ", text)
    if EMAIL_RE.search(text):
        detected.append("email")
        text = EMAIL_RE.sub("[EMAIL]", text)
    if PHONE_RE.search(text):
        detected.append("phone")
        text = PHONE_RE.sub("[PHONE]", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text, detected


def count_quantified_bullets(text: str) -> tuple[int, int]:
    """Return (bullets_with_numbers, total_bullets_or_lines) for impact scoring."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    bullets = [ln for ln in lines if BULLET_RE.match(ln)] or [
        ln for ln in lines if len(ln.split()) >= 6
    ]
    quantified = [ln for ln in bullets if QUANTIFIED_RE.search(ln)]
    return len(quantified), len(bullets)


def estimate_min_years(text: str) -> int | None:
    years = [int(m.group(1)) for m in YEARS_RE.finditer(text) if int(m.group(1)) <= 40]
    return min(years) if years else None


def estimate_seniority(title: str, text: str) -> str:
    t = normalise(title + " " + text[:400])
    if re.search(r"\b(intern|internship)\b", t):
        return "intern"
    if re.search(r"\b(principal|staff|distinguished|director|head of|vp)\b", t):
        return "staff+"
    if re.search(r"\b(senior|sr\.?|lead)\b", t):
        return "senior"
    if re.search(r"\b(junior|jr\.?|entry[- ]level|graduate|associate)\b", t):
        return "junior"
    if title:
        return "mid"
    return "unknown"
