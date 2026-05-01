from typing import Any, TypedDict
from dataclass import Document, ArticlePolicy, KeywordNode

class WorkflowState(TypedDict, total=False):
    # Inputs
    document_paths: list[str]
    # reference_html_path: str
    # keyword_csv_path: str
    chunk_chars: int
    overlap_chars: int
    mapping_bundle_size: int
    mapping_max_bundles: int

    # Derived/working
    documents: list[Document]
    full_text: str
    article_policies: list[ArticlePolicy]
    relevant_articles: list[ArticlePolicy]
    scope: dict[str, Any]
    chunks: list[dict[str, Any]]
    article_store: dict[int, list[dict[str, Any]]]

    # Dummy-RAG
    rag_articles: dict[int, dict[str, Any]]  # article_number -> {text, summary, used}

    # Checks + outputs
    findings: list[dict[str, Any]]
    hil_queue: list[dict[str, Any]]
    report: dict[str, Any]

    # P1 gate / early exit
    halted: bool
    vocab_ref: str | None
