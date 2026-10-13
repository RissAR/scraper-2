import asyncio
import os
import shutil
import tempfile
from config import settings
from database import DatabaseManager
from keyboards import (
    admin_main_keyboard,
    realtor_main_keyboard,
    unauthorized_keyboard,
    access_request_admin_keyboard,
    users_menu_keyboard,
    user_detail_keyboard,
    pagination_keyboard,
)


async def test_rbac_and_parsers():
    temp_dir = tempfile.mkdtemp()
    temp_db_path = os.path.join(temp_dir, "test.db")
    db = DatabaseManager(db_path=temp_db_path)
    await db.init_db()

    # 1. Test Parsers State
    state = await db.get_parsers_state()
    assert state["olx"] is True, f"Expected olx=True, got {state['olx']}"
    assert state["dimria"] is True, f"Expected dimria=True, got {state['dimria']}"

    await db.set_parser_state("olx", False)
    state = await db.get_parsers_state()
    assert state["olx"] is False, f"Expected olx=False, got {state['olx']}"
    assert state["dimria"] is True, f"Expected dimria=True, got {state['dimria']}"

    await db.set_all_parsers_state(True)
    state = await db.get_parsers_state()
    assert state["olx"] is True and state["dimria"] is True, "Expected all True"

    await db.set_all_parsers_state(False)
    state = await db.get_parsers_state()
    assert state["olx"] is False and state["dimria"] is False, "Expected all False"

    # 2. Test RBAC Users
    await db.add_user(111, "admin_user", "Admin One", role="admin", is_active=True)
    await db.add_user(222, "realtor_user", "Realtor One", role="realtor", is_active=True)
    await db.add_user(333, "pending_user", "Pending One", role="pending", is_active=False)

    role1 = await db.get_user_role(111)
    assert role1 == "admin", f"Expected admin, got {role1}"

    role2 = await db.get_user_role(222)
    assert role2 == "realtor", f"Expected realtor, got {role2}"

    role3 = await db.get_user_role(333)
    assert role3 == "deactivated", f"Expected deactivated for inactive pending, got {role3}"

    user3 = await db.get_user(333)
    assert user3["role"] == "pending", f"Expected pending, got {user3['role']}"

    recipients = await db.get_active_recipients()
    assert 111 in recipients, "Admin should be in recipients"
    assert 222 in recipients, "Realtor should be in recipients"
    assert 333 not in recipients, "Inactive pending should NOT be in recipients"

    # Test status change
    await db.set_user_status(222, False)
    recipients_after = await db.get_active_recipients()
    assert 222 not in recipients_after, "Deactivated realtor should NOT be in recipients"

    # 3. Test Keyboards
    kb_admin = admin_main_keyboard(is_olx=True, is_dimria=False)
    assert kb_admin.inline_keyboard[0][0].text == "🟢 OLX: Зупинити"
    assert kb_admin.inline_keyboard[0][1].text == "🔴 DIM.RIA: Запустити"
    assert kb_admin.inline_keyboard[1][0].text == "🚀 Запустити ВСІ"

    kb_admin_all = admin_main_keyboard(is_olx=True, is_dimria=True)
    assert kb_admin_all.inline_keyboard[1][0].text == "⏸ Зупинити ВСІ"

    kb_realtor = realtor_main_keyboard()
    assert len(kb_realtor.inline_keyboard) == 2
    assert "Переглянути останні квартири" in kb_realtor.inline_keyboard[0][0].text

    kb_unauth = unauthorized_keyboard(444)
    assert kb_unauth.inline_keyboard[0][0].callback_data == "request_access:444"

    kb_req = access_request_admin_keyboard(444)
    assert kb_req.inline_keyboard[0][0].callback_data == "approve_user:444"
    assert kb_req.inline_keyboard[0][1].callback_data == "reject_user:444"

    users_list = await db.list_users()
    kb_users = users_menu_keyboard(users_list)
    assert len(kb_users.inline_keyboard) >= 3

    kb_pag_admin = pagination_keyboard(0, 5, "https://olx.ua", is_admin=True)
    assert kb_pag_admin.inline_keyboard[-1][0].callback_data == "back_to_main"

    kb_pag_realtor = pagination_keyboard(0, 5, "https://dim.ria.com", is_admin=False)
    assert kb_pag_realtor.inline_keyboard[-1][0].callback_data == "back_to_realtor"

    shutil.rmtree(temp_dir)
    print("ALL RBAC AND CONTROLS TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(test_rbac_and_parsers())
