# storage.py
import os, json, datetime, logging
from typing import Dict, Any, Optional

import redis
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text, Date, DateTime
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

# --- Redis ---
REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

# --- Postgres / SQLAlchemy ---
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False) if engine else None

Base = declarative_base()

class Summary(Base):
    __tablename__ = "summaries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    date = Column(Date, nullable=False, index=True)
    summary = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)

def _safe_init_db():
    if engine:
        try:
            Base.metadata.create_all(engine)
        except Exception as e:
            logger.error(f"[DB] create_all error: {e}")

_safe_init_db()

def _today(date: Optional[datetime.date] = None) -> datetime.date:
    return date or datetime.date.today()

def _answers_key(date: Optional[datetime.date] = None) -> str:
    d = _today(date)
    return f"answers:{d.isoformat()}"

# -------- conversation_id в Redis --------
def get_conversation_id(chat_id: int) -> Optional[str]:
    if not r:
        return None
    return r.get(f"conv:{chat_id}")

def set_conversation_id(chat_id: int, conv_id: str, ttl_days: int = 7) -> None:
    if not r:
        return
    key = f"conv:{chat_id}"
    r.set(key, conv_id, ex=ttl_days * 24 * 3600)

# -------- ответы за день (Redis + Postgres) --------
def save_answer(chat_id: int, name: str, summary: str, date: Optional[datetime.date] = None) -> None:
    """
    Пишем в Redis (за сегодня) + upsert в Postgres.
    """
    d = _today(date)

    # Redis
    if r:
        key = _answers_key(d)
        value = json.dumps({"name": name, "summary": summary}, ensure_ascii=False)
        r.hset(key, chat_id, value)
        r.expire(key, 21 * 24 * 3600)  # 21 день держим кэш

    # Postgres
    if not SessionLocal:
        return
    sess = SessionLocal()
    try:
        row = (sess.query(Summary)
                  .filter(Summary.chat_id == chat_id, Summary.date == d)
                  .one_or_none())
        now = datetime.datetime.utcnow()
        if row:
            row.summary = summary
            row.name = name
            row.updated_at = now
        else:
            row = Summary(chat_id=chat_id, name=name, date=d,
                          summary=summary, created_at=now, updated_at=now)
            sess.add(row)
        sess.commit()
    except SQLAlchemyError as e:
        sess.rollback()
        logger.error(f"[DB] save_answer error: {e}")
    finally:
        sess.close()

def load_answers_for(date: Optional[datetime.date] = None) -> Dict[str, Dict[str, Any]]:
    """
    Возвращает словарь: chat_id (str) -> {"name": ..., "summary": ...}
    Сначала Redis, если пусто — Postgres с прогревом Redis.
    """
    d = _today(date)

    # Redis
    if r:
        key = _answers_key(d)
        raw = r.hgetall(key)
        if raw:
            return {str(k): json.loads(v) for k, v in raw.items()}

    # Postgres
    out: Dict[str, Dict[str, Any]] = {}
    if not SessionLocal:
        return out

    sess = SessionLocal()
    try:
        rows = sess.query(Summary).filter(Summary.date == d).all()
        for row in rows:
            out[str(row.chat_id)] = {"name": row.name, "summary": row.summary}

        # Прогреем Redis
        if r and out:
            pipe = r.pipeline()
            key = _answers_key(d)
            for k, v in out.items():
                pipe.hset(key, k, json.dumps(v, ensure_ascii=False))
            pipe.expire(key, 21 * 24 * 3600)
            pipe.execute()
    except SQLAlchemyError as e:
        logger.error(f"[DB] load_answers_for error: {e}")
    finally:
        sess.close()

    return out