import asyncio
from shop_api.database import _ensure_engine, Base
import shop_api.models  # noqa: F401


async def _init_db():
    engine = _ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def pytest_configure(config):
    asyncio.run(_init_db())