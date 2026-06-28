"""
IE University waiver request guidelines — the source of truth the waiver agent
checks every student request against to identify gaps.

The canonical, human-editable copy lives at scripts/waiver_request_guidelines.md.
A copy is bundled next to this module (waiver_request_guidelines.md) so the rules
ship with both the Lambda asset and the AgentCore build. At runtime we load that
bundled copy as the human-readable text; if it is ever missing we fall back to the
embedded constant below so the agent never stalls. Keep all three in sync.
"""

import os

# Canonical structured criteria derived from the guidelines. required_fields are
# the exact snake_case keys the agent must use in collected_info, so that
# check_completeness can match them. field_labels render them back to humans.
IE_WAIVER_CRITERIA = {
    "required_fields": [
        "student_full_name",
        "ie_student_email",
        "program",
        "intake",
        "section",
        "passport_number",
        "absence_start_date",
        "absence_end_date",
        "reason",
    ],
    "field_labels": {
        "student_full_name": "Student's full name",
        "ie_student_email": "IE student email",
        "program": "Program",
        "intake": "Intake",
        "section": "Section",
        "passport_number": "Passport number",
        "absence_start_date": "Start date of the absence",
        "absence_end_date": "End date of the absence",
        "reason": "Reason for the request (Personal/Administrative, Health-related, or Career-related)",
    },
    # At least one piece of supporting documentation is always required; the
    # appropriate type depends on the stated reason (the agent judges this).
    "min_documents": 1,
    "required_documents": [],
    "document_examples": {
        "personal_administrative": "proof of travel delay (e.g. airline email), wedding/event invitation, visa-delay correspondence with authorities",
        "health_related": "doctor's note or surgery/appointment confirmation",
        "career_related": "interview invitation email or event invitation letter (career absences also require prior Program Management pre-approval)",
    },
    "approval_conditions": ["Reviewed and approved by the responsible department coordinator"],
    "source": "IE University waiver request guidelines",
}

# Field-label map promoted to module level so waiver_tools can render readable
# names in the email it sends to the student.
FIELD_LABELS = IE_WAIVER_CRITERIA["field_labels"]

_GUIDELINES_FALLBACK = """Required fields:
  Student's full name
  IE Student email
  Program
  Intake
  Section
  Passport number
  Start date of the absence
  End date of absence
  Reason of the request. 3 posible options:
    1. Personal and Administrative Matters – including visa delays, travel restrictions, administrative appointments, family celebrations, or other similar obligations.
    2. Health-Related Absences – only considered in cases of severe and/or prolonged medical conditions and must be supported by appropriate medical documentation.
    3. Career-Related Absences – such as job or internship interviews, which may be granted as exceptions only if pre-approved by Program Management on a case-by-case basis.

Attachments:
  1. At least once piece of supporting documentation is required.
    - For Personal and Administrative Matters: examples include proof of travel delays (e.g. email from airline), invitation letters for weddings/events, proof of visa delays (e.g. correspondence trail with concerned authorities)
    - Health-Related Absences: doctor's note, surgery appointment
    - Career-Related Absences: proof of appointment (e.g. interview invitation email, event invitation letter)
"""


def load_guidelines_text() -> str:
    """Return the human-readable guidelines, preferring the bundled .md copy."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "waiver_request_guidelines.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    except Exception as e:  # missing/unreadable bundle — use the embedded copy
        print(f"load_guidelines_text: using embedded fallback ({type(e).__name__}: {e})")
    return _GUIDELINES_FALLBACK


GUIDELINES_TEXT = load_guidelines_text()
