from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.routers.auth import router as auth_router
from app.routers.settings import router as settings_router
from app.routers.leads import router as leads_router
from app.routers.workspaces import router as workspaces_router

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
    workspaces_router,
    prefix="/api/workspaces",
    tags=["workspace authorization"],
)

