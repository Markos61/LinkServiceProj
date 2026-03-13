from contextlib import asynccontextmanager
import random
from fastapi_users import FastAPIUsers
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from auth.manager import get_user_manager
from auth.auth import auth_backend
from auth.schemas import UserRead, UserCreate
from pydantic import BaseModel, HttpUrl
from typing import Optional
from datetime import datetime, timedelta
import string
import asyncio
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from auth.database import Link, User, get_async_session, redis_client, async_session_maker

fastapi_users = FastAPIUsers[User, int](
    get_user_manager,
    [auth_backend], )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Функция для запуска автоматического удаления
     просроченных ссылок из БД"""
    print("LIFESPAN STARTING...")
    deletion_task = asyncio.create_task(auto_delete_expired_links())
    yield
    deletion_task.cancel()


app = FastAPI(lifespan=lifespan, title="Link Shortener API",
              root_path_in_servers=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/jwt",
    tags=["auth"],
)

app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)

current_user = fastapi_users.current_user()
current_active_user = fastapi_users.current_user(active=True)
optional_current_user = fastapi_users.current_user(active=True, optional=True)


@app.get("/protected-route")
def protected_route(user: User = Depends(current_user)):
    return f"Привет, {user.username}"


@app.get("/unprotected-route")
def unprotected_route():
    return f"Привет, аноним"


class LinkCreate(BaseModel):
    original_url: HttpUrl
    custom_alias: Optional[str] = None
    expires_at: Optional[datetime] = None


class LinkUpdate(BaseModel):
    original_url: Optional[HttpUrl] = None
    custom_alias: Optional[str] = None


class LinkResponse(BaseModel):
    short_code: str
    original_url: HttpUrl
    expires_at: Optional[datetime] = None


class LinkStatsResponse(BaseModel):
    original_url: HttpUrl
    created_at: datetime
    clicks: int
    last_used_at: Optional[datetime] = None


async def auto_delete_expired_links():
    while True:
        try:
            async with async_session_maker() as session:

                now = datetime.utcnow()
                stmt = delete(Link).where(
                    Link.expires_at.isnot(None),
                    Link.expires_at <= now
                )
                result = await session.execute(stmt)
                await session.commit()

                if result.rowcount > 0:
                    print(f" [CLEANUP] Удалено просроченных ссылок: {result.rowcount}")
                else:
                    print(f" [DEBUG] Проверка выполнена в {now}, ничего не удалено.")

        except Exception as e:
            print(f" [ERROR] Ошибка при очистке БД: {e}")

        await asyncio.sleep(60)


async def update_link_statistics(short_code: str):
    """Функция для фонового асинхронного обновления
     счётчика кликов и времени последнего использования"""
    async with async_session_maker() as session:
        query = (
            update(Link)
            .where(Link.short_code == short_code)
            .values(
                clicks=Link.clicks + 1,
                last_used_at=datetime.utcnow()
            )
        )
        await session.execute(query)
        await session.commit()


async def generate_code(length=6):
    """Функция для генерации ссылки"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


@app.post("/links/shorten", response_model=LinkResponse)
async def shorten_link(
        request: Request,
        data: LinkCreate,
        user: User = Depends(optional_current_user),  # Может быть None
        session: AsyncSession = Depends(get_async_session)
):
    """Функция для создания короткой ссылки с генерацией или кастомным alias"""
    short_code = data.custom_alias

    data.expires_at += timedelta(days=30)
    # data.expires_at += timedelta(minutes=3)  # Использовалось для отладки
    if data.expires_at and data.expires_at.tzinfo is not None:
        data.expires_at = data.expires_at.replace(tzinfo=None)

    if short_code:
        query = select(Link).where(Link.short_code == short_code)
        result = await session.execute(query)
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Этот alias уже занят")
    else:
        while True:
            short_code = await generate_code()
            query = select(Link).where(Link.short_code == short_code)
            result = await session.execute(query)
            if not result.scalar_one_or_none():
                break

    base_url = str(request.base_url).rstrip("/")

    new_link = Link(
        original_url=str(data.original_url),
        short_code=short_code,
        expires_at=data.expires_at,
        user_id=user.id if user else None
    )

    session.add(new_link)
    await session.commit()
    # return new_link
    return {
        "short_url": f"{base_url}/{short_code}",
        "short_code": short_code,
        "original_url": data.original_url
    }


@app.get("/{short_code}")
async def redirect_to_original(
        short_code: str,
        background_tasks: BackgroundTasks,
        session: AsyncSession = Depends(get_async_session)
):
    """Функция для вывода оригинального URL по короткой ссылке
    Сначала ищем в Redis и обновляем статистику в фоне,
     или обращаемся к базе данных.
    """
    redis_key = f"link:{short_code}"

    cached_url = await redis_client.get(redis_key)

    if cached_url:
        background_tasks.add_task(update_link_statistics, short_code)
        return RedirectResponse(url=cached_url)

    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    if link.expires_at and link.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Срок действия ссылки истек")

    cash_seconds = 86400  # По умолчанию кэшируем на 1 сутки

    if link.expires_at:
        time_left = link.expires_at.replace(tzinfo=None) - datetime.utcnow()
        cash_seconds = int(time_left.total_seconds())

    if cash_seconds > 0:
        # "ex=cash_seconds заставит Redis автоматически удалить ключ"
        await redis_client.set(redis_key, link.original_url, ex=cash_seconds)

    background_tasks.add_task(update_link_statistics, short_code)

    return RedirectResponse(url=link.original_url)


@app.get("/links/{short_code}/stats", response_model=LinkStatsResponse)
async def get_link_stats(
        short_code: str,
        session: AsyncSession = Depends(get_async_session)
):
    """Функция для просмотра статистики по короткой ссылке
     (оригинальный URL, время создания, кол-во кликов,
      время последнего использования)"""
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    return link


@app.delete("/links/{short_code}")
async def delete_link(
        short_code: str,
        user: User = Depends(current_active_user),  # Требуем логин
        session: AsyncSession = Depends(get_async_session)
):
    """Функция для удаления короткой ссылки, доступная только автору"""
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    if link.user_id != user.id:
        raise HTTPException(status_code=403, detail="Вы не можете удалить чужую ссылку")

    await session.delete(link)
    await session.commit()
    await redis_client.delete(f"link:{short_code}")
    return {"message": "Ссылка успешно удалена"}


@app.put("/links/{short_code}")
async def update_link(
        short_code: str,
        data: LinkUpdate,
        user: User = Depends(current_active_user),  # Требуем логин
        session: AsyncSession = Depends(get_async_session)
):
    """Функция для обновления ссылки, доступная только автору"""
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    if link.user_id != user.id:
        raise HTTPException(status_code=403, detail="Вы не можете изменять чужую ссылку")

    if data.custom_alias and data.custom_alias != short_code:
        alias_query = select(Link).where(Link.short_code == data.custom_alias)
        alias_result = await session.execute(alias_query)
        if alias_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Этот alias уже занят")
        link.short_code = data.custom_alias

    if data.original_url:
        link.original_url = str(data.original_url)

    await session.commit()
    await redis_client.delete(f"link:{short_code}")
    return {"message": "Ссылка обновлена"}


@app.get("/links/search/")
async def search_links(
        original_url: str,
        session: AsyncSession = Depends(get_async_session)
):
    """Функция для поиска короткой ссылки по оригинальному URL"""
    query = select(Link).where(Link.original_url == original_url)
    result = await session.execute(query)
    links = result.scalars().all()

    return [{"short_code": l.short_code, "original_url": l.original_url} for l in links]

# uvicorn main:app --host 127.0.0.1 --port 8000
# C:\Windows\System32\OpenSSH\ssh.exe -R 80:127.0.0.1:8000 serveo.net
