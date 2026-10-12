from dotenv import load_dotenv

load_dotenv()  # before app imports: they read configuration from the environment

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app import sessions  # noqa: E402
from app.db import init_db  # noqa: E402
from app.routes import install_error_handlers, router  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AetherCAD", lifespan=lifespan)


# Registered before CORS so it sits inside it: unexpected errors still get CORS
# headers, and the browser shows the real message instead of "Failed to fetch".
@app.middleware("http")
async def _json_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": f"Server error: {type(e).__name__}: {e}"})


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+):3000",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
app.include_router(sessions.router)
app.include_router(router)
install_error_handlers(app)


@app.get("/")
async def read_root():
    return {"message": "AetherCAD backend is running"}


if __name__ == "__main__":
    import uvicorn

    # reload_excludes keeps session data writes from restarting the server mid-request
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True,
                reload_excludes=["data/*", "freecad_bridge/*"])
