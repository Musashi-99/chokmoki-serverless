"""Admin CRUD for SMS templates (config/rate_limits.yaml precedent: admin-
only, Mongo-backed so template IDs can change without a redeploy) + a
send-a-real-test-SMS action + a read view over SystemLogService entries.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from api.bootstrap import Msg91Service, SmsTemplateService, SmsTemplateUpdate, SystemLogService, require_admin, require_scope_email

router = APIRouter()


@router.get("/api/admin/sms/templates")
async def admin_list_sms_templates(email: str = Depends(require_scope_email("settings", "write"))):
    if SmsTemplateService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    templates = await SmsTemplateService().list_all()
    return [t.model_dump() for t in templates]


@router.put("/api/admin/sms/templates/{key}")
async def admin_upsert_sms_template(
    key: str, payload: SmsTemplateUpdate, email: str = Depends(require_scope_email("settings", "write"))
):
    if SmsTemplateService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    template = await SmsTemplateService().upsert(key, payload, actor_email=email)
    return template.model_dump()


class SmsTestRequest(BaseModel):
    phone: str
    template_key: Optional[str] = None


@router.post("/api/admin/sms/test")
async def admin_send_test_sms(payload: SmsTestRequest, email: str = Depends(require_scope_email("settings", "write"))):
    if Msg91Service is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = Msg91Service()
    if not service.is_enabled():
        raise HTTPException(status_code=400, detail="MSG91 is not enabled/configured")

    if payload.template_key:
        sent = await service.send_template(
            payload.phone, payload.template_key, {"order_id": "TEST123", "customer_name": "Test Customer"}
        )
        if SystemLogService:
            await SystemLogService().log(
                component="sms",
                level="info" if sent else "error",
                message=f"Admin test SMS ({payload.template_key}) to {payload.phone}: {'sent' if sent else 'failed'}",
                context={"actor_email": email},
            )
        if not sent:
            raise HTTPException(status_code=502, detail="Template send failed — check template is enabled and configured")
        return {"success": True}

    # No template — send a raw OTP as the simplest possible reachability check.
    request_id = await service.send_otp(payload.phone)
    if not request_id:
        raise HTTPException(status_code=502, detail="MSG91 send failed")
    return {"success": True, "request_id": request_id}


@router.get("/api/admin/sms/logs")
async def admin_sms_logs(limit: int = 100, email: str = Depends(require_scope_email("settings", "write"))):
    if SystemLogService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return await SystemLogService().list_logs(component="sms", limit=limit)
