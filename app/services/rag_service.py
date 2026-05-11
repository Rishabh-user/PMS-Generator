"""
RAG (Retrieval-Augmented Generation) service for PMS generation.

Hybrid pipeline:
  Dense  — FAISS vector search (OpenAI text-embedding-3-small)
  Sparse — BM25 keyword search (exact ASME codes, material grades)
  Rerank — Cross-encoder ms-marco-MiniLM-L-6-v2 re-scores top candidates

On first call the retriever is built and cached in-process.
If OPENAI_API_KEY is not set or anything fails, all functions silently
return None so generation continues without RAG context.
"""
import logging
from pathlib import Path

from huggingface_hub import login as hf_login
from langchain_community.retrievers import BM25Retriever
from langchain_community.cross_encoders.huggingface import HuggingFaceCrossEncoder
from langchain_community.vectorstores import FAISS
from langchain_classic.retrievers import EnsembleRetriever, ContextualCompressionRetriever
from langchain_classic.retrievers.document_compressors.cross_encoder_rerank import CrossEncoderReranker
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter

from app.config import settings

_HF_TOKEN = settings.huggingface_api_key

logger = logging.getLogger(__name__)

_FAISS_INDEX_PATH = str(Path(__file__).resolve().parent.parent / "faiss_index")
_STANDARD_DATA_DIR = Path(__file__).resolve().parent.parent / "standard_data"

# Singleton — built once on first retrieve_context() call
_retriever = None
_init_failed = False

# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_md_splits():
    """Load and chunk all standard documents for BM25.

    For each PDF in standard_data/:
      - Use the same-named .md file when available (preferred)
      - Fall back to pymupdf4llm PDF extraction when no .md exists
    """
    from langchain_core.documents import Document

    headers_to_split_on = [("#", "Header 1")]
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)

    pdf_files = sorted(_STANDARD_DATA_DIR.glob("*.pdf"))
    if not pdf_files:
        logger.warning("RAG: no PDF files found in standard_data/")
        return []

    raw_docs: list[Document] = []
    for pdf_path in pdf_files:
        md_path = pdf_path.with_suffix(".md")
        if md_path.exists():
            text   = md_path.read_text(encoding="utf-8")
            source = str(md_path)
        else:
            # Fallback: extract markdown from the PDF
            try:
                import pymupdf4llm  # type: ignore
                text   = pymupdf4llm.to_markdown(str(pdf_path))
                source = str(pdf_path)
                logger.info("RAG: extracted %s via pymupdf4llm (no .md found)", pdf_path.name)
            except Exception as exc:
                logger.warning("RAG: skipping %s — cannot extract (%s)", pdf_path.name, exc)
                continue
        raw_docs.append(Document(page_content=text, metadata={"source": source}))

    md_splits = []
    for doc in raw_docs:
        splits = markdown_splitter.split_text(doc.page_content)
        parent_src = doc.metadata.get("source", "")
        for split in splits:
            split.metadata["source"] = parent_src
        md_splits.extend(splits)

    logger.info("RAG: loaded %d chunks from %d document(s) in standard_data/",
                len(md_splits), len(raw_docs))
    return md_splits


def _build_retriever():
    """Build and cache the hybrid + rerank retrieval pipeline."""
    global _retriever, _init_failed

    if not settings.openai_api_key:
        logger.warning("RAG: OPENAI_API_KEY not set — RAG disabled")
        _init_failed = True
        return

    try:
        embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small",
            api_key=settings.openai_api_key,
        )

        # ── Dense retriever (existing FAISS index) ────────────────────────
        vectorstore = FAISS.load_local(
            _FAISS_INDEX_PATH,
            embeddings,
            allow_dangerous_deserialization=True,
        )
        dense_retriever = vectorstore.as_retriever(search_kwargs={"k": 10})
        logger.info("RAG: FAISS index loaded from %s", _FAISS_INDEX_PATH)

        # ── Sparse retriever (BM25 over markdown chunks) ──────────────────
        md_splits = _load_md_splits()
        if not md_splits:
            logger.warning("RAG: no markdown chunks found — RAG disabled")
            _init_failed = True
            return
        bm25_retriever = BM25Retriever.from_documents(md_splits)
        bm25_retriever.k = 10

        # ── Hybrid (ensemble) ─────────────────────────────────────────────
        hybrid = EnsembleRetriever(
            retrievers=[dense_retriever, bm25_retriever],
            weights=[0.5, 0.5],
        )

        # ── Cross-encoder reranker ─────────────────────────────────────────
        try:
            hf_login(token=_HF_TOKEN)
        except Exception as _hf_err:
            logger.warning("RAG: HuggingFace login failed (%s) — model will try anonymous", _hf_err)
        cross_encoder = HuggingFaceCrossEncoder(
            model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        reranker = CrossEncoderReranker(model=cross_encoder, top_n=10)

        _retriever = ContextualCompressionRetriever(
            base_compressor=reranker,
            base_retriever=hybrid,
        )
        logger.info("RAG: hybrid retriever ready")

    except Exception as exc:
        logger.warning("RAG: initialisation failed (%s) — generation continues without RAG", exc)
        _init_failed = True


# ──────────────────────────────────────────────────────────────────────────────
# Query builder
# ──────────────────────────────────────────────────────────────────────────────

_RATING_TERMS = {
    "150#":   "Class 150 150 pound 150# flange pressure-temperature ASME B16.5",
    "300#":   "Class 300 300 pound 300# flange pressure-temperature ASME B16.5",
    "600#":   "Class 600 600 pound 600# flange pressure-temperature ASME B16.5",
    "900#":   "Class 900 900 pound 900# RTJ ring type joint flange ASME B16.5",
    "1500#":  "Class 1500 1500 pound 1500# RTJ ring type joint flange ASME B16.5",
    "2500#":  "Class 2500 2500 pound 2500# RTJ ring type joint flange ASME B16.5",
    "5000#":  "Class 5000 5000 pound 5000 psi API 6A wellhead Christmas tree equipment working pressure",
    "10000#": "Class 10000 10000 pound 10000 psi API 6A wellhead high pressure working pressure",
}

_MATERIAL_TERMS = {
    "CS":       "Carbon Steel CS ASTM A106 A234 WPB A105N pipe fittings flange",
    "LTCS":     "Low Temperature Carbon Steel LTCS ASTM A333 Gr.6 A420 WPL6 A350 LF2",
    "SS316L":   "Stainless Steel 316L SS316L ASTM A312 TP316L A403 WP316L A182 F316L B36.19M",
    "DSS":      "Duplex Stainless Steel DSS UNS S31803 ASTM A790 S31803 A815 WP-S WP-WX B36.19M",
    "SDSS":     "Super Duplex Stainless Steel SDSS UNS S32750 ASTM A790 S32750 A815 B36.19M",
    "CuNi":     "Copper Nickel CuNi 90-10 EEMUA 234 ASTM B466 UNS C70600",
    "Copper":   "Copper ASTM B42 B124 C12200 C11000",
    "GRE":      "Glass Reinforced Epoxy GRE fiberglass ASTM D2996 D4024",
    "CPVC":     "Chlorinated Polyvinyl Chloride CPVC ASTM F441",
    "Titanium": "Titanium ASTM B861 Grade 2 Gr. 2",
}

_COMMON_STANDARDS = (
    "Project PMS class sheet valve table "
    "Project Valve Material Specification VMS VDS valve code "
    "40801-SPE-80000-PP-SP-0002 valve data sheet "
    "ASME B16.5 flange dimensions pressure rating "
    "ASME B36.10M B36.19M pipe wall thickness schedule "
    "ASME B16.9 butt weld fittings elbow tee reducer "
    "ASME B16.20 spiral wound gasket flexible graphite "
    "ASME B16.48 spectacle blind spacer "
    "ASME B16.34 valves API 6D ball valve API 600 gate valve "
    "API 602 globe valve API 594 check valve API 609 butterfly valve "
    "ASME B31.3 process piping design code"
)


def _valve_query_terms(piping_class: str, rating: str) -> str:
    """Add exact VDS-code terms so RAG keeps valve rows near the top.

    The standard query is intentionally broad across pipe, flange, fittings,
    and P-T data. Valve rows are compact and easy for BM25/rerank to miss, so
    we add the exact class-bearing prefixes the project sheet is expected to
    contain. This improves retrieval without hardcoding a completed valve row.
    """
    cls = (piping_class or "").strip().upper()
    if not cls:
        return ""

    end = "J" if (rating or "").strip() in {"900#", "1500#", "2500#", "5000#", "10000#"} else "R"
    if cls.startswith(("A30", "A40", "A50", "A51", "A52", "A60")):
        end = "F"

    generic = [
        f"BLRT{cls}{end}", f"BLFT{cls}{end}",
        f"BLRP{cls}{end}", f"BLFP{cls}{end}",
        f"GAYM{cls}{end}", f"GLYM{cls}{end}",
        f"CHPM{cls}{end}", f"CHSM{cls}{end}", f"CHDM{cls}{end}",
        f"BFWT{cls}{end}", f"BFTP{cls}{end}", f"BFTT{cls}{end}",
        f"DBRP{cls}{end}", f"DBRM{cls}{end}", f"DBRP{cls}{end}T",
    ]
    return " ".join(generic)


def _build_query(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
) -> str:
    """
    Compose a standalone search query that retrieves the most relevant
    sections from the ASME standard index for this piping class.

    The query is intentionally verbose — it concatenates the class ID,
    decoded rating, full material terminology, service keywords, and the
    standards most likely to contain needed data.  This saturates both
    the semantic (dense) and keyword (BM25) channels of the hybrid
    retriever so neither channel misses obvious hits.

    Example — G20, DSS, NIL CA, Utility/Instrument/Low Temp:
      "Piping class G20 Class 2500 2500 pound RTJ ring type joint flange
       Duplex Stainless Steel DSS UNS S31803 ASTM A790 S31803 A815 WP-S
       WP-WX B36.19M corrosion allowance NIL service Utility Instrument
       Low Temperature ASME B16.5 flange dimensions ... ASME B31.3 ..."
    """
    rating_str   = _RATING_TERMS.get(rating, rating)
    material_str = _MATERIAL_TERMS.get(material, material)

    # Surface low-temperature terms when present in service description
    svc_lower = service.lower()
    svc_extras = ""
    if "low temp" in svc_lower or "lt " in svc_lower:
        svc_extras = "low temperature impact test Charpy ASME B31.3 low temperature"
    if "sour" in svc_lower or "nace" in svc_lower or "h2s" in svc_lower:
        svc_extras += " NACE MR-01-75 ISO 15156 sour service HIC SSC"
    if "cryogenic" in svc_lower:
        svc_extras += " cryogenic low temperature ductility impact"

    parts = [
        f"Piping class {piping_class}",
        rating_str,
        material_str,
        f"corrosion allowance {corrosion_allowance}",
        f"service {service}",
        svc_extras,
        _valve_query_terms(piping_class, rating),
        _COMMON_STANDARDS,
    ]
    return " ".join(p for p in parts if p)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def retrieve_context(
    piping_class: str,
    material: str,
    corrosion_allowance: str,
    service: str,
    rating: str,
) -> tuple[str, dict[int, str]] | tuple[None, None]:
    """
    Retrieve relevant ASME standard excerpts for the given PMS request.

    Returns (context_string, source_map) where source_map is {1: "ASME B16.5_2020", ...}.
    Each chunk in context_string is labelled [1], [2], … so the LLM can reference them
    by number and the caller can resolve numbers back to document names reliably.
    Returns (None, None) when RAG is unavailable or retrieval fails.
    """
    global _retriever, _init_failed

    if not settings.rag_enabled:
        return None, None

    if _init_failed:
        return None, None

    if _retriever is None:
        _build_retriever()
        if _retriever is None:
            return None, None

    try:
        query = _build_query(piping_class, material, corrosion_allowance, service, rating)
        logger.info("RAG: querying for %s (%d chars query)", piping_class, len(query))

        docs = _retriever.invoke(query)
        if not docs:
            logger.info("RAG: no documents returned for %s", piping_class)
            return None, None

        source_map: dict[int, str] = {}
        parts = []
        for idx, doc in enumerate(docs, start=1):
            raw_src = doc.metadata.get("source", "")
            src_name = Path(raw_src).stem if raw_src else f"Doc{idx}"
            source_map[idx] = src_name
            parts.append(f"[{idx}] {src_name}\n{doc.page_content}")

        context = "\n\n---\n\n".join(parts)
        logger.info(
            "RAG: retrieved %d chunks for %s (%d chars total)",
            len(docs), piping_class, len(context),
        )
        return context, source_map

    except Exception as exc:
        logger.warning("RAG: retrieval error for %s (%s) — skipping", piping_class, exc)
        return None, None
