"""Render synthetic prior-authorization packets and their ground truth.

The two images supplied with the case study are a pediatric clinic note and a
blank pharmacy PA form. Neither is a bariatric request, so neither can exercise a
bariatric policy: the only correct outcome for them is "no applicable guideline".
Demonstrating that the pipeline reaches a provisional affirmation, and that it
refuses to when a date falls one window short, needs packets that the policy
actually governs.

These are invented. Every packet carries a SYNTHETIC banner, and no file here
contains or is derived from real patient information.

    python scripts/make_synthetic_packets.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import PROJECT_ROOT


OUTPUT_DIR = PROJECT_ROOT / "data" / "synthetic"
PAGE = (1240, 1754)
MARGIN = 80
ORDER_DATE = "2026-09-01"
SURGERY_DATE = "2026-10-06"

# Criteria that a complete TORe packet satisfies, and the line that satisfies each.
# Keys are criterion ids; values are the packet line a reviewer would point at.
TORE_MET_BY_DEFAULT = {
    "MGB-008.TORe.1": "Age",
    "MGB-008.TORe.2": "Request type",
    "MGB-008.TORe.3": "Primary surgery outcome",
    "MGB-008.TORe.4": "BMI",
    "MGB-008.TORe.5": "Primary surgery date",
    "MGB-008.TORe.6": "Diet and activity adherence",
    "MGB-008.TORe.7": "Substance use",
    "MGB-008.TORe.8": "Psychiatric history",
    "MGB-008.TORe.9": "Psychosocial evaluation",
    "MGB-008.TORe.10": "Tobacco history",
    "MGB-008.TORe.11": "Dietary consultation",
    "MGB-008.TORe.12": "Procedure understanding",
    "MGB-008.TORe.13": "Follow-up plan",
    "MGB-008.TORe.14": "Pregnancy attestation",
    "MGB-008.TORe.15": "Facility",
    "MGB-008.TORe.16": "Primary bariatric procedure",
}


@dataclass
class Case:
    name: str
    description: str
    expected_outcome: str
    expected_statuses: dict[str, str]
    documents: dict[str, list[tuple[str, str]]]
    line_of_business: str = "Commercial"
    procedure: str = "Transoral outlet reduction (TORe)"
    expected_policy_id: str | None = "MGB-008"
    expected_route: str | None = "TORe"
    teaching_point: str = ""
    source_images: list[str] = field(default_factory=list)


def tore_packet(
    *,
    primary_surgery_date: str = "2019-06-14",
    bmi_lines: list[tuple[str, str]] | None = None,
    tobacco: str = "Never used tobacco. Lifetime non-smoker.",
    line_of_business: str = "Commercial",
    procedure: str = "Transoral outlet reduction (TORe)",
) -> dict[str, list[tuple[str, str]]]:
    """A complete TORe packet. Individual cases perturb one field."""

    clinical = [
        ("Age", "44 years"),
        ("Sex", "Female"),
    ]
    clinical += bmi_lines if bmi_lines is not None else [
        ("Height", "64 in"),
        ("Weight", "222 lb"),
        ("BMI", "38.2 kg/m2 measured 2026-08-20"),
    ]
    clinical += [
        ("Tobacco history", tobacco),
        ("Substance use", "No substance use disorder documented."),
        ("Psychiatric history", "No psychiatric disorder documented."),
        (
            "Psychosocial evaluation",
            "Completed 2026-07-15. Cleared for surgery by behavioral health provider.",
        ),
        ("Dietary consultation", "Completed 2026-07-02 with registered dietitian."),
        (
            "Diet and activity adherence",
            "Documented adherence to diet and physical activity since the primary "
            "surgery; visit notes on file for 2024, 2025 and 2026.",
        ),
        (
            "Procedure understanding",
            "Member attests she understands the surgical procedure and post "
            "procedure compliance.",
        ),
        (
            "Pregnancy attestation",
            "Member attests she is not pregnant and does not plan to become "
            "pregnant for at least 18 months after surgery.",
        ),
        ("Follow-up plan", "Follow-up planned with the bariatric care team at 2, 6 and 12 weeks."),
    ]
    return {
        "pa_request": [
            ("Member", "Dana R. Whitfield"),
            ("Member ID", "MGB-4471902"),
            ("Date of birth", "1982-04-11"),
            ("Line of business", line_of_business),
            ("Requested procedure", procedure),
            ("CPT / HCPCS", "C9785"),
            ("Request type", "Revisional bariatric procedure"),
            ("Order date", ORDER_DATE),
            ("Planned surgery date", SURGERY_DATE),
            ("Facility", "Bariatric surgery center, Metro West Surgical"),
            ("Prescriber", "A. Okonkwo, MD, Bariatric Surgery, NPI 1730154298"),
            ("Diagnosis", "Weight regain after Roux-en-Y gastric bypass"),
        ],
        "clinical_summary": clinical,
        "surgical_history": [
            ("Primary bariatric procedure", "RYGB"),
            ("Primary surgery date", primary_surgery_date),
            (
                "Primary surgery outcome",
                "Member was unable to maintain at least 50% excess body weight "
                "loss; EWL 31% at 24 months post-operatively.",
            ),
            ("Prior revisions", "None"),
            ("Complications", "None documented."),
        ],
    }


def build_cases() -> list[Case]:
    all_met = dict.fromkeys(TORE_MET_BY_DEFAULT, "met")

    cases = [
        Case(
            name="tore_affirm",
            description="Complete TORe revision packet; every criterion documented.",
            expected_outcome="provisional_affirmation",
            expected_statuses=all_met,
            documents=tore_packet(),
            teaching_point=(
                "The clear-cut approval the case study asks the system to clear "
                "without a person."
            ),
        ),
        Case(
            name="tore_temporal_fail",
            description="Identical packet; the primary surgery is eight months old.",
            expected_outcome="refer_to_human",
            expected_statuses={**all_met, "MGB-008.TORe.5": "not_met"},
            documents=tore_packet(primary_surgery_date="2026-01-02"),
            teaching_point=(
                "The temporal criterion end to end: the window is anchored to the "
                "order date, and one clause failing is enough to refer."
            ),
        ),
        Case(
            name="tore_bmi_missing",
            description="Complete packet with no height, weight, or BMI recorded.",
            expected_outcome="refer_to_human",
            expected_statuses={**all_met, "MGB-008.TORe.4": "unknown"},
            documents=tore_packet(
                bmi_lines=[("Height", "Not recorded"), ("Weight", "Not recorded")]
            ),
            teaching_point="Missing data is unknown, never a failed criterion.",
        ),
        Case(
            name="tore_tobacco_recent",
            description="Tobacco use six days inside the six-week pre-operative window.",
            expected_outcome="refer_to_human",
            expected_statuses={**all_met, "MGB-008.TORe.10": "not_met"},
            documents=tore_packet(
                tobacco="Last tobacco use 2026-08-26. Quit date documented."
            ),
            teaching_point=(
                "A second temporal window anchored to a different date: the planned "
                "surgery date, not the order date. 2026-08-26 + 6 weeks is "
                "2026-10-07, one day past the planned surgery."
            ),
        ),
        Case(
            name="tore_ambiguous_date",
            description="Primary surgery date written 09/03/25, with no other context.",
            expected_outcome="refer_to_human",
            expected_statuses={**all_met, "MGB-008.TORe.5": "unknown"},
            documents=tore_packet(primary_surgery_date="09/03/25"),
            teaching_point=(
                "Read as 3 September 2025 the surgery is two days short of a year; "
                "read as 9 March 2025 it clears. The readings disagree, so the "
                "system reports that rather than picking one."
            ),
        ),
        Case(
            name="primary_rygb_delegated",
            description="Primary RYGB, which the policy delegates to InterQual.",
            expected_outcome="refer_to_human",
            expected_statuses={"MGB-008.commercial.external": "unknown"},
            expected_route="external:InterQual (adults 18 and older)",
            procedure="Roux-en-Y gastric bypass",
            documents={
                **tore_packet(procedure="Roux-en-Y gastric bypass"),
                "surgical_history": [
                    ("Primary bariatric procedure", "None"),
                    ("Request type", "Primary bariatric surgery"),
                    ("Diagnosis", "Severe obesity, BMI 42.1 kg/m2"),
                ],
            },
            teaching_point=(
                "The commonest bariatric request is governed by criteria this "
                "policy does not contain. The system says so instead of guessing."
            ),
        ),
        Case(
            name="medicare_tore",
            description="Same TORe packet under Medicare Advantage.",
            expected_outcome="refer_to_human",
            expected_statuses={"MGB-008.medicare-advantage.external": "unknown"},
            expected_route="external:CMS NCD 100.1 and LCD L35022",
            line_of_business="Medicare Advantage",
            documents=tore_packet(line_of_business="Medicare Advantage"),
            teaching_point=(
                "Same procedure, same chart, different line of business, different "
                "governing criteria. Routing happens before any evidence is read."
            ),
        ),
        Case(
            name="out_of_domain",
            description=(
                "The two images supplied with the case study: a pediatric clinic "
                "note and a blank pharmacy PA form."
            ),
            expected_outcome="refer_to_human",
            expected_statuses={},
            expected_policy_id=None,
            expected_route=None,
            line_of_business="",
            procedure="",
            documents={},
            source_images=["med2.webp", "Prior-Authorization-Form.jpg"],
            teaching_point=(
                "No bariatric request is present, so no policy applies. The correct "
                "answer is to refer with a stated reason, not to score the packet "
                "against a guideline that does not govern it."
            ),
        ),
    ]
    return cases


# -- rendering ----------------------------------------------------------------


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def render_document(title: str, rows: list[tuple[str, str]], destination: Path) -> None:
    """Draw a plain EHR-style printout: a title, a rule, and label/value rows."""

    image = Image.new("RGB", PAGE, "white")
    draw = ImageDraw.Draw(image)
    heading = load_font(34, bold=True)
    label_font = load_font(23, bold=True)
    value_font = load_font(23)
    small = load_font(18)

    y = MARGIN
    draw.text((MARGIN, y), "SYNTHETIC RECORD - NOT A REAL PATIENT", font=small, fill="#a33")
    y += 40
    draw.text((MARGIN, y), title, font=heading, fill="black")
    y += 52
    draw.line([(MARGIN, y), (PAGE[0] - MARGIN, y)], fill="#888", width=2)
    y += 30

    label_width = 340
    wrap_width = PAGE[0] - MARGIN * 2 - label_width
    for label, value in rows:
        draw.text((MARGIN, y), f"{label}:", font=label_font, fill="#222")
        lines = wrap_text(draw, value, value_font, wrap_width)
        for offset, line in enumerate(lines):
            draw.text(
                (MARGIN + label_width, y + offset * 30), line, font=value_font, fill="black"
            )
        y += max(len(lines) * 30, 30) + 16
        draw.line([(MARGIN, y - 8), (PAGE[0] - MARGIN, y - 8)], fill="#e2e2e2", width=1)

    draw.text(
        (MARGIN, PAGE[1] - MARGIN),
        "Generated by scripts/make_synthetic_packets.py for evaluation only.",
        font=small,
        fill="#777",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if draw.textlength(candidate, font=font) <= width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines or [""]


TITLES = {
    "pa_request": "PRIOR AUTHORIZATION REQUEST",
    "clinical_summary": "CLINICAL SUMMARY",
    "surgical_history": "SURGICAL HISTORY",
}


def main() -> None:
    samples = PROJECT_ROOT / "knowledge" / "samples"
    written = 0
    for case in build_cases():
        case_dir = OUTPUT_DIR / case.name
        case_dir.mkdir(parents=True, exist_ok=True)
        documents: list[str] = []

        for key, rows in case.documents.items():
            filename = f"{key}.png"
            render_document(TITLES.get(key, key.upper()), rows, case_dir / filename)
            documents.append(filename)
            written += 1

        for name in case.source_images:
            source = samples / name
            if source.is_file():
                destination = case_dir / name
                destination.write_bytes(source.read_bytes())
                documents.append(name)
            else:
                print(f"  ! missing sample image: {source}")

        expected = {
            "case": case.name,
            "description": case.description,
            "teaching_point": case.teaching_point,
            "documents": documents,
            "input": {
                "line_of_business": case.line_of_business,
                "procedure": case.procedure,
                "order_date": ORDER_DATE,
                "planned_surgery_date": SURGERY_DATE,
            },
            "expected": {
                "outcome": case.expected_outcome,
                "policy_id": case.expected_policy_id,
                "criteria_ref": case.expected_route,
                "criteria": case.expected_statuses,
            },
        }
        (case_dir / "expected.json").write_text(
            json.dumps(expected, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{case.name:26} {len(documents)} document(s) -> {case.expected_outcome}")

    print(f"\n{written} page(s) rendered under {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
