import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import DateTime, Integer, String, event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("guessing-api")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://game:game@localhost:5432/game",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "2"))
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "5"))
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "10"))

engine = create_async_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_pre_ping=True,
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
redis_client = Redis.from_url(
    REDIS_URL,
    max_connections=REDIS_MAX_CONNECTIONS,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)

db_pool_size = Gauge("app_db_pool_size", "Configured SQLAlchemy connections per process")
db_pool_checked_out = Gauge("app_db_pool_checked_out", "Currently checked out DB connections")
db_pool_checked_out_peak = Gauge("app_db_pool_checked_out_peak", "Peak checked-out DB connections since process start")
db_pool_overflow = Gauge("app_db_pool_overflow", "Currently used overflow DB connections")
db_pool_capacity = Gauge("app_db_pool_capacity", "Maximum DB connections per process")
redis_pool_capacity = Gauge(
    "app_redis_pool_capacity",
    "Maximum Redis client connections per process",
)
http_requests = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)
http_request_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)
checked_out_connections = 0
peak_checked_out_connections = 0


@event.listens_for(engine.sync_engine, "checkout")
def record_connection_checkout(_dbapi_connection, _connection_record, _connection_proxy):
    global checked_out_connections, peak_checked_out_connections
    checked_out_connections += 1
    peak_checked_out_connections = max(peak_checked_out_connections, checked_out_connections)
    db_pool_checked_out.set(checked_out_connections)
    db_pool_checked_out_peak.set(peak_checked_out_connections)


@event.listens_for(engine.sync_engine, "checkin")
def record_connection_checkin(_dbapi_connection, _connection_record):
    global checked_out_connections
    checked_out_connections = max(0, checked_out_connections - 1)
    db_pool_checked_out.set(checked_out_connections)


class Base(DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    upper_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    target: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreateGameRequest(BaseModel):
    upper_bound: int = Field(default=100, ge=2, le=1000)


class GuessRequest(BaseModel):
    guess: int = Field(ge=1, le=1000)


class GameResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    upper_bound: int
    attempts: int
    status: str
    created_at: datetime
    completed_at: datetime | None


class GuessResponse(BaseModel):
    game_id: UUID
    outcome: str
    attempts: int
    status: str


class StatsResponse(BaseModel):
    total_games: int
    completed_games: int
    cache: str


def refresh_pool_metrics() -> None:
    pool = engine.sync_engine.pool
    db_pool_size.set(DB_POOL_SIZE)
    db_pool_checked_out.set(checked_out_connections)
    db_pool_checked_out_peak.set(peak_checked_out_connections)
    db_pool_overflow.set(max(0, pool.overflow()))
    db_pool_capacity.set(DB_POOL_SIZE + DB_MAX_OVERFLOW)
    redis_pool_capacity.set(REDIS_MAX_CONNECTIONS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(
    title="Guessing Game API",
    version="1.0.0",
    description="A small API demo with PostgreSQL persistence, Redis caching, and pool metrics.",
    lifespan=lifespan,
)


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        route = request.scope.get("route")
        path = route.path if route else "/unmatched"
        http_requests.labels(request.method, path, "500").inc()
        http_request_duration.labels(request.method, path).observe(time.perf_counter() - started)
        raise
    route = request.scope.get("route")
    path = route.path if route else "/unmatched"
    if path != "/metrics":
        http_requests.labels(request.method, path, str(response.status_code)).inc()
        http_request_duration.labels(request.method, path).observe(time.perf_counter() - started)
    return response


@app.get("/health/live", include_in_schema=False)
async def liveness():
    return {"status": "alive"}


@app.get("/health/ready", include_in_schema=False)
async def readiness():
    try:
        async with SessionFactory() as session:
            await session.execute(select(1))
        await redis_client.ping()
    except Exception as exc:
        logger.warning("Readiness check failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Dependencies unavailable") from exc
    return {"status": "ready"}


@app.post("/api/games", response_model=GameResponse, status_code=status.HTTP_201_CREATED)
async def create_game(payload: CreateGameRequest):
    async with SessionFactory() as session:
        game = Game(upper_bound=payload.upper_bound, target=random.randint(1, payload.upper_bound))
        session.add(game)
        await session.commit()
        await session.refresh(game)
        return game


@app.post("/api/games/{game_id}/guesses", response_model=GuessResponse)
async def submit_guess(game_id: UUID, payload: GuessRequest):
    async with SessionFactory() as session:
        game = await session.get(Game, str(game_id))
        if game is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Game not found")
        if game.status != "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is already complete")
        if payload.guess > game.upper_bound:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"guess must be between 1 and {game.upper_bound}",
            )

        game.attempts += 1
        if payload.guess < game.target:
            outcome = "too_low"
        elif payload.guess > game.target:
            outcome = "too_high"
        else:
            outcome = "correct"
            game.status = "completed"
            game.completed_at = datetime.now(timezone.utc)
        await session.commit()
        return GuessResponse(
            game_id=UUID(game.id),
            outcome=outcome,
            attempts=game.attempts,
            status=game.status,
        )


@app.get("/api/games", response_model=list[GameResponse])
async def list_games(limit: Annotated[int, Query(ge=1, le=100)] = 20):
    async with SessionFactory() as session:
        result = await session.scalars(select(Game).order_by(Game.created_at.desc()).limit(limit))
        return list(result)


@app.get("/api/stats", response_model=StatsResponse)
async def game_stats():
    cached = await redis_client.get("game-stats")
    if cached:
        values = json.loads(cached)
        return StatsResponse(**values, cache="hit")

    async with SessionFactory() as session:
        total, completed = (
            await session.execute(
                select(
                    func.count(Game.id),
                    func.count(Game.id).filter(Game.status == "completed"),
                )
            )
        ).one()
    values = {"total_games": total, "completed_games": completed}
    await redis_client.set("game-stats", json.dumps(values), ex=30)
    return StatsResponse(**values, cache="miss")


@app.get("/metrics", include_in_schema=False)
async def metrics():
    refresh_pool_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
