"""Workflow routes — trigger, monitor, and bbox-annotate LangGraph document pipelines."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from db import UploadRunRow
from workflow_db import db as workflow_db_instance
from graph import GraphState, NodeInterrupt, document_graph
# from graph import GraphState, NodeInterrupt, document_graph  # Moved inside functions
from models import (
    CountryCode,
    InvoiceDocument,
    ResumeRequest,
    WorkflowCreateRequest,
    WorkflowRecord,
    WorkflowResponse,
    WorkflowStatus,
    WorkflowStep,
)

log = structlog.get_logger(__name__)
router = APIRouter()

# In-memory store — replace with SQLModel/DB in production
_workflows: dict[str, WorkflowRecord] = {}


# ---------------------------------------------------------------------------
# Bbox models
# ---------------------------------------------------------------------------

class BBoxEntry(BaseModel):
    """A single field-level bounding box in the PDF viewer response."""

    field_name: str = Field(description="Invoice field name, e.g. 'invoice_number'")
    value: str = Field(description="Extracted field value as a string")
    bbox: list[float] = Field(
        description="[x1, y1, x2, y2] in PDF points, origin at top-left of page"
    )
    page: int = Field(default=1, description="1-based page number")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Match confidence derived from text overlap"
    )
    source: str = Field(default="invoice", description="'invoice' or 'bl'")


class StatusResponse(WorkflowResponse):
    """WorkflowResponse extended with structured bbox data for the PDF viewer."""

    bboxes: list[BBoxEntry] = Field(
        default_factory=list,
        description="Field-level bounding boxes derived from OCR provenance data",
    )
    invoice_pdf_url: str | None = Field(default=None, description="URL to the invoice PDF")
    bl_pdf_url: str | None = Field(default=None, description="URL to the bill of lading PDF")

# ---------------------------------------------------------------------------
# Bbox helper
# ---------------------------------------------------------------------------

#: Invoice field names surfaced in the status response (in display order).
_INVOICE_FIELDS: tuple[str, ...] = (
    "invoice_number",
    "date",
    "seller",
    "buyer",
    "total_amount",
    "gross_weight_kg",
)


def map_fields_to_bboxes(
    invoice: InvoiceDocument | None,
    bboxes: list[dict[str, Any]] | None,
) -> list[BBoxEntry]:
    """Map structured invoice fields back to their OCR bounding boxes.

    Algorithm:
      For each field value, scan the OCR bbox list and find the entry whose
      `text` best overlaps the field value (case-insensitive substring match).
      The overlap score is the character length of the matched substring;
      longer matches beat shorter ones.  Entries with zero overlap are skipped.

    Args:
        invoice:  Structured invoice extracted by field_extract_node.
        bboxes:   Raw OCR bbox list produced by ocr_extract_node.
                  Each entry: {text, bbox: [l,t,r,b], page, source}.

    Returns:
        List of BBoxEntry objects, one per matched field (unmatched fields
        are silently omitted).
    """
    if not invoice or not bboxes:
        return []

    # Support both Pydantic model and plain dict invoice representations.
    def _fget(obj: Any, field: str, default: Any = "") -> str:
        val = obj.get(field, default) if isinstance(obj, dict) else getattr(obj, field, default)
        return "" if val is None else str(val)

    # Build a flat map of field_name → string value for the fields we expose.
    field_values: dict[str, str] = {
        "invoice_number":  _fget(invoice, "invoice_number"),
        "date":            _fget(invoice, "date"),
        "seller":          _fget(invoice, "seller"),
        "buyer":           _fget(invoice, "buyer"),
        "total_amount":    _fget(invoice, "total_amount"),
        "gross_weight_kg": _fget(invoice, "gross_weight_kg"),
    }

    result: list[BBoxEntry] = []

    for field_name in _INVOICE_FIELDS:
        raw_value = field_values.get(field_name, "")
        if not raw_value:
            continue

        val_lower = raw_value.lower().strip()
        best_entry: dict[str, Any] | None = None
        best_score: int = 0

        for ocr_entry in bboxes:
            ocr_text = (ocr_entry.get("text") or "").lower().strip()
            if not ocr_text:
                continue

            # Prefer the match direction that yields the longer overlap.
            if val_lower in ocr_text:
                score = len(val_lower)
            elif ocr_text in val_lower:
                score = len(ocr_text)
            else:
                continue

            if score > best_score:
                best_score = score
                best_entry = ocr_entry

        if best_entry is None:
            log.debug("map_fields_to_bboxes.no_match", field=field_name, value=raw_value[:40])
            continue

        confidence = min(1.0, best_score / max(len(val_lower), 1))
        result.append(
            BBoxEntry(
                field_name=field_name,
                value=raw_value,
                bbox=best_entry.get("bbox", [0.0, 0.0, 0.0, 0.0]),
                page=best_entry.get("page", 1),
                confidence=round(confidence, 3),
                source=best_entry.get("source", "invoice"),
            )
        )
        log.debug(
            "map_fields_to_bboxes.matched",
            field=field_name,
            score=best_score,
            confidence=round(confidence, 3),
        )

    return result


# ---------------------------------------------------------------------------
# Background pipeline runner
# ---------------------------------------------------------------------------

def _state_get(state: Any, key: str, default: Any = None) -> Any:
    """Get a value from either a dict state or Pydantic BaseModel state."""
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _handle_blocked(wf: WorkflowRecord, workflow_id: str, msg: str, config: dict) -> None:
    """Shared logic for setting wf to BLOCKED after a NodeInterrupt."""
    wf.status = WorkflowStatus.BLOCKED
    from graph import document_graph
    saved_state = document_graph.get_state(config)
    issues: list[dict] = []
    if saved_state and getattr(saved_state, "values", None):
        cr = saved_state.values.get("compliance_result")
        if cr:
            issues = [i.model_dump() for i in cr.issues]
    wf.result = {"hitl_required": True, "message": msg, "bboxes": [], "issues": issues}
    wf.updated_at = datetime.utcnow()
    log.warning("workflow.hitl_interrupt", workflow_id=workflow_id, detail=msg)


async def _run_graph(
    workflow_id: str,
    document_id: str,
    country: str,
    invoice_pdf_path: str,
    bl_pdf_path: str,
) -> None:
    """Background task: run the LangGraph pipeline and update workflow state."""

    # Guard: backend may have restarted, wiping the in-memory dict.
    wf = _workflows.get(workflow_id)
    if not wf:
        log.error("workflow.run_graph.missing", workflow_id=workflow_id)
        return

    # Set RUNNING immediately so the UI reflects progress.
    wf.status = WorkflowStatus.RUNNING
    wf.updated_at = datetime.utcnow()
    log.info("workflow.run_graph.start", workflow_id=workflow_id,
             invoice=invoice_pdf_path, bl=bl_pdf_path)

    initial_state = GraphState(
        document_id=document_id,
        country=country,
        invoice_pdf_path=invoice_pdf_path,
        bl_pdf_path=bl_pdf_path,
    )

    config = {"configurable": {"thread_id": workflow_id}}

    try:
        result = await document_graph.ainvoke(initial_state, config)

        # LangGraph 1.1.6+ does NOT re-raise NodeInterrupt — ainvoke returns a
        # dict containing '__interrupt__' when a node pauses execution.
        if isinstance(result, dict) and "__interrupt__" in result:
            interrupts = result["__interrupt__"]
            msg = str(interrupts[0].value) if interrupts else "HITL interrupt"
            _handle_blocked(wf, workflow_id, msg, config)
            return

        # Normal completion — result may be a dict or a GraphState Pydantic model.
        cr = _state_get(result, "compliance_result")
        invoice = _state_get(result, "invoice")
        invoice_bboxes = _state_get(result, "invoice_bboxes") or []
        bl_bboxes = _state_get(result, "bl_bboxes") or []
        audit_trail = _state_get(result, "audit_trail") or []

        mapped_bboxes = map_fields_to_bboxes(invoice, invoice_bboxes + bl_bboxes)

        wf.status = WorkflowStatus.COMPLETED
        wf.result = {
            "declaration":       _state_get(result, "declaration"),
            "summary":           _state_get(result, "summary", ""),
            "compliance_status": cr.status if cr else None,
            "compliance_issues": len(cr.issues) if cr else 0,
            "audit_events":      len(audit_trail),
            "bboxes":            [b.model_dump() for b in mapped_bboxes],
        }
        wf.steps = [
            WorkflowStep(name=e.node_name, status=WorkflowStatus.COMPLETED)
            for e in audit_trail
        ]
        log.info("workflow.completed", workflow_id=workflow_id, bboxes=len(mapped_bboxes))

    except NodeInterrupt as exc:
        # Fallback: older LangGraph behaviour where NodeInterrupt propagates.
        _handle_blocked(wf, workflow_id, str(exc), config)

    except Exception as exc:
        wf.status = WorkflowStatus.FAILED
        wf.result = {"error": str(exc), "bboxes": []}
        log.error("workflow.failed", workflow_id=workflow_id, error=str(exc))

    wf.updated_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a processing workflow for a document pair",
)
async def create_workflow(
    body: WorkflowCreateRequest,
    background_tasks: BackgroundTasks,
) -> WorkflowResponse:
    workflow_id = str(uuid.uuid4())

    wf = WorkflowRecord(
        id=uuid.UUID(workflow_id),
        document_id=body.document_id,
        country=body.country,
        status=WorkflowStatus.QUEUED,
    )
    _workflows[workflow_id] = wf

    # Resolve actual file paths from the DB row written by the upload route.
    run_id_str = str(body.document_id)
    with workflow_db_instance.session() as db_session:
        upload_row = db_session.get(UploadRunRow, run_id_str)

    if upload_row:
        invoice_path = upload_row.invoice_path
        bl_path      = upload_row.bl_path
    else:
        # Fallback for uploads made before this DB-backed path was introduced.
        invoice_path = f"uploads/{run_id_str}_invoice.pdf"
        bl_path      = f"uploads/{run_id_str}_bl.pdf"
        log.warning(
            "workflow.upload_row_missing",
            run_id=run_id_str,
            fallback_invoice=invoice_path,
            fallback_bl=bl_path,
        )

    background_tasks.add_task(
        _run_graph,
        workflow_id=workflow_id,
        document_id=run_id_str,
        country=body.country.value,
        invoice_pdf_path=invoice_path,
        bl_pdf_path=bl_path,
    )

    log.info("workflow.queued", workflow_id=workflow_id, document_id=run_id_str)
    return WorkflowResponse(**wf.model_dump())


# NOTE: /status/{run_id} MUST be declared before /{workflow_id} so FastAPI
# does not swallow the literal word "status" as a workflow_id path parameter.
@router.get(
    "/status/{run_id}",
    response_model=StatusResponse,
    summary="Get workflow status with field-level bbox annotations",
)
async def get_run_status(run_id: str) -> StatusResponse:
    """Return the workflow record plus a `bboxes` list for the PDF viewer overlay.

    `run_id` is the same UUID returned by POST /workflow/ (i.e. the workflow_id).
    The `bboxes` field is populated once the pipeline has completed OCR and
    extraction; it is an empty list while the run is still in progress.
    """
    wf = _workflows.get(run_id)
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    result = wf.result.copy() if wf.result else {}
    steps = wf.steps or []

    if wf.status == WorkflowStatus.BLOCKED:
        from pathlib import Path
        ckpt_path = Path(f"/tmp/{run_id}/checkpoint.pkl")
        if ckpt_path.exists():
            try:
                import pickle
                with open(ckpt_path, "rb") as f:
                    chk = pickle.load(f)
                    
                    if hasattr(chk, "values"):
                        st_vals = chk.values
                    elif isinstance(chk, dict) and "channel_values" in chk:
                        st_vals = chk["channel_values"]
                    elif isinstance(chk, dict):
                        st_vals = chk
                    else:
                        st_vals = getattr(chk, "__dict__", {})

                    if "invoice" in st_vals:
                        inv = st_vals["invoice"]
                        result["invoice"] = inv if isinstance(inv, dict) else getattr(inv, "model_dump", lambda: inv.__dict__)()
                        
                    if "bill_of_lading" in st_vals:
                        bl = st_vals["bill_of_lading"]
                        result["bill_of_lading"] = bl if isinstance(bl, dict) else getattr(bl, "model_dump", lambda: bl.__dict__)()

                    if "audit_trail" in st_vals:
                        trails = st_vals["audit_trail"]
                        steps = [WorkflowStep(name=getattr(e, "node_name", e.get("node_name")), status=WorkflowStatus.COMPLETED) for e in trails]
                        
            except Exception as e:
                log.error("workflow.status.checkpoint_read_error", error=str(e))

    raw_bboxes: list[dict[str, Any]] = result.get("bboxes", [])  # type: ignore[assignment]
    bboxes = [BBoxEntry(**b) for b in raw_bboxes]

    base_data = wf.model_dump(exclude={"result", "steps"})

    return StatusResponse(
        **base_data,
        result=result,
        steps=steps,
        bboxes=bboxes,
        invoice_pdf_url=f"http://localhost:8000/uploads/{wf.document_id}_invoice.pdf",
        bl_pdf_url=f"http://localhost:8000/uploads/{wf.document_id}_bl.pdf"
    )


@router.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Get workflow status and results",
)
async def get_workflow(workflow_id: str) -> WorkflowResponse:
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return WorkflowResponse(**wf.model_dump())


@router.get(
    "/",
    response_model=list[WorkflowResponse],
    summary="List all workflows",
)
async def list_workflows() -> list[WorkflowResponse]:
    return [WorkflowResponse(**wf.model_dump()) for wf in _workflows.values()]


@router.post(
    "/resume/{run_id}",
    response_model=WorkflowResponse,
    summary="Resume a blocked HITL workflow with corrected values",
)
async def resume_workflow(
    run_id: str,
    body: ResumeRequest,
    background_tasks: BackgroundTasks,
) -> WorkflowResponse:
    wf = _workflows.get(run_id)
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    if wf.status != WorkflowStatus.BLOCKED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow is not blocked")

    config = {"configurable": {"thread_id": run_id}}
    
    # Update state synchronously here
    from graph import document_graph
    current_state = document_graph.get_state(config)
    if not getattr(current_state, "values", None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Graph state not found")
    
    state_vals = dict(current_state.values)

    if body.gross_weight_kg is not None:
        # Update the B/L weight — this is what the operator is correcting.
        bl = state_vals.get("bill_of_lading")
        if bl is not None:
            if isinstance(bl, dict):
                bl["gross_weight_kg"] = body.gross_weight_kg
            else:
                bl.gross_weight_kg = body.gross_weight_kg
            state_vals["bill_of_lading"] = bl

        # Also align the invoice weight if it is missing/zero.
        inv = state_vals.get("invoice")
        if inv is not None:
            inv_weight = inv.get("gross_weight_kg") if isinstance(inv, dict) else inv.gross_weight_kg
            if not inv_weight:
                if isinstance(inv, dict):
                    inv["gross_weight_kg"] = body.gross_weight_kg
                else:
                    inv.gross_weight_kg = body.gross_weight_kg
                state_vals["invoice"] = inv

        # Clear all BLOCK-severity issues related to gross_weight so
        # interrupt_node does not fire again after resume.
        cr = state_vals.get("compliance_result")
        if cr:
            cr.issues = [
                i for i in cr.issues
                if not (i.severity == "block" and "gross_weight" in i.field)
            ]
            if any(i.severity == "block" for i in cr.issues):
                cr.status = "BLOCK"
            elif any(i.severity == "warn" for i in cr.issues):
                cr.status = "WARN"
            else:
                cr.status = "PASS"
            state_vals["compliance_result"] = cr

        await document_graph.aupdate_state(config, state_vals)

    # Spawn background task to continue from interruption
    background_tasks.add_task(
        _resume_graph,
        workflow_id=run_id
    )

    wf.status = WorkflowStatus.RUNNING
    wf.updated_at = datetime.utcnow()
    log.info("workflow.resumed", workflow_id=run_id)
    return WorkflowResponse(**wf.model_dump())


async def _resume_graph(workflow_id: str) -> None:
    from graph import document_graph, NodeInterrupt
    wf = _workflows[workflow_id]
    config = {"configurable": {"thread_id": workflow_id}}
    try:
        result = await document_graph.ainvoke(None, config)

        # LangGraph 1.1.6+: interrupt returns dict with '__interrupt__' key.
        if isinstance(result, dict) and "__interrupt__" in result:
            interrupts = result["__interrupt__"]
            msg = str(interrupts[0].value) if interrupts else "HITL interrupt"
            _handle_blocked(wf, workflow_id, msg, config)
            return

        cr = _state_get(result, "compliance_result")
        audit_trail = _state_get(result, "audit_trail") or []
        mapped_bboxes = map_fields_to_bboxes(
            _state_get(result, "invoice"),
            (_state_get(result, "invoice_bboxes") or []) + (_state_get(result, "bl_bboxes") or []),
        )

        wf.status = WorkflowStatus.COMPLETED
        wf.result = {
            "declaration":       _state_get(result, "declaration"),
            "summary":           _state_get(result, "summary", ""),
            "compliance_status": cr.status if cr else None,
            "compliance_issues": len(cr.issues) if cr else 0,
            "audit_events":      len(audit_trail),
            "bboxes":            [b.model_dump() for b in mapped_bboxes],
        }
        wf.steps = [
            WorkflowStep(name=e.node_name, status=WorkflowStatus.COMPLETED)
            for e in audit_trail
        ]
        log.info("workflow.completed", workflow_id=workflow_id)

    except NodeInterrupt as exc:
        _handle_blocked(wf, workflow_id, str(exc), config)

    except Exception as exc:
        wf.status = WorkflowStatus.FAILED
        wf.result = {"error": str(exc), "bboxes": []}
        log.error("workflow.failed", workflow_id=workflow_id, error=str(exc))

    wf.updated_at = datetime.utcnow()


@router.get(
    "/declaration/{run_id}",
    summary="Get final extracted declaration",
)
async def get_declaration(run_id: str) -> dict[str, Any]:
    wf = _workflows.get(run_id)
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    if wf.status != WorkflowStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow is not complete yet")
    
    decl = wf.result.get("declaration")
    if not decl:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Declaration not generated")
    
    return decl
