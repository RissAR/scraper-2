from typing import Any, Sequence
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_main_keyboard(is_olx: bool, is_dimria: bool) -> InlineKeyboardMarkup:
    """Головне меню керування ботом для Адміністратора з роздільними кнопками парсерів."""
    btn_olx = InlineKeyboardButton(
        text="🟢 OLX: Зупинити" if is_olx else "🔴 OLX: Запустити",
        callback_data="toggle_olx",
    )
    btn_dimria = InlineKeyboardButton(
        text="🟢 DIM.RIA: Зупинити" if is_dimria else "🔴 DIM.RIA: Запустити",
        callback_data="toggle_dimria",
    )

    all_active = is_olx and is_dimria
    btn_all = InlineKeyboardButton(
        text="⏸ Зупинити ВСІ" if all_active else "🚀 Запустити ВСІ",
        callback_data="toggle_all",
    )

    keyboard = [
        [btn_olx, btn_dimria],
        [btn_all],
        [
            InlineKeyboardButton(text="🔍 Тест OLX (1 коло)", callback_data="test_olx_now"),
            InlineKeyboardButton(text="🔍 Тест DIM.RIA (1 коло)", callback_data="test_dimria_now"),
        ],
        [
            InlineKeyboardButton(text="📦 Об'єкти в базі", callback_data="view_objects:0"),
            InlineKeyboardButton(text="📊 Експорт в Excel", callback_data="export_excel"),
        ],
        [
            InlineKeyboardButton(text="👥 Користувачі", callback_data="users_menu"),
            InlineKeyboardButton(text="🌐 Меню проксі", callback_data="proxy_menu"),
        ],
        [
            InlineKeyboardButton(text="🍪 OLX Cookies", callback_data="cookies_menu"),
            InlineKeyboardButton(text="ℹ️ Системний статус", callback_data="show_status"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def cookies_menu_keyboard(has_cookies: bool) -> InlineKeyboardMarkup:
    """Клавіатура керування сесійними cookies OLX."""
    buttons = [
        [
            InlineKeyboardButton(text="✏️ Встановити / Оновити Cookies", callback_data="input_cookies"),
        ]
    ]
    if has_cookies:
        buttons.append([
            InlineKeyboardButton(text="🗑 Очистити Cookies", callback_data="clear_cookies"),
        ])
    buttons.append([
        InlineKeyboardButton(text="« Назад у меню", callback_data="back_to_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu_keyboard(is_active: bool) -> InlineKeyboardMarkup:
    """Сумісність зі старим викликом."""
    return admin_main_keyboard(is_active, is_active)


def realtor_main_keyboard() -> InlineKeyboardMarkup:
    """Головне меню Кабінету Ріелтора (чистий клієнтський інтерфейс)."""
    keyboard = [
        [
            InlineKeyboardButton(text="📦 Переглянути останні квартири", callback_data="view_objects:0"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Мій статус та підтримка", callback_data="realtor_status"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def unauthorized_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавіатура для неавторизованих користувачів (подача запиту на доступ)."""
    keyboard = [
        [
            InlineKeyboardButton(
                text="📩 Надіслати запит на доступ",
                callback_data=f"request_access:{user_id}",
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def access_request_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавіатура адміністратора під час отримання запиту на доступ від користувача."""
    keyboard = [
        [
            InlineKeyboardButton(text="✅ Схвалити (Ріелтор)", callback_data=f"approve_user:{user_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject_user:{user_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def users_menu_keyboard(users: Sequence[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавіатура списку користувачів для адміністратора."""
    keyboard = []
    for u in users[:10]:
        u_id = u["telegram_id"]
        name = u.get("full_name") or u.get("username") or f"ID {u_id}"
        role = u.get("role", "realtor")
        is_act = bool(u.get("is_active", 1))

        if role == "admin":
            icon = "👑"
            role_label = "Адмін"
        elif role == "pending":
            icon = "⏳"
            role_label = "Очікує"
        elif is_act:
            icon = "🟢"
            role_label = "Ріелтор"
        else:
            icon = "🔴"
            role_label = "Деакт."

        btn_text = f"{icon} {name[:16]} ({role_label})"
        keyboard.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"user_detail:{u_id}")
        ])

    keyboard.append([
        InlineKeyboardButton(text="◀️ Назад в адмін-панель", callback_data="back_to_main")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def user_detail_keyboard(user_id: int, current_role: str, is_active: bool) -> InlineKeyboardMarkup:
    """Кнопки керування конкретним користувачем."""
    keyboard = []
    if current_role == "pending":
        keyboard.append([
            InlineKeyboardButton(text="✅ Схвалити як Ріелтора", callback_data=f"approve_user:{user_id}"),
            InlineKeyboardButton(text="❌ Відхилити запит", callback_data=f"reject_user:{user_id}"),
        ])
    elif current_role != "admin":
        toggle_text = "🔴 Деактивувати" if is_active else "🟢 Активувати"
        keyboard.append([
            InlineKeyboardButton(text=toggle_text, callback_data=f"user_toggle_act:{user_id}")
        ])
        keyboard.append([
            InlineKeyboardButton(text="👑 Призначити Адміном", callback_data=f"make_admin:{user_id}")
        ])

    keyboard.append([
        InlineKeyboardButton(text="◀️ До списку користувачів", callback_data="users_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Кнопка повернення в головне меню адміністратора."""
    keyboard = [
        [
            InlineKeyboardButton(text="◀️ Назад в адмін-панель", callback_data="back_to_main"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def back_to_realtor_keyboard() -> InlineKeyboardMarkup:
    """Кнопка повернення в Кабінет Ріелтора."""
    keyboard = [
        [
            InlineKeyboardButton(text="◀️ Назад в кабінет", callback_data="back_to_realtor"),
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
        InlineKeyboardButton(text="◀️ Назад в адмін-панель", callback_data="back_to_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def proxy_delete_keyboard(proxies: Sequence[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавіатура вибору проксі для видалення."""
    keyboard = []
    for p in proxies:
        p_id = p["id"]
        raw_url = p["url"]
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
    current_index: int, total_count: int, url: str, is_admin: bool = True
) -> InlineKeyboardMarkup:
    """
    Клавіатура компактного перегляду об'єктів у базі без засмічення стрічки.
    Підтримує посилання як на OLX, так і на DIM.RIA.
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

    back_callback = "back_to_main" if is_admin else "back_to_realtor"
    back_text = "◀️ Назад в адмін-панель" if is_admin else "◀️ Назад в кабінет"

    keyboard = [
        [InlineKeyboardButton(text="🔗 Відкрити оголошення", url=url)],
        nav_row,
        [InlineKeyboardButton(text=back_text, callback_data=back_callback)],
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
