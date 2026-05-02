from pathlib import Path

from graph import build_graph
from report import make_report

def load_data(path : str | None = None):
    if path is None:

        for label, folder, ext in [('Post-GDPR', '../data/post_gdpr', '*.xml')]:
            post_gdpr_files = list(Path(folder).rglob(ext))
            print(f'{label}: {len(post_gdpr_files)} {ext} files found')
        for label, folder, ext in [('Pre-GDPR', '../data/pre_gdpr', '*.md')]:
            pre_gdpr_files = list(Path(folder).rglob(ext))
            print(f'{label}: {len(pre_gdpr_files)} {ext} files found')

        files = post_gdpr_files + pre_gdpr_files
    else:
        files = list(Path(path).rglob('*.xml')) + list(Path(path).rglob('*.md'))
    return files

def run_workflow(document_paths: list[str], local: bool = False):
    compiled_graph = build_graph(local=local)
    out = compiled_graph.invoke(
        {
            "document_paths": document_paths,
 
            "chunk_chars": 1200,
            "overlap_chars": 200,
            "mapping_bundle_size": 7,
            "mapping_max_bundles": 7,
        }
    )
    return out.get("report") or {}


if __name__ == "__main__":

    files = load_data("../data/sample")
    report = run_workflow([str(files[0])], local=False)
    pdf_path = make_report(report)
    print(f"Report saved in reports/{pdf_path.name}")
    print(report)
    # print(files)
    # txt_files = [preprocess_file(str(file)) for file in files]




