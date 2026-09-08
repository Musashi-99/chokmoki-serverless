"""Admin order management + dashboard stats."""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
import json
from api.bootstrap import (
    OrderService,
    OrderStatus,
    ProductService,
    ShiprocketAPIError,
    db,
    get_client_ip,
    logger,
    require_admin,
    require_scope_email,
)
from api.json_utils import JSONEncoder, _json_response_content
from src.services import order_ledger
from src.shipping.courier_provider import CourierUnavailableError, get_courier_provider, order_country
from src.utils.money import money
from src.utils.revenue import revenue_mongo_match

router = APIRouter()


@router.get("/api/admin/orders")
async def admin_list_orders(
    skip: int = 0,
    limit: int = 50,
    status: Optional[str] = None,
    search: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    coupon: Optional[str] = None,
    country: Optional[str] = None,
    email: str = Depends(require_scope_email("orders", "read")),
):
    """List all orders for the admin dashboard with optional filtering."""
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = OrderService()
    orders = await service.list(
        skip=skip, limit=limit,
        status=status, search=search,
        from_date=from_date, to_date=to_date,
        coupon=coupon, country=country,
    )
    total = await service.count(
        status=status, search=search,
        from_date=from_date, to_date=to_date,
        coupon=coupon, country=country,
    )
    return JSONResponse(content=json.loads(json.dumps({
        "data": [order.model_dump(by_alias=True) for order in orders],
        "count": total,
    }, cls=JSONEncoder)))


@router.post("/api/admin/orders")
async def admin_create_order(
    request: Request, payload: Dict[str, Any], email: str = Depends(require_scope_email("orders", "write"))
):
    """Create an order from the admin dashboard (phone / manual orders)."""
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        service = OrderService()
        client_ip = get_client_ip(request) if get_client_ip else None
        order = await service.create_from_admin(
            payload,
            ip=client_ip,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if logger:
            logger.error(f"Admin order creation failed: {e}")
        raise HTTPException(status_code=500) from e

    return JSONResponse(
        content=json.loads(
            json.dumps(order.model_dump(by_alias=True), cls=JSONEncoder)
        )
    )


@router.put("/api/admin/orders/{order_id}/status")
async def admin_update_order_status(
    order_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("orders", "write"))
):
    """Update an order's status by order_id."""
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        status = OrderStatus(**payload.get("status", {}))
        service = OrderService()
        order = await service.get_by_id(order_id)
        if not order:
            order = await service.get_by_mongo_id(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        updated = await service.update_status(order.order_id, status, actor=email)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=json.loads(json.dumps(
        updated.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.get("/api/admin/orders/{order_id}")
async def admin_get_order(order_id: str, email: str = Depends(require_scope_email("orders", "read"))):
    """Get a single order by order_id."""
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = OrderService()
    order = await service.get_by_id(order_id)
    if not order:
        order = await service.get_by_mongo_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=json.loads(json.dumps(
        order.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.get("/api/admin/orders/{order_id}/events")
async def admin_get_order_events(order_id: str, email: str = Depends(require_scope_email("orders", "read"))):
    """Unified per-order timeline — merges creation, payment, status,
    fulfillment, shipment, and note events into one chronological view.
    """
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    service = OrderService()
    events = await service.get_events(order_id)
    return {"data": events}


@router.get("/api/admin/orders/{order_id}/invoice.pdf")
async def admin_order_invoice_pdf(
    order_id: str,
    doc_type: str = "tax_invoice",
    email: str = Depends(require_scope_email("orders", "read")),
):
    """Branded GST document PDF, generated lazily on request — tax_invoice
    (default), receipt, or bill_of_supply. The invoice number is assigned
    atomically on first generation and reused forever after; the PDF itself
    is rebuilt per request (pure function of order + config, nothing to
    store). reportlab is CPU-bound, so it runs in a thread — a big PDF can
    never stall the request-serving event loop.
    """
    import asyncio

    from fastapi.responses import Response
    from src.services.invoice_service import DOC_TITLES, InvoiceService

    if doc_type not in DOC_TITLES:
        raise HTTPException(status_code=400, detail=f"doc_type must be one of {sorted(DOC_TITLES)}")

    database = await db.get_database()
    order_doc = await database["orders"].find_one({"order_id": order_id})
    if not order_doc:
        raise HTTPException(status_code=404, detail="Order not found")

    service = InvoiceService()
    if doc_type == "tax_invoice" and not service.is_india_order(order_doc):
        raise HTTPException(
            status_code=400,
            detail="Tax Invoice (GST) is only issued for orders shipping within India — use Bill of Supply for this order.",
        )
    invoice_number, invoice_date = await service.get_or_assign_invoice_number(order_id)
    pdf_bytes = await asyncio.to_thread(
        service.build_pdf,
        order_doc,
        doc_type=doc_type,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
    )
    filename = f"{DOC_TITLES[doc_type].title().replace(' ', '_')}_{invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/api/admin/orders/{order_id}/notes")
async def admin_add_order_note(
    order_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("orders", "write"))
):
    """Append-only admin annotation — no edit/delete, this is a record."""
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")

    service = OrderService()
    updated = await service.add_note(order_id, text, email)
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=json.loads(json.dumps(
        updated.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.post("/api/admin/orders/{order_id}/custom-status")
async def admin_set_custom_status(
    order_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("orders", "write"))
):
    """Admin-only operational tag, orthogonal to status.type — never
    touched by webhooks or system logic.
    """
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    custom_status = payload.get("custom_status")
    service = OrderService()
    updated = await service.set_custom_status(order_id, custom_status, email)
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=json.loads(json.dumps(
        updated.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.post("/api/admin/orders/{order_id}/mark-payment-collected")
async def admin_mark_payment_collected(order_id: str, email: str = Depends(require_scope_email("orders", "write"))):
    """Explicit admin action confirming COD cash was actually collected —
    payment_status starts 'pending' for every order now, this is the step
    that replaces the old implicit "COD = paid at order time" assumption.
    """
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = OrderService()
    updated = await service.mark_payment_collected(order_id, email)
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=json.loads(json.dumps(
        updated.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.get("/api/admin/stats")
async def admin_get_stats(email: str = Depends(require_scope_email("orders", "read"))):
    """Dashboard overview stats: order counts, revenue, product count."""
    if OrderService is None or ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    database = await db.get_database()

    orders_collection = database["orders"]
    products_collection = database["products"]

    total_orders = await orders_collection.count_documents({})

    revenue_pipeline = [
        {"$match": revenue_mongo_match()},
        {"$group": {"_id": None, "total": {"$sum": "$total_amount"}}},
    ]
    revenue_result = await orders_collection.aggregate(revenue_pipeline).to_list(1)
    total_revenue = money(revenue_result[0]["total"] if revenue_result else 0)

    status_pipeline = [
        {"$group": {"_id": "$status.type", "count": {"$sum": 1}}},
    ]
    status_result = await orders_collection.aggregate(status_pipeline).to_list(100)
    status_counts = {item["_id"]: item["count"] for item in status_result if item["_id"]}

    # Align "today" with store timezone (IST) — Mongo stores naive UTC datetimes.
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = today_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    orders_today = await orders_collection.count_documents({"created_at": {"$gte": today_start}})

    total_products = await products_collection.count_documents({})
    active_products = await products_collection.count_documents({"active": True})

    return JSONResponse(content=json.loads(json.dumps({
        "totalOrders": total_orders,
        "totalRevenue": total_revenue,
        "ordersToday": orders_today,
        "statusCounts": status_counts,
        "totalProducts": total_products,
        "activeProducts": active_products,
    }, cls=JSONEncoder)))


# ========== Shiprocket fulfillment (admin-driven) ==========

@router.post("/api/admin/orders/{order_id}/pack")
async def admin_mark_order_packed(order_id: str, email: str = Depends(require_scope_email("orders", "write"))):
    """Mark an order as physically packed — no Shiprocket calls yet."""
    if OrderService is None or db is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = OrderService()
    order = await service.get_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    database = await db.get_database()
    await database["orders"].update_one(
        {"order_id": order.order_id}, {"$set": {"fulfillment_status": "packed"}}
    )
    await order_ledger.append_event(
        order.order_id, "fulfillment_changed", email, {"from": order.fulfillment_status, "to": "packed"},
    )
    return {"success": True, "fulfillment_status": "packed"}


async def _get_order_doc_or_404(order_id: str) -> Dict[str, Any]:
    database = await db.get_database()
    order_doc = await database["orders"].find_one({"order_id": order_id})
    if not order_doc:
        raise HTTPException(status_code=404, detail="Order not found")
    return order_doc


@router.get("/api/admin/orders/{order_id}/shiprocket/couriers")
async def admin_get_courier_quotes(order_id: str, email: str = Depends(require_scope_email("orders", "read"))):
    """Live courier quotes for the 'Ready to Ship' picker — no shipment created yet."""
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    order_doc = await _get_order_doc_or_404(order_id)
    provider = get_courier_provider(order_country(order_doc))

    try:
        quotes = await provider.get_courier_quotes(order_doc)
    except CourierUnavailableError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ShiprocketAPIError as e:
        # 400, not 502: Shiprocket failures here are business-rule rejections
        # an admin can act on (wallet balance, bad pickup config, no couriers
        # serviceable, etc) — not a security-sensitive internal error, so the
        # real message must reach the UI. 5xx details get redacted by
        # src/security/error_handling.py's sanitizer; 4xx details don't.
        raise HTTPException(status_code=400, detail=str(e))
    return {"data": quotes, "count": len(quotes)}


@router.post("/api/admin/orders/{order_id}/shiprocket/ship")
async def admin_ship_order(
    order_id: str, payload: Optional[Dict[str, Any]] = None, email: str = Depends(require_scope_email("orders", "write"))
):
    """'Ready to Ship': create the shipment, assign AWB (auto or the given
    courier_company_id), generate label + invoice, schedule pickup.
    """
    if OrderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    order_doc = await _get_order_doc_or_404(order_id)
    provider = get_courier_provider(order_country(order_doc))
    payload = payload or {}
    courier_company_id = payload.get("courier_company_id")

    try:
        result = await provider.ship_order(order_id, courier_company_id=courier_company_id)
    except CourierUnavailableError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ShiprocketAPIError as e:
        # 400, not 502: Shiprocket failures here are business-rule rejections
        # an admin can act on (wallet balance, bad pickup config, no couriers
        # serviceable, etc) — not a security-sensitive internal error, so the
        # real message must reach the UI. 5xx details get redacted by
        # src/security/error_handling.py's sanitizer; 4xx details don't.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if logger:
            logger.error(f"Shiprocket ship_order failed for {order_id}: {e}")
        raise HTTPException(status_code=500) from e

    return _json_response_content(result)


@router.post("/api/admin/orders/{order_id}/shiprocket/cancel")
async def admin_cancel_shipment(order_id: str, email: str = Depends(require_scope_email("orders", "write"))):
    order_doc = await _get_order_doc_or_404(order_id)
    provider = get_courier_provider(order_country(order_doc))
    try:
        result = await provider.cancel_shipment(order_id)
    except CourierUnavailableError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ShiprocketAPIError as e:
        # 400, not 502: Shiprocket failures here are business-rule rejections
        # an admin can act on (wallet balance, bad pickup config, no couriers
        # serviceable, etc) — not a security-sensitive internal error, so the
        # real message must reach the UI. 5xx details get redacted by
        # src/security/error_handling.py's sanitizer; 4xx details don't.
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/api/admin/orders/{order_id}/shiprocket/track")
async def admin_track_shipment(order_id: str, email: str = Depends(require_scope_email("orders", "read"))):
    order_doc = await _get_order_doc_or_404(order_id)
    provider = get_courier_provider(order_country(order_doc))
    try:
        tracking = await provider.track(order_id)
    except CourierUnavailableError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ShiprocketAPIError as e:
        # 400, not 502: Shiprocket failures here are business-rule rejections
        # an admin can act on (wallet balance, bad pickup config, no couriers
        # serviceable, etc) — not a security-sensitive internal error, so the
        # real message must reach the UI. 5xx details get redacted by
        # src/security/error_handling.py's sanitizer; 4xx details don't.
        raise HTTPException(status_code=400, detail=str(e))
    return _json_response_content(tracking)
