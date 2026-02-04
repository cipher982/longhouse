from fastapi import APIRouter

router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/checkout")
def checkout():
    return {"ok": False, "message": "Not implemented"}


@router.post("/portal")
def portal():
    return {"ok": False, "message": "Not implemented"}
