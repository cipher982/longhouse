from fastapi import APIRouter
from fastapi import Request

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/stripe")
async def stripe_webhook(request: Request):
    _ = await request.body()
    # TODO: validate Stripe signature + handle events
    return {"ok": True}
