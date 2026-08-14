import asyncio
import re
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= CONFIGURACIÓN =================
TOKEN = "8948969120:AAFc7_l9YgMY8psuQtfdhGY44TbU-FwCkyY"
BACKUP_CHANNEL_ID = -1004455894965  # ID de tu canal de respaldo

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

active_groups = {}          
authorized_users = {}       
active_timers = {}          

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

class BotStates(StatesGroup):
    waiting_for_id = State()

async def is_admin(chat_id: int, user_id: int) -> bool:
    if chat_id in authorized_users and user_id in authorized_users[chat_id]:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except:
        return False

# ================= TECLADOS EN LÍNEA =================
def get_main_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Cerrar Chat", callback_data=f"close_{group_id}"),
            InlineKeyboardButton(text="🔓 Abrir Chat", callback_data=f"open_{group_id}")
        ],
        [
            InlineKeyboardButton(text="👥 Autorizar ID", callback_data=f"addid_{group_id}"),
            InlineKeyboardButton(text="⏱️ Programar Cierre", callback_data=f"timer_{group_id}")
        ],
        [
            InlineKeyboardButton(text="💣 Destrucción Inmediata", callback_data=f"nuke_{group_id}")
        ],
        [
            InlineKeyboardButton(text="ℹ️ Ayuda", callback_data=f"help_{group_id}")
        ]
    ])

def get_timer_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="10 Min", callback_data=f"settimer_{group_id}_10"),
            InlineKeyboardButton(text="20 Min", callback_data=f"settimer_{group_id}_20"),
            InlineKeyboardButton(text="30 Min", callback_data=f"settimer_{group_id}_30")
        ],
        [
            InlineKeyboardButton(text="❌ Cancelar Temporizador", callback_data=f"canceltimer_{group_id}")
        ],
        [
            InlineKeyboardButton(text="⬅️ Volver", callback_data=f"back_{group_id}")
        ]
    ])

def get_back_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Volver al Panel", callback_data=f"back_{group_id}")]
    ])

# ================= COMANDOS EN GRUPO =================

@router.message(Command("panel"))
async def link_group_panel(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        if await is_admin(message.chat.id, message.from_user.id):
            active_groups[message.from_user.id] = message.chat.id
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Abrir Panel", url=f"t.me/{(await bot.me()).username}?start=panel")
            ]])
            await message.reply("Panel de control listo:", reply_markup=kb)
        else:
            await message.reply("❌ No tienes permisos.")

@router.message(Command("info"))
async def group_info_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        if await is_admin(message.chat.id, message.from_user.id):
            try:
                chat = await message.bot.get_chat(message.chat.id)
                member_count = await message.bot.get_chat_member_count(message.chat.id)
                bot_member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
                bot_is_admin = bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
                status_admin_text = "✅ Sí (Con privilegios)" if bot_is_admin else "⚠️ No (Funciones limitadas)"

                info_text = (
                    f"📊 **Información del Grupo**\n\n"
                    f"🏷️ **Nombre:** {chat.title}\n"
                    f"🆔 **ID del Grupo:** `{chat.id}`\n"
                    f"👥 **Miembros Totales:** {member_count}\n"
                    f"🔗 **Enlace Público:** {chat.invite_link or 'No disponible / Es privado'}\n"
                    f"🤖 **Estado del Bot:** {status_admin_text}\n"
                    f"📝 **Descripción:** {chat.description or 'Sin descripción'}"
                )
                await message.reply(info_text, parse_mode="Markdown")
                await message.delete()
            except Exception as e:
                await message.reply(f"❌ Error al obtener la información: {e}")

@router.message(Command("del"))
async def delete_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                await message.delete()
            except:
                pass

@router.message(Command("ban"))
async def ban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            user_to_ban = message.reply_to_message.from_user.id
            try:
                await bot.ban_chat_member(message.chat.id, user_to_ban)
                await message.reply_to_message.delete()
                confirm = await message.answer(f"🔨 Usuario baneado por {message.from_user.first_name}.")
                await message.delete()
                await asyncio.sleep(5)
                await confirm.delete()
            except:
                err = await message.answer("❌ No pude banear al usuario.")
                await message.delete()
                await asyncio.sleep(3)
                await err.delete()

@router.message(Command("unban"))
async def unban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        user_to_unban = None
        if message.reply_to_message:
            user_to_unban = message.reply_to_message.from_user.id
        else:
            args = message.text.split()
            if len(args) > 1 and args[1].isdigit():
                user_to_unban = int(args[1])
                
        if user_to_unban:
            try:
                await bot.unban_chat_member(message.chat.id, user_to_unban)
                confirm = await message.answer(f"✅ Usuario desbaneado con éxito.")
                await message.delete()
                await asyncio.sleep(5)
                await confirm.delete()
            except Exception as e:
                err = await message.answer("❌ No pude desbanear al usuario.")
                await message.delete()
                await asyncio.sleep(3)
                await err.delete()

# ================= CHAT PRIVADO (PANEL Y ESTADOS) =================

@router.message(CommandStart())
async def start_private_panel(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await state.clear()
        group_id = active_groups.get(message.from_user.id)
        if group_id:
            chat_info = await bot.get_chat(group_id)
            await message.answer(
                f"⚙️ **Panel de Control:** {chat_info.title}\nElige una opción:",
                reply_markup=get_main_keyboard(group_id),
                parse_mode="Markdown"
            )
        else:
            await message.answer("Usa /panel dentro de un grupo primero para vincularlo.")

@router.message(BotStates.waiting_for_id)
async def process_new_id(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("group_id")
    panel_msg_id = data.get("panel_msg_id")
    await message.delete() 
    
    try:
        new_id = int(message.text.strip())
        if group_id not in authorized_users:
            authorized_users[group_id] = set()
        authorized_users[group_id].add(new_id)
        
        await bot.edit_message_text(
            f"✅ **ID {new_id} autorizado con éxito.**\n\nPanel Principal:",
            chat_id=message.chat.id,
            message_id=panel_msg_id,
            reply_markup=get_main_keyboard(group_id),
            parse_mode="Markdown"
        )
    except ValueError:
        await bot.edit_message_text(
            "❌ **Error:** El ID debe ser numérico.\n\nPanel Principal:",
            chat_id=message.chat.id,
            message_id=panel_msg_id,
            reply_markup=get_back_keyboard(group_id),
            parse_mode="Markdown"
        )
    finally:
        await state.clear()

# ================= CALLBACKS (BOTONES) =================

@router.callback_query(F.data.startswith("back_"))
async def back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("⚙️ **Panel Principal**", reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("help_"))
async def help_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    help_text = (
        "📚 **Guía de Uso del Bot:**\n\n"
        "🔸 **Anti-Links:** Borra automáticamente mensajes con enlaces.\n"
        "🔸 **Respaldo:** Copia fotos/videos enviados al grupo directo al canal.\n"
        "🔸 **/info:** Muestra estadísticas y detalles técnicos del grupo.\n"
        "🔸 **/del, /ban y /unban:** Responde a mensajes para borrar, expulsar o desbanear.\n"
        "🔸 **Autorizar ID:** Permite a otros usuarios evadir reglas y usar comandos.\n"
        "🔸 **Programar Cierre:** Inicia una cuenta regresiva para la autodestrucción."
    )
    await callback.message.edit_text(help_text, reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("close_"))
async def close_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(can_send_messages=False))
        await callback.answer("Chat cerrado.", show_alert=True)
    except:
        await callback.answer("Error de permisos.", show_alert=True)

@router.callback_query(F.data.startswith("open_"))
async def open_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(
            can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_other_messages=True
        ))
        await callback.answer("Chat abierto.", show_alert=True)
    except:
        await callback.answer("Error de permisos.", show_alert=True)

@router.callback_query(F.data.startswith("addid_"))
async def addid_cb(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_id)
    await state.update_data(group_id=group_id, panel_msg_id=callback.message.message_id)
    
    await callback.message.edit_text(
        "✍️ **Envía un mensaje con el ID numérico del usuario.**",
        reply_markup=get_back_keyboard(group_id),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("timer_"))
async def timer_menu_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("⏱️ **Selecciona el tiempo para eliminar el grupo:**", reply_markup=get_timer_keyboard(group_id), parse_mode="Markdown")

async def countdown_task(group_id: int, minutes: int):
    try:
        msg = await bot.send_message(group_id, f"⚠️ **El grupo será eliminado en {minutes} minutos.**", parse_mode="Markdown")
        if minutes > 1:
            await asyncio.sleep((minutes - 1) * 60)
            await msg.edit_text("⚠️ **El grupo será eliminado en 1 minuto.**", parse_mode="Markdown")
            await asyncio.sleep(55)
        else:
            await asyncio.sleep(55)

        for i in range(5, 0, -1):
            await msg.edit_text(f"⚠️ **ELIMINACIÓN EN {i}...**", parse_mode="Markdown")
            await asyncio.sleep(1)
            
        await msg.edit_text("💥 **ELIMINANDO...**", parse_mode="Markdown")
        await bot.leave_chat(group_id)
    except asyncio.CancelledError:
        await bot.send_message(group_id, "🛑 **Autodestrucción cancelada.**", parse_mode="Markdown")

@router.callback_query(F.data.startswith("settimer_"))
async def settimer_cb(callback: CallbackQuery):
    _, group_id, mins = callback.data.split("_")
    group_id, minutes = int(group_id), int(mins)
    if group_id in active_timers:
        active_timers[group_id].cancel()
    task = asyncio.create_task(countdown_task(group_id, minutes))
    active_timers[group_id] = task
    await callback.message.edit_text(f"✅ **Temporizador iniciado.**\nEl grupo será destruido en {minutes} min.", reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("canceltimer_"))
async def canceltimer_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    if group_id in active_timers:
        active_timers[group_id].cancel()
        del active_timers[group_id]
        await callback.message.edit_text("🛑 **Temporizador cancelado.**", reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")
    else:
        await callback.answer("No hay ningún temporizador activo.", show_alert=True)

@router.callback_query(F.data.startswith("nuke_"))
async def nuke_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(can_send_messages=False))
        msg = await bot.send_message(group_id, "⚠️ **ELIMINACIÓN INMEDIATA...**", parse_mode="Markdown")
        for i in range(3, 0, -1):
            await msg.edit_text(f"⚠️ **ELIMINACIÓN EN {i}...**", parse_mode="Markdown")
            await asyncio.sleep(1)
        await bot.leave_chat(group_id)
        await callback.message.edit_text("✅ **Grupo destruido con éxito.**", reply_markup=None, parse_mode="Markdown")
    except:
        await callback.answer("Error al ejecutar la secuencia.", show_alert=True)

# ================= FILTRO GLOBAL =================

@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        content = message.text or message.caption
        if content and LINK_REGEX.search(content):
            if not await is_admin(message.chat.id, message.from_user.id):
                try:
                    await message.delete()
                    return
                except:
                    pass
        
        is_media = (
            message.photo or message.video or message.document or 
            message.audio or message.voice or message.video_note
        )
        if is_media:
            try:
                await message.copy_to(BACKUP_CHANNEL_ID)
            except Exception as e:
                logging.error(f"Error copiando al canal de respaldo: {e}")

# ================= SERVIDOR WEB FALSO PARA RENDER =================
async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()

# ================= EJECUCIÓN PRINCIPAL =================
async def main():
    dp.include_router(router)
    await web_server() # Inicia el servidor web falso para mantener vivo el Web Service en Render
    print("🤖 Bot Web Service iniciado y corriendo...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())