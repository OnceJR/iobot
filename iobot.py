import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import urllib.parse
import json
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, MessageEntityType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =====================================================================
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s"
)
logger = logging.getLogger("ImperioBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8611966815:AAE2biZEsdWl_r-k4E1EBBT0XOMqIuLEFk0")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net")
PORT = int(os.getenv("PORT", "10000"))

# Lista de IDs con acceso maestro absoluto
OWNER_IDS: Set[int] = {8983189714}

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/|telegram\.me/)', re.IGNORECASE)

# =====================================================================
# MOTOR DE BASE DE DATOS Y CACHÉ
# =====================================================================
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.imperio_bot

groups_col = db.groups
stats_col = db.stats
admins_col = db.admins
warns_col = db.warns
cleanup_queue_col = db.cleanup_queue

# Caché en memoria (TTL) para reducir llamadas a Telegram y Mongo
# Evita rate-limits de Telegram en grupos de alto tráfico
_ADMIN_CACHE: Dict[Tuple[int, int], Tuple[bool, datetime]] = {}
_BLACKLIST_CACHE: Dict[int, Tuple[List[str], datetime]] = {}
CACHE_TTL = timedelta(minutes=5)

async def is_admin(chat_id: int, user_id: int, bot_instance: Bot) -> bool:
    """Verifica permisos de administración con soporte para Owner y caché local."""
    if user_id in OWNER_IDS:
        return True

    now = datetime.now()
    cache_key = (chat_id, user_id)
    if cache_key in _ADMIN_CACHE:
        is_adm, expiry = _ADMIN_CACHE[cache_key]
        if now < expiry:
            return is_adm

    # 1. Chequeo de lista blanca en MongoDB
    group_data = await groups_col.find_one({"_id": chat_id}, {"authorized_users": 1})
    if group_data and user_id in group_data.get("authorized_users", []):
        _ADMIN_CACHE[cache_key] = (True, now + CACHE_TTL)
        return True

    # 2. Consulta a la API de Telegram
    try:
        member = await bot_instance.get_chat_member(chat_id, user_id)
        result = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
        _ADMIN_CACHE[cache_key] = (result, now + CACHE_TTL)
        return result
    except Exception:
        return False

async def get_cached_blacklist(chat_id: int) -> List[str]:
    """Obtiene la lista negra compilada desde caché o base de datos."""
    now = datetime.now()
    if chat_id in _BLACKLIST_CACHE:
        words, expiry = _BLACKLIST_CACHE[chat_id]
        if now < expiry:
            return words

    group_data = await groups_col.find_one({"_id": chat_id}, {"blacklist": 1})
    words = group_data.get("blacklist", []) if group_data else []
    _BLACKLIST_CACHE[chat_id] = (words, now + CACHE_TTL)
    return words

def invalidate_blacklist_cache(chat_id: int):
    _BLACKLIST_CACHE.pop(chat_id, None)

# =====================================================================
# DICCIONARIOS DE PERMISOS
# =====================================================================
PERM_MAPPING = {
    "msg": ("can_send_messages", "Mensajes"),
    "media": ("can_send_photos", "Multimedia"),
    "doc": ("can_send_documents", "Documentos"),
    "voice": ("can_send_voice_notes", "Notas de Voz"),
    "poll": ("can_send_polls", "Encuestas"),
    "web": ("can_add_web_page_previews", "Vista Previa"),
    "info": ("can_change_info", "Info Grupo"),
    "inv": ("can_invite_users", "Invitaciones"),
    "pin": ("can_pin_messages", "Fijar Mensajes")
}

ADMIN_PERMS = {
    "can_delete_messages": "Borrar Mensajes",
    "can_restrict_members": "Sancionar Usuarios",
    "can_promote_members": "Promover Administradores",
    "can_change_info": "Modificar Ajustes",
    "can_invite_users": "Gestionar Enlaces",
    "can_pin_messages": "Fijar Mensajes"
}

# =====================================================================
# INICIALIZACIÓN
# =====================================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

class BotStates(StatesGroup):
    waiting_for_id = State()
    waiting_for_rmid = State()
    waiting_for_badword = State()

# =====================================================================
# COMPONENTES DE INTERFAZ MODERNA (UI/UX TELEGRAM)
# =====================================================================
def get_main_dashboard_kb(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Cerrar Chat", callback_data=f"lock_confirm_{group_id}"),
            InlineKeyboardButton(text="🔓 Abrir Chat", callback_data=f"unlock_confirm_{group_id}")
        ],
        [
            InlineKeyboardButton(text="⚙️ Permisos", callback_data=f"perms_{group_id}"),
            InlineKeyboardButton(text="🔍 Auditoría", callback_data=f"botperms_{group_id}")
        ],
        [
            InlineKeyboardButton(text="🧹 Purga Automática", callback_data=f"cleanmenu_{group_id}"),
            InlineKeyboardButton(text="🚫 Filtro Palabras", callback_data=f"badwords_{group_id}")
        ],
        [
            InlineKeyboardButton(text="👑 Gestión Staff", callback_data=f"staffmenu_{group_id}"),
            InlineKeyboardButton(text="📖 Guía de Mando", callback_data=f"help_{group_id}")
        ]
    ])

def get_back_kb(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Regresar al Panel", callback_data=f"back_{group_id}")]
    ])

def get_permissions_kb(group_id: int, perms: ChatPermissions) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for key, (attr, name) in PERM_MAPPING.items():
        is_active = getattr(perms, attr, False)
        status = "🟢" if is_active else "🔴"
        row.append(InlineKeyboardButton(text=f"{status} {name}", callback_data=f"tp_{group_id}_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Volver", callback_data=f"back_{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# =====================================================================
# MOTOR DE LIMPIEZA ASÍNCRONA ROBUSTA
# =====================================================================
async def execute_cleanup(chat_id: int) -> int:
    """Ejecuta purga de mensajes pendientes controlando límites de API y caducidad."""
    records = await cleanup_queue_col.find({"chat_id": chat_id}).to_list(length=1000)
    if not records:
        await groups_col.update_one(
            {"_id": chat_id},
            {"$set": {"next_cleanup": datetime.now() + timedelta(hours=12)}},
            upsert=True
        )
        return 0

    message_ids = [r["message_id"] for r in records]
    total_purged = 0
    chunk_size = 100

    for i in range(0, len(message_ids), chunk_size):
        chunk = message_ids[i:i + chunk_size]
        try:
            # delete_messages arrojará TelegramBadRequest si hay mensajes de más de 48h
            await bot.delete_messages(chat_id, chunk)
            total_purged += len(chunk)
        except TelegramBadRequest:
            # Fallback seguro: eliminación individual de mensajes permitidos
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id, mid)
                    total_purged += 1
                except Exception:
                    pass
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.warning(f"Error parcial en purga masiva de chat {chat_id}: {e}")
        
        await asyncio.sleep(0.5)

    # Eliminar registros ya procesados de la cola
    await cleanup_queue_col.delete_many({"chat_id": chat_id, "message_id": {"$in": message_ids}})
    await groups_col.update_one(
        {"_id": chat_id},
        {"$set": {"next_cleanup": datetime.now() + timedelta(hours=12)}}
    )
    return total_purged

async def auto_cleanup_worker():
    """Worker en background para ejecutar purgas programadas cada 60 segundos."""
    while True:
        try:
            now = datetime.now()
            cursor = groups_col.find({"next_cleanup": {"$lte": now}})
            async for group in cursor:
                chat_id = group["_id"]
                purged = await execute_cleanup(chat_id)
                if purged > 0:
                    try:
                        notice = await bot.send_message(
                            chat_id,
                            f"🛡️ <b>MANTENIMIENTO DEL SISTEMA</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⚡ <b>Acción:</b> Limpieza Cíclica (12h)\n"
                            f"🗑️ <b>Archivos purgados:</b> <code>{purged}</code>\n"
                            f"<i>Este mensaje se autodestruirá en 45 segundos.</i>"
                        )
                        await asyncio.sleep(45)
                        await notice.delete()
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Error en auto_cleanup_worker: {e}")
        
        await asyncio.sleep(60)

# =====================================================================
# MODERACIÓN RÁPIDA EN GRUPOS
# =====================================================================
@router.message(Command("panel"))
async def link_group_panel(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        return

    if not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    await admins_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {"active_group": message.chat.id, "group_title": message.chat.title}},
        upsert=True
    )
    
    bot_info = await bot.me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚡ Abrir Consola Central", url=f"https://t.me/{bot_info.username}?start=panel")
    ]])
    
    await message.reply(
        f"┌── <b>TERMINAL ADMINISTRATIVO</b>\n"
        f"│ <b>Jurisdicción:</b> <code>{message.chat.title}</code>\n"
        f"│ <b>Operador:</b> <code>{message.from_user.first_name}</code>\n"
        f"└── <b>Estado:</b> <code>SESIÓN SINCRONIZADA</code>",
        reply_markup=kb
    )

@router.message(Command("del"))
async def delete_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id, bot):
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                await message.delete()
            except Exception:
                pass

@router.message(Command("ban"))
async def ban_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target:
        return await message.reply("⚠️ Debe responder al mensaje del usuario que desea expulsar.")

    if await is_admin(message.chat.id, target.id, bot):
        return await message.reply("🛑 No es posible sancionar a otro administrador.")

    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.reply_to_message.delete()
        notice = await message.answer(
            f"🚫 <b>SENTENCIA EJECUTADA</b>\n"
            f"👤 <b>Infractor:</b> <code>{target.first_name}</code> (<code>{target.id}</code>)\n"
            f"⚖️ <b>Sanción:</b> Expulsión permanente (BAN)."
        )
        await message.delete()
        await asyncio.sleep(5)
        await notice.delete()
    except Exception as e:
        logger.error(f"Error en ban_cmd: {e}")

@router.message(Command("unban"))
async def unban_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        parts = message.text.split()
        if len(parts) > 1 and parts[1].isdigit():
            user_id = int(parts[1])

    if not user_id:
        return await message.reply("⚠️ Especifique el ID numérico o responda al usuario a readmitir.")

    try:
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
        notice = await message.answer(f"✅ <b>AMNISTÍA CONCEDIDA:</b> Usuario <code>{user_id}</code> desbloqueado.")
        await message.delete()
        await asyncio.sleep(5)
        await notice.delete()
    except Exception as e:
        await message.reply(f"❌ Error al revocar sanción: {e}")

@router.message(Command("mute"))
async def mute_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    if not message.reply_to_message:
        return await message.reply("⚠️ Responda al usuario que desea silenciar.")

    target = message.reply_to_message.from_user
    if await is_admin(message.chat.id, target.id, bot):
        return await message.reply("🛑 No puede silenciar a un administrador.")

    args = message.text.split()
    duration_minutes = 60
    if len(args) > 1:
        param = args[1].lower()
        if param.endswith("m") and param[:-1].isdigit():
            duration_minutes = int(param[:-1])
        elif param.endswith("h") and param[:-1].isdigit():
            duration_minutes = int(param[:-1]) * 60
        elif param.endswith("d") and param[:-1].isdigit():
            duration_minutes = int(param[:-1]) * 1440
        elif param.isdigit():
            duration_minutes = int(param)

    until = datetime.now() + timedelta(minutes=duration_minutes)
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await message.reply_to_message.delete()
        notice = await message.answer(
            f"🤐 <b>ORDEN DE SILENCIO</b>\n"
            f"👤 <b>Usuario:</b> <code>{target.first_name}</code>\n"
            f"⏱️ <b>Duración:</b> <code>{duration_minutes} min</code>"
        )
        await message.delete()
        await asyncio.sleep(5)
        await notice.delete()
    except Exception as e:
        logger.error(f"Error en mute_cmd: {e}")

@router.message(Command("unmute"))
async def unmute_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    if not message.reply_to_message:
        return await message.reply("⚠️ Responda al usuario que desea reactivar.")

    target = message.reply_to_message.from_user
    try:
        # Restablece permisos completos según ChatPermissions modernos
        await bot.restrict_chat_member(
            message.chat.id,
            target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_documents=True,
                can_send_audios=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        notice = await message.answer(f"🔊 <b>VOZ RESTABLECIDA:</b> <code>{target.first_name}</code> puede interactuar.")
        await message.delete()
        await asyncio.sleep(5)
        await notice.delete()
    except Exception as e:
        await message.reply(f"❌ Error al levantar silencio: {e}")

@router.message(Command("warn"))
async def warn_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    if not message.reply_to_message:
        return await message.reply("⚠️ Responda al usuario para aplicar una advertencia.")

    target = message.reply_to_message.from_user
    if await is_admin(message.chat.id, target.id, bot):
        return await message.reply("🛑 No puede sancionar a un administrador.")

    res = await warns_col.find_one_and_update(
        {"chat_id": message.chat.id, "user_id": target.id},
        {"$inc": {"count": 1}},
        upsert=True,
        return_document=True
    )
    warns = res.get("count", 1)

    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception:
        pass

    if warns >= 3:
        try:
            await bot.ban_chat_member(message.chat.id, target.id)
            await warns_col.delete_one({"chat_id": message.chat.id, "user_id": target.id})
            notice = await message.answer(
                f"🚨 <b>LÍMITE DE ADVERTENCIAS (3/3)</b>\n"
                f"👤 <code>{target.first_name}</code> acumuló 3 faltas y fue expulsado definitivamente."
            )
        except Exception as e:
            notice = await message.answer(f"❌ Error al sancionar tras 3 warns: {e}")
    else:
        notice = await message.answer(
            f"⚠️ <b>ADVERTENCIA APLICADA</b>\n"
            f"👤 <b>Usuario:</b> <code>{target.first_name}</code>\n"
            f"📊 <b>Estado:</b> <code>[{warns}/3]</code> advertencias registradas."
        )

    await asyncio.sleep(6)
    try:
        await notice.delete()
    except Exception:
        pass

@router.message(Command("unwarn"))
async def unwarn_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    if not message.reply_to_message:
        return await message.reply("⚠️ Responda al usuario para perdonar sus faltas.")

    target = message.reply_to_message.from_user
    await warns_col.delete_one({"chat_id": message.chat.id, "user_id": target.id})
    notice = await message.answer(f"🕊️ <b>HISTORIAL RESTABLECIDO:</b> <code>{target.first_name}</code> está libre de faltas.")
    await message.delete()
    await asyncio.sleep(5)
    await notice.delete()

@router.message(Command("delall"))
async def delall_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"] or not await is_admin(message.chat.id, message.from_user.id, bot):
        return

    if not message.reply_to_message:
        return await message.reply("⚠️ Responda al usuario cuyo historial desea purgar.")

    target = message.reply_to_message.from_user
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Purgar Mensajes Recientes", callback_data=f"purge_msgs_{target.id}")],
        [InlineKeyboardButton(text="🔨 Purgar y Expulsar Permanentemente", callback_data=f"purge_ban_{target.id}")],
        [InlineKeyboardButton(text="❌ Cancelar", callback_data=f"purge_cancel_{target.id}")]
    ])
    await message.reply(
        f"┌── <b>PROTOCOLO DE PURGA</b>\n"
        f"│ <b>Objetivo:</b> <code>{target.first_name}</code>\n"
        f"│ <b>ID:</b> <code>{target.id}</code>\n"
        f"└── <i>Seleccione el nivel de erradicación:</i>",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("purge_"))
async def process_purge_action(callback: CallbackQuery):
    action, _, target_id_str = callback.data.partition("_")[2].partition("_")
    target_id = int(target_id_str)
    chat_id = callback.message.chat.id

    if not await is_admin(chat_id, callback.from_user.id, bot):
        return await callback.answer("🛑 Permiso denegado.", show_alert=True)

    if action == "cancel":
        return await callback.message.delete()

    try:
        # revoke_messages=True limpia el historial reciente del infractor en el chat
        await bot.ban_chat_member(chat_id, target_id, revoke_messages=True)
        if action == "msgs":
            await bot.unban_chat_member(chat_id, target_id)
            await callback.message.edit_text("🧹 <b>Historial de mensajes purgado con éxito.</b>")
        else:
            await callback.message.edit_text("⚡ <b>Purga total completada:</b> Historial eliminado y usuario expulsado.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Fallo en la purga: {e}")

@router.message(Command("pin"))
async def pin_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id, bot):
        if message.reply_to_message:
            try:
                await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
                await message.delete()
            except Exception:
                pass

@router.message(F.text.startswith(("/s ", ".s ")) | F.caption.startswith(("/s ", ".s ")))
async def ghost_broadcast_cmd(message: Message):
    """Mensaje eco institucional del bot."""
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id, bot):
        try:
            if message.text:
                content = message.text[3:].strip()
                await message.answer(content)
            elif message.caption:
                caption = message.caption[3:].strip()
                await message.copy_to(chat_id=message.chat.id, caption=caption)
            await message.delete()
        except Exception:
            pass

# =====================================================================
# ESTADÍSTICAS Y GRÁFICO PROFESIONAL
# =====================================================================
@router.message(Command("aportes"))
async def user_stats_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        return

    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    current_week = datetime.now().strftime("%Y-W%V")
    
    stat = await stats_col.find_one({"chat_id": message.chat.id, "user_id": target.id, "week": current_week})
    count = stat.get("count", 0) if stat else 0
    
    await message.reply(
        f"┌── <b>MÉTRICAS DE APORTES</b>\n"
        f"│ 👤 <b>Colaborador:</b> <code>{target.first_name}</code>\n"
        f"│ 📅 <b>Semana:</b> <code>{current_week}</code>\n"
        f"└── 📦 <b>Envíos multimedia:</b> <code>{count}</code>"
    )

@router.message(Command("topaportes"))
async def top_stats_cmd(message: Message):
    current_week = datetime.now().strftime("%Y-W%V")
    cursor = stats_col.find({"chat_id": message.chat.id, "week": current_week}).sort("count", -1).limit(10)
    top_users = await cursor.to_list(length=10)

    if not top_users:
        return await message.reply("📉 <b>Sin actividad:</b> No hay aportes registrados durante esta semana.")

    text = (
        f"🏛️ <b>CUADRO DE HONOR SEMANAL</b>\n"
        f"<i>Semana de corte: {datetime.now().strftime('%V / %Y')}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    labels, data_points = [], []
    for idx, user in enumerate(top_users, 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"<b>{idx}.</b>"
        u_name = user.get("name", "Anónimo")
        u_count = user.get("count", 0)
        text += f"{medal} <code>{u_name[:14]:<14}</code> ➜ <b>{u_count}</b> aportes\n"
        labels.append(u_name[:10])
        data_points.append(u_count)

    text += "\n<i>El contenido multimedia es depurado cíclicamente cada 12 horas.</i>"

    # QuickChart Engine con estética Dark Slate / Gold
    chart_config = {
        "type": "horizontalBar",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Aportes",
                "data": data_points,
                "backgroundColor": "rgba(220, 38, 38, 0.8)",
                "borderColor": "rgba(234, 179, 8, 1)",
                "borderWidth": 1.5,
                "borderRadius": 4
            }]
        },
        "options": {
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": "Líderes de Contenido Semanal",
                "fontColor": "#EAB308",
                "fontSize": 15
            },
            "scales": {
                "xAxes": [{"ticks": {"beginAtZero": True, "fontColor": "#9CA3AF"}}],
                "yAxes": [{"ticks": {"fontColor": "#F3F4F6", "fontSize": 11}}]
            }
        }
    }

    url = f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=650&h=350&bkg=rgb(17,24,39)"
    try:
        await bot.send_photo(chat_id=message.chat.id, photo=url, caption=text)
    except Exception:
        await message.reply(text)

@router.message(Command("leyes", "reglas"))
async def rules_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        return

    text = (
        "🏛️ <b>CÓDIGO DE NORMAS VIGENTES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1.</b> Cero enlaces, spam o publicidad no autorizada.\n"
        "<b>2.</b> Respeto incondicional; tolerancia cero a acoso o disputas.\n"
        "<b>3.</b> Prohibido material explícito o contenido sensible no solicitado.\n"
        "<b>4.</b> Comercio de archivos o servicios exclusivamente bajo permiso de administración.\n"
        "<b>5.</b> Idioma de comunicación exclusivo: <b>Español</b>.\n\n"
        "<i>⏳ Este comunicado se autodestruirá automáticamente en 30 segundos.</i>"
    )
    try:
        reply_msg = await message.reply(text)
        await asyncio.sleep(30)
        await reply_msg.delete()
        await message.delete()
    except Exception:
        pass

# =====================================================================
# CONSOLA DE ADMINISTRACIÓN PRIVADA (DASHBOARD)
# =====================================================================
@router.message(CommandStart())
async def start_private_panel(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return

    await state.clear()
    admin_data = await admins_col.find_one({"_id": message.from_user.id})
    group_id = admin_data.get("active_group") if admin_data else None

    if message.from_user.id not in OWNER_IDS and not group_id:
        return await message.answer("🛑 <b>ACCESO DENEGADO:</b> No tiene autorización para este panel de control.")

    if not group_id:
        return await message.answer(
            "⚠️ <b>SIN GRUPO VINCULADO</b>\n"
            "Ejecute el comando <code>/panel</code> en el grupo que desea administrar."
        )

    try:
        chat = await bot.get_chat(group_id)
        text = (
            f"┌── <b>CENTRO DE CONTROL SUPREMO</b>\n"
            f"│ 📍 <b>Jurisdicción:</b> <code>{chat.title}</code>\n"
            f"│ 🆔 <b>ID de Grupo:</b> <code>{group_id}</code>\n"
            f"│ 🛡️ <b>Operador:</b> <code>{message.from_user.first_name}</code>\n"
            f"└── <b>Conexión:</b> <code>ACTIVA (TLS 1.3)</code>\n\n"
            f"<i>Seleccione el módulo que desea gestionar:</i>"
        )
        await message.answer(text, reply_markup=get_main_dashboard_kb(group_id))
    except Exception as e:
        await message.answer(f"❌ Error al conectar con el grupo vinculado: {e}")

@router.callback_query(F.data.startswith("back_"))
async def back_to_dashboard(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group_id = int(callback.data.split("_")[1])
    try:
        chat = await bot.get_chat(group_id)
        text = (
            f"┌── <b>CENTRO DE CONTROL SUPREMO</b>\n"
            f"│ 📍 <b>Jurisdicción:</b> <code>{chat.title}</code>\n"
            f"│ 🆔 <b>ID de Grupo:</b> <code>{group_id}</code>\n"
            f"└── <i>Panel listo para operar:</i>"
        )
        await callback.message.edit_text(text, reply_markup=get_main_dashboard_kb(group_id))
    except Exception:
        await callback.answer("Error cargando el menú principal.", show_alert=True)

# --- CIERRE Y APERTURA CON CONFIRMACIÓN RÁPIDA (UX Sin Claves Expuestas) ---
@router.callback_query(F.data.startswith("lock_confirm_"))
async def lock_confirm_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Confirmar Cierre Inmediato", callback_data=f"execute_lock_{group_id}")],
        [InlineKeyboardButton(text="◀️ Cancelar", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(
        "🔒 <b>MODO ESTRICTO: CIERRE DE CHAT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "¿Confirmas que deseas bloquear la escritura para todos los miembros estándar?",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("execute_lock_"))
async def execute_lock_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(can_send_messages=False))
        await callback.answer("🔒 Grupo bloqueado exitosamente.", show_alert=False)
        await callback.message.edit_text(
            "✅ <b>MODO ESTRICTO ACTIVADO:</b> El chat ha sido cerrado para los miembros.",
            reply_markup=get_back_kb(group_id)
        )
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)

@router.callback_query(F.data.startswith("unlock_confirm_"))
async def unlock_confirm_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Confirmar Apertura", callback_data=f"execute_unlock_{group_id}")],
        [InlineKeyboardButton(text="◀️ Cancelar", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(
        "🔓 <b>MODO LIBRE: APERTURA DE CHAT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "¿Confirmas que deseas restaurar la capacidad de enviar mensajes a todos los usuarios?",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("execute_unlock_"))
async def execute_unlock_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    try:
        await bot.set_chat_permissions(
            group_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_documents=True,
                can_send_audios=True,
                can_send_voice_notes=True,
                can_send_other_messages=True
            )
        )
        await callback.answer("🔓 Grupo abierto al público.", show_alert=False)
        await callback.message.edit_text(
            "✅ <b>MODO LIBRE ACTIVADO:</b> El chat se encuentra abierto.",
            reply_markup=get_back_kb(group_id)
        )
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)

# --- GESTIÓN DE PERMISOS DINÁMICOS ---
@router.callback_query(F.data.startswith("perms_"))
async def show_perms_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    chat = await bot.get_chat(group_id)
    perms = chat.permissions or ChatPermissions()
    text = (
        "⚙️ <b>MATRIZ DE PERMISOS DEL GRUPO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Toque cualquier casilla para alternar el permiso en tiempo real:"
    )
    await callback.message.edit_text(text, reply_markup=get_permissions_kb(group_id, perms))

@router.callback_query(F.data.startswith("tp_"))
async def toggle_perm_cb(callback: CallbackQuery):
    _, group_id_str, key = callback.data.split("_", 2)
    group_id = int(group_id_str)
    
    try:
        chat = await bot.get_chat(group_id)
        cur = chat.permissions or ChatPermissions()
        p_dict = cur.model_dump()
        
        # Invertir el booleano
        target_attr = PERM_MAPPING[key][0]
        p_dict[target_attr] = not p_dict.get(target_attr, False)
        
        new_perms = ChatPermissions(**p_dict)
        await bot.set_chat_permissions(group_id, new_perms)
        await callback.answer("⚡ Permiso sincronizado.")
        await callback.message.edit_reply_markup(reply_markup=get_permissions_kb(group_id, new_perms))
    except Exception as e:
        await callback.answer(f"Fallo al actualizar permisos: {e}", show_alert=True)

# --- AUDITORÍA DE PRIVILEGIOS DEL BOT ---
@router.callback_query(F.data.startswith("botperms_"))
async def show_bot_perms_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        me = await bot.me()
        member = await bot.get_chat_member(group_id, me.id)
        text = "🤖 <b>AUDITORÍA DE CAPACIDADES DEL BOT</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for attr, label in ADMIN_PERMS.items():
            status = "🟢" if getattr(member, attr, False) else "🔴"
            text += f"{status} <b>{label}</b>\n"
        await callback.message.edit_text(text, reply_markup=get_back_kb(group_id))
    except Exception as e:
        await callback.answer(f"Error consultando bot: {e}", show_alert=True)

# --- MÓDULO DE STAFF ---
@router.callback_query(F.data.startswith("staffmenu_"))
async def staff_menu_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Añadir Staff", callback_data=f"addid_{group_id}"),
            InlineKeyboardButton(text="➖ Remover Staff", callback_data=f"rmid_{group_id}")
        ],
        [InlineKeyboardButton(text="👑 Lista de Staff Autorizado", callback_data=f"viewstaff_{group_id}")],
        [InlineKeyboardButton(text="◀️ Volver al Panel", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text("👥 <b>GESTIÓN DE STAFF INTERNO</b>\nElija una operación:", reply_markup=kb)

@router.callback_query(F.data.startswith("addid_"))
async def add_staff_start(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_id)
    await state.update_data(group_id=group_id, msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "✍️ <b>Envía el Telegram User ID del nuevo operador.</b>\n"
        "<i>Obtendrá acceso a funciones administrativas del bot.</i>",
        reply_markup=get_back_kb(group_id)
    )

@router.message(BotStates.waiting_for_id)
async def add_staff_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id, msg_id = data["group_id"], data["msg_id"]
    await message.delete()

    if not message.text.strip().isdigit():
        return

    new_id = int(message.text.strip())
    try:
        user_info = await bot.get_chat(new_id)
        name = user_info.first_name or "Desconocido"
    except Exception:
        name = "Operador"

    await groups_col.update_one(
        {"_id": group_id},
        {
            "$addToSet": {"authorized_users": new_id},
            "$set": {f"staff_details.{new_id}": {"name": name, "date": datetime.now().strftime("%d/%m/%Y")}}
        },
        upsert=True
    )
    _ADMIN_CACHE.pop((group_id, new_id), None)
    await state.clear()
    
    await bot.edit_message_text(
        f"✅ <b>Personal Registrado:</b>\n<code>{name}</code> (<code>{new_id}</code>) fue agregado al Staff.",
        chat_id=message.chat.id,
        message_id=msg_id,
        reply_markup=get_main_dashboard_kb(group_id)
    )

@router.callback_query(F.data.startswith("rmid_"))
async def remove_staff_start(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_rmid)
    await state.update_data(group_id=group_id, msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "✍️ <b>Envía el ID numérico del usuario a revocar:</b>",
        reply_markup=get_back_kb(group_id)
    )

@router.message(BotStates.waiting_for_rmid)
async def remove_staff_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id, msg_id = data["group_id"], data["msg_id"]
    await message.delete()

    if not message.text.strip().isdigit():
        return

    target_id = int(message.text.strip())
    await groups_col.update_one(
        {"_id": group_id},
        {
            "$pull": {"authorized_users": target_id},
            "$unset": {f"staff_details.{target_id}": ""}
        }
    )
    _ADMIN_CACHE.pop((group_id, target_id), None)
    await state.clear()

    await bot.edit_message_text(
        f"🗑️ <b>Privilegios Revocados:</b>\nID <code>{target_id}</code> removido del Staff.",
        chat_id=message.chat.id,
        message_id=msg_id,
        reply_markup=get_main_dashboard_kb(group_id)
    )

@router.callback_query(F.data.startswith("viewstaff_"))
async def view_staff_list(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    group = await groups_col.find_one({"_id": group_id})
    staff_ids = group.get("authorized_users", []) if group else []

    if not staff_ids:
        return await callback.answer("ℹ️ No hay operadores registrados.", show_alert=True)

    staff_details = group.get("staff_details", {})
    text = "👑 <b>NÓMINA DE STAFF REGISTRADO</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for uid in staff_ids:
        detail = staff_details.get(str(uid), {})
        name = detail.get("name", "Operador")
        date_reg = detail.get("date", "Preexistente")
        text += f"👤 <b>{name}</b> | <code>{uid}</code>\n📅 <i>Alta: {date_reg}</i>\n\n"

    await callback.message.edit_text(text, reply_markup=get_back_kb(group_id))

# --- MÓDULO DE LISTA NEGRA ---
@router.callback_query(F.data.startswith("badwords_"))
async def badwords_view(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    words = await get_cached_blacklist(group_id)

    formatted_words = ", ".join([f"<code>{w}</code>" for w in words]) if words else "<i>Lista vacía.</i>"
    text = (
        f"🚫 <b>FILTRO DE PALABRAS PROHIBIDAS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{formatted_words}\n\n"
        f"<i>Los mensajes que contengan estos términos exactos serán destruidos.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Añadir Palabras en Masa", callback_data=f"addword_{group_id}")],
        [InlineKeyboardButton(text="🗑️ Vaciar Lista Negra", callback_data=f"clearwords_{group_id}")],
        [InlineKeyboardButton(text="◀️ Volver", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data.startswith("addword_"))
async def badwords_add_start(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_badword)
    await state.update_data(group_id=group_id, msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "✍️ <b>Envía las palabras que deseas bloquear:</b>\n"
        "<i>Puedes separar varias palabras usando comas o saltos de línea.</i>",
        reply_markup=get_back_kb(group_id)
    )

@router.message(BotStates.waiting_for_badword)
async def badwords_add_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id, msg_id = data["group_id"], data["msg_id"]
    await message.delete()

    raw_words = [w.strip().lower() for w in re.split(r'[,\n]+', message.text) if len(w.strip()) > 1]
    if raw_words:
        await groups_col.update_one(
            {"_id": group_id},
            {"$addToSet": {"blacklist": {"$each": raw_words}}},
            upsert=True
        )
        invalidate_blacklist_cache(group_id)

    await state.clear()
    await bot.edit_message_text(
        f"✅ <b>Filtro Actualizado:</b> Se indexaron <code>{len(raw_words)}</code> términos prohibidos.",
        chat_id=message.chat.id,
        message_id=msg_id,
        reply_markup=get_main_dashboard_kb(group_id)
    )

@router.callback_query(F.data.startswith("clearwords_"))
async def clear_badwords(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    await groups_col.update_one({"_id": group_id}, {"$set": {"blacklist": []}})
    invalidate_blacklist_cache(group_id)
    await callback.answer("🧹 Lista negra vaciada.", show_alert=False)
    await callback.message.edit_text(
        "✅ <b>Filtro reiniciado:</b> Se eliminaron todas las palabras prohibidas.",
        reply_markup=get_back_kb(group_id)
    )

# --- MÓDULO DE PURGA INMEDIATA ---
@router.callback_query(F.data.startswith("cleanmenu_"))
async def cleanup_menu(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    pending_count = await cleanup_queue_col.count_documents({"chat_id": group_id})
    
    group_doc = await groups_col.find_one({"_id": group_id})
    next_time = group_doc.get("next_cleanup", datetime.now()) if group_doc else datetime.now()
    remaining = max(0, int((next_time - datetime.now()).total_seconds()))
    hours, mins = divmod(remaining // 60, 60)

    text = (
        f"🧹 <b>MÓDULO DE PURGA Y LIMPIEZA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Multimedia en cola:</b> <code>{pending_count}</code> archivos\n"
        f"⏱️ <b>Próxima ejecución:</b> <code>{hours}h {mins}m</code>\n\n"
        f"<i>La purga forzada destruirá de inmediato todos los archivos en espera.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Forzar Purga Inmediata", callback_data=f"forceclean_{group_id}")],
        [InlineKeyboardButton(text="◀️ Volver", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data.startswith("forceclean_"))
async def force_clean_action(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    await callback.answer("Iniciando purga masiva...", show_alert=False)
    purged = await execute_cleanup(group_id)
    await callback.message.edit_text(
        f"✅ <b>Operación Finalizada</b>\nSe purgaron <code>{purged}</code> elementos.\nReloj de 12 horas reiniciado.",
        reply_markup=get_back_kb(group_id)
    )

@router.callback_query(F.data.startswith("help_"))
async def guide_menu(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    text = (
        "📖 <b>MANUAL TÁCTICO DE OPERACIONES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "• <code>/panel</code>: Invoca la consola central en privado.\n"
        "• <code>/del</code>: Elimina el mensaje referenciado.\n"
        "• <code>/ban</code>: Expulsa permanentemente a un usuario.\n"
        "• <code>/unban [ID]</code>: Revoca una expulsión activa.\n"
        "• <code>/mute [30m/2h]</code>: Restringe el habla de forma temporal.\n"
        "• <code>/unmute</code>: Levanta la restricción de voz.\n"
        "• <code>/warn</code> / <code>/unwarn</code>: Gestión de faltas (3 = Ban).\n"
        "• <code>/delall</code>: Menú de purga exhaustiva de un usuario.\n"
        "• <code>/s [texto]</code>: Emite un mensaje fantasma oficial del bot.\n"
        "• <code>/aportes</code>: Estadísticas individuales de multimedia.\n"
        "• <code>/topaportes</code>: Ranking semanal con gráfico de barras.\n"
        "• <code>/leyes</code>: Proclama el reglamento durante 30 segundos."
    )
    await callback.message.edit_text(text, reply_markup=get_back_kb(group_id))

# =====================================================================
# INTERCEPTOR Y PROCESADOR CENTRAL DE MENSAJES (DEFENSA ACTIVA)
# =====================================================================
@router.message(F.new_chat_members)
async def anti_bot_guard(message: Message):
    """Bloquea la entrada de bots no autorizados."""
    if message.chat.type not in ["group", "supergroup"]:
        return

    adder_is_admin = await is_admin(message.chat.id, message.from_user.id, bot)
    for new_member in message.new_chat_members:
        if new_member.is_bot and new_member.id != bot.id and not adder_is_admin:
            try:
                await bot.ban_chat_member(message.chat.id, new_member.id)
                alert = await message.reply(f"🛡️ <b>ANTI-BOT:</b> Se expulsó a <code>{new_member.first_name}</code>.")
                await asyncio.sleep(8)
                await alert.delete()
            except Exception:
                pass

@router.message()
async def central_message_traffic_controller(message: Message):
    """Procesador de defensa en tiempo real: blacklist, enlaces y control de colas."""
    if message.chat.type not in ["group", "supergroup"]:
        return

    # 1. Anti-Bot intrusos
    if message.from_user.is_bot and message.from_user.id != bot.id:
        if not await is_admin(message.chat.id, message.from_user.id, bot):
            try:
                await bot.ban_chat_member(message.chat.id, message.from_user.id)
                await message.delete()
            except Exception:
                pass
            return

    sender_is_admin = await is_admin(message.chat.id, message.from_user.id, bot)
    content = message.text or message.caption or ""

    if not sender_is_admin and content:
        # 2. Filtro de Lista Negra con coincidencia de palabra completa (\bword\b)
        blacklist = await get_cached_blacklist(message.chat.id)
        if blacklist:
            content_lower = content.lower()
            for pattern in blacklist:
                # Evita falsos positivos como 'disputar' activando 'puta'
                if re.search(rf'\b{re.escape(pattern)}\b', content_lower):
                    try:
                        await message.delete()
                        return
                    except Exception:
                        pass

        # 3. Filtro Antienlaces Exhaustivo
        has_url_entity = any(
            e.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]
            for e in (message.entities or message.caption_entities or [])
        )
        if has_url_entity or LINK_REGEX.search(content):
            try:
                await message.delete()
                return
            except Exception:
                pass

    # 4. Encolado de multimedia para purga cíclica y conteo de estadísticas
    if message.photo or message.video or message.document:
        current_week = datetime.now().strftime("%Y-W%V")
        chat_id = message.chat.id
        user_id = message.from_user.id

        # Insertar en la cola separada para evitar desbordar el documento del grupo
        await cleanup_queue_col.insert_one({
            "chat_id": chat_id,
            "message_id": message.message_id,
            "created_at": datetime.now()
        })

        # Inicializar timer si es el primer elemento
        await groups_col.update_one(
            {"_id": chat_id},
            {"$setOnInsert": {"next_cleanup": datetime.now() + timedelta(hours=12)}},
            upsert=True
        )

        # Registro atómico de estadísticas semanales por usuario y grupo
        await stats_col.update_one(
            {"chat_id": chat_id, "user_id": user_id, "week": current_week},
            {
                "$inc": {"count": 1},
                "$set": {"name": message.from_user.first_name}
            },
            upsert=True
        )

# =====================================================================
# SERVIDOR DE SALUD (HEALTHCHECK) Y CICLO DE VIDA
# =====================================================================
async def web_health_handler(_: web.Request):
    return web.Response(text="Imperio Bot Core Engine: Running Smoothly", status=200)

async def start_background_tasks():
    app = web.Application()
    app.router.add_get("/", web_health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    asyncio.create_task(auto_cleanup_worker())

async def main():
    dp.include_router(router)
    await start_background_tasks()
    
    # Crear índices en colecciones críticas para consultas en milisegundos
    await cleanup_queue_col.create_index([("chat_id", 1), ("message_id", 1)])
    await stats_col.create_index([("chat_id", 1), ("week", 1), ("count", -1)])
    
    logger.info("Iniciando ImperioBot con arquitectura blindada...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot apagado ordenadamente.")