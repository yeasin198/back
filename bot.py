import os
import time
import random
import string
import asyncio
import aiohttp
import urllib.parse
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
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
db = mongo_client["AdvancedBot"]
users_db = db["users"]
settings_db = db["settings"]
files_db = db["files"]
redeem_db = db["redeem_codes"]
tokens_db = db["tokens"]

app = Client("MyBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= হেল্পার ফাংশন =================
async def get_settings():
    setting = await settings_db.find_one({"_id": "config"})
    if not setting:
        default = {
            "_id": "config", "links": {}, "force_subs": [], 
            "shorteners": {}, "batch_size": 1, "delete_time": 0, "locked": False,
            "custom_cmds": {}
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

async def get_short_link(long_url, shortener_name="default"):
    settings = await get_settings()
    shorteners = settings.get("shorteners", {})
    
    if not shorteners:
        return long_url
    
    # যেকোনো একটি শর্টনার বেছে নিবে অথবা ডিফল্ট
    shortener = list(shorteners.values())[0] 
    
    domain = shortener["domain"]
    token = shortener["token"]
    encoded_url = urllib.parse.quote(long_url)
    api_url = f"https://{domain}/api?api={token}&url={encoded_url}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    return data.get("shortenedUrl")
    except Exception as e:
        print("Shortener Error:", e)
    return long_url

async def send_file(client, chat_id, file_data):
    settings = await get_settings()
    protect = settings.get("locked", False)
    del_time = settings.get("delete_time", 0)
    batch_limit = settings.get("batch_size", 1)
    
    if batch_limit == 0:
        return await client.send_message(chat_id, "অ্যাডমিন সাময়িকভাবে ফাইল দেওয়া বন্ধ রেখেছেন।")
    
    msg = await client.copy_message(
        chat_id=chat_id, 
        from_chat_id=DB_CHANNEL_ID, 
        message_id=file_data["msg_id"],
        protect_content=protect
    )
    if del_time > 0:
        asyncio.create_task(auto_delete(msg, del_time))
        warn_msg = await client.send_message(chat_id, f"⚠️ এই ফাইলটি {del_time} সেকেন্ড পর ডিলিট হয়ে যাবে।")
        asyncio.create_task(auto_delete(warn_msg, del_time))

# ================= স্টার্ট এবং ফাইল হ্যান্ডলিং =================

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user = message.from_user
    settings = await get_settings()
    
    # Force Sub Check
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
        user_data = {"_id": user.id, "name": user.first_name, "premium_until": 0}
        await users_db.insert_one(user_data)
    else:
        await users_db.update_one({"_id": user.id}, {"$set": {"name": user.first_name}})

    is_premium = user_data.get("premium_until", 0) > time.time()

    # Payload Handling (Files & Verification)
    if len(message.command) > 1:
        payload = message.command[1]
        
        # ১. শর্টনার ভেরিফিকেশন লিংক
        if payload.startswith("verify_"):
            token_data = await tokens_db.find_one({"_id": payload})
            if not token_data or token_data["user_id"] != user.id:
                return await message.reply("❌ লিংকটি ইনভ্যালিড বা মেয়াদ উত্তীর্ণ হয়েছে।")
            
            file_data = await files_db.find_one({"_id": token_data["file_id"]})
            await send_file(client, user.id, file_data)
            await tokens_db.delete_one({"_id": payload})
            return

        # ২. ডিরেক্ট ফাইল লিংক
        elif payload.startswith("file_"):
            file_data = await files_db.find_one({"_id": payload})
            if not file_data:
                return await message.reply("❌ ফাইল পাওয়া যায়নি!")
            
            # শর্টনার চেক
            if not is_premium and settings.get("shorteners"):
                verify_token = "verify_" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
                await tokens_db.insert_one({"_id": verify_token, "user_id": user.id, "file_id": payload})
                
                bot_info = await client.get_me()
                target_url = f"https://t.me/{bot_info.username}?start={verify_token}"
                short_url = await get_short_link(target_url)
                
                btn = [[InlineKeyboardButton("🔓 লিংকে ক্লিক করে ফাইলটি ওপেন করুন", url=short_url)]]
                return await message.reply("⚠️ **আপনার ফাইলটি লক করা আছে!**\n\nদয়া করে নিচের বাটনে ক্লিক করে আনলক করুন।", reply_markup=InlineKeyboardMarkup(btn))
            
            await send_file(client, user.id, file_data)
            return

    # Start Message Interface Construction
    premium_text = "হ্যাঁ (Premium)" if is_premium else "না (Regular)"
    if is_premium:
        prem_date = datetime.fromtimestamp(user_data['premium_until']).strftime('%Y-%m-%d %H:%M')
        premium_text += f" [Valid till: {prem_date}]"

    text = (
        f"👋 হ্যালো {user.first_name} {user.last_name or ''}\n\n"
        f"🔹 **ইউজার আইডি:** `{user.id}`\n"
        f"🔹 **ইউজার নাম:** @{user.username or 'N/A'}\n"
        f"🔹 **প্রিমিয়াম স্ট্যাটাস:** {premium_text}\n"
    )

    buttons = []
    
    # 1. Add URL Links (/addlink)
    links = settings.get("links", {})
    for name, url in links.items():
        buttons.append([InlineKeyboardButton(name, url=url)])
        
    # 2. Add Custom Command Buttons (/addcmd)
    custom_cmds = settings.get("custom_cmds", {})
    for btn_name in custom_cmds.keys():
        buttons.append([InlineKeyboardButton(btn_name, callback_data=f"cmd_{btn_name}")])
    
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await message.reply(text, reply_markup=markup)

@app.on_callback_query(filters.regex("check_joined"))
async def check_joined_callback(client, query):
    if await check_force_sub(client, query.from_user.id):
        await query.message.delete()
        await start_cmd(client, query.message)
    else:
        await query.answer("আপনি এখনো জয়েন করেননি!", show_alert=True)


# ================= কাস্টম কমান্ড ও বাটন লিংক সিস্টেম =================

@app.on_message(filters.command("addlink") & filters.user(ADMIN_ID))
async def add_link(client, message):
    try:
        name = message.command[1]
        url = message.command[2]
        await settings_db.update_one({"_id": "config"}, {"$set": {f"links.{name}": url}})
        await message.reply("✅ বাটন লিংক এড করা হয়েছে।")
    except:
        await message.reply("ব্যবহার: `/addlink [Button_Name] [URL]`")

@app.on_message(filters.command("dellink") & filters.user(ADMIN_ID))
async def del_link(client, message):
    try:
        name = message.command[1]
        await settings_db.update_one({"_id": "config"}, {"$unset": {f"links.{name}": ""}})
        await message.reply("✅ বাটন লিংক ডিলিট করা হয়েছে।")
    except:
        await message.reply("ব্যবহার: `/dellink [Button_Name]`")

@app.on_message(filters.command("addcmd") & filters.user(ADMIN_ID))
async def add_custom_cmd(client, message):
    try:
        args = message.text.split(None, 2)
        btn_name = args[1]
        response = args[2] # Text or file_xxxx
        await settings_db.update_one({"_id": "config"}, {"$set": {f"custom_cmds.{btn_name}": response}})
        await message.reply(f"✅ `{btn_name}` নামের কাস্টম বাটন স্টার্ট মেনুতে এড করা হয়েছে।")
    except:
        await message.reply("ব্যবহার: `/addcmd ButtonName Message Text or file_12345`")

@app.on_callback_query(filters.regex(r"^cmd_(.*)"))
async def custom_btn_callback(client, query):
    btn_name = query.data.split("cmd_")[1]
    settings = await get_settings()
    response = settings.get("custom_cmds", {}).get(btn_name, "No data found.")
    
    if response.startswith("file_"):
        file_data = await files_db.find_one({"_id": response})
        if file_data:
            await send_file(client, query.from_user.id, file_data)
            await query.answer("ফাইল পাঠানো হয়েছে!")
        else:
            await query.answer("ফাইলটি ডাটাবেসে নেই!", show_alert=True)
    else:
        await query.message.reply(response)
        await query.answer()


# ================= সেটিং সিস্টেম (ডিলিট, লক, ব্যাচ, মাস্ট সাব) =================

@app.on_message(filters.command("setmast") & filters.user(ADMIN_ID))
async def set_mast(client, message):
    try:
        chat_id = message.command[1]
        await settings_db.update_one({"_id": "config"}, {"$addToSet": {"force_subs": chat_id}})
        await message.reply(f"✅ ফোর্স সাবস্ক্রাইব চ্যানেল এড করা হয়েছে: {chat_id}")
    except:
        await message.reply("ব্যবহার: `/setmast [Chat_ID বা Username]`")

@app.on_message(filters.command("setfile") & filters.user(ADMIN_ID))
async def set_file(client, message):
    try:
        count = int(message.command[1])
        await settings_db.update_one({"_id": "config"}, {"$set": {"batch_size": count}})
        await message.reply(f"✅ ব্যাচ সাইজ {count} সেট করা হয়েছে। (0 দিলে ইউজার ফাইল পাবে না)")
    except:
        await message.reply("ব্যবহার: `/setfile [Number]`")

@app.on_message(filters.command("setdelet") & filters.user(ADMIN_ID))
async def set_delete(client, message):
    try:
        sec = int(message.command[1])
        await settings_db.update_one({"_id": "config"}, {"$set": {"delete_time": sec}})
        await message.reply(f"✅ অটো ডিলিট {sec} সেকেন্ড সেট করা হয়েছে।")
    except:
        await message.reply("ব্যবহার: `/setdelet [Seconds]` (0 দিলে ডিলিট হবে না)")

@app.on_message(filters.command("lock") & filters.user(ADMIN_ID))
async def lock_forward(client, message):
    settings = await get_settings()
    current = settings.get("locked", False)
    new_status = not current
    await settings_db.update_one({"_id": "config"}, {"$set": {"locked": new_status}})
    await message.reply(f"✅ ফরোয়ার্ড প্রটেকশন {'চালু' if new_status else 'বন্ধ'} করা হয়েছে।")


# ================= লিংক শর্টনার সিস্টেম =================

@app.on_message(filters.command("addsort") & filters.user(ADMIN_ID))
async def add_sort(client, message):
    try:
        # Example: /addsort sort.vercel.app idndjdjndnd
        domain = message.command[1]
        token = message.command[2]
        await settings_db.update_one({"_id": "config"}, {"$set": {f"shorteners.{domain.replace('.','_')}": {"domain": domain, "token": token}}})
        await message.reply(f"✅ শর্টনার এড করা হয়েছে: `{domain}`")
    except:
        await message.reply("ব্যবহার: `/addsort domain.com api_key`")

@app.on_message(filters.command("delsort") & filters.user(ADMIN_ID))
async def del_sort(client, message):
    try:
        domain = message.command[1]
        await settings_db.update_one({"_id": "config"}, {"$unset": {f"shorteners.{domain.replace('.','_')}": ""}})
        await message.reply("✅ শর্টনার ডিলিট হয়েছে।")
    except:
        await message.reply("ব্যবহার: `/delsort domain.com`")


# ================= রিডিম কোড সিস্টেম =================

@app.on_message(filters.command("addreed") & filters.user(ADMIN_ID))
async def add_reed(client, message):
    try:
        duration_str = message.command[1] # e.g., 10m, 1h, 30d
        count = int(message.command[2])   # e.g., 10
        
        duration_sec = parse_time(duration_str)
        codes = []
        for _ in range(count):
            code = "PREM-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            codes.append({"_id": code, "duration": duration_sec})
        
        await redeem_db.insert_many(codes)
        code_list = "\n".join([f"`{c['_id']}`" for c in codes])
        await message.reply(f"✅ **{count} টি কোড তৈরি হয়েছে ({duration_str} এর জন্য):**\n\n{code_list}")
    except Exception as e:
        await message.reply("ব্যবহার: `/addreed 10m 10` (10m = 10 minutes, 1d = 1 day)")

@app.on_message(filters.command("reed"))
async def redeem_code(client, message):
    try:
        code = message.command[1]
        doc = await redeem_db.find_one({"_id": code})
        if not doc:
            return await message.reply("❌ কোডটি ভুল অথবা ইতিমধ্যে ব্যবহার করা হয়েছে।")
        
        user_id = message.from_user.id
        user = await users_db.find_one({"_id": user_id})
        
        current_time = int(time.time())
        current_premium = user.get("premium_until", current_time)
        if current_premium < current_time: current_premium = current_time
        
        new_premium = current_premium + doc["duration"]
        await users_db.update_one({"_id": user_id}, {"$set": {"premium_until": new_premium}})
        await redeem_db.delete_one({"_id": code}) # Delete after use
        
        valid_till = datetime.fromtimestamp(new_premium).strftime('%Y-%m-%d %H:%M')
        await message.reply(f"✅ **অভিনন্দন!** রিডিম সফল হয়েছে।\nএখন আপনি {valid_till} পর্যন্ত কোনো এড বা শর্টনার ছাড়াই ডিরেক্ট ফাইল পাবেন।")
    except:
        await message.reply("ব্যবহার: `/reed YOUR_CODE`")


# ================= ব্রডকাস্ট সিস্টেম =================

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
        except:
            failed += 1
        await asyncio.sleep(0.1) # Flood Control
        
    await status.edit(f"✅ **ব্রডকাস্ট সম্পন্ন!**\n\n✔️ সফল: {success}\n❌ ব্যর্থ: {failed}")


# ================= ফাইল স্টোরিং সিস্টেম =================

@app.on_message(filters.command("storem") & filters.user(ADMIN_ID) & filters.reply)
async def store_manual(client, message):
    msg = message.reply_to_message
    if msg.video or msg.audio or msg.document:
        copied = await msg.copy(DB_CHANNEL_ID)
        file_id = f"file_{copied.id}"
        await files_db.insert_one({"_id": file_id, "msg_id": copied.id})
        
        bot_user = await client.get_me()
        link = f"https://t.me/{bot_user.username}?start={file_id}"
        await message.reply(f"✅ ফাইল স্টোর হয়েছে!\nলিংক: `{link}`")
    else:
        await message.reply("দয়া করে কোনো ভিডিও, অডিও বা ফাইলে রিপ্লাই করুন।")

@app.on_message(filters.command("autostore") & filters.user(ADMIN_ID))
async def auto_store(client, message):
    try:
        link = message.command[1] # e.g., https://t.me/channel_name/123
        parts = link.split('/')
        msg_id = int(parts[-1])
        chat_id = parts[-2]
        
        if chat_id.isdigit() or chat_id == 'c':
            chat_id = int("-100" + parts[-2])

        count = 0
        status = await message.reply("অটো স্টোরিং চলছে... ⏳")
        
        async for msg in client.get_chat_history(chat_id, offset_id=msg_id, reverse=True):
            if msg.video or msg.audio or msg.document:
                copied = await msg.copy(DB_CHANNEL_ID)
                file_id = f"file_{copied.id}"
                await files_db.update_one({"_id": file_id}, {"$set": {"msg_id": copied.id}}, upsert=True)
                count += 1
                if count % 10 == 0:
                    await status.edit(f"এখনো পর্যন্ত {count} টি ফাইল স্টোর হয়েছে... ⏳")
                await asyncio.sleep(2) # Flood wait bypass
                
        await status.edit(f"✅ অটো স্টোর সম্পন্ন! মোট {count} টি ফাইল ডাটাবেসে সেভ হয়েছে।")
    except Exception as e:
        await message.reply(f"❌ এরর: {e}\nব্যবহার: `/autostore https://t.me/channel/msg_id`")


print("Ultimate Advanced Bot is Starting... Everything Loaded Successfully!")
app.run()
