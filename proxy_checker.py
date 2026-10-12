import logging
import time
from typing import Tuple

import httpx

logger = logging.getLogger(__name__)

# Тестовий URL для перевірки працездатності проксі (перевіряємо безпосередньо OLX або публічний endpoint)
TEST_URL = "https://www.olx.ua/api/v1/offers/?limit=1"


async def check_proxy_health(proxy_url: str, timeout: float = 7.0) -> Tuple[bool, float, str]:
    """
    Перевіряє доступність та швидкість відгуку проксі через тестовий запит до OLX.
    Повертає: (чи_працює, час_відгуку_мс, повідомлення_про_помилку_або_ок)
    """
    clean_proxy = proxy_url.strip()
    if not clean_proxy:
        return False, 0.0, "Порожня адреса"

    start_time = time.perf_counter()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            proxy=clean_proxy,
            timeout=timeout,
            follow_redirects=True,
            verify=False,  # Для підтримки різних типів SSL-проксі
        ) as client:
            resp = await client.get(TEST_URL, headers=headers)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            if resp.status_code in (200, 403, 429):
                # Якщо OLX повернув 403/429 — проксі живий, але заблокований OLX
                if resp.status_code == 200:
                    return True, round(elapsed_ms, 1), "OK (200)"
                else:
                    return False, round(elapsed_ms, 1), f"OLX Blocked ({resp.status_code})"
            else:
                return False, round(elapsed_ms, 1), f"HTTP {resp.status_code}"

    except httpx.ConnectTimeout:
        return False, 0.0, "Timeout підключення"
    except httpx.ReadTimeout:
        return False, 0.0, "Timeout читання"
    except httpx.ProxyError as err:
        return False, 0.0, f"Помилка проксі: {type(err).__name__}"
    except Exception as exc:
        return False, 0.0, f"Помилка: {type(exc).__name__}"
