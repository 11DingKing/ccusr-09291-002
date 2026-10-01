from fastapi import FastAPI
from app.config import settings
from app.database import engine, Base, get_db
from app.routers import materials, vehicles, suppliers, purchase, alternatives, statistics
from app.routers import supplier_confirmations, capacity
from app.data.seed import seed_all

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="国产自行车零部件供应协同系统 - 从一颗滚珠到整套飞轮，零部件供应协同平台",
    version="1.1.0"
)

app.include_router(materials.router, prefix=settings.API_V1_STR)
app.include_router(vehicles.router, prefix=settings.API_V1_STR)
app.include_router(suppliers.router, prefix=settings.API_V1_STR)
app.include_router(purchase.router, prefix=settings.API_V1_STR)
app.include_router(alternatives.router, prefix=settings.API_V1_STR)
app.include_router(statistics.router, prefix=settings.API_V1_STR)
app.include_router(supplier_confirmations.router, prefix=settings.API_V1_STR)
app.include_router(capacity.router, prefix=settings.API_V1_STR)

@app.on_event("startup")
def startup_event():
    db = next(get_db())
    try:
        seed_all(db)
        # 重启后落地已超期的预约状态；逐日占用在库里持久化，重启不改变既有占用
        try:
            from app.services.capacity import CapacityService
            CapacityService.expire_due(db)
        except Exception as exc:  # 启动清理失败不应阻断服务
            print(f"超期预约清理跳过: {exc}")
    finally:
        db.close()

@app.get("/")
def root():
    return {
        "message": "欢迎使用国产自行车零部件供应协同系统",
        "version": "1.0.0",
        "docs_url": "/docs",
        "api_prefix": settings.API_V1_STR
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
