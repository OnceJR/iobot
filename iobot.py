import asyncio
import re
import logging
from datetime import datetime, timedelta
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

# ================= CONFIGURACIÓN =================
TOKEN = "8617656338:AAHCIBGHaC3FFt2jbAMk5mcdWMU__p3qTOg"
BACKUP_CHANNEL_ID = -1003986866749  # ID DE TU CANAL PRIVADO UNICO

# ID DEL JEFE SUPREMO (Solo tú. Los demás se agregan desde el panel)
DESIGNATED_USERS = {8983189714}

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

active_groups = {}          
authorized_users = {}       
album_cache = {}  
media_counts = {}              

# Colas de tareas asíncronas (Antiflood)
backup_queue = asyncio.Queue()
admin_notifier_queue = asyncio.Queue()

media_to_delete = {}  
next_cleanup_time = {}  

# ================= DICCIONARIOS DE PERMISOS =================
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

# ================= INTERFAZ PROFESIONAL (TECLADOS) =================
def get_main_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Modo Estricto (Cerrar)", callback_data=f"close_{group_id}"), 
         InlineKeyboardButton(text="🔓 Modo Libre (Abrir)", callback_data=f"open_{group_id}")],
        [InlineKeyboardButton(text="👥 Gestionar Permisos", callback_data=f"perms_{group_id}"), 
         InlineKeyboardButton(text="🤖 Auditoría de Bot", callback_data=f"botperms_{group_id}")],
        [InlineKeyboardButton(text="🧹 Panel de Limpieza Automática", callback_data=f"cleanmenu_{group_id}")], 
        [InlineKeyboardButton(text="🔑 Autorizar Staff", callback_data=f"addid_{group_id}"), 
         InlineKeyboardButton(text="📖 Manual de Uso", callback_data=f"help_{group_id}")]
    ])

def get_back_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Regresar al Menú Principal", callback_data=f"back_{group_id}")]
    ])

def get_permissions_keyboard(group_id: int, perms: ChatPermissions) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for key, (attr, name) in PERM_MAPPING.items():
        icon = "✅" if getattr(perms, attr, False) else "❌"
        row.append(InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"tp_{group_id}_{key}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Regresar al Menú Principal", callback_data=f"back_{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= WORKERS EN SEGUNDO PLANO =================

# 1. Notificador de Admins (El Espejo Multimedia)
async def admin_notifier_worker():
    """Reenvía únicamente fotos, videos y documentos a ti y al staff autorizado."""
    while True:
        msg = await admin_notifier_queue.get()
        group_id = msg.chat.id
        
        receivers = set(DESIGNATED_USERS)
        if group_id in authorized_users:
            receivers.update(authorized_users[group_id])
            
        for admin_id in receivers:
            try:
                await msg.forward(chat_id=admin_id)
                await asyncio.sleep(0.5)  
            except Exception: 
                pass  
                
        admin_notifier_queue.task_done()

# 2. Respaldo al Canal Privado
async def backup_worker():
    """Sube fotos y videos al canal de respaldo de forma segura."""
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

# 3. Limpieza Automática de 12 horas
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
        except Exception: pass
        await asyncio.sleep(1) 
    
    media_to_delete[chat_id] = []
    next_cleanup_time[chat_id] = datetime.now() + timedelta(hours=12)
    tipo = "manual" if manual else "automática"
    
    try:
        msg = await bot.send_message(
            chat_id, 
            f"🛡️ **Mantenimiento del Grupo**\n\n✅ Se ha completado una limpieza {tipo}.\n🗑️ **Archivos eliminados:** `{count}`"
        )
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

# ================= COMANDOS DE MODERACIÓN (GRUPO) =================
@router.message(Command("panel"))
async def link_group_panel(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        active_groups[message.from_user.id] = message.chat.id
        if message.chat.id not in next_cleanup_time:
            next_cleanup_time[message.chat.id] = datetime.now() + timedelta(hours=12)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🖥️ Abrir Consola de Mando", url=f"t.me/{(await bot.me()).username}?start=panel")]])
        await message.reply("🛡️ **Conexión Establecida.**\nSu panel de control está listo para ser utilizado en el chat privado.", reply_markup=kb, parse_mode="Markdown")

@router.message(Command("info"))
async def group_info_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        try:
            chat = await message.bot.get_chat(message.chat.id)
            info = (
                f"🏛️ **Expediente del Grupo**\n\n"
                f"🔹 **Nombre:** {chat.title}\n"
                f"🔹 **ID Interno:** `{chat.id}`\n"
                f"🔹 **Población:** {await message.bot.get_chat_member_count(message.chat.id)} miembros"
            )
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
            c = await message.answer("🔨 **Sanción Ejecutada:** El usuario ha sido expulsado permanentemente.", parse_mode="Markdown")
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
                c = await message.answer("✅ **Amnistía Aprobada:** El usuario ha sido desbaneado con éxito.", parse_mode="Markdown")
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
        await message.reply(f"📈 **Estadísticas de Aportes:**\n👤 {target.first_name} ha compartido `{data['count']}` archivos multimedia.", parse_mode="Markdown")

@router.message(Command("topaportes"))
async def top_stats_cmd(message: Message):
    if not media_counts:
        return await message.reply("📉 Aún no hay registros de aportes en esta sesión.", parse_mode="Markdown")
    sorted_counts = sorted(media_counts.values(), key=lambda x: x["count"], reverse=True)[:10]
    text = "🏆 **Cuadro de Honor - Top 10 Aportadores:**\n\n"
    for i, data in enumerate(sorted_counts, 1):
        text += f"**{i}.** {data['name']} — `{data['count']}` archivos\n"
    await message.reply(text, parse_mode="Markdown")

# ================= SISTEMA PRIVADO DE PANEL (DM) =================
@router.message(CommandStart())
async def start_private_panel(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await state.clear()
        
        if message.from_user.id not in DESIGNATED_USERS and message.from_user.id not in active_groups:
            return await message.answer("Hola. Soy el sistema de gestión del Imperio Otomano.\n*No tienes autorización para acceder al panel de control.*", parse_mode="Markdown")

        group_id = active_groups.get(message.from_user.id)
        if group_id:
            chat = await bot.get_chat(group_id)
            texto = (
                f"🛡️ **SISTEMA CENTRAL DE GESTIÓN**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📍 **Jurisdicción Actual:** {chat.title}\n\n"
                f"Seleccione el módulo que desea configurar:"
            )
            await message.answer(texto, reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")
        else: 
            await message.answer("⚠️ **Conexión Requerida:**\nPor favor, ejecuta `/panel` dentro del grupo que deseas administrar para sincronizar la base de datos.", parse_mode="Markdown")

@router.callback_query(F.data.startswith("back_"))
async def back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group_id = int(callback.data.split("_")[1])
    chat = await bot.get_chat(group_id)
    texto = (
        f"🛡️ **SISTEMA CENTRAL DE GESTIÓN**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 **Jurisdicción Actual:** {chat.title}\n\n"
        f"Seleccione el módulo que desea configurar:"
    )
    await callback.message.edit_text(texto, reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")

# --- MÓDULO AUTORIZAR STAFF ---
@router.callback_query(F.data.startswith("addid_"))
async def addid_cb(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[1])
    await state.set_state(BotStates.waiting_for_id)
    await state.update_data(group_id=group_id, panel_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "✍️ **Envía el ID numérico del usuario a autorizar.**\n\n_El usuario obtendrá acceso a los comandos y empezará a recibir copias espejo de la multimedia._", 
        reply_markup=get_back_keyboard(group_id), 
        parse_mode="Markdown"
    )

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
        
        texto_exito = (
            f"✅ **Personal Autorizado**\n"
            f"El ID `{new_id}` ha sido añadido al Staff temporal.\n"
            f"Ahora podrá usar moderación y el Sistema Espejo le reenviará los archivos multimedia."
        )
        await bot.edit_message_text(texto_exito, chat_id=message.chat.id, message_id=panel_msg_id, reply_markup=get_main_keyboard(group_id), parse_mode="Markdown")
    except ValueError: pass
    finally: await state.clear()

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
        f"🧹 **MÓDULO DE LIMPIEZA MULTIMEDIA**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Archivos en cola para borrar:** `{pending_media}`\n"
        f"⏱️ **Próxima ejecución automática:** `{hours}h {minutes}m`\n\n"
        f"⚠️ *Nota: Forzar la limpieza borrará inmediatamente los archivos acumulados y reiniciará el temporizador de 12 horas.*"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Ejecutar Limpieza Inmediata", callback_data=f"forceclean_{group_id}")],
        [InlineKeyboardButton(text="🔙 Regresar al Menú Principal", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("forceclean_"))
async def force_clean_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    await callback.answer("⏳ Inicializando protocolo de limpieza...", show_alert=False)
    count = await execute_cleanup(group_id, manual=True)
    await callback.message.edit_text(
        f"✅ **Protocolo Finalizado Exitosamente**\nSe han purgado `{count}` archivos del servidor.\nEl reloj cíclico se ha restablecido a 12 horas.",
        reply_markup=get_back_keyboard(group_id),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("perms_"))
async def show_perms_cb(callback: CallbackQuery):
    g_id = int(callback.data.split("_")[1])
    try:
        chat = await bot.get_chat(g_id)
        text = (
            f"⚙️ **MÓDULO DE PERMISOS GLOBALES**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Toque un interruptor para habilitar (✅) o restringir (❌) la función para todos los miembros del grupo:"
        )
        await callback.message.edit_text(text, reply_markup=get_permissions_keyboard(g_id, chat.permissions or ChatPermissions()), parse_mode="Markdown")
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
    except: 
        await callback.answer("❌ Error: Verifica que el bot sea administrador con permisos de restricción.", show_alert=True)

@router.callback_query(F.data.startswith("close_"))
async def close_chat_cb(callback: CallbackQuery):
    try:
        await bot.set_chat_permissions(int(callback.data.split("_")[1]), ChatPermissions(can_send_messages=False))
        await callback.answer("🔒 Modo Estricto Activado: Grupo Silenciado.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("open_"))
async def open_chat_cb(callback: CallbackQuery):
    try:
        await bot.set_chat_permissions(int(callback.data.split("_")[1]), ChatPermissions(can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_documents=True, can_send_audios=True, can_send_voice_notes=True, can_send_other_messages=True))
        await callback.answer("🔓 Modo Libre Activado: Grupo Abierto.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("botperms_"))
async def show_bot_perms_cb(callback: CallbackQuery):
    g_id = int(callback.data.split("_")[1])
    try:
        member = await bot.get_chat_member(g_id, (await bot.me()).id)
        txt = "🤖 **AUDITORÍA DE SISTEMA (Privilegios del Bot):**\n━━━━━━━━━━━━━━━━━━\n\n"
        for attr, name in ADMIN_PERMS.items():
            txt += f"{'✅' if getattr(member, attr, False) else '❌'} {name}\n"
        await callback.message.edit_text(txt, reply_markup=get_back_keyboard(g_id), parse_mode="Markdown")
    except: pass

@router.callback_query(F.data.startswith("help_"))
async def help_cb(callback: CallbackQuery):
    texto = (
        "📖 **MANUAL DE OPERACIONES**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 **Sistema Espejo Multimedia:** Solo las fotos, videos y documentos enviados al grupo se reenvían al chat privado del staff.\n"
        "🔸 **Panel Interactivo:** Administra bloqueos, aperturas y permisos globales al instante.\n"
        "🔸 **Respaldo:** Todo archivo multimedia se copia automáticamente al canal bóveda.\n"
        "🔸 **Comandos Administrativos:** Responde a un mensaje con `/del`, `/ban`, `/unban`, `/pin` para moderación rápida."
    )
    await callback.message.edit_text(texto, reply_markup=get_back_keyboard(int(callback.data.split("_")[1])), parse_mode="Markdown")

# ================= AGRUPACIÓN DE ÁLBUMES =================
async def process_album(media_group_id: str, chat_title: str):
    await asyncio.sleep(3)  
    if media_group_id not in album_cache: return
    messages = album_cache.pop(media_group_id)
    media_group = []
    
    for idx, msg in enumerate(messages):
        caption = None
        if idx == 0:
            orig_cap = msg.caption or ""
            caption = f"{orig_cap}\n\n📌 *Respaldo | {chat_title}*" if orig_cap else f"📌 *Respaldo | {chat_title}*"
        if msg.photo: media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption, parse_mode="Markdown"))
        elif msg.video: media_group.append(InputMediaVideo(media=msg.video.file_id, caption=caption, parse_mode="Markdown"))
        elif msg.document: media_group.append(InputMediaDocument(media=msg.document.file_id, caption=caption, parse_mode="Markdown"))
    
    if media_group: await backup_queue.put({'type': 'album', 'media': media_group})

# ================= NÚCLEO: GESTOR DE MENSAJES =================
@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        
        content = message.text or message.caption or ""
        
        # 1. Filtro Anti-links
        if content and LINK_REGEX.search(content) and not await is_admin(message.chat.id, message.from_user.id):
            try: await message.delete(); return
            except: pass
        
        # 2. Respaldo Multimedia, Conteo y Espejo (Estrictamente Fotos, Videos y Documentos)
        if message.photo or message.video or message.document:
            u_id, c_id = message.from_user.id, message.chat.id
            
            # 🔄 SISTEMA ESPEJO MULTIMEDIA: Enviar a admins solo si es multimedia
            await admin_notifier_queue.put(message)
            
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
                new_cap = f"{orig_cap}\n\n📌 *Respaldo | {message.chat.title}*" if orig_cap else f"📌 *Respaldo | {message.chat.title}*"
                await backup_queue.put({'type': 'single', 'message': message, 'caption': new_cap})

# ================= RENDER Y EJECUCIÓN =================
async def handle(request): return web.Response(text="Bot of Imperio Otomano is running smoothly!")

async def web_server():
    app = web.Application(); app.router.add_get("/", handle)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 10000).start()

async def main():
    dp.include_router(router)
    asyncio.create_task(web_server())
    asyncio.create_task(backup_worker()) 
    asyncio.create_task(auto_cleanup_worker()) 
    asyncio.create_task(admin_notifier_worker()) 
    print("🛡️ Bot Iniciado: Espejo Multimedia Exclusivo Activo...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())