from fastapi_users import FastAPIUsers
from fastapi import FastAPI, Depends, HTTPException, APIRouter, status
from auth.database import User
from auth.manager import get_user_manager
from auth.auth import auth_backend
from auth.schemas import UserRead, UserCreate
from pydantic import BaseModel, HttpUrl
from typing import Optional
from datetime import datetime, timedelta
import random
import string
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from auth.database import Link, User, get_async_session


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


fastapi_users = FastAPIUsers[User, int](
    get_user_manager,
    [auth_backend],)

app = FastAPI()
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
    return f"Hello, {user.username}"


@app.get("/unprotected-route")
def unprotected_route():
    return f"Hello, anonymous"


def generate_code(length=6):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


# --- 1. СОЗДАНИЕ ССЫЛКИ (Доступно всем) ---
@app.post("/links/shorten", response_model=LinkResponse)
async def shorten_link(
        data: LinkCreate,
        user: User = Depends(optional_current_user),  # Может быть None
        session: AsyncSession = Depends(get_async_session)
):
    short_code = data.custom_alias

    data.expires_at += timedelta(days=30)
    if data.expires_at and data.expires_at.tzinfo is not None:
        data.expires_at = data.expires_at.replace(tzinfo=None)

    # Если передан алиас, проверяем его на занятость
    if short_code:
        query = select(Link).where(Link.short_code == short_code)
        result = await session.execute(query)
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Этот alias уже занят")
    else:
        # Генерируем уникальный код
        while True:
            short_code = generate_code()
            query = select(Link).where(Link.short_code == short_code)
            result = await session.execute(query)
            if not result.scalar_one_or_none():
                break

    new_link = Link(
        original_url=str(data.original_url),
        short_code=short_code,
        expires_at=data.expires_at,
        user_id=user.id if user else None  # <--- ВОТ ЗДЕСЬ МАГИЯ ПРОВЕРКИ
    )

    session.add(new_link)
    await session.commit()
    return new_link


# --- 2. РЕДИРЕКТ ПО ССЫЛКЕ (Доступно всем) ---
@app.get("/{short_code}")
async def redirect_to_original(
        short_code: str,
        session: AsyncSession = Depends(get_async_session)
):
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    # Проверка на истечение срока действия
    if link.expires_at and link.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Срок действия ссылки истек")

    # Обновляем статистику
    link.clicks += 1
    link.last_used_at = datetime.utcnow()
    await session.commit()

    return RedirectResponse(url=link.original_url)


# --- 3. ПОЛУЧЕНИЕ СТАТИСТИКИ (Доступно всем или можно ограничить) ---
@app.get("/links/{short_code}/stats", response_model=LinkStatsResponse)
async def get_link_stats(
        short_code: str,
        session: AsyncSession = Depends(get_async_session)
):
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    return link


# --- 4. УДАЛЕНИЕ ССЫЛКИ (Только автор) ---
@app.delete("/links/{short_code}")
async def delete_link(
        short_code: str,
        user: User = Depends(current_active_user),  # Требуем логин
        session: AsyncSession = Depends(get_async_session)
):
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    # Проверяем, что удаляет именно создатель
    if link.user_id != user.id:
        raise HTTPException(status_code=403, detail="Вы не можете удалить чужую ссылку")

    await session.delete(link)
    await session.commit()
    return {"message": "Ссылка успешно удалена"}


# --- 5. ОБНОВЛЕНИЕ ССЫЛКИ (Только автор) ---
@app.put("/links/{short_code}")
async def update_link(
        short_code: str,
        data: LinkUpdate,
        user: User = Depends(current_active_user),  # Требуем логин
        session: AsyncSession = Depends(get_async_session)
):
    query = select(Link).where(Link.short_code == short_code)
    result = await session.execute(query)
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    if link.user_id != user.id:
        raise HTTPException(status_code=403, detail="Вы не можете изменять чужую ссылку")

    if data.custom_alias and data.custom_alias != short_code:
        # Проверяем, не занят ли новый алиас
        alias_query = select(Link).where(Link.short_code == data.custom_alias)
        alias_result = await session.execute(alias_query)
        if alias_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Этот alias уже занят")
        link.short_code = data.custom_alias

    if data.original_url:
        link.original_url = str(data.original_url)

    await session.commit()
    return {"message": "Ссылка обновлена"}


# --- 6. ПОИСК ПО ОРИГИНАЛЬНОМУ URL ---
@app.get("/links/search/")
async def search_links(
        original_url: str,
        session: AsyncSession = Depends(get_async_session)
):
    query = select(Link).where(Link.original_url == original_url)
    result = await session.execute(query)
    links = result.scalars().all()

    return [{"short_code": l.short_code, "original_url": l.original_url} for l in links]
