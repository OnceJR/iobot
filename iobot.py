import asyncio
import re
import logging
import json
import urllib.parse
from datetime import datetime, timedelta
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= CONFIGURACIÓN =================
TOKEN = "8611966815:AAE2biZEsdWl_r-k4E1EBBT0XOMqIuLEFk0"
MONGO_URI = "mongodb+srv://carlosjrpelegrina_db_user:1DNyN9AFa9bh1tCr@cluster0.haf2f1l.mongodb.net" 

# ID DEL JEFE SUPREMO (Solo tú)
DESIGNATED_USERS = {8983189714}

LINK_REGEX = re.compile(r'(https?://|www\.|t\.me/)', re.IGNORECASE)

# ================= CONEXIÓN A MONGODB =================
client = AsyncIOMotorClient(MONGO_URI)
db = client.imperio_bot
groups_col = db.groups   # Colección para configuración de grupos y limpieza
stats_col = db.stats     # Colección para estadísticas de aportes semanales
admins_col = db.admins   # Colección para el panel privado

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
    "can_restrict_members": "Restringir/Banear",
    "can_promote_members": "Añadir Administradores",
    "can_change_info": "Cambiar Info del Grupo",
    "can_invite_users": "Invitar Usuarios",
    "can_pin_messages": "Fijar Mensajes"
}

# ================= INICIALIZACIÓN =================
logging.basicConfig(level=logging.INFO)
# Usamos HTML globalmente para evitar errores con nombres raros de usuarios
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in DESIGNATED_USERS: return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except: return False

# ================= INTERFAZ PROFESIONAL =================
def get_main_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Modo Estricto", callback_data=f"close_{group_id}"), 
         InlineKeyboardButton(text="🔓 Modo Libre", callback_data=f"open_{group_id}")],
        [InlineKeyboardButton(text="👥 Permisos", callback_data=f"perms_{group_id}"), 
         InlineKeyboardButton(text="🤖 Auditoría", callback_data=f"botperms_{group_id}")],
        [InlineKeyboardButton(text="🧹 Limpieza Automática", callback_data=f"cleanmenu_{group_id}")], 
        [InlineKeyboardButton(text="📖 Manual de Uso", callback_data=f"help_{group_id}")]
    ])

def get_back_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Regresar al Menú", callback_data=f"back_{group_id}")]
    ])

def get_permissions_keyboard(group_id: int, perms: ChatPermissions) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for key, (attr, name) in PERM_MAPPING.items():
        icon = "✅" if getattr(perms, attr, False) else "❌"
        row.append(InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"tp_{group_id}_{key}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Regresar", callback_data=f"back_{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= WORKERS EN SEGUNDO PLANO =================
async def execute_cleanup(chat_id: int, manual=False):
    group = await groups_col.find_one({"_id": chat_id})
    if not group: return 0
    
    messages = group.get("media_to_delete", [])
    if not messages:
        await groups_col.update_one({"_id": chat_id}, {"$set": {"next_cleanup": datetime.now() + timedelta(hours=12)}})
        return 0
        
    count = len(messages)
    chunk_size = 100
    for i in range(0, count, chunk_size):
        chunk = messages[i:i+chunk_size]
        try: await bot.delete_messages(chat_id, chunk)
        except Exception: pass
        await asyncio.sleep(1.5) 
    
    await groups_col.update_one(
        {"_id": chat_id}, 
        {"$set": {"media_to_delete": [], "next_cleanup": datetime.now() + timedelta(hours=12)}}
    )
    
    tipo = "manual" if manual else "automática"
    try:
        msg = await bot.send_message(
            chat_id, 
            f"🛡️ <b>Mantenimiento del Grupo</b>\n\n✅ Se ha completado una limpieza <b>{tipo}</b>.\n🗑️ <b>Archivos eliminados:</b> <code>{count}</code>"
        )
        await asyncio.sleep(60)
        await msg.delete()
    except: pass
    return count

async def auto_cleanup_worker():
    while True:
        now = datetime.now()
        cursor = groups_col.find({"next_cleanup": {"$lte": now}})
        async for group in cursor:
            await execute_cleanup(group["_id"])
        await asyncio.sleep(60) 

# ================= COMANDOS DE MODERACIÓN =================
@router.message(Command("panel"))
async def link_group_panel(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id):
        await admins_col.update_one({"_id": message.from_user.id}, {"$set": {"active_group": message.chat.id}}, upsert=True)
        group = await groups_col.find_one({"_id": message.chat.id})
        if not group or "next_cleanup" not in group:
            await groups_col.update_one({"_id": message.chat.id}, {"$set": {"next_cleanup": datetime.now() + timedelta(hours=12)}}, upsert=True)
            
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🖥️ Abrir Consola", url=f"t.me/{(await bot.me()).username}?start=panel")]])
        await message.reply("🛡️ <b>Conexión Establecida.</b>\nSu panel de control está listo en el chat privado.", reply_markup=kb)

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
            c = await message.answer("🔨 <b>Sanción Ejecutada:</b> El usuario ha sido expulsado.")
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
                c = await message.answer("✅ <b>Amnistía Aprobada:</b> El usuario ha sido desbaneado.")
                await message.delete()
                await asyncio.sleep(5); await c.delete()
            except: pass

@router.message(Command("pin"))
async def pin_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"] and await is_admin(message.chat.id, message.from_user.id) and message.reply_to_message:
        try: await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id); await message.delete()
        except: pass

# ================= MÓDULO: APORTES SEMANALES =================
@router.message(Command("aportes"))
async def check_stats_cmd(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        current_week = datetime.now().strftime("%Y-W%V")
        
        user_stat = await stats_col.find_one({"_id": target.id, "week": current_week})
        count = user_stat.get("count", 0) if user_stat else 0
        
        await message.reply(f"📈 <b>Estadísticas de {target.first_name}</b>\nHa aportado <code>{count}</code> archivos multimedia <b>esta semana</b>.")

@router.message(Command("topaportes"))
async def top_stats_cmd(message: Message):
    current_week = datetime.now().strftime("%Y-W%V")
    cursor = stats_col.find({"week": current_week}).sort("count", -1).limit(10)
    top_users = await cursor.to_list(length=10)
    
    if not top_users:
        return await message.reply("📉 <b>Aún no hay aportes esta semana.</b>\n¡Anímate a compartir material!")
        
    # Crear un texto estilizado
    text = f"🏆 <b>CUADRO DE HONOR SEMANAL</b>\n<i>Semana {datetime.now().strftime('%V del %Y')}</i>\n━━━━━━━━━━━━━━━━━━\n\n"
    
    labels = []
    data_points = []
    
    for i, data in enumerate(top_users, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"<b>{i}.</b>"
        text += f"{medal} <b>{data['name']}</b> — <code>{data['count']}</code> archivos\n"
        
        # Guardar datos para el gráfico
        labels.append(data['name'][:10]) # Truncar nombres muy largos
        data_points.append(data['count'])

    text += "\n<i>¡Gracias a todos por mantener el grupo activo!</i>"
    
    # Generar URL de la imagen del gráfico (Rápido, gratis y sin consumir RAM en Render)
    chart_config = {
        "type": "horizontalBar",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Aportes Semanales",
                "data": data_points,
                "backgroundColor": "rgba(54, 162, 235, 0.8)",
                "borderColor": "rgba(54, 162, 235, 1)",
                "borderWidth": 1
            }]
        },
        "options": {
            "plugins": {
                "datalabels": {"color": "#000", "font": {"weight": "bold"}}
            },
            "legend": {"display": False},
            "title": {"display": True, "text": "Top Aportadores de la Semana", "fontSize": 18}
        }
    }
    
    # Codificamos la configuración a URL
    encoded_config = urllib.parse.quote(json.dumps(chart_config))
    chart_url = f"https://quickchart.io/chart?c={encoded_config}&w=600&h=300&bkg=white"
    
    try:
        # Enviamos la foto del gráfico con el texto en el pie de foto
        await bot.send_photo(chat_id=message.chat.id, photo=chart_url, caption=text)
    except:
        # Si falla la imagen por algún motivo, enviamos solo el texto
        await message.reply(text)

# ================= SISTEMA PRIVADO DE PANEL (DM) =================
@router.message(CommandStart())
async def start_private_panel(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await state.clear()
        admin_data = await admins_col.find_one({"_id": message.from_user.id})
        group_id = admin_data.get("active_group") if admin_data else None

        if message.from_user.id not in DESIGNATED_USERS and not group_id:
            return await message.answer("🛑 <b>Acceso Denegado:</b>\nNo posees autorización para acceder al panel de control.")

        if group_id:
            chat = await bot.get_chat(group_id)
            texto = (
                f"🛡️ <b>SISTEMA CENTRAL DE GESTIÓN</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📍 <b>Jurisdicción Actual:</b> <code>{chat.title}</code>\n\n"
                f"Seleccione el módulo que desea configurar:"
            )
            await message.answer(texto, reply_markup=get_main_keyboard(group_id))
        else: 
            await message.answer("⚠️ <b>Conexión Requerida:</b>\nPor favor, ejecuta <code>/panel</code> dentro del grupo que deseas administrar.")

@router.callback_query(F.data.startswith("back_"))
async def back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group_id = int(callback.data.split("_")[1])
    chat = await bot.get_chat(group_id)
    texto = (
        f"🛡️ <b>SISTEMA CENTRAL DE GESTIÓN</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Jurisdicción Actual:</b> <code>{chat.title}</code>\n\n"
        f"Seleccione el módulo que desea configurar:"
    )
    await callback.message.edit_text(texto, reply_markup=get_main_keyboard(group_id))

@router.callback_query(F.data.startswith("cleanmenu_"))
async def clean_menu_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    group = await groups_col.find_one({"_id": group_id})
    pending_media = len(group.get("media_to_delete", [])) if group else 0
    next_time = group.get("next_cleanup", datetime.now()) if group else datetime.now()
    
    time_left = next_time - datetime.now()
    hours, remainder = divmod(max(0, int(time_left.total_seconds())), 3600)
    minutes, _ = divmod(remainder, 60)
    
    text = (
        f"🧹 <b>MÓDULO DE LIMPIEZA</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Archivos en cola:</b> <code>{pending_media}</code>\n"
        f"⏱️ <b>Próxima ejecución:</b> <code>{hours}h {minutes}m</code>\n\n"
        f"<i>⚠️ Nota: Forzar la limpieza borrará todos los archivos y reiniciará el reloj a 12 horas.</i>"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Limpiar Inmediatamente", callback_data=f"forceclean_{group_id}")],
        [InlineKeyboardButton(text="🔙 Regresar", callback_data=f"back_{group_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data.startswith("forceclean_"))
async def force_clean_cb(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    await callback.answer("⏳ Inicializando limpieza...", show_alert=False)
    count = await execute_cleanup(group_id, manual=True)
    await callback.message.edit_text(
        f"✅ <b>Protocolo Finalizado</b>\nSe han purgado <code>{count}</code> archivos.\nEl reloj cíclico se ha restablecido.",
        reply_markup=get_back_keyboard(group_id)
    )

@router.callback_query(F.data.startswith("perms_"))
async def show_perms_cb(callback: CallbackQuery):
    g_id = int(callback.data.split("_")[1])
    try:
        chat = await bot.get_chat(g_id)
        text = (
            f"⚙️ <b>PERMISOS GLOBALES</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Toque un interruptor para habilitar o restringir funciones para todos los miembros:"
        )
        await callback.message.edit_text(text, reply_markup=get_permissions_keyboard(g_id, chat.permissions or ChatPermissions()))
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
        await callback.answer("❌ El bot necesita ser admin con permisos completos.", show_alert=True)

@router.callback_query(F.data.startswith("close_"))
async def close_chat_cb(callback: CallbackQuery):
    try:
        await bot.set_chat_permissions(int(callback.data.split("_")[1]), ChatPermissions(can_send_messages=False))
        await callback.answer("🔒 Modo Estricto Activado.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("open_"))
async def open_chat_cb(callback: CallbackQuery):
    try:
        await bot.set_chat_permissions(int(callback.data.split("_")[1]), ChatPermissions(can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_documents=True, can_send_audios=True, can_send_voice_notes=True, can_send_other_messages=True))
        await callback.answer("🔓 Modo Libre Activado.", show_alert=True)
    except: pass

@router.callback_query(F.data.startswith("botperms_"))
async def show_bot_perms_cb(callback: CallbackQuery):
    g_id = int(callback.data.split("_")[1])
    try:
        member = await bot.get_chat_member(g_id, (await bot.me()).id)
        txt = "🤖 <b>AUDITORÍA DE SISTEMA (Privilegios):</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for attr, name in ADMIN_PERMS.items():
            txt += f"{'✅' if getattr(member, attr, False) else '❌'} {name}\n"
        await callback.message.edit_text(txt, reply_markup=get_back_keyboard(g_id))
    except: pass

@router.callback_query(F.data.startswith("help_"))
async def help_cb(callback: CallbackQuery):
    texto = (
        "📖 <b>MANUAL DE OPERACIONES</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 <b>Sistema Anti-Bots:</b> Todo bot no autorizado es baneado inmediatamente.\n"
        "🔸 <b>Limpieza 12hs:</b> Todos los archivos son guardados en cola y borrados tras 12hs.\n"
        "🔸 <b>Moderar Rápido:</b> Responde a un mensaje con <code>/del</code>, <code>/ban</code>, <code>/unban</code>, <code>/pin</code>."
    )
    await callback.message.edit_text(texto, reply_markup=get_back_keyboard(int(callback.data.split("_")[1])))

# ================= NÚCLEO: GESTOR DE MENSAJES =================
@router.message(F.new_chat_members)
async def anti_bot_new_members(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        is_adder_admin = await is_admin(message.chat.id, message.from_user.id)
        for member in message.new_chat_members:
            if member.is_bot and member.id != bot.id:
                if not is_adder_admin:
                    try:
                        await bot.ban_chat_member(message.chat.id, member.id)
                        await message.reply(f"🛡️ <b>Anti-Bots:</b> El bot {member.first_name} fue expulsado. Solo admins pueden agregarlos.")
                    except: pass

@router.message()
async def group_messages_processor(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        
        # 1. Anti-Bots Activo
        if message.from_user.is_bot and message.from_user.id != bot.id:
            if not await is_admin(message.chat.id, message.from_user.id):
                try:
                    await bot.ban_chat_member(message.chat.id, message.from_user.id)
                    await message.delete()
                except: pass
                return 
        
        content = message.text or message.caption or ""
        
        # 2. Filtro Anti-links
        if content and LINK_REGEX.search(content) and not await is_admin(message.chat.id, message.from_user.id):
            try: await message.delete(); return
            except: pass
        
        # 3. Limpieza y Estadísticas Semanales
        if message.photo or message.video or message.document:
            u_id, c_id = message.from_user.id, message.chat.id
            current_week = datetime.now().strftime("%Y-W%V") # Ejemplo: 2026-W37
            
            # Guardamos para la limpieza automática
            await groups_col.update_one(
                {"_id": c_id}, 
                {
                    "$push": {"media_to_delete": message.message_id},
                    "$setOnInsert": {"next_cleanup": datetime.now() + timedelta(hours=12)}
                }, 
                upsert=True
            )

            # Actualizamos estadísticas garantizando que sean de la semana actual
            user_stat = await stats_col.find_one({"_id": u_id, "week": current_week})
            
            if user_stat:
                await stats_col.update_one({"_id": u_id, "week": current_week}, {"$inc": {"count": 1}, "$set": {"name": message.from_user.first_name}})
            else:
                # Si no hay registro esta semana, lo creamos empezando en 1
                await stats_col.update_one(
                    {"_id": u_id, "week": current_week}, 
                    {"$set": {"count": 1, "name": message.from_user.first_name}}, 
                    upsert=True
                )

# ================= RENDER Y EJECUCIÓN =================
async def handle(request): return web.Response(text="Bot is running smoothly on MongoDB!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 10000).start()

async def main():
    dp.include_router(router)
    asyncio.create_task(web_server())
    asyncio.create_task(auto_cleanup_worker()) 
    print("🛡️ Bot Iniciado: Sistema de Gestión MongoDB y Anti-Bots Activos...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())