import os
import time
import random
import string
import asyncio
import aiohttp
import urllib.parse
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from pyrogram.errors import UserNotParticipant, FloodWait
from motor.motor_asyncio import AsyncIOMotorClient

# ================= কনফিগারেশন =================
API_ID = int(os.environ.get("API_ID", "আপনার_এপিআই_আইডি_এখানে"))
API_HASH = os.environ.get("API_HASH", "আপনার_এপিআই_হ্যাশ_এখানে")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "আপনার_বট_টোকেন")
MONGO_URI = os.environ.get("MONGO_URI", "আপনার_মঙ্গোডিবি_ইউআরএল")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "আপনার_টেলিগ্রাম_আইডি"))
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "-100আপনার_ডাটাবেস_চ্যানেল_আইডি"))

# ডাটাবেস সেটআপ
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["UltimateBot"]
users_db = db["users"]
settings_db = db["settings"]
files_db = db["files"]
redeem_db = db["redeem_codes"]
tokens_db = db["tokens"]
channels_db = db["channels"] # নতুন চ্যানেল স্টোর করার ডাটাবেস

app = Client("MyBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= হেল্পার ফাংশন =================
async def get_settings():
    setting = await settings_db.find_one({"_id": "config"})
    if not setting:
        default = {
            "_id": "config", "links": {}, "force_subs": [], 
            "shorteners": {}, "batch_size": 1, "delete_time": 0, "locked": False
        }
        await settings_db.insert_one(default)
        return default
    return setting

async def check_force_sub(client, user_id):
    settings = await get_settings()
    channels = settings.get("force_subs", [])
    for channel in channels:
        try:
            await client.get_chat_member(channel, user_id)
        except UserNotParticipant:
            return False
        except Exception:
            pass
    return True

async def auto_delete(message, delay):
    if delay > 0:
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except:
            pass

def parse_time(time_str):
    unit = time_str[-1].lower()
    val = int(time_str[:-1])
    if unit == 'm': return val * 60
    if unit == 'h': return val * 3600
    if unit == 'd': return val * 86400
    return val

async def get_short_link(long_url):
    settings = await get_settings()
    shorteners = settings.get("shorteners", {})
    if not shorteners: return long_url
    
    shortener = list(shorteners.values())[0] 
    domain, token = shortener["domain"], shortener["token"]
    encoded_url = urllib.parse.quote(long_url)
    api_url = f"https://{domain}/api?api={token}&url={encoded_url}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    return data.get("shortenedUrl")
    except:
        pass
    return long_url

async def send_file(client, chat_id, file_data):
    settings = await get_settings()
    protect = settings.get("locked", False)
    del_time = settings.get("delete_time", 0)
    
    msg = await client.copy_message(
        chat_id=chat_id, from_chat_id=DB_CHANNEL_ID, 
        message_id=file_data["msg_id"], protect_content=protect
    )
    if del_time > 0:
        asyncio.create_task(auto_delete(msg, del_time))
        warn_msg = await client.send_message(chat_id, f"⚠️ ফাইলটি {del_time} সেকেন্ড পর ডিলিট হবে।")
        asyncio.create_task(auto_delete(warn_msg, del_time))

async def send_serial_files(client, user_id, chat_id, start_idx, batch_size):
    cursor = files_db.find({"source_chat": chat_id}).sort("original_msg_id", 1).skip(start_idx).limit(batch_size)
    files = await cursor.to_list(length=batch_size)
    for f in files:
        await send_file(client, user_id, f)
    
    # প্রগ্রেস আপডেট
    new_progress = start_idx + len(files)
    await users_db.update_one({"_id": user_id}, {"$set": {f"progress.{chat_id}": new_progress}})


# ================= স্টার্ট এবং ভেরিফিকেশন =================

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user = message.from_user
    settings = await get_settings()
    
    if not await check_force_sub(client, user.id):
        buttons = []
        for ch in settings.get("force_subs", []):
            try:
                chat_info = await client.get_chat(ch)
                link = chat_info.invite_link or f"https://t.me/{chat_info.username}"
                buttons.append([InlineKeyboardButton(f"Join {chat_info.title}", url=link)])
            except:
                pass
        buttons.append([InlineKeyboardButton("🔄 Joined", callback_data="check_joined")])
        return await message.reply("❌ ফাইল পেতে হলে আমাদের চ্যানেলে জয়েন করুন!", reply_markup=InlineKeyboardMarkup(buttons))

    user_data = await users_db.find_one({"_id": user.id})
    if not user_data:
        user_data = {"_id": user.id, "name": user.first_name, "premium_until": 0, "progress": {}}
        await users_db.insert_one(user_data)

    is_premium = user_data.get("premium_until", 0) > time.time()

    # Payload Check (Single File or Serial verification)
    if len(message.command) > 1:
        payload = message.command[1]
        
        if payload.startswith("verify_"):
            token_data = await tokens_db.find_one({"_id": payload})
            if not token_data or token_data["user_id"] != user.id:
                return await message.reply("❌ লিংকটি ইনভ্যালিড বা মেয়াদ উত্তীর্ণ হয়েছে।")
            
            if token_data.get("action") == "serial":
                await send_serial_files(client, user.id, token_data["chat_id"], token_data["idx"], settings.get("batch_size", 1))
            elif token_data.get("action") == "single":
                file_data = await files_db.find_one({"_id": token_data["file_id"]})
                await send_file(client, user.id, file_data)
                
            await tokens_db.delete_one({"_id": payload})
            return

        elif payload.startswith("file_"):
            file_data = await files_db.find_one({"_id": payload})
            if not file_data: return await message.reply("❌ ফাইল পাওয়া যায়নি!")
            
            if not is_premium and settings.get("shorteners"):
                v_token = "verify_" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
                await tokens_db.insert_one({"_id": v_token, "user_id": user.id, "action": "single", "file_id": payload})
                short_url = await get_short_link(f"https://t.me/{client.me.username}?start={v_token}")
                btn = [[InlineKeyboardButton("🔓 লিংকে ক্লিক করে আনলক করুন", url=short_url)]]
                return await message.reply("⚠️ **ফাইলটি লক করা আছে!**", reply_markup=InlineKeyboardMarkup(btn))
            
            await send_file(client, user.id, file_data)
            return

    # Start Menu Interface
    premium_text = f"হ্যাঁ [Valid till: {datetime.fromtimestamp(user_data['premium_until']).strftime('%Y-%m-%d %H:%M')}]" if is_premium else "না (Regular)"
    
    text = (f"👋 হ্যালো {user.first_name}\n\n"
            f"🔹 **আইডি:** `{user.id}`\n"
            f"🔹 **প্রিমিয়াম:** {premium_text}\n\n"
            f"👇 নিচের বাটন থেকে ভিডিও/ফাইল সিলেক্ট করুন:")

    buttons = []
    
    # Serial Channel Buttons (/addcmd)
    async for ch in channels_db.find():
        buttons.append([InlineKeyboardButton(ch["title"], callback_data=f"serial_{ch['_id']}")])
        
    # URL Links (/addlink)
    for name, url in settings.get("links", {}).items():
        buttons.append([InlineKeyboardButton(name, url=url)])
    
    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

@app.on_callback_query(filters.regex("check_joined"))
async def check_joined_cb(client, query):
    if await check_force_sub(client, query.from_user.id):
        await query.message.delete()
        await start_cmd(client, query.message)
    else:
        await query.answer("আপনি এখনো জয়েন করেননি!", show_alert=True)


# ================= সিরিয়াল চ্যানেল বাটন লজিক =================

@app.on_callback_query(filters.regex(r"^serial_(.*)"))
async def serial_callback(client, query):
    chat_id = int(query.data.split("serial_")[1])
    user_id = query.from_user.id
    settings = await get_settings()
    batch_size = settings.get("batch_size", 1)
    
    if batch_size == 0:
        return await query.answer("অ্যাডমিন ফাইল দেওয়া বন্ধ রেখেছেন।", show_alert=True)
    
    user_data = await users_db.find_one({"_id": user_id})
    progress = user_data.get("progress", {}).get(str(chat_id), 0)
    
    total_files = await files_db.count_documents({"source_chat": chat_id})
    if total_files == 0:
        return await query.answer("এই চ্যানেলে এখনো কোনো ফাইল নেই!", show_alert=True)
    
    # ফাইল শেষ হলে লুপ (পুনরায় প্রথম থেকে শুরু)
    if progress >= total_files:
        progress = 0 
        await query.message.reply("✅ আপনি এই সিরিজের সকল ফাইল দেখেছেন! আবার প্রথম থেকে দেওয়া শুরু হচ্ছে...")
        
    is_premium = user_data.get("premium_until", 0) > time.time()
    
    # শর্টনার চেক
    if not is_premium and settings.get("shorteners"):
        v_token = "verify_" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
        await tokens_db.insert_one({"_id": v_token, "user_id": user_id, "action": "serial", "chat_id": chat_id, "idx": progress})
        short_url = await get_short_link(f"https://t.me/{client.me.username}?start={v_token}")
        btn = [[InlineKeyboardButton("🔓 লিংকে ক্লিক করে পরের পর্বগুলো দেখুন", url=short_url)]]
        await query.message.reply("⚠️ **ভিডিও লক করা আছে!**\n\nনিচের বাটনে ক্লিক করে স্কিপ করলে সিরিয়াল অনুযায়ী ভিডিও পাবেন।", reply_markup=InlineKeyboardMarkup(btn))
        await query.answer()
    else:
        await send_serial_files(client, user_id, chat_id, progress, batch_size)
        await query.answer("ফাইল পাঠানো হচ্ছে...")


# ================= স্কিপ সিস্টেম (নির্দিষ্ট ফাইল থেকে শুরু) =================

@app.on_message(filters.command("skip") & filters.private)
async def skip_cmd(client, message):
    channels = await channels_db.find().to_list(length=None)
    if not channels:
        return await message.reply("কোনো চ্যানেল এড করা নেই।")
    
    buttons = [[InlineKeyboardButton(ch["title"], callback_data=f"skipchan_{ch['_id']}")] for ch in channels]
    await message.reply("কোন চ্যানেলের ফাইল স্কিপ করতে চান?", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^skipchan_(.*)"))
async def skip_chan_cb(client, query):
    chat_id = int(query.data.split("skipchan_")[1])
    total = await files_db.count_documents({"source_chat": chat_id})
    user_data = await users_db.find_one({"_id": query.from_user.id})
    current = user_data.get("progress", {}).get(str(chat_id), 0) + 1
    
    if total == 0: return await query.answer("চ্যানেলে কোনো ফাইল নেই!", show_alert=True)
    
    msg = await query.message.reply(
        f"আপনি বর্তমানে **{current}** নাম্বার ফাইলে আছেন। (মোট ফাইল: {total})\n\n"
        f"কত নাম্বার ফাইল থেকে দেখতে চান তা **এই মেসেজে রিপ্লাই দিয়ে** নাম্বারটি লিখুন। (যেমন: `15`)",
        reply_markup=ForceReply(selective=True)
    )
    await users_db.update_one({"_id": query.from_user.id}, {"$set": {"awaiting_skip": {"chat_id": chat_id, "msg_id": msg.id}}})
    await query.answer()

@app.on_message(filters.reply & filters.private)
async def process_skip_reply(client, message):
    user = await users_db.find_one({"_id": message.from_user.id})
    skip_state = user.get("awaiting_skip")
    
    if skip_state and message.reply_to_message and message.reply_to_message.id == skip_state["msg_id"]:
        try:
            num = int(message.text)
            chat_id = skip_state["chat_id"]
            total = await files_db.count_documents({"source_chat": chat_id})
            
            if num < 1 or num > total:
                return await message.reply(f"❌ দয়া করে ১ থেকে {total} এর মধ্যে নাম্বার দিন।")
            
            # প্রগ্রেস আপডেট করে দেওয়া হলো যাতে এরপর থেকে সিরিয়ালি আসে
            await users_db.update_one({"_id": message.from_user.id}, {
                "$set": {f"progress.{chat_id}": num - 1},
                "$unset": {"awaiting_skip": ""}
            })
            await message.reply(f"✅ স্কিপ সফল! এখন /start এ গিয়ে বাটনে ক্লিক করলে {num} নাম্বার ফাইল থেকে আসা শুরু করবে।")
        except ValueError:
            await message.reply("❌ দয়া করে শুধুমাত্র সংখ্যা লিখুন।")


# ================= অ্যাডমিন সিরিয়াল চ্যানেল ও লিংক কমান্ড =================

@app.on_message(filters.command("addcmd") & filters.user(ADMIN_ID))
async def add_serial_channel(client, message):
    try:
        chat_id = int(message.command[1])
        chat = await client.get_chat(chat_id)
        await channels_db.update_one({"_id": chat.id}, {"$set": {"title": chat.title}}, upsert=True)
        await message.reply(f"✅ চ্যানেল **'{chat.title}'** সফলভাবে এড হয়েছে।\nএখন থেকে এই চ্যানেলে ভিডিও দিলে অটোমেটিক স্টোর হবে ও ইউজাররা বাটন ক্লিক করে সিরিয়ালি পাবে।")
    except Exception as e:
        await message.reply(f"ব্যবহার: `/addcmd -100xxxxxxxx`\n\nঅবশ্যই বটকে ওই চ্যানেলে এডমিন থাকতে হবে।\nError: {e}")

@app.on_message(filters.command("addlink") & filters.user(ADMIN_ID))
async def add_link(client, message):
    try:
        name = message.command[1]
        url = message.command[2]
        await settings_db.update_one({"_id": "config"}, {"$set": {f"links.{name}": url}})
        await message.reply("✅ বাটন লিংক এড করা হয়েছে।")
    except: pass

@app.on_message(filters.command("dellink") & filters.user(ADMIN_ID))
async def del_link(client, message):
    try:
        name = message.command[1]
        await settings_db.update_one({"_id": "config"}, {"$unset": {f"links.{name}": ""}})
        await message.reply("✅ বাটন লিংক ডিলিট করা হয়েছে।")
    except: pass


# ================= অটো এবং লাইভ স্টোরিং সিস্টেম =================

# লাইভ স্টোরিং: চ্যানেলগুলোতে নতুন ভিডিও আসলে অটোমেটিক ডাটাবেসে যাবে
@app.on_message((filters.video | filters.document | filters.audio) & filters.channel)
async def live_auto_store(client, message):
    channel = await channels_db.find_one({"_id": message.chat.id})
    if channel:
        copied = await message.copy(DB_CHANNEL_ID)
        await files_db.insert_one({
            "_id": f"file_{copied.id}",
            "msg_id": copied.id,
            "source_chat": message.chat.id,
            "original_msg_id": message.id
        })

@app.on_message(filters.command("autostore") & filters.user(ADMIN_ID))
async def auto_store(client, message):
    try:
        link = message.command[1] 
        parts = link.split('/')
        msg_id = int(parts[-1])
        chat_id = int("-100" + parts[-2]) if parts[-2].isdigit() or parts[-2] == 'c' else parts[-2]

        count = 0
        status = await message.reply("অটো স্টোরিং চলছে... ⏳")
        
        async for msg in client.get_chat_history(chat_id, offset_id=msg_id, reverse=True):
            if msg.video or msg.audio or msg.document:
                copied = await msg.copy(DB_CHANNEL_ID)
                file_id = f"file_{copied.id}"
                await files_db.update_one({"_id": file_id}, {
                    "$set": {"msg_id": copied.id, "source_chat": chat_id, "original_msg_id": msg.id}
                }, upsert=True)
                count += 1
                if count % 10 == 0: await status.edit(f"স্টোর হয়েছে: {count} ⏳")
                await asyncio.sleep(1.5)
                
        await status.edit(f"✅ অটো স্টোর সম্পন্ন! মোট {count} টি ফাইল ডাটাবেসে সেভ হয়েছে।")
    except Exception as e:
        await message.reply("ব্যবহার: `/autostore https://t.me/channel/msg_id`")

@app.on_message(filters.command("storem") & filters.user(ADMIN_ID) & filters.reply)
async def store_manual(client, message):
    msg = message.reply_to_message
    if msg.video or msg.audio or msg.document:
        copied = await msg.copy(DB_CHANNEL_ID)
        file_id = f"file_{copied.id}"
        await files_db.insert_one({"_id": file_id, "msg_id": copied.id, "source_chat": "manual"})
        await message.reply(f"✅ ফাইল স্টোর হয়েছে!\nলিংক: `https://t.me/{client.me.username}?start={file_id}`")


# ================= অ্যাডমিন সেটিং (মাস্ট সাব, লিমিট, শর্টনার, ডিলিট, লক) =================

@app.on_message(filters.command("setmast") & filters.user(ADMIN_ID))
async def set_mast(client, message):
    try:
        chat_id = message.command[1]
        await settings_db.update_one({"_id": "config"}, {"$addToSet": {"force_subs": chat_id}})
        await message.reply("✅ ফোর্স সাবস্ক্রাইব চ্যানেল এড করা হয়েছে।")
    except: pass

@app.on_message(filters.command("setfile") & filters.user(ADMIN_ID))
async def set_file(client, message):
    try:
        count = int(message.command[1])
        await settings_db.update_one({"_id": "config"}, {"$set": {"batch_size": count}})
        await message.reply(f"✅ এক সাথে {count} ফাইল সেট করা হয়েছে।")
    except: pass

@app.on_message(filters.command("setdelet") & filters.user(ADMIN_ID))
async def set_delete(client, message):
    try:
        sec = int(message.command[1])
        await settings_db.update_one({"_id": "config"}, {"$set": {"delete_time": sec}})
        await message.reply(f"✅ অটো ডিলিট {sec} সেকেন্ড সেট করা হয়েছে।")
    except: pass

@app.on_message(filters.command("lock") & filters.user(ADMIN_ID))
async def lock_forward(client, message):
    settings = await get_settings()
    new_status = not settings.get("locked", False)
    await settings_db.update_one({"_id": "config"}, {"$set": {"locked": new_status}})
    await message.reply(f"✅ ফরোয়ার্ড প্রটেকশন {'চালু' if new_status else 'বন্ধ'} হয়েছে।")

@app.on_message(filters.command("addsort") & filters.user(ADMIN_ID))
async def add_sort(client, message):
    try:
        domain, token = message.command[1], message.command[2]
        await settings_db.update_one({"_id": "config"}, {"$set": {f"shorteners.{domain.replace('.','_')}": {"domain": domain, "token": token}}})
        await message.reply(f"✅ শর্টনার এড করা হয়েছে: `{domain}`")
    except: pass

@app.on_message(filters.command("delsort") & filters.user(ADMIN_ID))
async def del_sort(client, message):
    try:
        domain = message.command[1]
        await settings_db.update_one({"_id": "config"}, {"$unset": {f"shorteners.{domain.replace('.','_')}": ""}})
        await message.reply("✅ শর্টনার ডিলিট হয়েছে।")
    except: pass


# ================= রিডিম কোড ও ব্রডকাস্ট সিস্টেম =================

@app.on_message(filters.command("addreed") & filters.user(ADMIN_ID))
async def add_reed(client, message):
    try:
        duration_str, count = message.command[1], int(message.command[2])
        duration_sec = parse_time(duration_str)
        codes = [{"_id": "PREM-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8)), "duration": duration_sec} for _ in range(count)]
        await redeem_db.insert_many(codes)
        code_list = "\n".join([f"`{c['_id']}`" for c in codes])
        await message.reply(f"✅ **{count} টি কোড তৈরি হয়েছে:**\n\n{code_list}")
    except: pass

@app.on_message(filters.command("reed"))
async def redeem_code(client, message):
    try:
        code = message.command[1]
        doc = await redeem_db.find_one({"_id": code})
        if not doc: return await message.reply("❌ কোডটি ভুল বা ব্যবহৃত।")
        
        user_id = message.from_user.id
        user = await users_db.find_one({"_id": user_id})
        current_time = int(time.time())
        current_premium = max(user.get("premium_until", current_time), current_time)
        new_premium = current_premium + doc["duration"]
        
        await users_db.update_one({"_id": user_id}, {"$set": {"premium_until": new_premium}})
        await redeem_db.delete_one({"_id": code}) 
        await message.reply("✅ **অভিনন্দন!** রিডিম সফল হয়েছে। আপনি এখন অ্যাড/শর্টনার ছাড়াই ফাইল পাবেন।")
    except: pass

@app.on_message(filters.command("brodcast") & filters.user(ADMIN_ID) & filters.reply)
async def broadcast_msg(client, message):
    msg_to_broadcast = message.reply_to_message
    users = await users_db.find().to_list(length=None)
    status = await message.reply("📢 ব্রডকাস্ট শুরু হয়েছে... ⏳")
    success, failed = 0, 0
    for u in users:
        try:
            await msg_to_broadcast.copy(u["_id"])
            success += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await msg_to_broadcast.copy(u["_id"])
            success += 1
        except: failed += 1
        await asyncio.sleep(0.1)
    await status.edit(f"✅ **ব্রডকাস্ট সম্পন্ন!**\n✔️ সফল: {success}\n❌ ব্যর্থ: {failed}")

print("Ultimate Serial Channel Bot Running Perfectly...")
app.run()
