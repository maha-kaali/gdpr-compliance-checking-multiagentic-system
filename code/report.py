"""Create a professional PDF report (and optional annotated policy PDF).

Output structure (single final PDF):
- Page 1: Executive summary (overall + chapter scores, severity counts, top gaps)
- Page 2: Scorecard table (one row per checked article)
- Pages 3–N (PDF inputs only): Annotated policy pages (highlights)
- Appendix: Finding detail (per-article)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


@dataclass(frozen=True)
class ScoredFinding:
    article_number: int
    article_title: str
    priority: str
    chapter: str | None
    score: int | None
    severity: str  # critical|warning|info
    status_label: str
    risk: str | None
    evidence: list[str]
    gaps: list[str]
    notes: str | None
    raw: dict[str, Any]


def _slug(s: str, max_len: int = 72) -> str:
    s = re.sub(r"[^\w\-]+", "_", (s or "").strip(), flags=re.UNICODE)
    s = s.strip("_") or "policy"
    return s[:max_len]


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    t = escape(str(text or ""))
    t = t.replace("\n", "<br/>")
    return Paragraph(t, style)


def _clamp_int(v: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(v))))


def _chapter_weight(chapter: str | None) -> float:
    # Based on your stated architecture defaults.
    ch = (chapter or "").lower()
    if "ch.2" in ch or "principles" in ch:
        return 1.4
    if "ch.3" in ch or "rights" in ch:
        return 1.4
    if "ch.5" in ch or "transfers" in ch:
        return 1.2
    if "ch.4" in ch or "controller" in ch or "processor" in ch:
        return 1.0
    if "ch.9" in ch:
        return 1.0
    return 0.8


def _score_article(f: dict[str, Any]) -> tuple[int | None, str, str]:
    """Return (score_0_100 or None, severity, status_label)."""
    p = (f.get("priority") or "").lower()

    # P2: auto-scoreable
    if p == "p2":
        status = (str(f.get("status") or "")).lower()
        gaps = f.get("gaps") or []
        base = {"pass": 100, "partial": 60, "fail": 0}.get(status, 50)

        # Proportional penalty for gaps; keeps it simple and predictable.
        try:
            n_gaps = len(gaps) if isinstance(gaps, list) else 0
        except Exception:
            n_gaps = 0
        score = base - min(50, n_gaps * 8)
        score = _clamp_int(score)

        if status == "fail":
            return score, "critical", status
        if status == "partial":
            return score, "warning", status
        # pass
        return score, "info", status

    # P3: presence + implementation unverified => capped at 70 if present
    if p == "p3":
        present = bool(f.get("policy_present"))
        if present:
            return 70, "warning", "present (unverified)"
        return 0, "critical", "missing"

    # P4: conditional; only included when triggered; no numeric score
    if p == "p4":
        return None, "warning", "triggered (HIL)"

    return None, "info", "n/a"


def _to_scored_findings(report: dict[str, Any]) -> list[ScoredFinding]:
    out: list[ScoredFinding] = []
    for f in (report.get("findings") or []):
        try:
            art_no = int(f.get("article_number"))
        except Exception:
            continue
        title = str(f.get("article_title") or "")
        pr = str(f.get("priority") or "")
        ch = f.get("chapter")
        score, sev, st_label = _score_article(f)
        out.append(
            ScoredFinding(
                article_number=art_no,
                article_title=title,
                priority=pr,
                chapter=str(ch) if ch is not None else None,
                score=score,
                severity=sev,
                status_label=st_label,
                risk=str(f.get("risk") or "") or None,
                evidence=list(f.get("evidence") or []) if isinstance(f.get("evidence") or [], list) else [],
                gaps=list(f.get("gaps") or []) if isinstance(f.get("gaps") or [], list) else [],
                notes=str(f.get("notes") or "") or None,
                raw=f,
            )
        )
    out.sort(key=lambda x: (x.article_number, x.priority))
    return out


def _compute_scores(scored: list[ScoredFinding], *, halted: bool) -> dict[str, Any]:
    # Chapter rollups (weighted avg of article scores where score != None)
    ch_groups: dict[str, list[tuple[int, float]]] = {}
    for s in scored:
        if s.score is None:
            continue
        key = s.chapter or "Unknown chapter"
        ch_groups.setdefault(key, []).append((s.score, _chapter_weight(s.chapter)))

    chapter_scores: dict[str, int] = {}
    for ch, items in ch_groups.items():
        num = sum(score * w for score, w in items)
        den = sum(w for _, w in items) or 1.0
        chapter_scores[ch] = _clamp_int(num / den)

    # Overall weighted average across all scored articles
    all_items: list[tuple[int, float]] = []
    for s in scored:
        if s.score is None:
            continue
        all_items.append((s.score, _chapter_weight(s.chapter)))
    if all_items:
        num = sum(score * w for score, w in all_items)
        den = sum(w for _, w in all_items) or 1.0
        overall = _clamp_int(num / den)
    else:
        overall = 0

    # Hard ceiling: if P1 fails/halts, cap at 40 (per your model)
    if halted:
        overall = min(overall, 40)

    # Severity counts
    sev_counts = {"critical": 0, "warning": 0, "info": 0}
    for s in scored:
        sev_counts[s.severity] = sev_counts.get(s.severity, 0) + 1

    return {
        "overall_score_pct": overall,
        "chapter_scores": chapter_scores,
        "severity_counts": sev_counts,
    }


def _top_gaps(scored: list[ScoredFinding], k: int = 3) -> list[str]:
    msgs: list[str] = []
    for s in scored:
        if s.severity not in ("critical", "warning"):
            continue
        if s.priority == "p2" and s.gaps:
            msgs.append(f"Art. {s.article_number} ({s.article_title}): {s.gaps[0]}")
        elif s.priority == "p3":
            msgs.append(f"Art. {s.article_number} ({s.article_title}): topic {'present but unverified' if s.raw.get('policy_present') else 'not present'}")
        elif s.priority == "p4":
            w = s.raw.get("what_to_review")
            if w:
                msgs.append(f"Art. {s.article_number} ({s.article_title}): {str(w)[:140]}")
            else:
                msgs.append(f"Art. {s.article_number} ({s.article_title}): triggered; human review required")
        if len(msgs) >= k:
            break
    return msgs


def _build_cover_and_scorecard_pdf(
    *,
    out_path: Path,
    report: dict[str, Any],
    scored: list[ScoredFinding],
    scoring: dict[str, Any],
) -> None:
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name="TitleCustom",
        parent=styles["Heading1"],
        fontSize=18,
        spaceAfter=12,
        textColor=colors.HexColor("#1a365d"),
        alignment=TA_CENTER,
    )
    h2 = ParagraphStyle(
        name="H2Custom",
        parent=styles["Heading2"],
        fontSize=13,
        spaceBefore=10,
        spaceAfter=8,
        textColor=colors.HexColor("#2c5282"),
    )
    body = ParagraphStyle(name="BodyCustom", parent=styles["Normal"], fontSize=10, leading=14, alignment=TA_LEFT)
    small = ParagraphStyle(
        name="SmallMono",
        parent=styles["Normal"],
        fontName="Courier",
        fontSize=7.5,
        leading=9,
        alignment=TA_LEFT,
    )

    halted = bool(report.get("halted"))
    summary = report.get("summary") or {}
    inputs = report.get("inputs") or {}
    doc_paths = inputs.get("document_paths") or []

    story: list = []
    story.append(_p("GDPR compliance report", title_style))
    story.append(Spacer(1, 0.1 * cm))
    story.append(_p(f"<b>Generated:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", body))
    story.append(_p(f"<b>Halted (P1):</b> {halted}", body))
    story.append(_p(f"<b>Overall score:</b> {scoring.get('overall_score_pct', 0)}%", body))
    story.append(
        _p(
            f"<b>Critical / Warning / Info:</b> "
            f"{scoring.get('severity_counts', {}).get('critical', 0)} / "
            f"{scoring.get('severity_counts', {}).get('warning', 0)} / "
            f"{scoring.get('severity_counts', {}).get('info', 0)}",
            body,
        )
    )
    story.append(Spacer(1, 0.25 * cm))

    if doc_paths:
        story.append(_p("<b>Documents</b>", h2))
        for p in doc_paths:
            story.append(_p(f"• {p}", body))
        story.append(Spacer(1, 0.15 * cm))

    story.append(_p("<b>Chapter scores</b>", h2))
    ch_scores = scoring.get("chapter_scores") or {}
    if ch_scores:
        rows = [["Chapter", "Score"]]
        for ch, sc in sorted(ch_scores.items(), key=lambda kv: kv[0]):
            rows.append([ch, f"{sc}%"])
        t = Table(rows, colWidths=[13.5 * cm, 3 * cm])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c5282")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e0")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(t)
    else:
        story.append(_p("No chapter scores available (no scored findings).", body))

    story.append(Spacer(1, 0.25 * cm))
    story.append(_p("<b>Top urgent gaps</b>", h2))
    gaps = _top_gaps(scored, k=3)
    if gaps:
        for g in gaps:
            story.append(_p(f"• {g}", body))
    else:
        story.append(_p("No urgent gaps detected.", body))

    # Page 2: scorecard
    story.append(PageBreak())
    story.append(_p("Scorecard", title_style))
    story.append(Spacer(1, 0.15 * cm))

    hdr = ["Art.", "Chapter", "Title", "P", "Score", "Severity", "Status"]
    rows2: list[list[str]] = [hdr]
    for s in scored:
        rows2.append(
            [
                str(s.article_number),
                (s.chapter or "")[:24],
                (s.article_title or "")[:40],
                s.priority,
                "—" if s.score is None else f"{s.score}",
                s.severity,
                s.status_label[:28],
            ]
        )
    t2 = Table(rows2, colWidths=[1.0 * cm, 3.1 * cm, 5.5 * cm, 0.9 * cm, 1.2 * cm, 1.6 * cm, 3.2 * cm])
    t2.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#edf2f7")]),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(t2)

    # Lightweight scope/summary (small mono) at bottom for debugging parity with pipeline
    story.append(Spacer(1, 0.25 * cm))
    story.append(_p("Scope (Art.2/3) + pipeline summary (raw JSON)", h2))
    blob = {
        "scope": report.get("scope") or {},
        "pipeline_summary": summary,
        "computed_scoring": scoring,
    }
    story.append(Preformatted(json.dumps(blob, indent=2, ensure_ascii=False)[:9000], small, maxLineLength=96))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=1.6 * cm,
        rightMargin=1.6 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
        title="GDPR compliance report",
    )
    doc.build(story)


def _build_appendix_pdf(*, out_path: Path, report: dict[str, Any], scored: list[ScoredFinding]) -> None:
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name="TitleCustom",
        parent=styles["Heading1"],
        fontSize=16,
        spaceAfter=12,
        textColor=colors.HexColor("#1a365d"),
        alignment=TA_CENTER,
    )
    h2 = ParagraphStyle(
        name="H2Custom",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=10,
        spaceAfter=6,
        textColor=colors.HexColor("#2c5282"),
    )
    body = ParagraphStyle(name="BodyCustom", parent=styles["Normal"], fontSize=10, leading=14, alignment=TA_LEFT)
    small = ParagraphStyle(
        name="SmallMono",
        parent=styles["Normal"],
        fontName="Courier",
        fontSize=7.5,
        leading=9,
        alignment=TA_LEFT,
    )

    story: list = []
    story.append(_p("Appendix — finding detail", title_style))
    story.append(Spacer(1, 0.15 * cm))

    for s in scored:
        story.append(_p(f"Art. {s.article_number} — {s.article_title}", h2))
        story.append(_p(f"<b>Priority:</b> {s.priority} &nbsp; <b>Chapter:</b> {s.chapter or '—'}", body))
        story.append(_p(f"<b>Severity:</b> {s.severity} &nbsp; <b>Status:</b> {s.status_label}", body))
        if s.score is not None:
            story.append(_p(f"<b>Score:</b> {s.score}/100", body))
        if s.risk:
            story.append(_p(f"<b>Risk:</b> {s.risk}", body))
        if s.gaps:
            story.append(_p("<b>Gaps</b>", body))
            for g in s.gaps[:8]:
                story.append(_p(f"• {g}", body))
        if s.evidence:
            story.append(_p("<b>Evidence</b>", body))
            for ev in s.evidence[:8]:
                story.append(_p(f"• {ev}", body))
        if s.notes:
            story.append(_p("<b>Notes</b>", body))
            story.append(_p(s.notes, body))

        # Include raw for debugging/audit trail (truncated)
        raw_blob = json.dumps(s.raw, indent=2, ensure_ascii=False)
        if len(raw_blob) > 4500:
            raw_blob = raw_blob[:4500] + "\n… (truncated)"
        story.append(Preformatted(raw_blob, small, maxLineLength=96))
        story.append(Spacer(1, 0.2 * cm))

    # HIL queue detail
    hil = report.get("hil_queue") or []
    if hil:
        story.append(PageBreak())
        story.append(_p("Appendix — human review queue", title_style))
        story.append(Preformatted(json.dumps(hil, indent=2, ensure_ascii=False)[:12000], small, maxLineLength=96))

    if report.get("hil_handoff"):
        story.append(PageBreak())
        story.append(_p("Appendix — early handoff (P1)", title_style))
        story.append(Preformatted(json.dumps(report.get("hil_handoff"), indent=2, ensure_ascii=False), small, maxLineLength=96))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=1.6 * cm,
        rightMargin=1.6 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
        title="GDPR compliance report — appendix",
    )
    doc.build(story)


def _annotate_policy_pdf(
    *,
    input_pdf: Path,
    out_pdf: Path,
    scored: list[ScoredFinding],
) -> bool:
    """Create an annotated copy of the input PDF using evidence strings.

    Returns True if an annotated PDF was produced.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        return False

    doc = fitz.open(str(input_pdf))
    any_marks = False

    def color_for(sev: str) -> tuple[float, float, float]:
        if sev == "critical":
            return (1.0, 0.2, 0.2)  # red
        if sev == "warning":
            return (1.0, 0.85, 0.2)  # yellow
        return (0.2, 0.9, 0.3)  # green

    for s in scored:
        # For annotation stability, use a few longer evidence strings.
        evid = [e for e in (s.evidence or []) if isinstance(e, str) and len(e.strip()) >= 18]
        evid = evid[:6]
        if not evid:
            continue

        col = color_for(s.severity)
        for ev in evid:
            needle = ev.strip()
            if len(needle) > 160:
                needle = needle[:160]
            # Search page-by-page; stop after a few hits.
            hits = 0
            for page in doc:
                rects = page.search_for(needle)
                for r in rects[:3]:
                    annot = page.add_highlight_annot(r)
                    annot.set_colors(stroke=col)
                    annot.set_opacity(0.30)
                    annot.update()
                    any_marks = True
                    hits += 1
                if hits >= 3:
                    break

    if not any_marks:
        return False

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf), incremental=False, deflate=True)
    doc.close()
    return True


def _merge_pdfs(*, out_path: Path, parts: list[Path]) -> None:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    for p in parts:
        r = PdfReader(str(p))
        for page in r.pages:
            w.add_page(page)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        w.write(f)


def make_report(
    report: dict[str, Any],
    *,
    output_path: str | Path | None = None,
    reports_dir: str | Path | None = None,
) -> Path:
    """Write a *single* final PDF report under `reports/` and return its path.

    If the input is a PDF, also creates an annotated copy and merges it into the final report
    between the scorecard and appendix.
    """
    base = Path(reports_dir) if reports_dir is not None else Path(__file__).resolve().parent / "reports"
    base.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = (report.get("inputs") or {}).get("document_paths") or []
    first = Path(str(paths[0])) if paths else None
    stem = _slug(first.stem) if first is not None else "policy"

    if output_path is None:
        final_out = (base / f"compliance_report_{stem}_{ts}.pdf").resolve()
    else:
        p = Path(output_path)
        final_out = (p if p.is_absolute() else (base / p.name)).resolve()

    scored = _to_scored_findings(report)
    scoring = _compute_scores(scored, halted=bool(report.get("halted")))

    cover_pdf = (base / f".tmp_cover_{stem}_{ts}.pdf").resolve()
    appendix_pdf = (base / f".tmp_appendix_{stem}_{ts}.pdf").resolve()
    annotated_pdf = (base / f".tmp_annotated_{stem}_{ts}.pdf").resolve()

    _build_cover_and_scorecard_pdf(out_path=cover_pdf, report=report, scored=scored, scoring=scoring)
    _build_appendix_pdf(out_path=appendix_pdf, report=report, scored=scored)

    parts: list[Path] = [cover_pdf]

    # Annotated policy pages only if the input is a PDF and we can meaningfully highlight.
    if first is not None and first.suffix.lower() == ".pdf" and first.exists():
        ok = _annotate_policy_pdf(input_pdf=first, out_pdf=annotated_pdf, scored=scored)
        if ok:
            parts.append(annotated_pdf)

    parts.append(appendix_pdf)

    # Merge to the final output.
    _merge_pdfs(out_path=final_out, parts=parts)

    # Best-effort cleanup of temp parts
    for p in parts:
        if p.name.startswith(".tmp_") and p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    return final_out


__all__ = ["make_report"]

