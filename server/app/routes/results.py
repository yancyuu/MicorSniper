"""Run-result retrieval and export (FR-6)."""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from app.store import STORE
import csv
import io

router = APIRouter(prefix="/api/results")


@router.get("/")
async def list_results():
    return await STORE.list_results()


@router.get("/{workflow_id}")
async def get_result(workflow_id: str):
    result = await STORE.load_result(workflow_id)
    if not result:
        raise HTTPException(404, "No result for this workflow")
    return result


@router.get("/{workflow_id}/export")
async def export_result(workflow_id: str, format: str = Query("json", pattern="^(json|csv)$")):
    result = await STORE.load_result(workflow_id)
    if not result:
        raise HTTPException(404, "No result for this workflow")

    if format == "json":
        return JSONResponse(
            content=result.model_dump(),
            headers={"Content-Disposition": f'attachment; filename="{workflow_id}.json"'},
        )

    # CSV: flatten the primary items list
    items = result.items or []
    rows = [it for it in items if isinstance(it, dict)]
    fieldnames: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames or ["value"])
    writer.writeheader()
    if rows:
        for r in rows:
            writer.writerow({k: _stringify(r.get(k)) for k in fieldnames})
    else:
        for it in items:
            writer.writerow({"value": _stringify(it)})

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{workflow_id}.csv"'},
    )


def _stringify(value) -> str:
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)
