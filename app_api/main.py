import uvicorn

from api.app import app  # noqa: F401
from api.config import settings

if __name__ == '__main__':
    uvicorn.run(
        'api.app:app',
        host=settings.API_HOST,
        port=settings.API_PORT,
        workers=settings.API_WORKERS,
    )
