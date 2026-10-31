from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.sensitive_query import SensitiveQueryRedactionMiddleware
from app.routers.auth import router as auth_router
from app.routers.settings import router as settings_router
from app.routers.leads import router as leads_router
from app.routers.lead_tasks import lead_task_router, task_router
from app.routers.lead_engagements import router as lead_engagements_router
from app.routers.mail import router as mail_router
from app.routers.mail_templates import router as mail_templates_router
from app.routers.message_templates import router as message_templates_router
from app.routers.workspaces import router as workspaces_router
from app.routers.customers import customer_router, lead_conversion_router
from app.routers.outbound_messages import router as outbound_messages_router
from app.routers.outbound_batches import router as outbound_batches_router
from app.routers.sms import router as sms_router
from app.routers.whatsapp import router as whatsapp_router, webhook_router as whatsapp_webhook_router
from app.routers.meta_whatsapp import (
    router as meta_whatsapp_router,
    webhook_router as meta_whatsapp_webhook_router,
)
from app.routers.meta_messenger import (
    router as meta_messenger_router,
    webhook_router as meta_messenger_webhook_router,
)
from app.routers.meta_instagram import (
    oauth_router as meta_instagram_oauth_router,
    router as meta_instagram_router,
    webhook_router as meta_instagram_webhook_router,
)

app = FastAPI(
    title="PowerLead Response API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SensitiveQueryRedactionMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exception: RequestValidationError,
) -> JSONResponse:
    del request

    errors = []
    for error in exception.errors():
        location = [str(part) for part in error["loc"] if part != "body"]
        message = error["msg"].removeprefix("Value error, ")
        errors.append(
            {
                "field": ".".join(location) or "request",
                "message": message,
            }
        )

    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": "Invalid request data",
            "errors": errors,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exception: Exception) -> JSONResponse:
    del request, exception
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "An unexpected error occurred",
        },
    )


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth_router, prefix="/api/auth", tags=["authentication"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(leads_router, prefix="/api/leads", tags=["leads"])
app.include_router(
    lead_conversion_router,
    prefix="/api/leads",
    tags=["lead conversion"],
)
app.include_router(
    outbound_messages_router,
    prefix="/api/mail/messages",
    tags=["outbound messages"],
)
app.include_router(
    outbound_batches_router,
    prefix="/api/mail/batches",
    tags=["outbound email batches"],
)
app.include_router(customer_router, prefix="/api/customers", tags=["customers"])
app.include_router(
    lead_engagements_router,
    prefix="/api/leads",
    tags=["lead replies and bookings"],
)
app.include_router(lead_task_router, prefix="/api/leads", tags=["lead tasks"])
app.include_router(task_router, prefix="/api/tasks", tags=["lead tasks"])
app.include_router(mail_router, prefix="/api/mail", tags=["mail sender accounts"])
app.include_router(
    mail_templates_router,
    prefix="/api/mail/templates",
    tags=["mail templates"],
)
app.include_router(
    message_templates_router,
    prefix="/api/message-templates",
    tags=["message templates"],
)
app.include_router(sms_router, prefix="/api/sms", tags=["lead SMS"])
app.include_router(
    whatsapp_router,
    prefix="/api/messages/whatsapp",
    tags=["WhatsApp inbox"],
)
app.include_router(whatsapp_webhook_router, tags=["Twilio webhooks"])
app.include_router(
    meta_whatsapp_router,
    prefix="/api/messages/meta/whatsapp",
    tags=["Meta WhatsApp inbox"],
)
app.include_router(meta_whatsapp_webhook_router, tags=["Meta WhatsApp webhooks"])
app.include_router(
    meta_messenger_router,
    prefix="/api/messages/meta/messenger",
    tags=["Meta Messenger inbox"],
)
app.include_router(
    meta_messenger_webhook_router,
    tags=["Meta Messenger webhooks"],
)
app.include_router(
    meta_instagram_router,
    prefix="/api/messages/meta/instagram",
    tags=["Meta Instagram inbox"],
)
app.include_router(
    meta_instagram_webhook_router,
    tags=["Meta Instagram webhooks"],
)
app.include_router(
    meta_instagram_oauth_router,
    tags=["Meta Instagram authorization"],
)
app.include_router(
    workspaces_router,
    prefix="/api/workspaces",
    tags=["workspace authorization"],
)

