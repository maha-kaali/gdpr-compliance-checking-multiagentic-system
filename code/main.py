from pathlib import Path

from graph import build_graph
from report import make_report
import json
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
            "mapping_max_bundles": 0,
        }
    )
    return out.get("report") or {}


if __name__ == "__main__":

    # files = load_data("../data/test_Policies_pass")
    local = False


    # files = load_data("../data/post_gdpr/data/GoPPC-150")
    files = load_data("../data/testing_files/md_files_pre_gdpr")
    print(f"Found {len(files)} files")
    if len(files) > 3: 
        print(f"Found {len(files)} files to process. Too many?")
        if local:
            print("This model is running locally, so it will take a while to process the files.")
        else:
            print("This program is using API calls, it will be costly to process the files.")

        response = input("Proceed anyway? (y/n):  ")
        if response == "clip":
            print("Clipping files to 3")
            # files = files[:]
        elif response != "y":
            print("Exiting")
            exit()
        

    for file in files:
        report = run_workflow([str(file)], local=local)
        print(report)
        # print(:)
        report_json_path = f"reports/{file.stem}.json"
        print("Saving report to", report_json_path)
        # try:
        #     with open(report_json_path, "w") as f:
        #         json.dump(report, f)
        # except Exception as e:
        #     print(f"Error saving report to {report_json_path}: {e}")
        #     continue
        # pdf_path = make_report(report)
        # print(f"Report saved in reports/{pdf_path.name}")
        print(report)


