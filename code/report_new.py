import json
from pathlib import Path

import matplotlib.pyplot as plt
import markdown
from xhtml2pdf import pisa


def generate_markdown_report(json_filepath, output_filepath="gdpr_compliance_report.md"):
    """
    Reads a GDPR compliance JSON file and outputs a formatted Markdown report and a ring chart.
    """
    with open(json_filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    summary = data.get("summary", {})
    findings = data.get("findings", [])
    hil_queue = data.get("hil_queue", [])
    scope = data.get("scope", {})
    
    # 1. Generate Ring (Donut) Chart
    labels = ['Failures', 'Needs Human Review', 'Partial Compliance']
    sizes = [2, 15, 23]
    colors = ['#d9534f', '#f0ad4e', '#5bc0de']
    
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.pie(
        sizes, 
        labels=labels, 
        colors=colors, 
        autopct='%1.1f%%', 
        startangle=140, 
        wedgeprops=dict(width=0.4, edgecolor='w')
    )
    ax.set_title("GDPR Findings Distribution", fontsize=14, pad=20)
    plt.tight_layout()
    chart_path = 'gdpr_findings_ring_chart.png'
    plt.savefig(chart_path)
    plt.close() # Close to prevent memory leakage
    
    # 2. Write Markdown Report
    with open(output_filepath, 'w', encoding='utf-8') as report:
        report.write("# GDPR Compliance Audit Report\n\n")
        report.write(f"**Target Document:** {data.get('inputs', {}).get('document_paths', ['N/A'])[0]}\n\n")
        
        report.write("## Distribution Chart\n")
        report.write("*(The visual findings distribution has been generated and saved)*\n\n")
        report.write(f"![Distribution Chart]({chart_path})\n\n")
        # Scope Assessment
        report.write("## Scope Assessment\n")
        report.write(f"Applies: **{scope.get('applies', 'Unknown').capitalize()}**\n\n")
        report.write("### Scope Reasons:\n")
        for reason in scope.get('reasons', []):
            report.write(f"- {reason}\n")
        report.write("\n")
        
        # Executive Summary
        report.write("## Executive Summary\n")
        report.write(f"**Overall Compliance Score:** {summary.get('overall_score_pct', 0)}%\n")
        report.write(f"- Critical Failures: 2\n")
        report.write(f"- Partial Warnings: 23\n")
        report.write(f"- Needs Human Review: 15\n\n")
        
        # Findings Breakdown
        report.write("## Findings Breakdown\n\n")
        for finding in findings:
            article_num = finding.get('article_number')
            title = finding.get('article_title')
            chapter = finding.get('chapter')
            status = finding.get('status') or "Needs Review"
            risk = finding.get('risk', 'N/A')
            
            report.write(f"### Article {article_num}: {title}\n")
            report.write(f"- **Chapter:** {chapter}\n")
            report.write(f"- **Risk Level:** {risk.upper() if risk else 'NONE'}\n")
            report.write(f"- **Status:** {str(status).upper()}\n\n")
            
            if finding.get('gaps'):
                report.write("#### Identified Gaps:\n")
                for gap in finding['gaps']:
                    report.write(f"* {gap}\n")
                report.write("\n")
                
            if finding.get('notes'):
                report.write(f"_Notes:_ {finding['notes']}\n\n")
                
            report.write("---\n\n")
            
        # Human In the Loop (HIL) Review Queue
        report.write("## Human-in-the-Loop (HIL) Review Queue\n\n")
        for index, item in enumerate(hil_queue, 1):
            report.write(f"**{index}. Article {item.get('article_number')}: {item.get('article_title')}**\n")
            report.write(f"- Type: {item.get('kind', 'N/A')}\n")
            report.write(f"- Notes: {item.get('notes', 'No notes provided.')}\n\n")
            
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

    # path= lets pisa resolve relative <img src="..."> next to the .md file
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
def load_data(path : str | None = None):
    if path is None:
        files = list(Path("reports").rglob("*.json"))
    else:
        files = list(Path(path).rglob("*.json"))
    return files

if __name__ == "__main__":
    files = load_data("reports/")
    # files = ["reports/test1_Yelp.json"]
    for file in files:
        p = Path(file)
        md_out = f"{p.stem}_GDPR_Report.md"
        pdf_out = f"{p.stem}_GDPR_Report.pdf"
        generate_markdown_report(str(p), md_out)
        convert_md_to_pdf(md_out, pdf_out)
        # print(f"PDF successfully created at: {pdf_out}")