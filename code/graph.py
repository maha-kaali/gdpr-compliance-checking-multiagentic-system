import json
import math
from typing import Any, TypedDict, Iterable
from pathlib import Path 
from langgraph.graph import END, START, StateGraph

from dataclass import Document, ArticlePolicy, KeywordNode, Prompts
from states import WorkflowState
from local_model import _build_json_llm_agent
from utils import (
    _normalize_text,
    preprocess_file,
    _load_scope_articles_text,
    _chunk_text,
    _normalize_scope_output,
    _format_chunks_for_prompt,
)
from prompts import DEFAULT_PROMPTS
from rag import dummy_rag_fetch_article


def load_metadata(*, load_article_policies: bool = False, load_keyword_nodes: bool = False):
    """Load one or both metadata files; only requested sides are read (no accidental overwrite)."""
    article_policies = None
    keyword_nodes = None
    if load_article_policies:
        with open("../metadata/article_policies.json", "r", encoding="utf-8") as f:
            article_policies = [ArticlePolicy(**p) for p in json.load(f)]
    if load_keyword_nodes:
        with open("../metadata/keyword_nodes.json", "r", encoding="utf-8") as f:
            keyword_nodes = [KeywordNode(**k) for k in json.load(f)]
    return article_policies, keyword_nodes

def filter_nonskip_articles(articles: Iterable[ArticlePolicy]) -> Iterable[ArticlePolicy]:
    for a in articles:
        if a.priority != "skip":
            yield a

def _p1_should_halt(scope: dict[str, Any]) -> tuple[bool, str]:
    """Halt after scope gate using only the scope LLM output (Art.2/3), not keyword heuristics."""
    applies = (str(scope.get("applies") or "")).strip().lower()
    if applies != "yes":
        return True, f"GDPR scope not confirmed as in-scope (applies={scope.get('applies')!r})"
    if _as_bool(scope.get("hil_required")):
        return True, "Scope gate flagged human escalation (hil_required)"
    return False, ""

def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y")
    return bool(v)

def hil_handoff(*, reason: str) -> dict[str, Any]:
    """Human-in-the-loop handoff placeholder: stop pipeline and surface HIL.

    Returns a small dict the report/hil_queue can embed; extend later with tickets/UI.
    """
    return {
        "human_intervention": "Human intervention needed",
        "reason": reason,
    }

def iter_mapping_articles(articles: Iterable[ArticlePolicy]) -> Iterable[ArticlePolicy]:
    """Articles that participate in chunk→article mapping (excludes p1 scope/vocab and skip)."""
    for a in articles:
        if a.priority in ("p2", "p3", "p4"):
            yield a



def build_graph(local : bool = False):

    prompts = DEFAULT_PROMPTS

    scope_agent = _build_json_llm_agent(required_keys=["applies", "reasons", "evidence", "hil_required"], local=local)
    mapping_agent = _build_json_llm_agent(required_keys=["article_numbers", "notes"], local=local)
    p2_check_agent = _build_json_llm_agent(required_keys=["status", "gaps", "evidence", "risk", "notes"], local=local)
    p3_detect_agent = _build_json_llm_agent(required_keys=["policy_present", "evidence", "notes"], local=local)
    p4_conditional_agent = _build_json_llm_agent(
        required_keys=["triggered", "evidence", "what_to_review", "notes"], local=local
    )

    def ingestion_node(state: WorkflowState) -> WorkflowState:
        print("Inside ingestion_node")
        paths = state.get("document_paths") or []
        docs = [preprocess_file(p) for p in paths] # todo : here provision for multiple documents with policies for the same company, can add multiple documents with policies for the same company, 
        full_text = _normalize_text("\n\n".join(d for d in docs))
        print("Ingestion node completed")
        return {"documents": docs, "full_text": full_text}
    
    def load_reference_node(state: WorkflowState) -> WorkflowState:
        print("Inside load_reference_node")
        article_policies, _ = load_metadata(load_article_policies=True, load_keyword_nodes=False)
        article_policies = article_policies or []
        relevant = list(filter_nonskip_articles(article_policies))
        print("Load reference node completed")
        return {"relevant_articles": relevant}

    def scope_gate_node(state: WorkflowState) -> WorkflowState:
        print("Inside scope_gate_node")
        scope_ref_text = _load_scope_articles_text("../metadata/scope_gate.json")
        full_text = state.get("full_text") or ""
        user_prompt = (
            "Use the following GDPR reference text for scope assessment.\n\n"
            f"{scope_ref_text}\n\n"
            "Now analyze this company policy text:\n\n"
            + (full_text[:1200])
        )
        scope = _normalize_scope_output(scope_agent(prompts.scope_gate_system, user_prompt) or {}) or {
            "applies": "unclear",
            "reasons": [],
            "evidence": [],
            "hil_required": True,
        }
        print("Scope gate node completed")
        return {"scope": scope}

    def p1_gate_node(state: WorkflowState) -> WorkflowState:
        print("Inside p1_gate_node")
        scope = state.get("scope") or {}
        halt, reason = _p1_should_halt(scope)
        if halt:
            hi = hil_handoff(reason=reason)
            document_paths = state.get("document_paths") or []
            hil_queue = [hi]
            report: dict[str, Any] = {
                "halted": True,
                "hil_handoff": hi,
                "vocab_preview": None,
                "inputs": {"document_paths": document_paths},
                "scope": scope,
                "summary": {
                    "overall_score_pct": 0,
                    "p2_score": 0.0,
                    "p2_findings_total": 0,
                    "p3_findings_total": 0,
                    "p4_triggered_total": 0,
                    "p4_articles_not_triggered": 0,
                    "findings_total": 0,
                    "hil_queue_total": len(hil_queue),
                },
                "findings": [],
                "hil_queue": hil_queue,
            }
            print("P1 gate node completed. Halted.")
            return {"halted": True, "report": report, "findings": [], "hil_queue": hil_queue}
        print("P1 gate node completed. Not halted.")
        return {"halted": False}

    def build_vocab_node(state: WorkflowState) -> WorkflowState:
        print("Inside build_vocab_node")
        p = Path("../metadata/art4_vocab.json")
        if not p.is_file():
            return {"vocab_ref": ""}
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {"vocab_ref": ""}
        text = (data.get("text") or "").strip()
        print("Build vocab node completed")
        return {"vocab_ref": text[:14000]}

    def chunking_node(state: WorkflowState) -> WorkflowState:
        print("Inside chunking_node")
        full_text = state.get("full_text") or ""
        chunk_chars = int(state.get("chunk_chars") or 1200)
        overlap_chars = int(state.get("overlap_chars") or 200)
        chunks = _chunk_text(full_text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
        print("Chunking node completed. Total chunks:", len(chunks))
        return {"chunks": chunks}

    def mapping_node(state: WorkflowState) -> WorkflowState:
        print("Inside mapping_node")
        kw_json_path = "../metadata/keyword_nodes.json"
        with open(kw_json_path, "r", encoding="utf-8") as f:
            kw_nodes_raw = json.load(f)

        chunks = state.get("chunks") or []
        relevant_articles = state.get("relevant_articles") or []
        mapping_articles = list(iter_mapping_articles(relevant_articles))
        mapping_bundle_size = max(1, int(state.get("mapping_bundle_size") or 7))
        mapping_max_bundles = int(state.get("mapping_max_bundles") or 0)

        relevant_set = {int(a.number) for a in mapping_articles}
        kw_filtered: list[Any] = []
        for n in kw_nodes_raw:
            if not isinstance(n, dict):
                continue
            try:
                node_no = int(n.get("node", -1))
            except Exception:
                continue
            if node_no in relevant_set:
                kw_filtered.append(n)

        vocab = (state.get("vocab_ref") or "").strip()
        vocab_block = ""
        if vocab:
            vocab_block = "\n\nShared GDPR definitions (Art.4 vocabulary):\n" + vocab[:8000] + "\n"

        keyword_reference = json.dumps(kw_filtered, ensure_ascii=False)
        mapping_system_prompt = (
            "You are a GDPR mapping agent.\n"
            "Given a small bundle of policy text, return which GDPR articles it is relevant to.\n"
            "Only return article numbers present in the keyword reference (P2–P4 set).\n"
            "Return JSON with keys:\n"
            "- article_numbers: array of integers (GDPR article numbers). Use [] if none.\n"
            "- notes: short string.\n\n"
            "Keyword reference JSON (each item: {node:<article_number>, keywords:[...]}):\n"
            f"{keyword_reference}"
            f"{vocab_block}"
        )

        article_store: dict[int, list[dict[str, Any]]] = {n: [] for n in relevant_set}
        seen_by_art: dict[int, set[str]] = {n: set() for n in relevant_set}

        bundle_count = 0
        for i in range(0, len(chunks), mapping_bundle_size):
            if mapping_max_bundles > 0 and bundle_count >= mapping_max_bundles:
                break
            b_chunks = chunks[i : i + mapping_bundle_size]
            if not b_chunks:
                continue

            bundle_text = "\n\n".join((c.get("text") or "").strip() for c in b_chunks if (c.get("text") or "").strip())
            if not bundle_text:
                continue

            js = mapping_agent(mapping_system_prompt, bundle_text) or {}
            arts = js.get("article_numbers") or []
            if isinstance(arts, (int, str)):
                arts = [arts]

            cleaned: list[int] = []
            for a in arts:
                try:
                    n = int(str(a).strip())
                except Exception:
                    continue
                if n in relevant_set:
                    cleaned.append(n)

            for art_no in cleaned:
                for c in b_chunks:
                    cid = str(c.get("chunk_id"))
                    if cid in seen_by_art[art_no]:
                        continue
                    article_store[art_no].append(c)
                    seen_by_art[art_no].add(cid)

            bundle_count += 1
        print("Mapping node completed.")
        return {"article_store": article_store}

    def rag_fetch_node(state: WorkflowState) -> WorkflowState:
        print("Inside rag_fetch_node")
        relevant_articles = state.get("relevant_articles") or []
        rag: dict[int, dict[str, Any]] = {}
        for art in iter_mapping_articles(relevant_articles):
            rag[art.number] = dummy_rag_fetch_article(art.number)
        print("Rag fetch node completed.")
        return {"rag_articles": rag}
    
    def run_checks_node(state: WorkflowState) -> WorkflowState:
        print("Inside run_checks_node")
        relevant_articles = state.get("relevant_articles") or []
        article_store = state.get("article_store") or {}
        rag_articles = state.get("rag_articles") or {}
        vocab = (state.get("vocab_ref") or "").strip()
        vocab_suffix = ""
        if vocab:
            vocab_suffix = "\n\n(Art.4 definitions excerpt for terminology)\n" + vocab[:4000]

        findings: list[dict[str, Any]] = []
        p2_articles = [a for a in relevant_articles if a.priority == "p2"]
        p3_articles = [a for a in relevant_articles if a.priority == "p3"]
        p4_articles = [a for a in relevant_articles if a.priority == "p4"]

        for art in p2_articles:
            rel_chunks = article_store.get(art.number, []) or []
            rag = rag_articles.get(art.number) or {}
            article_material = rag.get("text") or rag.get("summary") or ""
            chunks_str = _format_chunks_for_prompt(rel_chunks)
            user_prompt = (
                f"GDPR Article {art.number}: {art.title}\n"
                f"Agent guidance: {art.action}\n\n"
                "(Reference article material from RAG)\n"
                + article_material
                + "\n\nRELEVANT POLICY CHUNKS:\n"
                + chunks_str
                + vocab_suffix
            )
            js = p2_check_agent(prompts.p2_core_system, user_prompt) or {}
            st = (str(js.get("status") or "")).lower()
            if st not in ("pass", "partial", "fail"):
                st = "partial"
            rk = (str(js.get("risk") or "")).lower()
            if rk not in ("low", "medium", "high", "critical"):
                rk = "medium"
            findings.append(
                {
                    "article_number": art.number,
                    "article_title": art.title,
                    "chapter": art.chapter,
                    "priority": "p2",
                    "status": st,
                    "gaps": js.get("gaps") or [],
                    "evidence": js.get("evidence") or [],
                    "risk": rk,
                    "needs_human_review": False,
                    "notes": js.get("notes"),
                    "article_material_used": rag.get("used"),
                }
            )

        for art in p3_articles:
            rel_chunks = article_store.get(art.number, []) or []
            chunks_str = _format_chunks_for_prompt(rel_chunks)
            user_prompt = (
                f"GDPR Article {art.number}: {art.title}\n"
                f"What to look for (agent action): {art.action}\n\n"
                "Does the policy materially mention or address the topics this article covers?\n\n"
                "RELEVANT POLICY CHUNKS:\n"
                + chunks_str
                + vocab_suffix
            )
            js = p3_detect_agent(prompts.p3_detect_system, user_prompt) or {}
            pres = _as_bool(js.get("policy_present"))
            findings.append(
                {
                    "article_number": art.number,
                    "article_title": art.title,
                    "chapter": art.chapter,
                    "priority": "p3",
                    "policy_present": pres,
                    "implementation_unverified": bool(pres),
                    "needs_human_review": True,
                    "evidence": js.get("evidence") or [],
                    "notes": js.get("notes"),
                    "status": None,
                    "gaps": [],
                    "risk": None,
                }
            )

        full_text = state.get("full_text") or ""
        policy_excerpt = full_text[:12000]

        for art in p4_articles:
            user_prompt = (
                f"GDPR Article {art.number}: {art.title}\n"
                f"Conditional scenario / agent action: {art.action}\n\n"
                "POLICY TEXT (excerpt):\n"
                + policy_excerpt
            )
            js = p4_conditional_agent(prompts.p4_conditional_system, user_prompt) or {}
            if not _as_bool(js.get("triggered")):
                continue
            findings.append(
                {
                    "article_number": art.number,
                    "article_title": art.title,
                    "chapter": art.chapter,
                    "priority": "p4",
                    "p4_triggered": True,
                    "needs_human_review": True,
                    "evidence": js.get("evidence") or [],
                    "what_to_review": js.get("what_to_review"),
                    "notes": js.get("notes"),
                    "status": None,
                    "gaps": [],
                    "risk": None,
                }
            )

        findings.sort(key=lambda x: int(x.get("article_number") or 0))
        print("Run checks node completed. Total findings:", len(findings))
        return {"findings": findings}
    
    def hil_router_node(state: WorkflowState) -> WorkflowState:
        print("Inside hil_router_node")
        findings = state.get("findings") or []
        hil_queue: list[dict[str, Any]] = []
        for f in findings:
            p = f.get("priority")
            if p == "p3":
                hil_queue.append(
                    {
                        "kind": "p3_verify",
                        "article_number": f.get("article_number"),
                        "article_title": f.get("article_title"),
                        "policy_present": f.get("policy_present"),
                        "implementation_unverified": f.get("implementation_unverified"),
                        "evidence": f.get("evidence"),
                        "notes": f.get("notes"),
                    }
                )
            elif p == "p4" and f.get("p4_triggered"):
                hil_queue.append(
                    {
                        "kind": "p4_conditional",
                        "article_number": f.get("article_number"),
                        "article_title": f.get("article_title"),
                        "what_to_review": f.get("what_to_review"),
                        "evidence": f.get("evidence"),
                        "notes": f.get("notes"),
                    }
                )
        print("Hil router node completed. Total hil queue:", len(hil_queue))
        return {"hil_queue": hil_queue}
    
    def report_node(state: WorkflowState) -> WorkflowState:
        print("Inside report_node")
        findings = state.get("findings") or []
        scope = state.get("scope") or {}
        document_paths = state.get("document_paths") or []
        halted = bool(state.get("halted"))
        vocab_ref = (state.get("vocab_ref") or "").strip()
        vocab_preview = (vocab_ref[:600] + "…") if len(vocab_ref) > 600 else (vocab_ref or None)

        p2_findings = [f for f in findings if f.get("priority") == "p2"]
        p3_findings = [f for f in findings if f.get("priority") == "p3"]
        p4_findings = [f for f in findings if f.get("priority") == "p4"]

        def score_p2(fs: list[dict[str, Any]]) -> float:
            if not fs:
                return 0.0
            m = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
            vals = [m.get((str(f.get("status") or "")).lower(), 0.25) for f in fs]
            return float(sum(vals) / len(vals))

        p2_score = score_p2(p2_findings)
        overall_pct = int(math.floor(p2_score * 100))

        rel = state.get("relevant_articles") or []
        p4_article_total = sum(1 for a in rel if a.priority == "p4")

        hil_queue = state.get("hil_queue") or []
        report = {
            "halted": halted,
            "inputs": {"document_paths": document_paths},
            "scope": scope,
            "vocab_preview": vocab_preview,
            "summary": {
                "overall_score_pct": overall_pct,
                "p2_score": p2_score,
                "p2_findings_total": len(p2_findings),
                "p3_findings_total": len(p3_findings),
                "p4_triggered_total": len(p4_findings),
                "p4_articles_not_triggered": max(0, p4_article_total - len(p4_findings)),
                "findings_total": len(findings),
                "hil_queue_total": len(hil_queue),
            },
            "findings": findings,
            "hil_queue": hil_queue,
        }
        print("Report node completed.")
        return {"report": report}

    g = StateGraph(WorkflowState)
    g.add_node("ingestion", ingestion_node)
    g.add_node("load_reference", load_reference_node)
    g.add_node("scope_gate", scope_gate_node)
    g.add_node("p1_gate", p1_gate_node)
    g.add_node("build_vocab", build_vocab_node)
    g.add_node("chunking", chunking_node)
    g.add_node("mapping", mapping_node)
    g.add_node("rag_fetch", rag_fetch_node)
    g.add_node("run_checks", run_checks_node)
    g.add_node("hil_router", hil_router_node)
    g.add_node("report", report_node)

    g.add_edge(START, "ingestion")
    g.add_edge("ingestion", "load_reference")
    g.add_edge("load_reference", "scope_gate")
    g.add_edge("scope_gate", "p1_gate")

    def _route_after_p1(state: WorkflowState) -> str:
        return "halt" if state.get("halted") else "continue"

    g.add_conditional_edges(
        "p1_gate",
        _route_after_p1,
        {"halt": END, "continue": "build_vocab"},
    )
    g.add_edge("build_vocab", "chunking")
    g.add_edge("chunking", "mapping")
    g.add_edge("mapping", "rag_fetch")
    g.add_edge("rag_fetch", "run_checks")
    g.add_edge("run_checks", "hil_router")
    g.add_edge("hil_router", "report")
    g.add_edge("report", END)

    return g.compile()




        