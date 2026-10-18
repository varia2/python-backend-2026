import os
import tempfile
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any, Generator, AsyncGenerator
from uuid import uuid4
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:/tmp/test_shop.db"

from faker import Faker
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from shop_api.database import Base as DB_Base, get_db as original_get_db
from shop_api.routes import router_cart, router_item
from fastapi import FastAPI

# Create a temporary SQLite file for testing
_test_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db_file.close()
_test_db_path = Path(_test_db_file.name)
TEST_DATABASE_URL = f"sqlite+aiosqlite:///{_test_db_path}"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def tables_exist():
    async with test_engine.begin() as conn:
        await conn.run_sync(DB_Base.metadata.create_all)


@pytest.fixture(scope="session", autouse=True)
def with_test_db():
    async def _test_get_db() -> AsyncGenerator[AsyncSession, None]:
        session = TestSessionLocal()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    _test_app.dependency_overrides[original_get_db] = _test_get_db
    yield
    _test_app.dependency_overrides.clear()


@asynccontextmanager
async def _lifespan(app_local):
    yield


_test_app = FastAPI(lifespan=_lifespan)
_test_app.include_router(router_cart)
_test_app.include_router(router_item)


client = TestClient(_test_app)
faker = Faker()


@pytest.fixture()
def existing_empty_cart_id() -> int:
    return client.post("/cart").json()["id"]


@pytest.fixture(scope="session")
def existing_items() -> list[int]:
    items = [
        {
            "name": f"Тестовый товар {i}",
            "price": faker.pyfloat(positive=True, min_value=10.0, max_value=500.0),
        }
        for i in range(10)
    ]

    return [client.post("/item", json=item).json()["id"] for item in items]


@pytest.fixture(scope="session", autouse=True)
def existing_not_empty_carts(existing_items: list[int]) -> list[int]:
    carts = []

    for i in range(20):
        cart_id: int = client.post("/cart").json()["id"]
        for item_id in faker.random_elements(existing_items, unique=False, length=i):
            client.post(f"/cart/{cart_id}/add/{item_id}")

        carts.append(cart_id)

    return carts


@pytest.fixture()
def existing_not_empty_cart_id(
    existing_empty_cart_id: int,
    existing_items: list[int],
) -> int:
    for item_id in faker.random_elements(existing_items, unique=False, length=3):
        client.post(f"/cart/{existing_empty_cart_id}/add/{item_id}")

    return existing_empty_cart_id


@pytest.fixture()
def existing_item() -> dict[str, Any]:
    return client.post(
        "/item",
        json={
            "name": f"Тестовый товар {uuid4().hex}",
            "price": faker.pyfloat(min_value=10.0, max_value=100.0),
        },
    ).json()


@pytest.fixture()
def deleted_item(existing_item: dict[str, Any]) -> dict[str, Any]:
    item_id = existing_item["id"]
    client.delete(f"/item/{item_id}")

    existing_item["deleted"] = True
    return existing_item


def test_post_cart() -> None:
    response = client.post("/cart")

    assert response.status_code == HTTPStatus.CREATED
    assert "location" in response.headers
    assert "id" in response.json()


@pytest.mark.parametrize(
    ("cart", "not_empty"),
    [
        ("existing_empty_cart_id", False),
        ("existing_not_empty_cart_id", True),
    ],
)
def test_get_cart(request, cart: int, not_empty: bool) -> None:
    cart_id = request.getfixturevalue(cart)

    response = client.get(f"/cart/{cart_id}")

    assert response.status_code == HTTPStatus.OK
    response_json = response.json()

    len_items = len(response_json["items"])
    assert len_items > 0 if not_empty else len_items == 0

    if not_empty:
        price = 0

        for item in response_json["items"]:
            item_id = item["id"]
            price += client.get(f"/item/{item_id}").json()["price"] * item["quantity"]

        assert response_json["price"] == pytest.approx(price, 1e-8)
    else:
        assert response_json["price"] == 0.0


@pytest.mark.parametrize(
    ("query", "status_code"),
    [
        ({}, HTTPStatus.OK),
        ({"offset": 1, "limit": 2}, HTTPStatus.OK),
        ({"min_price": 1000.0}, HTTPStatus.OK),
        ({"max_price": 20.0}, HTTPStatus.OK),
        ({"min_quantity": 1}, HTTPStatus.OK),
        ({"max_quantity": 0}, HTTPStatus.OK),
        ({"offset": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"limit": 0}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"limit": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"min_price": -1.0}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"max_price": -1.0}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"min_quantity": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"max_quantity": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
    ],
)
def test_get_cart_list(query: dict[str, Any], status_code: int):
    response = client.get("/cart", params=query)

    assert response.status_code == status_code

    if status_code == HTTPStatus.OK:
        data = response.json()

        assert isinstance(data, list)

        if "min_price" in query:
            assert all(item["price"] >= query["min_price"] for item in data)

        if "max_price" in query:
            assert all(item["price"] <= query["max_price"] for item in data)

        quantity = sum(item["quantity"] for cart in data for item in cart["items"])

        if "min_quantity" in query:
            assert quantity >= query["min_quantity"]

        if "max_quantity" in query:
            assert quantity <= query["max_quantity"]


def test_post_item() -> None:
    item = {"name": "test item", "price": 9.99}
    response = client.post("/item", json=item)

    assert response.status_code == HTTPStatus.CREATED

    data = response.json()
    assert item["price"] == data["price"]
    assert item["name"] == data["name"]


def test_get_item(existing_item: dict[str, Any]) -> None:
    item_id = existing_item["id"]

    response = client.get(f"/item/{item_id}")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == existing_item


@pytest.mark.parametrize(
    ("query", "status_code"),
    [
        ({"offset": 2, "limit": 5}, HTTPStatus.OK),
        ({"min_price": 5.0}, HTTPStatus.OK),
        ({"max_price": 5.0}, HTTPStatus.OK),
        ({"show_deleted": True}, HTTPStatus.OK),
        ({"offset": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"limit": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"limit": 0}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"min_price": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"max_price": -1}, HTTPStatus.UNPROCESSABLE_ENTITY),
    ],
)
def test_get_item_list(query: dict[str, Any], status_code: int) -> None:
    response = client.get("/item", params=query)

    assert response.status_code == status_code

    if status_code == HTTPStatus.OK:
        data = response.json()

        assert isinstance(data, list)

        if "min_price" in query:
            assert all(item["price"] >= query["min_price"] for item in data)

        if "max_price" in query:
            assert all(item["price"] <= query["max_price"] for item in data)

        if "show_deleted" in query and query["show_deleted"] is False:
            assert all(item["deleted"] is False for item in data)


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        ({}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"price": 9.99}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"name": "new name", "price": 9.99}, HTTPStatus.OK),
    ],
)
def test_put_item(
    existing_item: dict[str, Any],
    body: dict[str, Any],
    status_code: int,
) -> None:
    item_id = existing_item["id"]
    response = client.put(f"/item/{item_id}", json=body)

    assert response.status_code == status_code

    if status_code == HTTPStatus.OK:
        new_item = existing_item.copy()
        new_item.update(body)
        assert response.json() == new_item


@pytest.mark.parametrize(
    ("item", "body", "status_code"),
    [
        ("deleted_item", {}, HTTPStatus.NOT_MODIFIED),
        ("deleted_item", {"price": 9.99}, HTTPStatus.NOT_MODIFIED),
        ("deleted_item", {"name": "new name", "price": 9.99}, HTTPStatus.NOT_MODIFIED),
        ("existing_item", {}, HTTPStatus.OK),
        ("existing_item", {"price": 9.99}, HTTPStatus.OK),
        ("existing_item", {"name": "new name", "price": 9.99}, HTTPStatus.OK),
        (
            "existing_item",
            {"name": "new name", "price": 9.99, "odd": "value"},
            HTTPStatus.UNPROCESSABLE_ENTITY,
        ),
        (
            "existing_item",
            {"name": "new name", "price": 9.99, "deleted": True},
            HTTPStatus.UNPROCESSABLE_ENTITY,
        ),
    ],
)
def test_patch_item(request, item: str, body: dict[str, Any], status_code: int) -> None:
    item_data: dict[str, Any] = request.getfixturevalue(item)
    item_id = item_data["id"]
    response = client.patch(f"/item/{item_id}", json=body)

    assert response.status_code == status_code

    if status_code == HTTPStatus.OK:
        patch_response_body = response.json()

        response = client.get(f"/item/{item_id}")
        patched_item = response.json()

        assert patched_item == patch_response_body


def test_delete_item(existing_item: dict[str, Any]) -> None:
    item_id = existing_item["id"]

    response = client.delete(f"/item/{item_id}")
    assert response.status_code == HTTPStatus.OK

    response = client.get(f"/item/{item_id}")
    assert response.status_code == HTTPStatus.NOT_FOUND

    response = client.delete(f"/item/{item_id}")
    assert response.status_code == HTTPStatus.OK
