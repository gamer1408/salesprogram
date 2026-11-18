import os
import re
import json
import asyncio
import logging
import tempfile
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

from sheets_db import SheetsDB, TZ, today_str, to_int, parse_datetime_safe
from ai_voice import VoiceCRMProcessor

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Sale_P_Bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ALLOWED_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

db = SheetsDB()
voice_ai = VoiceCRMProcessor()


# ==========================================
# FSM HOLATLARI
# ==========================================
class AddClientFSM(StatesGroup):
    ism = State()
    telefon = State()
    tg_username = State()
    viloyat = State()
    nav = State()
    soni = State()


class EditClientFSM(StatesGroup):
    waiting_voice = State()
    waiting_call_note = State()
    waiting_price_zaklad = State()
    waiting_new_promise = State()
    waiting_paket_info = State()
    waiting_reminder_time = State()


# ==========================================
# YORDAMCHI FUNKSIYALAR
# ==========================================
def is_admin(user_id: int) -> bool:
    if not ALLOWED_ADMIN_IDS:
        return True
    return user_id in ALLOWED_ADMIN_IDS


def fmt_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def build_progress_bar(pct: int, length: int = 10) -> str:
    filled = int(round((pct / 100) * length))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


async def safe_edit_message(
    bot_instance: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    try:
        await bot_instance.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Edit xatosi: {e}")


def get_next_call_info(client: dict, settings: dict) -> tuple[int, str]:
    for idx in range(1, 5):
        if not str(client.get(f"Qongiroq_{idx}", "")).strip():
            plan = settings.get(
                f"QONGIROQ_{idx}_REJA",
                "Mijoz ehtiyojini aniqlab, zaklad olishga yo'naltiring.",
            )
            return idx, plan
    return 4, settings.get("QONGIROQ_4_REJA", "Yuklash va qoldiq to'lovni tasdiqlang.")


def build_tg_url(client: dict) -> str:
    username = str(client.get("TG_Username", "")).replace("@", "").strip()
    if username:
        return f"https://t.me/{username}"
    raw_phone = str(client.get("Telefon", "")).strip()
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    if digits:
        if not digits.startswith("998") and len(digits) == 9:
            digits = "998" + digits
        return f"https://t.me/+{digits}"
    return "https://t.me/"


# ==========================================
# BOSH MENYU (FOKUS PANELI)
# ==========================================
async def render_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    stats = await db.get_dashboard_stats()
    bar = build_progress_bar(stats["today_pct"])

    text = (
        "🌱 <b>MALINACHI_ | SALE_P SOTUV CRM</b>\n"
        "<i>Angren ko‘chatxonasi: Atlant & Maravilla</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🚨 <b>Shoshilinch (2+ kun kutganlar):</b> <code>{stats['emergency_count']} ta</code>\n"
        f"📞 <b>Bugungi qo‘ng‘iroqlar progressi:</b>\n"
        f"<code>[{bar}] {stats['today_pct']}% ({stats['done_today']}/{stats['total_today']})</code>\n"
        f"🎯 <b>Oylik maqsad:</b> <code>{fmt_money(stats['sold_seedlings'])} / {fmt_money(stats['monthly_goal'])} ta ko‘chat</code>\n"
        f"💰 <b>Jami tushgan zaklad:</b> <code>{fmt_money(stats['total_zaklad'])} so‘m</code>\n"
        f"⏳ <b>Kutilayotgan qoldiq:</b> <code>{fmt_money(stats['total_qoldiq'])} so‘m</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <i>Hozir qaysi mijozlar bilan ishlaymiz?</i>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🚨 Shoshilinch qo‘ng‘iroqlar ({stats['emergency_count']})",
                    callback_data="list:emergency:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📋 Barcha mijozlar ({stats['total_clients']})",
                    callback_data="list:all:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💰 Zaklad to‘laganlar ({stats['zaklad_count']})",
                    callback_data="list:zaklad:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Yangi mijoz qo‘shish",
                    callback_data="add_client:start",
                ),
                InlineKeyboardButton(
                    text="🔄 Yangilash",
                    callback_data="menu:home",
                ),
            ],
        ]
    )
    return text, kb


# ==========================================
# MIJOZLAR RO'YXATI (PRIORITY + SAHIFALASH)
# ==========================================
async def render_client_list(mode: str = "all", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    clients = await db.get_all_clients()

    if mode == "emergency":
        filtered = [c for c in clients if c["is_emergency"]]
        title = "🚨 SHOSHILINCH QO‘NG‘IROQLAR (2+ KUN KUTGANLAR)"
    elif mode == "zaklad":
        filtered = [c for c in clients if c["Status"] in ("Zaklad_olindi", "Yuklandi")]
        title = "💰 ZAKLAD TO‘LAGAN VA YUKLANADIGAN MIJOZLAR"
    else:
        filtered = clients
        title = "📋 BARCHA MIJOZLAR (PRIORITY TARTIBIDA)"

    per_page = 6
    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    page_items = filtered[start_idx : start_idx + per_page]

    text = (
        f"<b>{title}</b>\n"
        f"Jami: <b>{total} ta</b> | Sahifa: <b>{page + 1}/{total_pages}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )
    if not page_items:
        text += "<i>Bu bo‘limda hozircha mijozlar yo‘q.</i>"
    else:
        text += "<i>Mijoz ustiga bosing — kartochka ochiladi:</i>"

    buttons = []
    for c in page_items:
        cid = c["id_int"]
        ism = c.get("Ism", "Noma'lum")
        hudud = c.get("Viloyat_Tuman", "-")
        soni = c.get("soni_int", 0)
        days = c.get("days_waiting", 0)

        if c["is_emergency"]:
            btn_text = f"🚨 {days} KUN KUTYAPTI! #{cid} {ism} | {hudud} | {soni} ta"
        elif c["is_today_plan"]:
            btn_text = f"🔥 BUGUN: #{cid} {ism} | {hudud} | {soni} ta"
        elif c["Status"] in ("Zaklad_olindi", "Yuklandi"):
            btn_text = f"🟢 #{cid} {ism} | {hudud} | {soni} ta"
        else:
            btn_text = f"⚪ #{cid} {ism} | {hudud} | {soni} ta"

        buttons.append(
            [InlineKeyboardButton(text=btn_text[:60], callback_data=f"card:{cid}:{mode}:{page}")]
        )

    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"list:{mode}:{page - 1}")
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"list:{mode}:{page + 1}")
        )
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="menu:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ==========================================
# MIJOZ KARTOCHKASI
# ==========================================
async def render_client_card(
    client_id: int, back_mode: str = "all", back_page: int = 0, extra_banner: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    clients = await db.get_all_clients()
    settings = await db.get_settings()

    client = next((c for c in clients if c["id_int"] == client_id), None)
    if not client:
        return "❌ Mijoz topilmadi.", InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="menu:home")]]
        )

    # Kechikish holati
    days = client["days_waiting"]
    if client["is_emergency"]:
        delay_line = f"⚠️ <b>{days} kundan beri aloqaga chiqilmagan!</b>"
    elif days == 0:
        delay_line = "✅ Bugun aloqa qilingan / qo‘shilgan"
    else:
        delay_line = f"🕒 Oxirgi aloqaga {days} kun bo‘ldi"

    # Va'dalar cheklisti
    vadalar = client.get("vadalar_list", [])
    if vadalar:
        vada_lines = []
        for v in vadalar:
            mark = "✅" if v.get("done") else "⬜"
            vada_lines.append(f"  {mark} {v.get('text', '')}")
        vada_block = "\n".join(vada_lines)
    else:
        vada_block = "  <i>Hozircha va’dalar kiritilmagan</i>"

    # 4 bosqichli qo'ng'iroq tarixi
    call_lines = []
    for idx in range(1, 5):
        note = str(client.get(f"Qongiroq_{idx}", "")).strip()
        if note:
            call_lines.append(f"  ✅ <b>{idx}-qo‘ng‘iroq:</b> {note}")
        else:
            call_lines.append(f"  ⬜ <b>{idx}-qo‘ng‘iroq:</b> <i>kutilmoqda</i>")
    calls_block = "\n".join(call_lines)

    # Shpargalka
    next_call_num, next_call_plan = get_next_call_info(client, settings)

    # Keyingi shoshilinch mijozni topish
    emergencies = [c for c in clients if c["is_emergency"] and c["id_int"] != client_id]
    next_em_id = emergencies[0]["id_int"] if emergencies else None

    tg_user_display = f"@{client['TG_Username']}" if client.get("TG_Username") else "yo‘q"
    paket_info = client.get("Paket_Ogit_Info") or "Belgilanmagan"
    next_rem = client.get("Keyingi_Qongiroq_Vaqti") or "Belgilanmagan"

    banner_txt = f"{extra_banner}\n━━━━━━━━━━━━━━━━━━━━\n" if extra_banner else ""

    text = (
        f"{banner_txt}"
        f"👤 <b>MIJOZ #{client['id_int']}: {client.get('Ism')}</b>\n"
        f"📞 Tel: <code>{client.get('Telefon') or 'yo‘q'}</code> | TG: {tg_user_display}\n"
        f"📍 Hudud: <b>{client.get('Viloyat_Tuman') or '-'}</b> | Holat: <b>{client.get('Status')}</b>\n"
        f"{delay_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌱 <b>BUYURTMA VA MOLIYA:</b>\n"
        f"• Nav: <b>{client.get('Nav') or 'Atlant'}</b> | Soni: <b>{fmt_money(client['soni_int'])} ta</b>\n"
        f"• Narx (1 dona): <b>{fmt_money(client['narx_int'])} so‘m</b>\n"
        f"• Jami summa: <b>{fmt_money(client['jami_int'])} so‘m</b>\n"
        f"• 💰 Zaklad: <b>{fmt_money(client['zaklad_int'])} so‘m</b> | Qoldiq: <b>{fmt_money(client['qoldiq_int'])} so‘m</b>\n"
        f"• 📦 Paket/O‘g‘it: {paket_info}\n"
        f"• ⏰ Keyingi aloqa: <code>{next_rem}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🤝 <b>BERILGAN VA’DALAR:</b>\n{vada_block}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📞 <b>QO‘NG‘IROQ TARIXI:</b>\n{calls_block}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>{next_call_num}-QO‘NG‘IROQ SHPARGALKASI:</b>\n"
        f"<i>{next_call_plan}</i>"
    )

    tg_url = build_tg_url(client)
    kb_rows = [
        [
            InlineKeyboardButton(text="✈️ Telegramda yozish", url=tg_url),
            InlineKeyboardButton(
                text="🎙 Ovozli yangilash",
                callback_data=f"act:voice:{client_id}:{back_mode}:{back_page}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📞 1-4 Qo‘ng‘iroq yozish",
                callback_data=f"act:calls:{client_id}:{back_mode}:{back_page}",
            ),
            InlineKeyboardButton(
                text="💰 Narx / Zaklad",
                callback_data=f"act:money:{client_id}:{back_mode}:{back_page}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤝 Va’dalar cheklisti",
                callback_data=f"act:promises:{client_id}:{back_mode}:{back_page}",
            ),
            InlineKeyboardButton(
                text="📦 Paket / O‘g‘it",
                callback_data=f"act:paket:{client_id}:{back_mode}:{back_page}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔄 Status",
                callback_data=f"act:status:{client_id}:{back_mode}:{back_page}",
            ),
            InlineKeyboardButton(
                text="⏰ Eslatma qo‘yish",
                callback_data=f"act:remind:{client_id}:{back_mode}:{back_page}",
            ),
        ],
    ]

    bottom_row = []
    if next_em_id:
        bottom_row.append(
            InlineKeyboardButton(
                text="➡️ Keyingi shoshilinch mijoz",
                callback_data=f"card:{next_em_id}:emergency:0",
            )
        )
    bottom_row.append(
        InlineKeyboardButton(
            text="⬅️ Ro‘yxatga qaytish",
            callback_data=f"list:{back_mode}:{back_page}",
        )
    )
    kb_rows.append(bottom_row)

    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ==========================================
# /START VA ASOSIY NAVIGATSIYA
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Sizda bu botdan foydalanish huquqi yo‘q.")
        return

    await state.clear()
    text, kb = await render_main_menu()
    sent = await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(panel_msg_id=sent.message_id)


@router.callback_query(F.data == "menu:home")
async def cb_menu_home(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    text, kb = await render_main_menu()
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await state.update_data(panel_msg_id=call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("list:"))
async def cb_list_clients(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    _, mode, page_str = call.data.split(":")
    page = int(page_str)
    text, kb = await render_client_list(mode, page)
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await state.update_data(panel_msg_id=call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("card:"))
async def cb_open_card(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    _, cid_str, mode, page_str = call.data.split(":")
    text, kb = await render_client_card(int(cid_str), mode, int(page_str))
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await state.update_data(panel_msg_id=call.message.message_id)
    await call.answer()


# ==========================================
# YANGI MIJOZ QO'SHISH (SINGLE-MESSAGE FSM)
# ==========================================
@router.callback_query(F.data == "add_client:start")
async def cb_add_client_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AddClientFSM.ism)
    await state.update_data(panel_msg_id=call.message.message_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")]]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        "➕ <b>YANGI MIJOZ QO‘SHISH (1/6)</b>\n\nMijozning <b>ismini</b> yozing:",
        kb,
    )
    await call.answer()


@router.message(AddClientFSM.ism)
async def fsm_add_ism(message: Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_msg_id")
    await state.update_data(Ism=message.text.strip())
    await state.set_state(AddClientFSM.telefon)
    try:
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")]]
    )
    await safe_edit_message(
        bot,
        message.chat.id,
        panel_id,
        f"➕ <b>YANGI MIJOZ (2/6)</b>\nIsm: <b>{message.text.strip()}</b>\n\n📞 Telefon raqamini kiriting (masalan: <code>+998901234567</code>):",
        kb,
    )


@router.message(AddClientFSM.telefon)
async def fsm_add_tel(message: Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_msg_id")
    await state.update_data(Telefon=message.text.strip())
    await state.set_state(AddClientFSM.tg_username)
    try:
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ O‘tkazib yuborish", callback_data="add_client:skip_tg")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")],
        ]
    )
    await safe_edit_message(
        bot,
        message.chat.id,
        panel_id,
        "➕ <b>YANGI MIJOZ (3/6)</b>\n\n✈️ Telegram username kiriting (masalan: <code>@akmal_agro</code>) yoki o‘tkazib yuboring:",
        kb,
    )


@router.callback_query(AddClientFSM.tg_username, F.data == "add_client:skip_tg")
async def cb_skip_tg(call: CallbackQuery, state: FSMContext):
    await state.update_data(TG_Username="")
    await state.set_state(AddClientFSM.viloyat)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")]]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        "➕ <b>YANGI MIJOZ (4/6)</b>\n\n📍 Viloyat va tumanini yozing (masalan: <code>Andijon, Asaka</code>):",
        kb,
    )
    await call.answer()


@router.message(AddClientFSM.tg_username)
async def fsm_add_tg(message: Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_msg_id")
    await state.update_data(TG_Username=message.text.replace("@", "").strip())
    await state.set_state(AddClientFSM.viloyat)
    try:
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")]]
    )
    await safe_edit_message(
        bot,
        message.chat.id,
        panel_id,
        "➕ <b>YANGI MIJOZ (4/6)</b>\n\n📍 Viloyat va tumanini yozing (masalan: <code>Andijon, Asaka</code>):",
        kb,
    )


@router.message(AddClientFSM.viloyat)
async def fsm_add_viloyat(message: Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_msg_id")
    await state.update_data(Viloyat_Tuman=message.text.strip())
    await state.set_state(AddClientFSM.nav)
    try:
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌱 Atlant", callback_data="add_nav:Atlant"),
                InlineKeyboardButton(text="🍓 Maravilla", callback_data="add_nav:Maravilla"),
            ],
            [InlineKeyboardButton(text="🌱🍓 Atlant + Maravilla", callback_data="add_nav:Atlant + Maravilla")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")],
        ]
    )
    await safe_edit_message(
        bot,
        message.chat.id,
        panel_id,
        "➕ <b>YANGI MIJOZ (5/6)</b>\n\nQaysi malina naviga qiziqyapti?",
        kb,
    )


@router.callback_query(AddClientFSM.nav, F.data.startswith("add_nav:"))
async def cb_add_nav(call: CallbackQuery, state: FSMContext):
    nav = call.data.split(":", 1)[1]
    await state.update_data(Nav=nav)
    await state.set_state(AddClientFSM.soni)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="menu:home")]]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"➕ <b>YANGI MIJOZ (6/6)</b>\nNav: <b>{nav}</b>\n\n🔢 Nechta ko‘chat olmoqchi? (Faqat son yozing, masalan: <code>1000</code>):",
        kb,
    )
    await call.answer()


@router.message(AddClientFSM.soni)
async def fsm_add_soni(message: Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_msg_id")
    soni = to_int(message.text)
    try:
        await message.delete()
    except Exception:
        pass

    data["Soni"] = soni
    new_id = await db.add_client(data)
    await state.clear()

    text, kb = await render_client_card(
        new_id, "all", 0, extra_banner=f"✅ <b>Yangi mijoz #{new_id} muvaffaqiyatli qo‘shildi!</b>"
    )
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# 1. OVOZLI YANGILASH (GROQ VOICE-TO-CRM)
# ==========================================
@router.callback_query(F.data.startswith("act:voice:"))
async def cb_act_voice(call: CallbackQuery, state: FSMContext):
    _, _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    await state.set_state(EditClientFSM.waiting_voice)
    await state.update_data(
        client_id=cid,
        back_mode=mode,
        back_page=int(page_str),
        panel_msg_id=call.message.message_id,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Bekor qilish", callback_data=f"card:{cid}:{mode}:{page_str}"
                )
            ]
        ]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"🎙 <b>MIJOZ #{cid} — OVOZLI YANGILASH</b>\n\n"
        "Mijoz bilan suhbat natijasini <b>ovozli xabar (Voice)</b> qilib yuboring.\n"
        "<i>Masalan: \"Akmal aka bilan gaplashdim, 1000 ta Atlantni 22 mingdan kelishdik, "
        "ertaga 3 million zaklad tashlaydi, gumus paketini ham qo‘shdik, video tashlab berishga va’da berdim.\"</i>",
        kb,
    )
    await call.answer()


@router.message(EditClientFSM.waiting_voice, F.voice | F.audio)
async def fsm_receive_voice(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data["client_id"]
    mode = data["back_mode"]
    page = data["back_page"]
    panel_id = data["panel_msg_id"]

    await safe_edit_message(
        bot,
        message.chat.id,
        panel_id,
        "⏳ <b>Groq AI ovozni tahlil qilmoqda va Google Sheets’ga yozmoqda...</b>",
        None,
    )

    client = await db.get_client_by_id(cid)
    if not client:
        await state.clear()
        return

    file_obj = message.voice or message.audio
    tg_file = await bot.get_file(file_obj.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await bot.download_file(tg_file.file_path, destination=tmp_path)
        ai_res = await voice_ai.process_voice(tmp_path, client)
    except Exception as e:
        logger.error(f"Voice AI xatosi: {e}")
        try:
            await message.delete()
        except Exception:
            pass
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Kartochkaga qaytish", callback_data=f"card:{cid}:{mode}:{page}")]
            ]
        )
        await safe_edit_message(
            bot,
            message.chat.id,
            panel_id,
            f"❌ <b>Ovozni tahlil qilishda xatolik:</b>\n<code>{e}</code>",
            kb,
        )
        await state.clear()
        return
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    try:
        await message.delete()
    except Exception:
        pass

    # Ma'lumotlarni yangilash
    updates = {"Oxirgi_Aloqa_Sana": today_str()}

    summary = ai_res.get("call_summary") or ai_res.get("transcript", "")
    # Bo'sh bo'lgan birinchi qo'ng'iroq katagiga yozamiz (yoki 4-ga qo'shamiz)
    placed = False
    for idx in range(1, 5):
        if not str(client.get(f"Qongiroq_{idx}", "")).strip():
            updates[f"Qongiroq_{idx}"] = f"[{today_str()}] {summary}"
            placed = True
            break
    if not placed:
        prev = client.get("Qongiroq_4", "")
        updates["Qongiroq_4"] = f"{prev} | [{today_str()}] {summary}"

    if ai_res.get("nav"):
        updates["Nav"] = ai_res["nav"]
    if ai_res.get("soni") is not None:
        updates["Soni"] = to_int(ai_res["soni"])
    if ai_res.get("narx_1_dona") is not None:
        updates["Narx_1_Dona"] = to_int(ai_res["narx_1_dona"])
    if ai_res.get("zaklad_summa") is not None:
        updates["Zaklad_Summa"] = to_int(ai_res["zaklad_summa"])
    if ai_res.get("paket_ogit_info"):
        updates["Paket_Ogit_Info"] = ai_res["paket_ogit_info"]
    if ai_res.get("status"):
        updates["Status"] = ai_res["status"]
    if ai_res.get("keyingi_qongiroq_vaqti"):
        updates["Keyingi_Qongiroq_Vaqti"] = ai_res["keyingi_qongiroq_vaqti"]

    # Yangi va'dalarni mavjud cheklistga qo'shish
    vadalar = client.get("vadalar_list", [])
    for p_text in ai_res.get("yangi_vadalar", []):
        if p_text and str(p_text).strip():
            vadalar.append({"text": str(p_text).strip(), "done": False})
    updates["Vadalar_JSON"] = vadalar

    await db.update_client(cid, updates)
    await state.clear()

    banner = f"🎙 <b>AI Xulosa saqlandi:</b> <i>{summary}</i>"
    text, kb = await render_client_card(cid, mode, page, extra_banner=banner)
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# 2. 1-4 QO'NG'IROQ YOZISH
# ==========================================
@router.callback_query(F.data.startswith("act:calls:"))
async def cb_act_calls(call: CallbackQuery, state: FSMContext):
    _, _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    client = await db.get_client_by_id(cid)
    settings = await db.get_settings()
    if not client:
        return

    kb_rows = []
    for idx in range(1, 5):
        status_icon = "✅" if str(client.get(f"Qongiroq_{idx}", "")).strip() else "➕"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon} {idx}-qo‘ng‘iroq natijasini yozish",
                    callback_data=f"call_slot:{idx}:{cid}:{mode}:{page_str}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"card:{cid}:{mode}:{page_str}")]
    )

    next_num, next_plan = get_next_call_info(client, settings)
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"📞 <b>MIJOZ #{cid} — QO‘NG‘IROQ BOSQICHLARI</b>\n\n"
        f"💡 <b>Tavsiya ({next_num}-qo‘ng‘iroq):</b>\n<i>{next_plan}</i>\n\n"
        "Qaysi qo‘ng‘iroq natijasini kiritasiz?",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("call_slot:"))
async def cb_call_slot(call: CallbackQuery, state: FSMContext):
    _, slot_str, cid_str, mode, page_str = call.data.split(":")
    slot = int(slot_str)
    cid = int(cid_str)
    settings = await db.get_settings()
    plan = settings.get(f"QONGIROQ_{slot}_REJA", "")

    await state.set_state(EditClientFSM.waiting_call_note)
    await state.update_data(
        client_id=cid,
        call_slot=slot,
        back_mode=mode,
        back_page=int(page_str),
        panel_msg_id=call.message.message_id,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"card:{cid}:{mode}:{page_str}")]
        ]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"✍️ <b>{slot}-QO‘NG‘IROQ NATIJASINI YOZING</b>\n\n"
        f"💡 <i>Reja: {plan}</i>\n\n"
        "Suhbat xulosasini matn ko‘rinishida yuboring:",
        kb,
    )
    await call.answer()


@router.message(EditClientFSM.waiting_call_note)
async def fsm_save_call_note(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data["client_id"]
    slot = data["call_slot"]
    mode = data["back_mode"]
    page = data["back_page"]
    panel_id = data["panel_msg_id"]

    note = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    updates = {
        f"Qongiroq_{slot}": f"[{today_str()}] {note}",
        "Oxirgi_Aloqa_Sana": today_str(),
    }
    if slot == 1:
        updates["Status"] = "Muzokara"

    await db.update_client(cid, updates)
    await state.clear()

    text, kb = await render_client_card(
        cid, mode, page, extra_banner=f"✅ <b>{slot}-qo‘ng‘iroq tarixi saqlandi!</b>"
    )
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# 3. NARX / ZAKLAD / SONI YANGILASH
# ==========================================
@router.callback_query(F.data.startswith("act:money:"))
async def cb_act_money(call: CallbackQuery, state: FSMContext):
    _, _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    client = await db.get_client_by_id(cid)
    if not client:
        return

    await state.set_state(EditClientFSM.waiting_price_zaklad)
    await state.update_data(
        client_id=cid,
        back_mode=mode,
        back_page=int(page_str),
        panel_msg_id=call.message.message_id,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"card:{cid}:{mode}:{page_str}")]
        ]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"💰 <b>MOLIYA VA BUYURTMA (MIJOZ #{cid})</b>\n"
        f"Joriy: <b>{client['soni_int']} ta</b> × <b>{fmt_money(client['narx_int'])} so‘m</b> | "
        f"Zaklad: <b>{fmt_money(client['zaklad_int'])} so‘m</b>\n\n"
        "Quyidagi formatlardan birida yozing:\n"
        "• <code>narx 22000</code> — 1 dona ko‘chat narxini belgilash\n"
        "• <code>zaklad 3000000</code> — tushgan zaklad summasini kiritish\n"
        "• <code>soni 1500</code> — ko‘chat sonini o‘zgartirish\n"
        "• Yoki hammasi birga: <code>1000 22000 5000000</code> (soni, narx, zaklad)",
        kb,
    )
    await call.answer()


@router.message(EditClientFSM.waiting_price_zaklad)
async def fsm_save_money(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data["client_id"]
    mode = data["back_mode"]
    page = data["back_page"]
    panel_id = data["panel_msg_id"]

    raw = message.text.lower().strip()
    try:
        await message.delete()
    except Exception:
        pass

    updates = {"Oxirgi_Aloqa_Sana": today_str()}
    nums = [int(x) for x in re.findall(r"\d+", raw.replace(" ", ""))] if raw.replace(" ", "").isdigit() else []

    if len(nums) == 3:
        updates["Soni"] = nums[0]
        updates["Narx_1_Dona"] = nums[1]
        updates["Zaklad_Summa"] = nums[2]
    else:
        parts = raw.split()
        if len(parts) >= 2 and parts[0] in ("narx", "zaklad", "soni"):
            val = to_int("".join(parts[1:]))
            if parts[0] == "narx":
                updates["Narx_1_Dona"] = val
            elif parts[0] == "zaklad":
                updates["Zaklad_Summa"] = val
            elif parts[0] == "soni":
                updates["Soni"] = val
        else:
            # Agar faqat bitta son yozsa zaklad deb qabul qilamiz
            val = to_int(raw)
            if val > 100000:
                updates["Zaklad_Summa"] = val
            elif val > 0:
                updates["Narx_1_Dona"] = val

    await db.update_client(cid, updates)
    await state.clear()

    text, kb = await render_client_card(
        cid, mode, page, extra_banner="💰 <b>Moliyaviy ma’lumotlar yangilandi!</b>"
    )
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# 4. VA'DALAR CHEKLISTI (INTERAKTIV TOGGLE)
# ==========================================
async def render_promises_menu(cid: int, mode: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    client = await db.get_client_by_id(cid)
    vadalar = client.get("vadalar_list", []) if client else []

    kb_rows = []
    for idx, v in enumerate(vadalar):
        icon = "✅" if v.get("done") else "⬜"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {v.get('text', '')[:45]}",
                    callback_data=f"prom_toggle:{idx}:{cid}:{mode}:{page}",
                )
            ]
        )

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="➕ Yangi va’da qo‘shish",
                callback_data=f"prom_add:{cid}:{mode}:{page}",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Kartochkaga qaytish", callback_data=f"card:{cid}:{mode}:{page}")]
    )

    text = (
        f"🤝 <b>MIJOZ #{cid} — VA’DALAR CHEKLISTI</b>\n\n"
        "Bajarilgan va’da ustiga bosing (holati darhol Google Sheets’da yangilanadi):"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.startswith("act:promises:"))
async def cb_act_promises(call: CallbackQuery):
    _, _, cid_str, mode, page_str = call.data.split(":")
    text, kb = await render_promises_menu(int(cid_str), mode, int(page_str))
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("prom_toggle:"))
async def cb_prom_toggle(call: CallbackQuery):
    _, idx_str, cid_str, mode, page_str = call.data.split(":")
    idx = int(idx_str)
    cid = int(cid_str)
    client = await db.get_client_by_id(cid)
    if not client:
        return
    vadalar = client.get("vadalar_list", [])
    if 0 <= idx < len(vadalar):
        vadalar[idx]["done"] = not vadalar[idx].get("done", False)
        await db.update_client(cid, {"Vadalar_JSON": vadalar})

    text, kb = await render_promises_menu(cid, mode, int(page_str))
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await call.answer("✅ Holat o‘zgartirildi!")


@router.callback_query(F.data.startswith("prom_add:"))
async def cb_prom_add(call: CallbackQuery, state: FSMContext):
    _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    await state.set_state(EditClientFSM.waiting_new_promise)
    await state.update_data(
        client_id=cid,
        back_mode=mode,
        back_page=int(page_str),
        panel_msg_id=call.message.message_id,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"act:promises:{cid}:{mode}:{page_str}")]
        ]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        "✍️ <b>Mijozga berilgan yangi va’dani yozing:</b>\n"
        "<i>Masalan: \"Angren ko‘chatxonasidan video tashlab berish\"</i>",
        kb,
    )
    await call.answer()


@router.message(EditClientFSM.waiting_new_promise)
async def fsm_save_new_promise(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data["client_id"]
    mode = data["back_mode"]
    page = data["back_page"]
    panel_id = data["panel_msg_id"]

    p_text = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    client = await db.get_client_by_id(cid)
    vadalar = client.get("vadalar_list", []) if client else []
    vadalar.append({"text": p_text, "done": False})
    await db.update_client(cid, {"Vadalar_JSON": vadalar})
    await state.clear()

    text, kb = await render_promises_menu(cid, mode, page)
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# 5. PAKET / O'G'IT MA'LUMOTI
# ==========================================
@router.callback_query(F.data.startswith("act:paket:"))
async def cb_act_paket(call: CallbackQuery, state: FSMContext):
    _, _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    await state.set_state(EditClientFSM.waiting_paket_info)
    await state.update_data(
        client_id=cid,
        back_mode=mode,
        back_page=int(page_str),
        panel_msg_id=call.message.message_id,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Go‘ng + Gumus paketi qo‘shildi",
                    callback_data=f"paket_quick:Go'ng + Gumus paketi:{cid}:{mode}:{page_str}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌱 Faqat ko‘chat (paketsiz)",
                    callback_data=f"paket_quick:Faqat ko'chat:{cid}:{mode}:{page_str}",
                )
            ],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"card:{cid}:{mode}:{page_str}")],
        ]
    )
    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"📦 <b>PAKET / O‘G‘IT MA’LUMOTI (MIJOZ #{cid})</b>\n\n"
        "Tayyor tugmani tanlang yoki o‘zingiz matn yozib yuboring:",
        kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("paket_quick:"))
async def cb_paket_quick(call: CallbackQuery, state: FSMContext):
    _, info, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    await db.update_client(cid, {"Paket_Ogit_Info": info, "Oxirgi_Aloqa_Sana": today_str()})
    await state.clear()
    text, kb = await render_client_card(
        cid, mode, int(page_str), extra_banner="📦 <b>Paket/O‘g‘it ma’lumoti yangilandi!</b>"
    )
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await call.answer()


@router.message(EditClientFSM.waiting_paket_info)
async def fsm_save_paket(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data["client_id"]
    mode = data["back_mode"]
    page = data["back_page"]
    panel_id = data["panel_msg_id"]

    info = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    await db.update_client(cid, {"Paket_Ogit_Info": info, "Oxirgi_Aloqa_Sana": today_str()})
    await state.clear()
    text, kb = await render_client_card(
        cid, mode, page, extra_banner="📦 <b>Paket/O‘g‘it ma’lumoti saqlandi!</b>"
    )
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# 6. STATUSNI O'ZGARTIRISH
# ==========================================
@router.callback_query(F.data.startswith("act:status:"))
async def cb_act_status(call: CallbackQuery):
    _, _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    statuses = [
        ("🆕 Yangi", "Yangi"),
        ("💬 Muzokara", "Muzokara"),
        ("💰 Zaklad olindi", "Zaklad_olindi"),
        ("🚚 Yuklandi", "Yuklandi"),
        ("❌ Rad etildi", "Rad_etildi"),
    ]
    kb_rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"set_status:{val}:{cid}:{mode}:{page_str}"
            )
        ]
        for label, val in statuses
    ]
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"card:{cid}:{mode}:{page_str}")]
    )

    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"🔄 <b>MIJOZ #{cid} STATUSINI TANLANG:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("set_status:"))
async def cb_set_status(call: CallbackQuery):
    _, new_st, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    await db.update_client(cid, {"Status": new_st, "Oxirgi_Aloqa_Sana": today_str()})
    text, kb = await render_client_card(
        cid, mode, int(page_str), extra_banner=f"🔄 <b>Status o‘zgartirildi: {new_st}</b>"
    )
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await call.answer()


# ==========================================
# 7. ESLATMA QO'YISH (KEYINGI QO'NG'IROQ VAQTI)
# ==========================================
@router.callback_query(F.data.startswith("act:remind:"))
async def cb_act_remind(call: CallbackQuery, state: FSMContext):
    _, _, cid_str, mode, page_str = call.data.split(":")
    cid = int(cid_str)
    now = datetime.now(TZ)
    in_2h = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    tmrw_10 = (now + timedelta(days=1)).strftime("%Y-%m-%d 10:00")
    tmrw_15 = (now + timedelta(days=1)).strftime("%Y-%m-%d 15:00")

    await state.set_state(EditClientFSM.waiting_reminder_time)
    await state.update_data(
        client_id=cid,
        back_mode=mode,
        back_page=int(page_str),
        panel_msg_id=call.message.message_id,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⏳ 2 soatdan keyin ({in_2h[-5:]})",
                    callback_data=f"rem_quick:{in_2h}:{cid}:{mode}:{page_str}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌅 Ertaga 10:00",
                    callback_data=f"rem_quick:{tmrw_10}:{cid}:{mode}:{page_str}",
                ),
                InlineKeyboardButton(
                    text="☀️ Ertaga 15:00",
                    callback_data=f"rem_quick:{tmrw_15}:{cid}:{mode}:{page_str}",
                ),
            ],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"card:{cid}:{mode}:{page_str}")],
        ]
    )

    await safe_edit_message(
        bot,
        call.message.chat.id,
        call.message.message_id,
        f"⏰ <b>ESLATMA VAQTINI TANLANG (MIJOZ #{cid})</b>\n\n"
        "Tezkor tugmani bosing yoki aniq vaqtni yozing:\n"
        "<i>Format: <code>2026-11-20 14:30</code></i>",
        kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("rem_quick:"))
async def cb_rem_quick(call: CallbackQuery, state: FSMContext):
    # rem_quick:2026-11-19 10:00:cid:mode:page
    parts = call.data.split(":")
    dt_val = f"{parts[1]}:{parts[2]}"
    cid = int(parts[3])
    mode = parts[4]
    page = int(parts[5])

    await db.update_client(cid, {"Keyingi_Qongiroq_Vaqti": dt_val})
    await state.clear()
    text, kb = await render_client_card(
        cid, mode, page, extra_banner=f"⏰ <b>Eslatma belgilandi: {dt_val}</b>"
    )
    await safe_edit_message(bot, call.message.chat.id, call.message.message_id, text, kb)
    await call.answer()


@router.message(EditClientFSM.waiting_reminder_time)
async def fsm_save_reminder(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data["client_id"]
    mode = data["back_mode"]
    page = data["back_page"]
    panel_id = data["panel_msg_id"]

    dt_str = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    await db.update_client(cid, {"Keyingi_Qongiroq_Vaqti": dt_str})
    await state.clear()
    text, kb = await render_client_card(
        cid, mode, page, extra_banner=f"⏰ <b>Eslatma saqlandi: {dt_str}</b>"
    )
    await safe_edit_message(bot, message.chat.id, panel_id, text, kb)


# ==========================================
# AVTOMATIK ESLATMALAR (APSCHEDULER)
# ==========================================
async def daily_morning_briefing():
    if not ALLOWED_ADMIN_IDS:
        return
    try:
        stats = await db.get_dashboard_stats()
        clients = stats["clients"]
        emergencies = [c for c in clients if c["is_emergency"]]
        today_calls = [c for c in clients if c["is_today_plan"] and not c["is_emergency"]]

        lines = [
            "☀️ <b>XAYRLI TONG! KUNLIK JANGOVAR REJA (SALE_P)</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🚨 <b>Emergency (2+ kun kutganlar):</b> {len(emergencies)} ta",
            f"🔥 <b>Bugungi rejali qo‘ng‘iroqlar:</b> {len(today_calls)} ta",
            f"🎯 <b>Oylik progress:</b> {fmt_money(stats['sold_seedlings'])} / {fmt_money(stats['monthly_goal'])} ta",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        if emergencies:
            lines.append("<b>🚨 ENG SHOSHILINCH MIJOZLAR:</b>")
            for c in emergencies[:5]:
                lines.append(
                    f"• #{c['id_int']} <b>{c['Ism']}</b> ({c['Viloyat_Tuman']}) — <i>{c['days_waiting']} kun kutgan!</i>"
                )

        if today_calls:
            lines.append("\n<b>🔥 BUGUNGI REJADAGILAR:</b>")
            for c in today_calls[:5]:
                lines.append(
                    f"• #{c['id_int']} <b>{c['Ism']}</b> ({c['Viloyat_Tuman']}) — {c['soni_int']} ta"
                )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🚨 Shoshilinch qo‘ng‘iroqlarni boshlash ({len(emergencies)})",
                        callback_data="list:emergency:0",
                    )
                ],
                [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="menu:home")],
            ]
        )

        msg_text = "\n".join(lines)
        for admin_id in ALLOWED_ADMIN_IDS:
            try:
                await bot.send_message(admin_id, msg_text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logger.error(f"09:00 xabar xatosi ({admin_id}): {e}")
    except Exception as e:
        logger.error(f"daily_morning_briefing xatosi: {e}")


async def check_15min_reminders():
    if not ALLOWED_ADMIN_IDS:
        return
    try:
        clients = await db.get_all_clients()
        now = datetime.now(TZ)
        window_start = now - timedelta(minutes=15)

        for c in clients:
            if c["Status"] in ("Zaklad_olindi", "Yuklandi", "Rad_etildi"):
                continue
            rem_dt = parse_datetime_safe(c.get("Keyingi_Qongiroq_Vaqti"))
            if rem_dt and (window_start <= rem_dt <= now):
                cid = c["id_int"]
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"👤 Kartochkani ochish (#{cid} {c['Ism']})",
                                callback_data=f"card:{cid}:all:0",
                            )
                        ],
                        [InlineKeyboardButton(text="✈️ Telegramda yozish", url=build_tg_url(c))],
                    ]
                )
                text = (
                    f"⏰ <b>QO‘NG‘IROQ VAQTI BO‘LDI!</b>\n\n"
                    f"👤 Mijoz: <b>#{cid} {c['Ism']}</b> ({c['Viloyat_Tuman']})\n"
                    f"📞 Tel: <code>{c['Telefon']}</code>\n"
                    f"🌱 Buyurtma: <b>{c['Nav']} — {c['soni_int']} ta</b>"
                )
                for admin_id in ALLOWED_ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, text, reply_markup=kb, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Eslatma yuborish xatosi: {e}")
    except Exception as e:
        logger.error(f"check_15min_reminders xatosi: {e}")


# ==========================================
# RENDER.COM KEEP-ALIVE WEB SERVER (/health)
# ==========================================
async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "Sale_P_CRM_Bot",
            "time": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Keep-alive web server {port}-portda ishga tushdi.")


# ==========================================
# MAIN ENTRYPOINT
# ==========================================
async def main():
    logger.info("Google Sheets ulanmoqda...")
    await db.init_db()
    logger.info("MalinachiBaza tayyor!")

    # Render web serverni ishga tushirish
    await start_web_server()

    # Scheduler sozlash (Asia/Tashkent)
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(daily_morning_briefing, "cron", hour=9, minute=0)
    scheduler.add_job(check_15min_reminders, "interval", minutes=15)
    scheduler.start()

    logger.info("Sale_P Telegram Bot ishga tushdi!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())