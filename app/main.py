"""应用入口：建表、种子数据、路由、静态页面、统一错误响应。"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import Base, SessionLocal, engine
from .routers import dispatch, events, mock, public
from .seed import seed_if_empty
from .services import BizError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="景区缆车停运退票与候补改签系统")


@app.exception_handler(BizError)
async def biz_error_handler(_request: Request, exc: BizError):
    return JSONResponse(status_code=exc.status,
                        content={"detail": {"code": exc.code, "message": exc.message}})


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()


app.include_router(public.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(dispatch.router, prefix="/api")
app.include_router(mock.router, prefix="/api")

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
