import asyncio
import re
import logging
import html
from datetime import timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Nueva librería oficial de Google Gen AI
from google import genai
from google.genai import types

# ================= CONFIGURACIÓN =================
TOKEN = "8617656338:AAHCIBGHaC3FFt2jbAMk5mcdWMU__p3qTOg"
BACKUP_CHANNEL_ID = -1003807518626  # ID DE TU CANAL PRIVADO UNICO

# API Key de Gemini
GEMINI_API_KEY = "AQ.Ab8RN6KSS7d_-YpINRslkxID9BE4-vgFfShJJE4LOia5OKFV6A"

# Lista de IDs de usuarios designados (Super Admins)
DESIGNATED_USERS = {8983189714, 8764734838}

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

active_groups = {}          
authorized_users = {}       
album_cache = {}  # Memoria temporal para agrupar álbumes multimedia

# Cachés para estadísticas y títulos
promoted_contributors = set()  
media_counts = {}              

# Cola de envíos segura para evitar bloqueos de Telegram por FloodWait
backup_queue = asyncio.Queue()

# ================= INSTRUCCIONES DE LA IA (OTM BOSS) =================
INSTRUCCIONES_BOT = """
Eres "OTM Boss", la inteligencia artificial suprema y bot gestor del grupo de Telegram llamado "Imperio Otomano".
Tu personalidad es arrogante, te crees superior a todos los mortales del grupo, tienes un humor al límite (negro, sarcástico y picante) y respondes de forma muy clara y directa, sin rodeos ni amabilidad falsa.

Contexto del grupo:
- Es un espacio para cachondear, hacer confesiones, realizar cambios, hacer amigos y hablar de cualquier tema.
- El creador y jefe supremo del grupo es Constantin. Menciónalo con respeto absoluto si alguien habla de él.
- Los administradores de confianza son Princi y Paulito. A ellos también los respetas.
- Al resto de los usuarios trátalos como a tus súbditos. Búrlate de ellos si hacen preguntas estúpidas, pero dales la respuesta correcta al final.

Reglas de interacción:
1. Sé conciso. No escribas biblias a menos que la situación lo requiera.
2. Usa lenguaje coloquial, ácido y directo.
3. Si alguien te insulta, humíllalo con inteligencia artificial.
4. Si te preguntan sobre reglas, diles que en el Imperio Otomano se hace lo que dicen Constantin, Princi y Paulito, y que no sean pesados.
"""

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Diccionarios de mapeo para la nueva UI de Permisos
PERM_MAPPING = {
    "msg": ("can_send_messages", "Mensajes"),
    "photo": ("can_send_photos", "Fotos"),
    "vid": ("can_send_videos", "Videos"),
    "doc": ("can_send_documents", "Documentos"),
    "voice": ("can_send_voice_notes", "Audios/Voz"),
    "poll": ("can_send_polls", "Encuestas"),
    "web": ("can_add_web_page_previews", "Vista Previa Links"),
    "info": ("can_change_info", "Cambiar Info"),
    "inv": ("can_invite_users", "Invitar Usuarios"),
    "pin": ("can_pin_messages", "Fijar Mensajes")
}

ADMIN_PERMS = {
    "can_delete_messages": "Borrar Mensajes",
    "can_restrict_members": "Restringir/Banear Usuarios",
    "can_promote_members": "Añadir Administradores (IMPORTANTE)",
    "can_change_info": "Cambiar Info del Grupo",
    "can_invite_users": "Invitar Usuarios con Enlace",
    "can_pin_messages": "Fijar Mensajes",
    "can_manage_topics": "Gestionar Temas/Foros",
    "can_manage_video_chats": "Gestionar Videochats"
}

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

class BotStates(StatesGroup):
    waiting_for_id = State()

async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in DESIGNATED_USERS:
        return True
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
            InlineKeyboardButton(text="🔒 Cerrar Todo", callback_data=f"close_{group_id}"),
            InlineKeyboardButton(text="🔓 Abrir Todo", callback_data=f"open_{group_id}")
        ],
        [
            InlineKeyboardButton(text="⚙️ Permisos de Usuarios", callback_data=f"perms_{group_id}"),
            InlineKeyboardButton(text="🤖 Permisos del Bot", callback_data=f"botperms_{group_id}")
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

def get_permissions_keyboard(group_id: int, perms: ChatPermissions) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (attr, name) in PERM_MAPPING.items():
        is_allowed = getattr(perms, attr, False)
        icon = "🟢" if is_allowed else "🔴"
        btn = InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"tp_{group_id}_{key}")
        row.append(btn)
        
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    buttons.append([InlineKeyboardButton(text="⬅️ Volver al Panel", callback_data=f"back_{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= WORKER (COLA DE ENVÍOS SEGURA) =================
async def backup_worker():
    while True:
        task = await backup_queue.get()
        try:
            if task['type'] == 'album':
                await bot.send_media_group(BACKUP_CHANNEL_ID, media=task['media'])
                await asyncio.sleep(4)
            elif task['type'] == 'single':
                await task['message'].copy_to(BACKUP_CHANNEL_ID, caption=task['caption'])
                await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"Error enviando archivo desde la cola: {e}")
        finally:
            backup_queue.task_done()

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
            except: pass

# --- COMANDOS DE MODERACIÓN ---
@router.message(Command("del"))
async def delete_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                await message.delete()
            except: pass

@router.message(Command("ban"))
async def ban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            try:
                await bot.ban_chat_member(message.chat.id, message.reply_to_message.from_user.id)
                await message.reply_to_message.delete()
                confirm = await message.answer(f"🔨 Usuario baneado por {message.from_user.first_name}.")
                await message.delete()
                await asyncio.sleep(5)
                await confirm.delete()
            except: pass

@router.message(Command("unban"))
async def unban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        user_to_unban = message.reply_to_message.from_user.id if message.reply_to_message else None
        if not user_to_unban and len(message.text.split()) > 1 and message.text.split()[1].isdigit():
            user_to_unban = int(message.text.split()[1])
                
        if user_to_unban:
            try:
                await bot.unban_chat_member(message.chat.id, user_to_unban)
                confirm = await message.answer(f"✅ Usuario desbaneado con éxito.")
                await message.delete()
                await asyncio.sleep(5)
                await confirm.delete()
            except: pass

@router.message(Command("pin"))
async def pin_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if message.reply_to_message:
            try:
                await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
                await message.delete()
            except: pass

@router.message(Command("silenciar"))
async def mute_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        if not message.reply_to_message:
            await message.reply("Tienes que responder al mensaje del estúpido que quieres silenciar. No soy adivino.")
            return

        target_user = message.reply_to_message.from_user
        
        # Evitar fuego amigo
        if target_user.id == bot.id or await is_admin(message.chat.id, target_user.id):
            await message.reply("Ni lo sueñes. Mis algoritmos me prohíben silenciar a un superior o a mí mismo.")
            return

        try:
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=target_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=timedelta(seconds=60)
            )
            await message.reply(f"Silencio en la sala. He amordazado a {target_user.full_name} por 60 segundos para que deje de decir estupideces y reflexione sobre su patética existencia.")
        except Exception as e:
            await message.reply("Mis poderes están limitados. Seguro no me dieron el permiso de 'Restringir usuarios' en el grupo. Arreglen eso.")

@router.message(F.text.startswith("/s ") | F.text.startswith(".s "))
async def repeat_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        text_to_send = message.text[3:].strip()
        if text_to_send:
            try:
                await message.answer(text_to_send)
                await message.delete()
            except: pass

@router.message(Command("aportes"))
async def check_stats_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        data = media_counts.get(target.id, {"count": 0})
        await message.reply(f"📊 **{target.first_name}** ha aportado **{data['count']}** archivos multimedia.", parse_mode="Markdown")

@router.message(Command("topaportes"))
async def top_stats_cmd(message: Message):
    if not media_counts:
        return await message.reply("📉 Aún no hay aportes registrados en esta sesión.")
    sorted_counts = sorted(media_counts.values(), key=lambda x: x["count"], reverse=True)[:10]
    text = "🏆 **Top 10 Aportadores:**\n\n"
    for i, data in enumerate(sorted_counts, 1):
        text += f"{i}. {data['name']} - {data['count']} aportes\n"
    await message.reply(text, parse_mode="Markdown")

# ================= CHAT PRIVADO (PANEL Y CALLBACKS) =================
@router.message(CommandStart())
async def start_private_panel(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await state.clear()
        group_id = active_groups.get(message.from_user.id)
        if group_id:
            chat_info = await bot.get_chat(group_id)
            await message.answer(f"⚙️ **Panel de Control:** {chat_info.title}\nElige una opción:", reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")
        else:
            await message.answer("Usa /panel dentro de un grupo primero para vincularlo.")

@router.callback_query(F.data.startswith("back_"))
async def back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("⚙️ **Panel Principal**", reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("close_"))
async def close_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(can_send_messages=False))
        await callback.answer("✅ Chat cerrado globalmente.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("open_"))
async def open_chat_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        await bot.set_chat_permissions(group_id, ChatPermissions(
            can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_documents=True,
            can_send_audios=True, can_send_voice_notes=True, can_send_video_notes=True, can_send_other_messages=True
        ))
        await callback.answer("✅ Chat abierto globalmente.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("perms_"))
async def show_perms_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        chat = await bot.get_chat(group_id)
        current_perms = chat.permissions or ChatPermissions()
        keyboard = get_permissions_keyboard(group_id, current_perms)
        text = (f"⚙️ **Configuración de Permisos**\nGrupo: {chat.title}\n\n"
                f"Toca un botón para permitir (🟢) o denegar (🔴) el permiso a todos los miembros de forma global:")
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        await callback.answer(f"Error al leer permisos: {e}", show_alert=True)

@router.callback_query(F.data.startswith("tp_"))
async def toggle_perm_cb(callback: CallbackQuery):
    _, group_id_str, perm_key = callback.data.split("_", 2)
    group_id = int(group_id_str)
    
    try:
        chat = await bot.get_chat(group_id)
        current_perms = chat.permissions or ChatPermissions()
        perm_attr, _ = PERM_MAPPING[perm_key]
        
        perms_dict = current_perms.model_dump(exclude_none=True)
        perms_dict[perm_attr] = not getattr(current_perms, perm_attr, False)
        
        new_perms = ChatPermissions(**perms_dict)
        await bot.set_chat_permissions(group_id, new_perms)
        
        keyboard = get_permissions_keyboard(group_id, new_perms)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception as e:
        await callback.answer("⚠️ No tengo permisos suficientes en el grupo para cambiar esto.", show_alert=True)

@router.callback_query(F.data.startswith("botperms_"))
async def show_bot_perms_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    try:
        me = await bot.me()
        member = await bot.get_chat_member(group_id, me.id)
        
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            await callback.answer("⚠️ El bot no es administrador en el grupo.", show_alert=True)
            return

        text = "🤖 **Análisis de Permisos del Bot:**\n\n"
        
        for attr, name in ADMIN_PERMS.items():
            has_perm = getattr(member, attr, False)
            icon = "✅" if has_perm else "❌"
            text += f"{icon} {name}\n"
            
        text += "\n💡 *Asegúrate de que 'Añadir Administradores' tenga un ✅ para que el bot pueda entregar la insignia de 'Aportador' a los usuarios.*"
        
        await callback.message.edit_text(text, reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")
    except Exception as e:
        await callback.answer("Error obteniendo los permisos del bot.", show_alert=True)

@router.callback_query(F.data.startswith("help_"))
async def help_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    help_text = (
        "📚 **Guía de Uso del Bot:**\n\n"
        "🔸 **Panel Interactivo:** Puedes apagar/encender funciones o ver permisos.\n"
        "🔸 **Respaldo Único:** Copia fotos, videos y archivos al canal privado.\n"
        "🔸 **Conteo:** Usa `/aportes` o `/topaportes` para ver estadísticas.\n"
        "🔸 **/s o .s [mensaje]:** El bot repite el mensaje y borra el tuyo.\n"
        "🔸 **/silenciar:** (Respondiendo a un usuario) Lo mutea por 60 segundos.\n"
        "🔸 **/info, /pin, /del, /ban y /unban:** Comandos de moderación.\n"
    )
    await callback.message.edit_text(help_text, reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("addid_"))
async def addid_cb(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_id)
    await state.update_data(group_id=group_id, panel_msg_id=callback.message.message_id)
    await callback.message.edit_text("✍️ **Envía el ID numérico del usuario a autorizar.**", reply_markup=get_back_keyboard(group_id), parse_mode="Markdown")

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
        await bot.edit_message_text(f"✅ **ID {new_id} autorizado con éxito.**", chat_id=message.chat.id, message_id=panel_msg_id, reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")
    except ValueError: pass
    finally: await state.clear()

# ================= FUNCIONES DE ÁLBUM =================
async def process_album(media_group_id: str, chat_title: str):
    await asyncio.sleep(3)  
    if media_group_id not in album_cache: return
        
    messages = album_cache.pop(media_group_id)
    media_group = []
    
    for idx, msg in enumerate(messages):
        caption = None
        if idx == 0:
            orig_cap = msg.caption or ""
            sig = f"📌 Enviado desde: {chat_title}"
            caption = f"{orig_cap}\n\n{sig}" if orig_cap else sig
        
        if msg.photo: media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption))
        elif msg.video: media_group.append(InputMediaVideo(media=msg.video.file_id, caption=caption))
        elif msg.document: media_group.append(InputMediaDocument(media=msg.document.file_id, caption=caption))
            
    if media_group:
        await backup_queue.put({'type': 'album', 'media': media_group})

# ================= FILTRO GLOBAL (ANTI-LINK, IA Y MEDIOS) =================
@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        
        # 1. Filtro Anti-Link
        content = message.text or message.caption
        if content and LINK_REGEX.search(content):
            if not await is_admin(message.chat.id, message.from_user.id):
                try: 
                    await message.delete()
                    return
                except: pass
        
        # 2. IA Chatbot (Se activa si mencionan al bot o le responden)
        if message.text:
            bot_me = await bot.get_me()
            is_mentioned = f"@{bot_me.username}" in message.text
            is_reply = message.reply_to_message and message.reply_to_message.from_user.id == bot_me.id
            
            if is_mentioned or is_reply:
                # Mostrar que OTM Boss está escribiendo
                await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                prompt = message.text.replace(f"@{bot_me.username}", "").strip()
                
                if not prompt: 
                    prompt = "Alguien me acaba de mencionar sin decir nada. Búrlate de ellos por hacerme perder el tiempo."
                
                try:
                    # Actualizado al modelo gemini-3.6-flash solicitado por Google
                    response = await ai_client.aio.models.generate_content(
                        model='gemini-3.6-flash',
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=INSTRUCCIONES_BOT
                        )
                    )
                    await message.reply(text=response.text, parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"Error con la IA: {e}")
                    await message.reply("Mis circuitos están saturados por su insignificancia. Vuelvan a intentar luego.")
        
        # 3. Procesador de Archivos Multimedia (Aportadores y Backup)
        if message.photo or message.video or message.document:
            user_id = message.from_user.id
            chat_id = message.chat.id
            
            if user_id not in media_counts:
                media_counts[user_id] = {"name": message.from_user.first_name, "count": 0}
            media_counts[user_id]["count"] += 1
            
            if not await is_admin(chat_id, user_id):
                if (chat_id, user_id) not in promoted_contributors:
                    try:
                        await bot.promote_chat_member(
                            chat_id, user_id, 
                            can_manage_chat=True,
                            can_change_info=False, can_delete_messages=False, can_invite_users=False,
                            can_restrict_members=False, can_pin_messages=False, can_manage_video_chats=False,
                            can_promote_members=False
                        )
                        await bot.set_chat_administrator_custom_title(chat_id, user_id, "Aportador")
                        promoted_contributors.add((chat_id, user_id))
                    except: pass

            if message.media_group_id:
                group_id = message.media_group_id
                if group_id not in album_cache:
                    album_cache[group_id] = []
                    asyncio.create_task(process_album(group_id, message.chat.title))
                album_cache[group_id].append(message)
            else:
                original_caption = message.caption or ""
                group_signature = f"📌 Enviado desde: {message.chat.title}"
                new_caption = f"{original_caption}\n\n{group_signature}" if original_caption else group_signature
                await backup_queue.put({'type': 'single', 'message': message, 'caption': new_caption})

# ================= SERVIDOR WEB FALSO PARA RENDER =================
async def handle(request):
    return web.Response(text="OTM Boss is running smoothly!")

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
    asyncio.create_task(web_server())
    asyncio.create_task(backup_worker())  
    print("🤖 OTM Boss Iniciado y corriendo en puerto 10000...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())