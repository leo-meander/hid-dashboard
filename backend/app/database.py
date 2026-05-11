from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

# Supabase pooler (session mode, port 5432) caps at 15 concurrent client
# connections — pool_size + max_overflow MUST stay under that, with extra
# headroom for rolling deploys (old pod + new pod both holding connections
# briefly). Keeping total at 8 per pod lets two pods overlap safely.
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=3,
    pool_timeout=10,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a DB session and closes it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
