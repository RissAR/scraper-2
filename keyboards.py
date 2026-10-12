from typing import Any, Sequence
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard(is_active: bool) -> InlineKeyboardMarkup:
    """Головне меню керування ботом на інлайн-кнопках."""
    toggle_text = "⏸ Зупинити моніторинг" if is_active else "▶️ Запустити моніторинг"
    toggle_data = "monitoring_pause" if is_active else "monitoring_start"

    keyboard = [
        [
            InlineKeyboardButton(text=toggle_text, callback_data=toggle_data),
        ],
        [
            InlineKeyboardButton(text="🔍 Перевірити OLX зараз", callback_data="manual_check"),
        ],
        [
            InlineKeyboardButton(text="📦 Об'єкти в базі", callback_data="view_objects:0"),
            InlineKeyboardButton(text="📊 Експорт в Excel", callback_data="export_excel"),
        ],
        [
            InlineKeyboardButton(text="🌐 Меню проксі", callback_data="proxy_menu"),
            InlineKeyboardButton(text="ℹ️ Статус системи", callback_data="show_status"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Кнопка повернення в головне меню."""
    keyboard = [
        [
            InlineKeyboardButton(text="◀️ Назад в головне меню", callback_data="back_to_main"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def cancel_fsm_keyboard() -> InlineKeyboardMarkup:
    """Кнопка скасування введення."""
    keyboard = [
        [
            InlineKeyboardButton(text="❌ Скасувати дію", callback_data="back_to_proxy_menu"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def proxy_menu_keyboard(has_proxies: bool = False) -> InlineKeyboardMarkup:
    """Меню керування проксі."""
    keyboard = [
        [
            InlineKeyboardButton(text="🔄 Перевірити всі проксі", callback_data="proxy_check_all"),
        ],
        [
            InlineKeyboardButton(text="➕ Додати проксі", callback_data="proxy_add_start"),
        ],
    ]
    if has_proxies:
        keyboard.append([
            InlineKeyboardButton(text="❌ Видалити проксі", callback_data="proxy_delete_menu"),
        ])
    keyboard.append([
        InlineKeyboardButton(text="◀️ Назад в головне меню", callback_data="back_to_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def proxy_delete_keyboard(proxies: Sequence[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавіатура вибору проксі для видалення."""
    keyboard = []
    for p in proxies:
        p_id = p["id"]
        raw_url = p["url"]
        # Маскуємо пароль, якщо він є
        masked = raw_url.split("@")[-1] if "@" in raw_url else raw_url
        btn_text = f"🗑️ #{p_id}: {masked[:28]}"
        keyboard.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"proxy_del:{p_id}")
        ])

    keyboard.append([
        InlineKeyboardButton(text="◀️ Назад до меню проксі", callback_data="proxy_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def pagination_keyboard(
    current_index: int, total_count: int, url: str
) -> InlineKeyboardMarkup:
    """
    Клавіатура компактного перегляду об'єктів у базі без засмічення стрічки.
    """
    nav_row = []

    if current_index > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Попередній", callback_data=f"view_objects:{current_index - 1}"
            )
        )
    else:
        nav_row.append(InlineKeyboardButton(text="⛔️", callback_data="noop"))

    nav_row.append(
        InlineKeyboardButton(
            text=f"{current_index + 1} / {total_count}", callback_data="noop"
        )
    )

    if current_index + 1 < total_count:
        nav_row.append(
            InlineKeyboardButton(
                text="Наступний ➡️", callback_data=f"view_objects:{current_index + 1}"
            )
        )
    else:
        nav_row.append(InlineKeyboardButton(text="⛔️", callback_data="noop"))

    keyboard = [
        [InlineKeyboardButton(text="🔗 Відкрити на OLX", url=url)],
        nav_row,
        [InlineKeyboardButton(text="◀️ Назад в головне меню", callback_data="back_to_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def new_listings_notification_keyboard(count: int) -> InlineKeyboardMarkup:
    """
    Інлайн-кнопка до сповіщення про завершення парсингу:
    'Парсинг завершено успішно, об'єктів додано: [кількість]'
    знизу кнопка 'Показати об'єкти'.
    """
    keyboard = [
        [
            InlineKeyboardButton(
                text=f"📋 Показати об'єкти ({count})",
                callback_data=f"show_feed:{count}",
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
