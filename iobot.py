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
TOKEN = "8515941177:AAHF-I0U5EB-zidhrnGbZVQuAdw13ArQpjU"
BACKUP_CHANNEL_ID = -1003986866749  # <-- REEMPLAZA CON EL ID DE TU CANAL PRIVADO UNICO

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

active_groups = {}          
authorized_users = {}       

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
            InlineKeyboardButton(text="ℹ️ Ayuda", callback_data=f"help_{group_id}")
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
                
                info_text = (
                    f"📊 **Información del Grupo**\n\n"
                    f"🏷️ **Nombre:** {chat.title}\n"
                    f"🆔 **ID del Grupo:** `{chat.id}`\n"
                    f"👥 **Miembros Totales:** {member_count}\n"
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

# ================= COMANDO REPETIR Y BORRAR (/s O .s) =================
@router.message(F.text.startswith("/s ") | F.text.startswith(".s "))
async def repeat_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        if await is_admin(message.chat.id, message.from_user.id):
            # Extraemos todo lo que haya después de los 3 primeros caracteres ("/s " o ".s ")
            text_to_send = message.text[3:].strip()
            if text_to_send:
                try:
                    await message.answer(text_to_send)
                    await message.delete()
                except:
                    pass

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
        "🔸 **Respaldo Único:** Copia multimedia al canal añadiendo el nombre del grupo.\n"
        "🔸 **/s o .s [mensaje]:** El bot repite el mensaje y borra el tuyo.\n"
        "🔸 **/info:** Muestra estadísticas del grupo.\n"
        "🔸 **/del, /ban y /unban:** Responde a mensajes para borrar, expulsar o desbanear.\n"
        "🔸 **Autorizar ID:** Permite a otros usuarios evadir reglas y usar comandos."
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

# ================= FILTRO GLOBAL =================

@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        # Filtro de enlaces
        content = message.text or message.caption
        if content and LINK_REGEX.search(content):
            if not await is_admin(message.chat.id, message.from_user.id):
                try:
                    await message.delete()
                    return
                except:
                    pass
        
        # Filtro de Respaldo Multimedia
        is_media = (
            message.photo or message.video or message.document or 
            message.audio or message.voice or message.animation
        )
        
        if is_media:
            try:
                # Obtenemos el texto original (si tiene) y le agregamos la firma del grupo
                original_caption = message.caption or ""
                group_signature = f"📌 Enviado desde: {message.chat.title}"
                
                if original_caption:
                    new_caption = f"{original_caption}\n\n{group_signature}"
                else:
                    new_caption = group_signature

                # Solo pasamos la nueva descripción a los tipos de mensaje que lo soportan
                kwargs = {}
                if not message.sticker and not message.video_note:
                     kwargs['caption'] = new_caption

                await message.copy_to(BACKUP_CHANNEL_ID, **kwargs)
            except Exception as e:
                logging.error(f"Error copiando al canal: {e}")

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
    await web_server()
    print("🤖 Bot Web Service iniciado y corriendo...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())