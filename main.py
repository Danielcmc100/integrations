from fastapi import FastAPI

from integration.webhooks import router as webhooks_router

app = FastAPI(title="PSTG Integrations")
app.include_router(webhooks_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
