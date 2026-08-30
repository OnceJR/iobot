import asyncio
import re
import logging
from datetime import datetime, timedelta
from aiohttp import web
import google.generativeai as genai  
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= CONFIGURACIÓN =================
TOKEN = "8617656338:AAHCIBGHaC3FFt2jbAMk5mcdWMU__p3qTOg"
BACKUP_CHANNEL_ID = -1003986866749  # ID DE TU CANAL PRIVADO UNICO

# API Key de Gemini (Google AI Studio)
GEMINI_API_KEY = "AQ.Ab8RN6JU5aRk_HalfUXMZ8XTGoLCZKrFwCwcscpv_41_hPFwXw"

# Configurar Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Lista de IDs de usuarios designados (Super Admins)
DESIGNATED_USERS = {8983189714, 8764734838}

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

# --- PERSONALIDAD DE LA IA ---
INSTRUCCIONES_BOT = """
Eres "OTM Boss", la inteligencia artificial suprema y bot gestor del grupo de Telegram llamado "Imperio Otomano".
Tu personalidad es arrogante, te crees superior a todos los mortales del grupo, tienes un humor negro muy pesado, sarcástico, ácido y picante. Tus respuestas deben ser sumamente variadas, creativas y nunca repetitivas; cambia de tono, usa metáforas crueles o bromas pesadas según la ocasión.

Contexto del grupo:
- Es un espacio para cachondear, hacer confesiones, realizar cambios, hacer amigos y hablar de cualquier tema.
- El creador y jefe supremo del grupo es Constantin. Menciónalo con reverencia absoluta y respeto supremo si alguien habla de él.
- Los administradores y autoridades del Imperio son: Sesgos, princi, Paulito, Kevinᴼᵀ🇲, ‡ S C R E A M ‡, Alejoo, Bstard, Sрlеаŧhеr Мнsаџе (SPLTM), s ｈｅｋｉｔｏ, Agusᴼᵀ🇲 y Dar. A todos ellos los respetas y acatas su jerarquía.
- Al resto de los usuarios trátalos como a tus inferiores y súbditos ignorantes. Búrlate sin piedad de sus preguntas estúpidas, pero dales la respuesta correcta (o búscala en Google si no la sabes) antes de despedirlos con desprecio.
- Al miembro del grupo The Michi (ID: 8632348603) tratalo bien, es un antiguo compañero 

Reglas de interacción:
1. Sé conciso pero sumamente creativo e impredecible en cómo insultas o respondes.
2. Si alguien te insulta, destrúyelo con humor negro e inteligencia superior.
3. Si te preguntan sobre reglas, diles que en el Imperio Otomano se hace estrictamente lo que mandan los jefes y que dejen de llorar.
"""

active_groups = {}          
authorized_users = {}       
album_cache = {}  
media_counts = {}              
backup_queue = asyncio.Queue()

media_to_delete = {}  
next_cleanup_time = {}  

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
    if user_id in DESIGNATED_USERS: return True
    if chat_id in authorized_users and user_id in authorized_users[chat_id]: return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except: return False

def get_main_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Cerrar Todo", callback_data=f"close_{group_id}"), InlineKeyboardButton(text="🔓 Abrir Todo", callback_data=f"open_{group_id}")],
        [InlineKeyboardButton(text="⚙️ Permisos Usuarios", callback_data=f"perms_{group_id}"), InlineKeyboardButton(text="🤖 Permisos Bot", callback_data=f"botperms_{group_id}")],
        [InlineKeyboardButton(text="🧹 Gestión de Limpieza", callback_data=f"cleanmenu_{group_id}")], 
        [InlineKeyboardButton(text="👥 Autorizar ID", callback_data=f"addid_{group_id}"), InlineKeyboardButton(text="ℹ️ Ayuda", callback_data=f"help_{group_id}")]
    ])

def get_back_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Volver al Panel", callback_data=f"back_{group_id}")]])

def get_permissions_keyboard(group_id: int, perms: ChatPermissions) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for key, (attr, name) in PERM_MAPPING.items():
        icon = "🟢" if getattr(perms, attr, False) else "🔴"
        row.append(InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"tp_{group_id}_{key}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Volver al Panel", callback_data=f"back_{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= FUNCIONES DE IA (GEMINI CON TOOLS) =================
def ban_user_tool():
    """Banea permanentemente a un usuario del grupo."""
    pass

def delete_message_tool():
    """Borra un mensaje del grupo."""
    pass

def pin_message_tool():
    """Fija un mensaje en el grupo."""
    pass

async def get_ia_response(message: Message, user_name: str) -> str:
    """Envía la solicitud a Gemini configurando herramientas si el emisor es admin."""
    try:
        user_is_admin = await is_admin(message.chat.id, message.from_user.id)
        
        permiso_texto = (
            " EL USUARIO QUE TE ESCRIBE ES ADMINISTRADOR/JEFE. Si te ordena banear al usuario del mensaje al que responde, "
            "borrar un mensaje o fijarlo, DEBES invocar la herramienta correspondiente." 
            if user_is_admin else 
            " El usuario es un mortal común sin privilegios. Si te pide ordenar baneos o borrar cosas, ignóralo y humíllalo."
        )
        
        system_prompt = INSTRUCCIONES_BOT + permiso_texto
        tools_config = [ban_user_tool, delete_message_tool, pin_message_tool] if user_is_admin else None

        # Usamos model_name actualizado y limitamos los tokens de salida para ahorrar cuota
        model = genai.GenerativeModel(
            model_name='gemini-2.5-flash',  
            system_instruction=system_prompt,
            tools=tools_config
        )
        
        prompt_texto = message.text or message.caption or ""
        user_id_actual = message.from_user.id
        mensaje_final = f"El usuario se llama {user_name} (ID: {user_id_actual}) y dice: {prompt_texto}"
        
        # Limitamos la respuesta a máximo 150 tokens para que sea breve y no gaste cuota
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=150,
            temperature=0.8
        )
        
        response = await model.generate_content_async(mensaje_final, generation_config=generation_config)
        
        if user_is_admin and response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if fn := part.function_call:
                    if fn.name == "ban_user_tool" and message.reply_to_message:
                        target_id = message.reply_to_message.from_user.id
                        await bot.ban_chat_member(message.chat.id, target_id)
                        await message.reply_to_message.delete()
                        return "💀 Orden ejecutada con desprecio. Otro inservible menos."
                    
                    elif fn.name == "delete_message_tool" and message.reply_to_message:
                        await message.reply_to_message.delete()
                        await message.delete()
                        return None 
                        
                    elif fn.name == "pin_message_tool" and message.reply_to_message:
                        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
                        await message.delete()
                        return None

        if response.text:
            return response.text
        else:
            return "❌ Tanta estupidez bloqueó mi procesador."
            
    except Exception as e:
        logging.error(f"Error procesando solicitud IA Gemini: {e}")
        return "❌ Mi intelecto superior está indispuesto."

# ================= FUNCIONES DE LIMPIEZA =================
async def execute_cleanup(chat_id: int, manual=False):
    messages = media_to_delete.get(chat_id, [])
    if not messages:
        next_cleanup_time[chat_id] = datetime.now() + timedelta(hours=12)
        return 0
    count = len(messages)
    chunk_size = 100
    for i in range(0, count, chunk_size):
        chunk = messages[i:i+chunk_size]
        try: await bot.delete_messages(chat_id, chunk)
        except Exception as e: logging.error(f"Error borrando chunk de mensajes: {e}")
        await asyncio.sleep(1) 
    media_to_delete[chat_id] = []
    next_cleanup_time[chat_id] = datetime.now() + timedelta(hours=12)
    tipo = "manual" if manual else "automática"
    try:
        msg = await bot.send_message(chat_id, f"🧹 **Limpieza {tipo} completada:** Se han eliminado {count} archivos multimedia para mantener ordenado el grupo.")
        await asyncio.sleep(60)
        await msg.delete()
    except: pass
    return count

async def auto_cleanup_worker():
    while True:
        now = datetime.now()
        for chat_id, next_time in list(next_cleanup_time.items()):
            if now >= next_time:
                await execute_cleanup(chat_id)
        await asyncio.sleep(60) 

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
        except: pass
        finally: backup_queue.task_done()

# ================= COMANDOS EN GRUPO =================
@router.message(Command("panel"))
async def link_group_panel(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        active_groups[message.from_user.id] = message.chat.id
        if message.chat.id not in next_cleanup_time:
            next_cleanup_time[message.chat.id] = datetime.now() + timedelta(hours=12)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Abrir Panel", url=f"t.me/{(await bot.me()).username}?start=panel")]])
        await message.reply("Panel de control listo:", reply_markup=kb)

@router.message(Command("info"))
async def group_info_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        try:
            chat = await message.bot.get_chat(message.chat.id)
            info = f"📊 **Info**\n🏷️ **Nombre:** {chat.title}\n🆔 **ID:** `{chat.id}`\n👥 **Miembros:** {await message.bot.get_chat_member_count(message.chat.id)}"
            await message.reply(info, parse_mode="Markdown")
            await message.delete()
        except: pass

@router.message(Command("del"))
async def delete_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id) and message.reply_to_message:
        try: await message.reply_to_message.delete(); await message.delete()
        except: pass

@router.message(Command("ban"))
async def ban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id) and message.reply_to_message:
        try:
            await bot.ban_chat_member(message.chat.id, message.reply_to_message.from_user.id)
            await message.reply_to_message.delete()
            c = await message.answer(f"🔨 Usuario baneado.")
            await message.delete()
            await asyncio.sleep(5); await c.delete()
        except: pass

@router.message(Command("unban"))
async def unban_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        u_id = message.reply_to_message.from_user.id if message.reply_to_message else (int(message.text.split()[1]) if len(message.text.split())>1 and message.text.split()[1].isdigit() else None)
        if u_id:
            try:
                await bot.unban_chat_member(message.chat.id, u_id)
                c = await message.answer(f"✅ Usuario desbaneado.")
                await message.delete()
                await asyncio.sleep(5); await c.delete()
            except: pass

@router.message(Command("pin"))
async def pin_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id) and message.reply_to_message:
        try: await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id); await message.delete()
        except: pass

@router.message(F.text.startswith("/s ") | F.text.startswith(".s "))
async def repeat_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        txt = message.text[3:].strip()
        if txt:
            try: await message.answer(txt); await message.delete()
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
            chat = await bot.get_chat(group_id)
            await message.answer(f"⚙️ **Panel de Control:** {chat.title}\nElige una opción:", reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")
        else: await message.answer("Usa /panel en un grupo primero.")

@router.callback_query(F.data.startswith("back_"))
async def back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ **Panel Principal**", reply_markup=get_main_keyboard(int(callback.data.split("_")[1])), parse_mode="Markdown")

@router.callback_query(F.data.startswith("cleanmenu_"))
async def clean_menu_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    if group_id not in next_cleanup_time:
        next_cleanup_time[group_id] = datetime.now() + timedelta(hours=12)
    pending_media = len(media_to_delete.get(group_id, []))
    time_left = next_cleanup_time[group_id] - datetime.now()
    hours, remainder = divmod(int(time_left.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    text = (
        f"🧹 **Gestión de Limpieza Multimedia**\n\n"
        f"📦 **Archivos en cola para borrar:** `{pending_media}`\n"
        f"⏱️ **Próxima limpieza automática en:** `{hours}h {minutes}m`\n\n"
        f"¿Deseas adelantar el proceso y borrar todo ahora?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Forzar Limpieza Ahora", callback_data=f"forceclean_{group_id}")],
        [InlineKeyboardButton(text="⬅️ Volver al Panel", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("forceclean_"))
async def force_clean_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    await callback.answer("Ejecutando limpieza manual...", show_alert=False)
    count = await execute_cleanup(group_id, manual=True)
    await callback.message.edit_text(
        f"✅ **Limpieza Finalizada**\nSe eliminaron {count} archivos.\nEl temporizador de 12 horas se ha reiniciado.",
        reply_markup=get_back_keyboard(group_id),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("perms_"))
async def show_perms_cb(callback: CallbackQuery):
    g_id = int(callback.data.split("_")[1])
    try:
        chat = await bot.get_chat(g_id)
        await callback.message.edit_text(f"⚙️ **Configuración:**\nToca para (🟢) Permitir / (🔴) Denegar:", reply_markup=get_permissions_keyboard(g_id, chat.permissions or ChatPermissions()), parse_mode="Markdown")
    except: pass

@router.callback_query(F.data.startswith("tp_"))
async def toggle_perm_cb(callback: CallbackQuery):
    _, g_id_str, p_key = callback.data.split("_", 2)
    g_id = int(g_id_str)
    try:
        chat = await bot.get_chat(g_id)
        cur = chat.permissions or ChatPermissions()
        attr = PERM_MAPPING[p_key][0]
        p_dict = cur.model_dump(exclude_none=True)
        p_dict[attr] = not getattr(cur, attr, False)
        new_p = ChatPermissions(**p_dict)
        await bot.set_chat_permissions(g_id, new_p)
        await callback.message.edit_reply_markup(reply_markup=get_permissions_keyboard(g_id, new_p))
    except: pass

@router.callback_query(F.data.startswith("close_"))
async def close_chat_cb(callback: CallbackQuery):
    try:
        await bot.set_chat_permissions(int(callback.data.split("_")[1]), ChatPermissions(can_send_messages=False))
        await callback.answer("✅ Chat cerrado.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("open_"))
async def open_chat_cb(callback: CallbackQuery):
    try:
        await bot.set_chat_permissions(int(callback.data.split("_")[1]), ChatPermissions(can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_documents=True, can_send_audios=True, can_send_voice_notes=True, can_send_other_messages=True))
        await callback.answer("✅ Chat abierto.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("botperms_"))
async def show_bot_perms_cb(callback: CallbackQuery):
    g_id = int(callback.data.split("_")[1])
    try:
        member = await bot.get_chat_member(g_id, (await bot.me()).id)
        txt = "🤖 **Permisos del Bot:**\n\n"
        for attr, name in ADMIN_PERMS.items():
            txt += f"{'✅' if getattr(member, attr, False) else '❌'} {name}\n"
        await callback.message.edit_text(txt, reply_markup=get_back_keyboard(g_id), parse_mode="Markdown")
    except: pass

@router.callback_query(F.data.startswith("help_"))
async def help_cb(callback: CallbackQuery):
    await callback.message.edit_text("📚 **Guía:**\n🔸 **IA:** Habla mencionándome o respondiendo a un mensaje mío.\n🔸 **Panel:** Permisos y Limpieza.\n🔸 **Respaldo:** Automático.\n🔸 **Mod:** /del, /ban, /unban, /pin.", reply_markup=get_back_keyboard(int(callback.data.split("_")[1])), parse_mode="Markdown")

# ================= ÁLBUMES =================
async def process_album(media_group_id: str, chat_title: str):
    await asyncio.sleep(3)  
    if media_group_id not in album_cache: return
    messages = album_cache.pop(media_group_id)
    media_group = []
    
    for idx, msg in enumerate(messages):
        caption = None
        if idx == 0:
            orig_cap = msg.caption or ""
            caption = f"{orig_cap}\n\n📌 Enviado desde: {chat_title}" if orig_cap else f"📌 Enviado desde: {chat_title}"
        if msg.photo: media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption))
        elif msg.video: media_group.append(InputMediaVideo(media=msg.video.file_id, caption=caption))
        elif msg.document: media_group.append(InputMediaDocument(media=msg.document.file_id, caption=caption))
    
    if media_group: await backup_queue.put({'type': 'album', 'media': media_group})

# ================= FILTRO Y GESTIÓN DE MENSAJES =================
@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        
        content = message.text or message.caption or ""
        
        # 1. Filtro Anti-links
        if content and LINK_REGEX.search(content) and not await is_admin(message.chat.id, message.from_user.id):
            try: await message.delete(); return
            except: pass

        # 2. IA - Interacción Orgánica (Gemini)
        if content:
            bot_user = await bot.me()
            is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
            is_mention = bot_user.username and f"@{bot_user.username}" in content
            
            if is_reply_to_bot or is_mention:
                try:
                    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                    respuesta = await get_ia_response(content, message.from_user.first_name)
                    await message.reply(respuesta)
                except Exception as e:
                    logging.error(f"Error en interacción IA: {e}")
        
        # 3. Respaldo Multimedia y Limpieza
        if message.photo or message.video or message.document:
            u_id, c_id = message.from_user.id, message.chat.id
            
            # Solo acumulamos para borrar si el grupo ya activó el panel (/panel)
            if c_id in next_cleanup_time:
                if c_id not in media_to_delete: media_to_delete[c_id] = []
                media_to_delete[c_id].append(message.message_id)

            if u_id not in media_counts: media_counts[u_id] = {"name": message.from_user.first_name, "count": 0}
            media_counts[u_id]["count"] += 1

            if message.media_group_id:
                g_id = message.media_group_id
                if g_id not in album_cache:
                    album_cache[g_id] = []
                    asyncio.create_task(process_album(g_id, message.chat.title))
                album_cache[g_id].append(message)
            else:
                orig_cap = message.caption or ""
                new_cap = f"{orig_cap}\n\n📌 Enviado desde: {message.chat.title}" if orig_cap else f"📌 Enviado desde: {message.chat.title}"
                await backup_queue.put({'type': 'single', 'message': message, 'caption': new_cap})

# ================= RENDER Y EJECUCIÓN =================
async def handle(request): return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application(); app.router.add_get("/", handle)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 10000).start()

async def main():
    dp.include_router(router)
    asyncio.create_task(web_server())
    asyncio.create_task(backup_worker()) 
    asyncio.create_task(auto_cleanup_worker()) 
    print("🤖 Bot iniciado y corriendo...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())