"""Render docs/architecture.png for the hackathon submission."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "docs" / "architecture.png"
W, H = 2400, 1560

BG = (244, 246, 248)
INK = (31, 41, 51)
MUTED = (82, 96, 109)
WHITE = (255, 255, 255)
LINE = (148, 163, 184)
NAVY = (15, 40, 80)
BLUE = (26, 115, 232)
GOLD = (249, 171, 0)
GREEN = (24, 128, 56)
PURPLE = (118, 39, 187)
TEAL = (13, 148, 136)
AMBER = (234, 134, 0)

FONT_R = "C:/Windows/Fonts/segoeui.ttf"
FONT_B = "C:/Windows/Fonts/segoeuib.ttf"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B if bold else FONT_R, size)


def rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill, outline=None, width=2, radius=16):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text(draw: ImageDraw.ImageDraw, xy, s, size, *, bold=False, fill=INK, anchor="lt"):
    draw.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor)


def multiline(draw: ImageDraw.ImageDraw, x, y, lines, size, *, fill=MUTED, gap=6, bold=False):
    for i, line in enumerate(lines):
        text(draw, (x, y + i * (size + gap)), line, size, fill=fill, bold=bold)


def arrow_down(draw: ImageDraw.ImageDraw, x, y1, y2, color=LINE, label: str | None = None):
    draw.line((x, y1, x, y2 - 12), fill=color, width=4)
    draw.polygon([(x - 8, y2 - 14), (x + 8, y2 - 14), (x, y2)], fill=color)
    if label:
        text(draw, (x + 12, (y1 + y2) // 2), label, 15, fill=MUTED, anchor="lm")


def arrow_right(draw: ImageDraw.ImageDraw, x1, y, x2, color=LINE):
    draw.line((x1, y, x2 - 12, y), fill=color, width=4)
    draw.polygon([(x2 - 14, y - 8), (x2 - 14, y + 8), (x2, y)], fill=color)


def header_bar(draw: ImageDraw.ImageDraw, box, color, title: str):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=16, fill=WHITE, outline=color, width=3)
    draw.rounded_rectangle((x1, y1, x2, y1 + 48), radius=16, fill=color)
    draw.rectangle((x1, y1 + 24, x2, y1 + 48), fill=color)
    text(draw, ((x1 + x2) // 2, y1 + 24), title, 20, bold=True, fill=WHITE, anchor="mm")


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    text(d, (80, 36), "DataMesh Warden", 44, bold=True, fill=NAVY)
    text(
        d,
        (80, 92),
        "Async agent fleet  ·  GenAI SDK  ·  Gemini + Gemma  ·  Cloud Run  ·  Firestore  ·  BigQuery",
        22,
        fill=MUTED,
    )
    text(d, (2320, 48), "All Things Agentic Hackathon", 18, fill=MUTED, anchor="rt")
    text(d, (2320, 78), "Complement: datamesh_pipeline", 18, fill=MUTED, anchor="rt")

    # --- Sources ---
    header_bar(d, (80, 140, 1160, 360), AMBER, "datamesh_pipeline  ·  real ELT job")
    multiline(
        d,
        110,
        208,
        [
            "Cloud Scheduler  (hourly)  →  Cloud Run Job  pg-to-bq-sync",
            "Neon Postgres  bronze.{cliente, pedido, detalle_pedido}",
            "→  BigQuery  pg_bronze_replica  (WRITE_TRUNCATE snapshot)",
            "Failure → Cloud Run Job = Failed  +  Cloud Logging error line",
        ],
        20,
        fill=INK,
        gap=8,
    )

    header_bar(d, (1240, 140, 2320, 360), BLUE, "Incident War Room  ·  Cloud Run warden-ui")
    multiline(
        d,
        1270,
        208,
        [
            "Streamlit dashboard  (public Cloud Run service)",
            "Preset incidents  ·  Check pipeline health  ·  history",
            "Live timeline  ·  diagnosis  ·  SQL diff  ·  governance",
            "Human-in-the-loop:  Approve & execute  /  Reject",
        ],
        20,
        fill=INK,
        gap=8,
    )

    arrow_down(d, 620, 360, 430, AMBER, "job Failed  →  POST /pipelines/{job}/check")
    arrow_down(d, 1780, 360, 430, BLUE, "POST /events/ingest")

    # --- API ---
    rounded(d, (80, 430, 2320, 1040), WHITE, outline=BLUE, width=4, radius=20)
    d.rectangle((80, 430, 2320, 488), fill=NAVY)
    d.pieslice((80, 430, 120, 470), 180, 270, fill=NAVY)
    d.pieslice((2280, 430, 2320, 470), 270, 360, fill=NAVY)
    text(
        d,
        (1200, 459),
        "Cloud Run  warden-api   (private)   ·   FastAPI + asyncio   ·   returns 202, orchestrates in background",
        22,
        bold=True,
        fill=WHITE,
        anchor="mm",
    )

    rounded(d, (110, 510, 2290, 640), (232, 240, 254), outline=BLUE, width=2, radius=12)
    text(d, (140, 528), "WardenOrchestrator", 24, bold=True, fill=NAVY)
    text(d, (140, 566), "Gemini 3.1 Pro   ·   google-genai (GenAI SDK) function-calling loop", 20, fill=INK)
    text(
        d,
        (140, 598),
        "Tools: investigate_incident_logs  ·  generate_and_test_patch  ·  verify_governance_policy",
        20,
        fill=MUTED,
    )
    text(d, (2258, 575), "max 8 turns  ·  90s/turn  ·  240s budget", 18, fill=MUTED, anchor="rm")

    # Sub-agents
    agents = [
        (
            110,
            GOLD,
            "Sub-agent 1  ·  log triage",
            [
                "Gemma 2  (gemma-2-9b-it)",
                "Reads Cloud Logging / incident evidence",
                "Returns DiagnosticFinding",
                "Schema drift · permissions · broken job",
            ],
        ),
        (
            850,
            GREEN,
            "Sub-agent 2  ·  patch + sandbox",
            [
                "Gemini  (patch generation)",
                "BigQuery dry-run + table CLONE sandbox",
                "Returns SQLPatchPayload",
                "Production is never touched here",
            ],
        ),
        (
            1590,
            PURPLE,
            "Sub-agent 3  ·  governance",
            [
                "Gemini + policy rules",
                "PII drop guards · steward bindings",
                "Returns GovernanceAudit",
                "BLOCK disables Approve in the UI",
            ],
        ),
    ]
    for x, color, title, lines in agents:
        header_bar(d, (x, 670, x + 700, 1010), color, title)
        multiline(d, x + 28, 740, lines, 20, fill=INK, gap=10)

    arrow_down(d, 460, 1040, 1110, GOLD)
    arrow_down(d, 1200, 1040, 1110, GREEN)
    arrow_down(d, 1940, 1040, 1110, PURPLE)

    # --- Data plane ---
    header_bar(d, (80, 1110, 780, 1400), AMBER, "Firestore  ·  memory + audit trail")
    multiline(
        d,
        110,
        1178,
        [
            "incidents/{id}                 IncidentState",
            "…/steps/{n}                    AgentStepLog",
            "…/findings/{fid}               DiagnosticFinding",
            "…/patches/{pid}                SQLPatchPayload",
            "…/audits/{aid}                 GovernanceAudit",
        ],
        18,
        fill=INK,
        gap=8,
    )

    header_bar(d, (850, 1110, 1550, 1400), BLUE, "BigQuery  ·  target + sandbox")
    multiline(
        d,
        880,
        1178,
        [
            "Pipeline replica: pg_bronze_replica",
            "Sandbox: clone table, run proposed DDL",
            "Dry-run production SQL before apply",
            "RemediationExecutor writes only after",
            "human Approve  +  governance PASS",
        ],
        18,
        fill=INK,
        gap=8,
    )

    header_bar(d, (1620, 1110, 2320, 1400), TEAL, "Vertex AI / Gemini API")
    multiline(
        d,
        1650,
        1178,
        [
            "Gemini 3.1 Pro  — orchestrator",
            "Gemini 3.5 Flash — patch + governance",
            "Gemma 2 — log triage sub-agent",
            "Accessed via google-genai SDK",
            "Local mode: in-memory stubs, no GCP",
        ],
        18,
        fill=INK,
        gap=8,
    )

    text(
        d,
        (1200, 1470),
        "Human-in-the-loop: production SQL runs only after Approve.  UI never calls Gemini — it only polls the private API.",
        20,
        fill=MUTED,
        anchor="mm",
    )
    text(
        d,
        (1200, 1512),
        "GCP project dataengineering-505822  ·  region us-central1  ·  repos jaime-sql/datamesh_warden + jaime-sql/datamesh_pipeline",
        18,
        fill=MUTED,
        anchor="mm",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT}  {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
