import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import markdown
from xhtml2pdf import pisa


def _status_lower(f: dict) -> str:
    return (str(f.get("status") or "")).lower()


def summarize_report_json(data: dict[str, Any]) -> dict[str, Any]:
    """
    Derive counts for P1 (scope), P2–P4 (findings), and summary fields for charts and prose.
    """
    summary = data.get("summary") or {}
    findings: list[dict[str, Any]] = data.get("findings") or []
    scope = data.get("scope") or {}

    p2 = [f for f in findings if f.get("priority") == "p2"]
    p3 = [f for f in findings if f.get("priority") == "p3"]
    p4 = [f for f in findings if f.get("priority") == "p4"]

    n_p2_fail = sum(1 for f in p2 if _status_lower(f) == "fail")
    n_p2_partial = sum(1 for f in p2 if _status_lower(f) == "partial")
    n_p2_pass = sum(1 for f in p2 if _status_lower(f) == "pass")
    n_p2_other = len(p2) - n_p2_fail - n_p2_partial - n_p2_pass

    p3_present_true = sum(1 for f in p3 if f.get("policy_present") is True)
    p3_present_false = sum(1 for f in p3 if f.get("policy_present") is False)
    p3_present_unknown = len(p3) - p3_present_true - p3_present_false

    p4_triggered_in_findings = len(p4)
    p4_not_triggered = int(summary.get("p4_articles_not_triggered") or 0)
    p4_triggered_summary = int(summary.get("p4_triggered_total") or p4_triggered_in_findings)

    applies_raw = str(scope.get("applies") or "unknown").strip()
    applies_key = applies_raw.lower() if applies_raw else "unknown"

    return {
        "scope": scope,
        "summary": summary,
        "applies_display": applies_raw or "unknown",
        "applies_key": applies_key,
        "scope_hil_required": bool(scope.get("hil_required")),
        "halted": bool(data.get("halted")),
        "p2_total": len(p2),
        "p2_fail": n_p2_fail,
        "p2_partial": n_p2_partial,
        "p2_pass": n_p2_pass,
        "p2_other": n_p2_other,
        "p3_total": len(p3),
        "p3_present_true": p3_present_true,
        "p3_present_false": p3_present_false,
        "p3_present_unknown": p3_present_unknown,
        "p4_triggered_findings": p4_triggered_in_findings,
        "p4_not_triggered": p4_not_triggered,
        "p4_triggered_summary": p4_triggered_summary,
        "findings_total": int(summary.get("findings_total", len(findings))),
        "hil_queue_total": int(summary.get("hil_queue_total", len(data.get("hil_queue") or []))),
    }


# Semantic dashboard colors (shared meaning across P3/P4)
_COLOR_GOOD = "#2e7d32"  # green — favourable outcome
_COLOR_NEUTRAL = "#9e9e9e"  # grey — unknown / not scored
_COLOR_WARN = "#f57c00"  # orange — should fix, not worst case
_COLOR_BAD = "#c62828"  # red — high severity / scenario fired


def _draw_pie(ax, sizes: list[int], labels: list[str], colors: list[str], title: str) -> None:
    wedges = [(s, lab, col) for s, lab, col in zip(sizes, labels, colors) if s > 0]
    ax.set_title(title, fontsize=10, pad=8)
    if not wedges or sum(s for s, _, _ in wedges) == 0:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=10, transform=ax.transAxes)
        ax.set_axis_off()
        return
    sz, lab, col = zip(*wedges)
    ax.pie(
        sz,
        labels=lab,
        colors=col,
        autopct="%1.1f%%",
        startangle=140,
        wedgeprops=dict(width=0.45, edgecolor="w"),
        textprops={"fontsize": 8},
    )


def _render_priority_dashboard(metrics: dict[str, Any], chart_path: str) -> None:
    """2×2 figure: P1 scope text, P2 status donut, P3 presence donut, P4 triggered vs not."""
    fig, axes = plt.subplots(2, 2, figsize=(10, 9))

    # P1 — scope (not article rows in JSON; comes from `scope`)
    ax1 = axes[0, 0]
    ax1.set_axis_off()
    sc = metrics.get("scope") or {}
    reasons_n = len(sc.get("reasons") or [])
    title = "P1 — Scope gate"
    body = (
        f"Applies: {metrics.get('applies_display', 'N/A')}\n"
        f"HIL required: {metrics.get('scope_hil_required')}\n"
        f"Reason bullets in JSON: {reasons_n}"
    )
    ax1.text(0.5, 0.55, title, ha="center", va="center", fontsize=12, fontweight="bold", transform=ax1.transAxes)
    ax1.text(0.5, 0.28, body, ha="center", va="center", fontsize=10, linespacing=1.35, transform=ax1.transAxes)

    # P2 — pass / partial / fail / other
    ax2 = axes[0, 1]
    p2_sizes = [
        metrics["p2_fail"],
        metrics["p2_partial"] + metrics["p2_other"],
        metrics["p2_pass"],
    ]
    p2_labels = ["P2 fail", "P2 partial / other", "P2 pass"]
    p2_colors = [_COLOR_BAD, _COLOR_WARN, _COLOR_GOOD]
    if metrics["p2_total"] == 0:
        ax2.text(
            0.5,
            0.5,
            "No P2 findings\n(no article checks\nor pipeline halted)",
            ha="center",
            va="center",
            fontsize=9,
            transform=ax2.transAxes,
        )
        ax2.set_axis_off()
        ax2.set_title("P2 — Core checks (status)", fontsize=10, pad=8)
    else:
        _draw_pie(ax2, p2_sizes, p2_labels, p2_colors, "P2 — Core checks (status)")

    # P3 — policy_present (green = topic seen in policy, grey = unknown, orange = gap)
    ax3 = axes[1, 0]
    p3_sizes = [metrics["p3_present_true"], metrics["p3_present_false"], metrics["p3_present_unknown"]]
    p3_labels = ["P3 topic present", "P3 topic absent", "P3 unknown"]
    p3_colors = [_COLOR_GOOD, _COLOR_WARN, _COLOR_NEUTRAL]
    if metrics["p3_total"] == 0:
        ax3.text(0.5, 0.5, "No P3 findings", ha="center", va="center", fontsize=10, transform=ax3.transAxes)
        ax3.set_axis_off()
        ax3.set_title("P3 — Topic presence", fontsize=10, pad=8)
    else:
        _draw_pie(ax3, p3_sizes, p3_labels, p3_colors, "P3 — Topic presence")

    # P4 — triggered vs not in scope (summary carries not_triggered count)
    ax4 = axes[1, 1]
    t = metrics["p4_triggered_summary"]
    nt = metrics["p4_not_triggered"]
    if t == 0 and nt == 0:
        ax4.text(0.5, 0.5, "No P4 in-scope\narticles to chart", ha="center", va="center", fontsize=9, transform=ax4.transAxes)
        ax4.set_axis_off()
        ax4.set_title("P4 — Conditional scenarios", fontsize=10, pad=8)
    else:
        # P4: not triggered = good (green); triggered = severe (red)
        _draw_pie(
            ax4,
            [t, nt],
            ["P4 triggered\n(condition applies)", "P4 not triggered\n(no finding row)"],
            [_COLOR_BAD, _COLOR_GOOD],
            "P4 — Conditional scenarios",
        )

    fig.suptitle("GDPR pipeline: P1 scope · P2 status · P3 presence · P4 conditionals", fontsize=12, y=1.02)
    plt.tight_layout()
    fig.subplots_adjust(bottom=0.08)
    fig.text(
        0.5,
        0.02,
        "Color key — green: good/pass  ·  grey: neutral/unknown  ·  orange: gap/partial concern  ·  red: fail / P4 triggered",
        ha="center",
        fontsize=8,
        style="italic",
        color="#444444",
    )
    Path(chart_path).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(chart_path, bbox_inches="tight")
    plt.close()


def generate_markdown_report(json_filepath, output_filepath="gdpr_compliance_report.md"):
    """
    Reads a GDPR compliance JSON file and outputs a formatted Markdown report and a dashboard chart.
    """
    with open(json_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    summary = data.get("summary", {})
    findings = data.get("findings", [])
    hil_queue = data.get("hil_queue", [])
    scope = data.get("scope", {})
    halted = bool(data.get("halted"))
    json_stem = Path(json_filepath).stem

    metrics = summarize_report_json(data)
    chart_path = f"reports/{json_stem}_gdpr_findings_ring_chart.png"
    _render_priority_dashboard(metrics, chart_path)

    with open(output_filepath, "w", encoding="utf-8") as report:
        report.write("# GDPR Compliance Audit Report\n\n")
        report.write(f"**Target Document:** {data.get('inputs', {}).get('document_paths', ['N/A'])[0]}\n\n")

        report.write("## Distribution chart (P1–P4)\n")
        report.write("*(P1 = scope gate in JSON `scope`; P2–P4 = `findings` by `priority`.)*\n\n")
        report.write(f"![Distribution chart]({chart_path})\n\n")

        report.write("## Scope assessment (P1)\n")
        report.write(f"Applies: **{scope.get('applies', 'Unknown')}**\n\n")
        report.write(f"HIL required at scope: **{scope.get('hil_required', 'N/A')}**\n\n")
        report.write("### Scope reasons\n")
        for reason in scope.get("reasons", []):
            report.write(f"- {reason}\n")
        report.write("\n")

        report.write("## Executive summary\n")
        score_pct = summary.get("overall_score_pct", 0)
        findings_total = metrics["findings_total"]
        report.write(f"**Overall compliance score (P2-only index):** {score_pct}%")
        if halted and findings_total == 0:
            report.write(
                " — _not based on article checks; workflow halted at scope / P1 gate "
                "before mapping and P2 scoring._"
            )
        report.write("\n\n")

        report.write("### Summary block (`summary` in JSON)\n")
        for key in sorted(summary.keys()):
            report.write(f"- **{key}:** {summary[key]}\n")
        report.write("\n")

        report.write("### Counts used in the chart\n")
        report.write(
            f"- **P2:** total {metrics['p2_total']} — fail / partial / pass / other: "
            f"{metrics['p2_fail']} / {metrics['p2_partial']} / {metrics['p2_pass']} / {metrics['p2_other']}\n"
        )
        report.write(
            f"- **P3:** total {metrics['p3_total']} — topic present / absent / unknown: "
            f"{metrics['p3_present_true']} / {metrics['p3_present_false']} / {metrics['p3_present_unknown']}\n"
        )
        report.write(
            f"- **P4:** triggered (summary) {metrics['p4_triggered_summary']}, "
            f"triggered rows in `findings` {metrics['p4_triggered_findings']}, "
            f"not triggered in scope {metrics['p4_not_triggered']}\n"
        )
        report.write(f"- **HIL queue items:** {metrics['hil_queue_total']}\n\n")

        report.write("## Findings breakdown (P2 / P3 / P4)\n\n")
        for finding in findings:
            pri = finding.get("priority") or "unknown"
            article_num = finding.get("article_number")
            title = finding.get("article_title")
            chapter = finding.get("chapter")
            status = finding.get("status")
            risk = finding.get("risk", "N/A")

            report.write(f"### Article {article_num}: {title}\n")
            report.write(f"- **Priority:** {pri.upper()}\n")
            report.write(f"- **Chapter:** {chapter}\n")
            if pri == "p3":
                report.write(f"- **Policy present:** {finding.get('policy_present')}\n")
            if pri == "p4":
                report.write(f"- **P4 triggered:** {finding.get('p4_triggered')}\n")
                if finding.get("what_to_review"):
                    report.write(f"- **What to review:** {finding['what_to_review']}\n")
            report.write(f"- **Risk level:** {risk.upper() if risk else 'NONE'}\n")
            st_display = status if status is not None else "N/A (P3/P4 or unscored)"
            report.write(f"- **Status:** {str(st_display).upper()}\n\n")

            if finding.get("gaps"):
                report.write("#### Identified gaps\n")
                for gap in finding["gaps"]:
                    report.write(f"* {gap}\n")
                report.write("\n")

            if finding.get("notes"):
                report.write(f"_Notes:_ {finding['notes']}\n\n")

            report.write("---\n\n")

        report.write("## Human-in-the-loop (HIL) review queue\n\n")
        for index, item in enumerate(hil_queue, 1):
            if item.get("kind"):
                report.write(f"**{index}. Article {item.get('article_number')}: {item.get('article_title')}**\n")
                report.write(f"- Type: {item.get('kind', 'N/A')}\n")
                report.write(f"- Notes: {item.get('notes', 'No notes provided.')}\n\n")
            else:
                report.write(f"**{index}. Gate handoff**\n")
                report.write(f"- {item.get('human_intervention', 'Human review')}\n")
                report.write(f"- Reason: {item.get('reason', 'N/A')}\n\n")

    print(f"Report successfully generated at: {output_filepath}")


def convert_md_to_pdf(md_filepath, output_pdf_filepath):
    """
    Converts Markdown to HTML and renders PDF with xhtml2pdf (ReportLab-based, no native Cairo stack).
    """
    md_path = Path(md_filepath).resolve()
    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    html_body = markdown.markdown(md_content)

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>GDPR Audit Report</title>
    <style>
        body {{
            font-family: Arial, Helvetica, sans-serif;
            margin: 20px;
            color: #333;
        }}
        h1, h2, h3 {{
            color: #2c3e50;
        }}
        img {{
            max-width: 80%;
            height: auto;
            display: block;
            margin: 20px auto;
        }}
        hr {{
            border: 0;
            border-top: 1px solid #ccc;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
{html_body}
</body>
</html>
"""

    with open(output_pdf_filepath, "wb") as pdf_file:
        status = pisa.CreatePDF(
            html_content,
            dest=pdf_file,
            encoding="utf-8",
            path=str(md_path),
        )
    if status.err:
        raise RuntimeError(f"PDF conversion failed ({status.err} renderer error(s)); check logs above.")
    print(f"PDF successfully created at: {output_pdf_filepath}")


def load_data(path: str | None = None):
    if path is None:
        files = list(Path("reports").rglob("*.json"))
    else:
        files = list(Path(path).rglob("*.json"))
    return files


if __name__ == "__main__":
    files = load_data("reports/")
    for file in files:
        p = Path(file)
        md_out = f"{p.stem}_GDPR_Report.md"
        pdf_out = f"{p.stem}_GDPR_Report.pdf"
        generate_markdown_report(str(p), md_out)
        convert_md_to_pdf(md_out, pdf_out)
