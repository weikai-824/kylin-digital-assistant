import json

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeMeta, declarative_base, sessionmaker

from kylin.platform.config.settings import Settings


def _engine_kwargs(uri: str) -> dict:
    kw: dict = {
        "json_serializer": lambda obj: json.dumps(obj, ensure_ascii=False),
    }
    if uri.startswith("mysql"):
        kw["pool_pre_ping"] = True
        kw["pool_recycle"] = 3600
    return kw


engine = create_engine(
    Settings.basic_settings.SQLALCHEMY_DATABASE_URI,
    **_engine_kwargs(Settings.basic_settings.SQLALCHEMY_DATABASE_URI),
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base: DeclarativeMeta = declarative_base()
