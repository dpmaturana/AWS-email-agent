#!/usr/bin/env python3
"""
Generate tests/emails/test-email-waiver-medical.eml — a COMPLETE Health-Related
waiver request that contains every field required by the IE waiver guidelines
plus a generated doctor's note PDF as supporting documentation.

This one is meant to pass the completeness check and go straight to approval.
Re-run after editing to regenerate the .eml:

    .venv/bin/python scripts/make_medical_waiver_eml.py
"""

import pathlib
from email.message import EmailMessage

OUT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "emails" / "test-email-waiver-medical.eml"

STUDENT_NAME = "Irene Gracia"
STUDENT_EMAIL = "irene.gracia@student.ie.edu"   # From: address — replies come back here (SES-verified)


def build_doctor_note_pdf() -> bytes:
    """Build a minimal, valid single-page PDF doctor's note (no dependencies)."""
    lines = [
        ("HF", 16, "Clinica Salud Madrid - Internal Medicine"),
        ("F", 10, "Calle Mayor 14, 28013 Madrid, Spain  |  Tel: +34 91 555 0190"),
        ("", 10, ""),
        ("HF", 13, "MEDICAL CERTIFICATE"),
        ("", 10, ""),
        ("F", 11, "Date of issue: 26 June 2026"),
        ("F", 11, "Patient: Irene Gracia"),
        ("F", 11, "Passport number: PAX1234567"),
        ("", 10, ""),
        ("F", 11, "This is to certify that the above-named patient attended this"),
        ("F", 11, "clinic and, due to an acute medical condition requiring rest and"),
        ("F", 11, "treatment, is medically unfit to attend classes during the period:"),
        ("", 10, ""),
        ("HF", 12, "From 6 July 2026 to 10 July 2026 (both inclusive)."),
        ("", 10, ""),
        ("F", 11, "A follow-up review is scheduled after this period. Please consider"),
        ("F", 11, "this absence as medically justified."),
        ("", 10, ""),
        ("F", 11, "Dr. Alejandro Ruiz, MD"),
        ("F", 11, "Collegiate no.: 28-45-67890"),
    ]

    # Build the text content stream. TD moves the text cursor down per line.
    parts = ["BT", "1 0 0 1 56 770 Tm"]
    for font, size, text in lines:
        fref = "F2" if font == "HF" else "F1"   # F2 = Helvetica-Bold
        esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"/{fref} {size} Tf")
        parts.append(f"({esc}) Tj")
        parts.append("0 -20 TD")
    parts.append("ET")
    content = "\n".join(parts).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + body + b"\nendobj\n"

    xref_pos = len(pdf)
    n = len(objects) + 1
    pdf += b"xref\n0 %d\n" % n
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % n
    pdf += b"startxref\n%d\n%%%%EOF" % xref_pos
    return bytes(pdf)


def build_email() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{STUDENT_NAME} <{STUDENT_EMAIL}>"
    msg["To"] = "agent@ie.edu"
    msg["Subject"] = "Waiver request for class absence - medical (doctor's note attached)"

    body = """Dear Student Services,

I would like to formally request a waiver for an absence from my classes due to a Health-Related reason. I have attached the doctor's note as supporting documentation.

My details:
- Full name: Irene Gracia
- IE student email: irene.gracia@student.ie.edu
- Program: Master in Computer Science and Business Technology (MCSBT)
- Intake: September 2025
- Section: Section 1
- Passport number: PAX1234567
- Start date of the absence: 6 July 2026
- End date of the absence: 10 July 2026
- Reason for the request: Health-Related Absence (acute medical condition)

The attached medical certificate from my doctor confirms that I am medically unfit to attend classes during the dates above.

Please let me know if you need anything else to process this request.

Best regards,
Irene Gracia
"""
    msg.set_content(body)

    pdf_bytes = build_doctor_note_pdf()
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename="doctors_note.pdf",
    )
    return msg


def main():
    msg = build_email()
    OUT.write_bytes(msg.as_bytes())
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
