from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    """Whether the API is up and has data to serve; no key needed, nothing else told."""
    published = request.app.state.api.settings.batch.serving_path.exists()
    return {"status": "ok", "data": "published" if published else "none yet"}
