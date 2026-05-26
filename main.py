from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from integration.admin import router as admin_router
from integration.logging_config import configure_logging
from integration.metrics import REGISTRY
from integration.webhooks import router as webhooks_router

configure_logging()

app = FastAPI(title="PSTG Integrations")
app.include_router(webhooks_router)
app.include_router(admin_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


def main() -> None:
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
