import os
import time
import random
import string
import asyncio
import aiohttp
import urllib.parse
import logging
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from pyrogram.errors import UserNotParticipant, FloodWait, ChatAdminRequired
from motor.motor_asyncio import AsyncIOMotorClient

# ================= লগিং সেটআপ (এরর ট্র্যাক করার জন্য) =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ================= কনফিগারেশন =================
API_ID = int(os.environ.get("API_ID", "আপনার_এপিআই_আইডি"))
API_HASH = os.environ.get("API_HASH", "আপনার_এপিআই_হ্যাশ")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "আপনার_বট_টোকেন")
MONGO_URI = os.environ.get("MONGO_URI", "আপনার_মঙ্গোডিবি_ইউআরএল")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "আপনার_টেলিগ্রাম_আইডি"))
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "-100আপনার_ডাটাবেস_চ্যানেল_আইডি"))

# ================= ডাটাবেস সেটআপ =================
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["UltimateProBot"]
users_db = db["users"]
settings_db = db["settings"]
files_db = db["files"]
redeem_db = db["redeem_codes"]
tokens_db = db["tokens"]
channels_db = db["channels"]

app = Client("MyBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= হেল্পার ফাংশনস =================

async def get_settings():
    """সেটিংস ডাটাবেস থেকে লোড করবে, না থাকলে ডিফল্ট তৈরি করবে"""
    setting = await settings_db.find_one({"_id": "config"})
    if not setting:
        default = {
            "_id": "config", 
            "links": {}, 
            "force_subs": [], 
            "shorteners": {}, 
            "batch_size": 1, 
            "delete_time": 0, 
            "locked": False
        }
        await settings_db.insert_one(default)
        return default
    return setting

async def check_force_sub(client, user_id):
    """ফোর্স সাবস্ক্রাইব চেক করার ফাংশন"""
    settings = await get_settings()
    channels = settings.get("force_subs", [])
    for channel in channels:
        try:
            await client.get_chat_member(channel, user_id)
        except UserNotParticipant:
            return False
        except Exception as e:
            logger.error(f"Force sub check error for channel {channel}: {e}")
            pass
    return True

async def auto_delete_message(message, delay):
    """নির্দিষ্ট সময় পর মেসেজ ডিলিট করবে"""
    if delay > 0:
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception as e:
            logger.error(f"Failed to auto delete message: {e}")

def parse_duration(time_str):
    """স্ট্রিং থেকে সেকেন্ডে কনভার্ট করবে (10m, 1h, 1d)"""
    try:
        unit = time_str[-1].lower()
        val = int(time_str[:-1])
        if unit == 'm': return val * 60
        if unit == 'h': return val * 3600
        if unit == 'd': return val * 86400
        return val
    except Exception:
        return 0

async def generate_short_link(long_url):
    """শর্টনার এপিআই দিয়ে লিংক শর্ট করবে (ভার্সেল বা অন্যান্য)"""
    settings = await get_settings()
    shorteners = settings.get("shorteners", {})
    if not shorteners:
        return long_url
    
    # যেকোনো একটি শর্টনার ব্যবহার করবে
    shortener = list(shorteners.values())[0] 
    domain = shortener.get("domain")
    token = shortener.get("token")
    
    encoded_url = urllib.parse.quote(long_url)
    api_url = f"https://{domain}/api?api={token}&url={encoded_url}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    return data.get("shortenedUrl")
                else:
                    logger.warning(f"Shortener API failed: {data}")
    except Exception as e:
        logger.error(f"Shortener Request Error: {e}")
        
    return long_url

async def send_file_to_user(client, chat_id, file_data):
    """ইউজারকে সুরক্ষিতভাবে ডাটাবেস থেকে ফাইল পাঠাবে"""
    settings = await get_settings()
    is_protected = settings.get("locked", False)
    delete_delay = settings.get("delete_time", 0)
    
    try:
        msg = await client.copy_message(
            chat_id=chat_id, 
            from_chat_id=DB_CHANNEL_ID, 
            message_id=file_data["msg_id"], 
            protect_content=is_protected
        )
        
        if delete_delay > 0:
            asyncio.create_task(auto_delete_message(msg, delete_delay))
            warn_msg = await client.send_message(chat_id, f"⚠️ **সতর্কতা:** এই ফাইলটি {delete_delay} সেকেন্ড পর অটোমেটিক ডিলিট হয়ে যাবে।")
            asyncio.create_task(auto_delete_message(warn_msg, delete_delay))
    except Exception as e:
        logger.error(f"Error sending file to {chat_id}: {e}")
        await client.send_message(chat_id, "❌ ফাইলটি পাঠাতে সমস্যা হয়েছে।")

async def process_serial_files(client, user_id, chat_id, start_idx, batch_size):
    """সিরিয়াল অনুযায়ী ইউজারকে ফাইল পাঠাবে এবং প্রগ্রেস সেভ করবে"""
    cursor = files_db.find({"source_chat": chat_id}).sort("original_msg_id", 1).skip(start_idx).limit(batch_size)
    files = await cursor.to_list(length=batch_size)
    
    if not files:
        return False # কোনো ফাইল পাওয়া যায়নি
        
    for f in files:
        await send_file_to_user(client, user_id, f)
        await asyncio.sleep(0.5) # Flood wait এড়াতে
    
    # ইউজারের প্রগ্রেস আপডেট করা হচ্ছে
    new_progress = start_idx + len(files)
    await users_db.update_one(
        {"_id": user_id}, 
        {"$set": {f"progress.{chat_id}": new_progress}}
    )
    return True

# ================= স্টার্ট কমান্ড এবং ভেরিফিকেশন সিস্টেম =================

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client, message):
    user = message.from_user
    settings = await get_settings()
    
    # ১. ফোর্স সাবস্ক্রাইব চেক
    if not await check_force_sub(client, user.id):
        buttons = []
        for ch in settings.get("force_subs", []):
            try:
                chat_info = await client.get_chat(ch)
                link = chat_info.invite_link or f"https://t.me/{chat_info.username}"
                buttons.append([InlineKeyboardButton(f"Join {chat_info.title}", url=link)])
            except Exception as e:
                logger.error(f"Cannot get invite link for {ch}: {e}")
                
        buttons.append([InlineKeyboardButton("🔄 Joined", callback_data="check_joined")])
        return await message.reply(
            "❌ **ফাইল পেতে হলে অবশ্যই আমাদের চ্যানেলে জয়েন করতে হবে!**\n\nনিচের বাটনগুলোতে ক্লিক করে জয়েন করুন এবং 'Joined' এ ক্লিক করুন।", 
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ২. ইউজার ডাটাবেস আপডেট
    user_data = await users_db.find_one({"_id": user.id})
    if not user_data:
        user_data = {"_id": user.id, "name": user.first_name, "premium_until": 0, "progress": {}}
        await users_db.insert_one(user_data)

    is_premium = user_data.get("premium_until", 0) > time.time()

    # ৩. স্টার্ট প্যারামিটার হ্যান্ডলিং (ফাইল রিকোয়েস্ট বা ভেরিফিকেশন)
    if len(message.command) > 1:
        payload = message.command[1]
        
        # যদি শর্টনার থেকে ঘুরে আসে
        if payload.startswith("verify_"):
            token_data = await tokens_db.find_one({"_id": payload})
            if not token_data or token_data.get("user_id") != user.id:
                return await message.reply("❌ আপনার লিংকটি ইনভ্যালিড অথবা মেয়াদ উত্তীর্ণ হয়ে গেছে। নতুন করে চেষ্টা করুন।")
            
            action = token_data.get("action")
            if action == "serial":
                await process_serial_files(client, user.id, token_data["chat_id"], token_data["idx"], settings.get("batch_size", 1))
            elif action == "single":
                file_data = await files_db.find_one({"_id": token_data["file_id"]})
                if file_data:
                    await send_file_to_user(client, user.id, file_data)
                
            # টোকেন ডিলিট করে দেওয়া হলো যাতে ২ বার ব্যবহার করতে না পারে
            await tokens_db.delete_one({"_id": payload})
            return

        # যদি ডিরেক্ট ফাইল লিংক হয়
        elif payload.startswith("file_"):
            file_data = await files_db.find_one({"_id": payload})
            if not file_data: 
                return await message.reply("❌ দুঃখিত, ফাইলটি আমাদের ডাটাবেসে পাওয়া যায়নি!")
            
            # শর্টনার চেক (যদি প্রিমিয়াম না হয়)
            if not is_premium and settings.get("shorteners"):
                v_token = "verify_" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
                await tokens_db.insert_one({
                    "_id": v_token, 
                    "user_id": user.id, 
                    "action": "single", 
                    "file_id": payload
                })
                
                bot_info = await client.get_me()
                target_url = f"https://t.me/{bot_info.username}?start={v_token}"
                short_url = await generate_short_link(target_url)
                
                btn = [[InlineKeyboardButton("🔓 লিংকে ক্লিক করে আনলক করুন", url=short_url)]]
                return await message.reply(
                    "⚠️ **আপনার কাঙ্ক্ষিত ফাইলটি লক করা আছে!**\n\nদয়া করে নিচের বাটনে ক্লিক করে আনলক করুন।", 
                    reply_markup=InlineKeyboardMarkup(btn)
                )
            
            # প্রিমিয়াম হলে ডিরেক্ট ফাইল দিয়ে দিবে
            await send_file_to_user(client, user.id, file_data)
            return

    # ৪. স্টার্ট মেনু ইন্টারফেস
    if is_premium:
        valid_date = datetime.fromtimestamp(user_data['premium_until']).strftime('%Y-%m-%d %I:%M %p')
        premium_text = f"✅ হ্যাঁ [মেয়াদ: {valid_date}]"
    else:
        premium_text = "❌ না (Regular)"
    
    text = (
        f"👋 হ্যালো **{user.first_name}**, আমাদের বটে আপনাকে স্বাগতম!\n\n"
        f"🔹 **আপনার আইডি:** `{user.id}`\n"
        f"🔹 **প্রিমিয়াম স্ট্যাটাস:** {premium_text}\n\n"
        f"👇 নিচের বাটনগুলো থেকে আপনার প্রয়োজনীয় ভিডিও বা ফাইল সিলেক্ট করুন:"
    )

    buttons = []
    
    # সিরিয়াল চ্যানেল বাটন লোড করা হচ্ছে (/addcmd)
    async for ch in channels_db.find():
        buttons.append([InlineKeyboardButton(ch["title"], callback_data=f"serial_{ch['_id']}")])
        
    # ইউআরএল লিংক বাটন লোড করা হচ্ছে (/addlink)
    for name, url in settings.get("links", {}).items():
        buttons.append([InlineKeyboardButton(name, url=url)])
    
    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

@app.on_callback_query(filters.regex("check_joined"))
async def callback_check_joined(client, query):
    """জয়েন করেছে কিনা চেক করার কলব্যাক"""
    if await check_force_sub(client, query.from_user.id):
        await query.message.delete()
        await cmd_start(client, query.message)
    else:
        await query.answer("আপনি এখনো সব চ্যানেলে জয়েন করেননি! দয়া করে জয়েন করুন।", show_alert=True)


# ================= সিরিয়াল চ্যানেল লজিক এবং স্কিপ সিস্টেম =================

@app.on_callback_query(filters.regex(r"^serial_(.*)"))
async def callback_serial_files(client, query):
    """সিরিয়াল বাটনে ক্লিক করলে ফাইল দেওয়ার লজিক"""
    try:
        chat_id = int(query.data.split("serial_")[1])
    except ValueError:
        return await query.answer("অকার্যকর চ্যানেল আইডি!", show_alert=True)
        
    user_id = query.from_user.id
    settings = await get_settings()
    batch_size = settings.get("batch_size", 1)
    
    if batch_size == 0:
        return await query.answer("অ্যাডমিন সাময়িকভাবে ফাইল দেওয়া বন্ধ রেখেছেন।", show_alert=True)
    
    user_data = await users_db.find_one({"_id": user_id})
    progress = user_data.get("progress", {}).get(str(chat_id), 0)
    
    total_files = await files_db.count_documents({"source_chat": chat_id})
    if total_files == 0:
        return await query.answer("এই চ্যানেলে এখনো কোনো ফাইল আপলোড করা হয়নি!", show_alert=True)
    
    # লুপ সিস্টেম: ফাইল দেখা শেষ হলে আবার প্রথম থেকে শুরু হবে
    if progress >= total_files:
        progress = 0 
        await query.message.reply("✅ আপনি এই সিরিজের সকল ফাইল দেখে ফেলেছেন! আবার প্রথম থেকে দেওয়া শুরু হচ্ছে...")
        
    is_premium = user_data.get("premium_until", 0) > time.time()
    
    # শর্টনার চেক
    if not is_premium and settings.get("shorteners"):
        v_token = "verify_" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
        await tokens_db.insert_one({
            "_id": v_token, 
            "user_id": user_id, 
            "action": "serial", 
            "chat_id": chat_id, 
            "idx": progress
        })
        
        bot_info = await client.get_me()
        short_url = await generate_short_link(f"https://t.me/{bot_info.username}?start={v_token}")
        btn = [[InlineKeyboardButton("🔓 লিংকে ক্লিক করে পরের পর্বগুলো দেখুন", url=short_url)]]
        
        await query.message.reply(
            "⚠️ **আপনার পরবর্তী ভিডিওগুলো লক করা আছে!**\n\nনিচের বাটনে ক্লিক করে আনলক করলে সিরিয়াল অনুযায়ী ভিডিও পাবেন।", 
            reply_markup=InlineKeyboardMarkup(btn)
        )
        await query.answer()
    else:
        success = await process_serial_files(client, user_id, chat_id, progress, batch_size)
        if success:
            await query.answer("ফাইল পাঠানো হচ্ছে...", show_alert=False)
        else:
            await query.answer("ফাইল পাঠাতে সমস্যা হয়েছে!", show_alert=True)

@app.on_message(filters.command("skip") & filters.private)
async def cmd_skip(client, message):
    """ফাইল স্কিপ করার কমান্ড"""
    channels = await channels_db.find().to_list(length=None)
    if not channels:
        return await message.reply("❌ বটের ডাটাবেসে কোনো সিরিয়াল চ্যানেল এড করা নেই।")
    
    buttons = [[InlineKeyboardButton(ch["title"], callback_data=f"skipchan_{ch['_id']}")] for ch in channels]
    await message.reply("👇 আপনি কোন চ্যানেলের ফাইল স্কিপ করতে চান তা সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^skipchan_(.*)"))
async def callback_skip_channel(client, query):
    """স্কিপ করার জন্য চ্যানেল সিলেক্ট করলে"""
    try:
        chat_id = int(query.data.split("skipchan_")[1])
    except ValueError:
        return await query.answer("এরর: চ্যানেল আইডি ভুল!", show_alert=True)
        
    total = await files_db.count_documents({"source_chat": chat_id})
    if total == 0: 
        return await query.answer("এই চ্যানেলে স্কিপ করার মতো কোনো ফাইল নেই!", show_alert=True)
        
    user_data = await users_db.find_one({"_id": query.from_user.id})
    current = user_data.get("progress", {}).get(str(chat_id), 0) + 1
    
    msg = await query.message.reply(
        f"📊 আপনি বর্তমানে **{current}** নাম্বার ফাইলে আছেন। (এই চ্যানেলে মোট ফাইল আছে: {total} টি)\n\n"
        f"➡️ আপনি কত নাম্বার ফাইল থেকে দেখতে চান তা **এই মেসেজটিতে রিপ্লাই দিয়ে** লিখে দিন। (যেমন: `15`)",
        reply_markup=ForceReply(selective=True)
    )
    
    # ইউজারের স্টেট সেভ করা হচ্ছে
    await users_db.update_one(
        {"_id": query.from_user.id}, 
        {"$set": {"awaiting_skip": {"chat_id": chat_id, "msg_id": msg.id}}}
    )
    await query.answer()

@app.on_message(filters.reply & filters.private)
async def handle_skip_reply(client, message):
    """স্কিপ নাম্বারের রিপ্লাই রিসিভ করার লজিক"""
    user = await users_db.find_one({"_id": message.from_user.id})
    if not user: return
    
    skip_state = user.get("awaiting_skip")
    
    if skip_state and message.reply_to_message and message.reply_to_message.id == skip_state["msg_id"]:
        try:
            num = int(message.text)
            chat_id = skip_state["chat_id"]
            total = await files_db.count_documents({"source_chat": chat_id})
            
            if num < 1 or num > total:
                return await message.reply(f"❌ ইনভ্যালিড নাম্বার! দয়া করে ১ থেকে {total} এর মধ্যে একটি নাম্বার দিন।")
            
            # ডাটাবেসে প্রগ্রেস আপডেট করে স্টেট ডিলিট করা হচ্ছে
            await users_db.update_one(
                {"_id": message.from_user.id}, 
                {
                    "$set": {f"progress.{chat_id}": num - 1},
                    "$unset": {"awaiting_skip": ""}
                }
            )
            await message.reply(f"✅ স্কিপ সফল হয়েছে! এখন স্টার্ট মেনু থেকে বাটনে ক্লিক করলে {num} নাম্বার ফাইল থেকেই সিরিয়ালি আসা শুরু করবে।")
        except ValueError:
            await message.reply("❌ ইনভ্যালিড ইনপুট! দয়া করে শুধুমাত্র সংখ্যা (Number) লিখে রিপ্লাই দিন।")


# ================= অ্যাডমিন কমান্ডস (চ্যানেল এড, স্টোর, লিংক) =================

@app.on_message(filters.command("addcmd") & filters.user(ADMIN_ID))
async def cmd_add_serial_channel(client, message):
    """সিরিয়াল চ্যানেল স্টার্ট মেনুতে এড করার কমান্ড"""
    if len(message.command) < 2:
        return await message.reply("⚠️ ব্যবহারবিধি: `/addcmd -100xxxxxxxx`\n\nঅবশ্যই বটকে ওই চ্যানেলে এডমিন থাকতে হবে।")
        
    try:
        chat_id = int(message.command[1])
        chat = await client.get_chat(chat_id)
        
        await channels_db.update_one(
            {"_id": chat.id}, 
            {"$set": {"title": chat.title}}, 
            upsert=True
        )
        await message.reply(f"✅ চ্যানেল **'{chat.title}'** সফলভাবে বাটনে এড হয়েছে!\nএখন এই চ্যানেলে কোনো ভিডিও আপলোড করলে অটোমেটিক সিরিয়ালে যুক্ত হবে।")
    except ChatAdminRequired:
        await message.reply("❌ বট ওই চ্যানেলে এডমিন নয়! দয়া করে বটকে এডমিন প্যানেলে যুক্ত করুন।")
    except Exception as e:
        await message.reply(f"❌ চ্যানেল এড করতে এরর হয়েছে:\n`{str(e)}`")

@app.on_message((filters.video | filters.document | filters.audio) & filters.channel)
async def live_auto_store_listener(client, message):
    """চ্যানেলে নতুন ভিডিও আসলে অটো স্টোর করার লিসেনার"""
    channel = await channels_db.find_one({"_id": message.chat.id})
    if channel:
        try:
            copied = await message.copy(DB_CHANNEL_ID)
            await files_db.insert_one({
                "_id": f"file_{copied.id}",
                "msg_id": copied.id,
                "source_chat": message.chat.id,
                "original_msg_id": message.id
            })
            logger.info(f"Live stored file from {message.chat.id}")
        except Exception as e:
            logger.error(f"Live store error: {e}")

@app.on_message(filters.command("autostore") & filters.user(ADMIN_ID))
async def cmd_auto_store(client, message):
    """চ্যানেলের পুরনো ফাইলগুলো অটো স্টোর করার কমান্ড"""
    if len(message.command) < 2:
        return await message.reply("⚠️ ব্যবহারবিধি: `/autostore https://t.me/channel_name/msg_id`")
        
    try:
        link = message.command[1] 
        parts = link.split('/')
        msg_id = int(parts[-1])
        
        raw_chat = parts[-2]
        if raw_chat.isdigit() or raw_chat == 'c':
            chat_id = int("-100" + parts[-2])
        else:
            chat_id = raw_chat

        status = await message.reply("🔄 অটো স্টোরিং প্রসেস শুরু হয়েছে... দয়া করে অপেক্ষা করুন ⏳")
        count = 0
        
        async for msg in client.get_chat_history(chat_id, offset_id=msg_id, reverse=True):
            if msg.video or msg.audio or msg.document:
                try:
                    copied = await msg.copy(DB_CHANNEL_ID)
                    file_id = f"file_{copied.id}"
                    
                    await files_db.update_one(
                        {"_id": file_id}, 
                        {"$set": {
                            "msg_id": copied.id, 
                            "source_chat": chat_id, 
                            "original_msg_id": msg.id
                        }}, 
                        upsert=True
                    )
                    count += 1
                    
                    if count % 10 == 0: 
                        await status.edit(f"🔄 স্টোর হচ্ছে... এখনো পর্যন্ত {count} টি ফাইল সেভ হয়েছে ⏳")
                        
                    await asyncio.sleep(1.5) # টেলিগ্রাম ফ্লাড লিমিট এড়াতে
                except Exception as loop_e:
                    logger.warning(f"Skipped a message due to error: {loop_e}")
                
        await status.edit(f"✅ অটো স্টোর সম্পূর্ণ হয়েছে! মোট **{count}** টি ফাইল সফলভাবে ডাটাবেসে সেভ হয়েছে।")
    except Exception as e:
        logger.error(f"Autostore failed: {e}")
        await message.reply(f"❌ এরর দেখা দিয়েছে:\n`{str(e)}`\n\nসঠিক লিংক দিয়েছেন কিনা চেক করুন।")


# ================= অ্যাডমিন সেটিংস ও শর্টনার =================

@app.on_message(filters.command("addsort") & filters.user(ADMIN_ID))
async def cmd_add_shortener(client, message):
    if len(message.command) < 3:
        return await message.reply("⚠️ ব্যবহারবিধি: `/addsort domain.com api_token`\nযেমন: `/addsort sort.vercel.app idndjdjndnd`")
        
    try:
        domain = message.command[1]
        token = message.command[2]
        key_name = domain.replace('.', '_')
        
        await settings_db.update_one(
            {"_id": "config"}, 
            {"$set": {f"shorteners.{key_name}": {"domain": domain, "token": token}}}
        )
        await message.reply(f"✅ লিংক শর্টনার সফলভাবে এড করা হয়েছে:\n**ডোমেইন:** `{domain}`\n**টোকেন:** `{token}`")
    except Exception as e:
        await message.reply(f"❌ এরর: `{e}`")

@app.on_message(filters.command("addlink") & filters.user(ADMIN_ID))
async def cmd_add_url_link(client, message):
    if len(message.command) < 3:
        return await message.reply("⚠️ ব্যবহারবিধি: `/addlink [Button_Name] [URL]`")
    
    try:
        name = message.command[1]
        url = message.command[2]
        await settings_db.update_one({"_id": "config"}, {"$set": {f"links.{name}": url}})
        await message.reply(f"✅ স্টার্ট মেনুতে '{name}' নামের লিংক বাটন এড করা হয়েছে।")
    except Exception as e:
        await message.reply(f"❌ এরর: `{e}`")

@app.on_message(filters.command("setfile") & filters.user(ADMIN_ID))
async def cmd_set_batch(client, message):
    if len(message.command) < 2:
        return await message.reply("⚠️ ব্যবহারবিধি: `/setfile [Number]`")
        
    try:
        count = int(message.command[1])
        await settings_db.update_one({"_id": "config"}, {"$set": {"batch_size": count}})
        await message.reply(f"✅ ব্যাচ সাইজ সফলভাবে {count} টি ফাইলে সেট করা হয়েছে।")
    except ValueError:
        await message.reply("❌ দয়া করে একটি সঠিক সংখ্যা দিন।")

@app.on_message(filters.command("setdelet") & filters.user(ADMIN_ID))
async def cmd_set_delete(client, message):
    if len(message.command) < 2:
        return await message.reply("⚠️ ব্যবহারবিধি: `/setdelet [Seconds]`")
        
    try:
        sec = int(message.command[1])
        await settings_db.update_one({"_id": "config"}, {"$set": {"delete_time": sec}})
        await message.reply(f"✅ অটো ডিলিট টাইমার {sec} সেকেন্ড সেট করা হয়েছে। (0 দিলে ডিলিট হবে না)")
    except ValueError:
        await message.reply("❌ দয়া করে একটি সঠিক সংখ্যা দিন।")


# ================= রিডিম কোড ও ব্রডকাস্ট সিস্টেম =================

@app.on_message(filters.command("addreed") & filters.user(ADMIN_ID))
async def cmd_create_redeem(client, message):
    if len(message.command) < 3:
        return await message.reply("⚠️ ব্যবহারবিধি: `/addreed 10m 5` (১০ মিনিটের ৫টি কোড)")
        
    try:
        duration_str = message.command[1] 
        count = int(message.command[2])
        
        duration_sec = parse_duration(duration_str)
        if duration_sec == 0:
            return await message.reply("❌ অকার্যকর সময়! সঠিক ফরম্যাট: 10m, 2h, 30d")
            
        codes = []
        for _ in range(count):
            code = "PREM-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            codes.append({"_id": code, "duration": duration_sec})
            
        await redeem_db.insert_many(codes)
        
        code_list = "\n".join([f"`{c['_id']}`" for c in codes])
        await message.reply(f"✅ **সফলভাবে {count} টি কোড তৈরি হয়েছে ({duration_str} এর জন্য):**\n\n{code_list}\n\nইউজারদের `/reed CODE` ব্যবহার করতে বলুন।")
    except Exception as e:
        await message.reply(f"❌ কোড তৈরি করতে সমস্যা হয়েছে: `{e}`")

@app.on_message(filters.command("reed"))
async def cmd_use_redeem(client, message):
    if len(message.command) < 2:
        return await message.reply("⚠️ ব্যবহারবিধি: `/reed আপনার_কোড_এখানে`")
        
    try:
        code = message.command[1]
        doc = await redeem_db.find_one({"_id": code})
        
        if not doc: 
            return await message.reply("❌ দুঃখিত, আপনার দেওয়া কোডটি ভুল অথবা ইতিমধ্যে ব্যবহার করা হয়েছে।")
        
        user_id = message.from_user.id
        user = await users_db.find_one({"_id": user_id})
        if not user:
            return await message.reply("❌ আপনাকে আগে /start কমান্ড ব্যবহার করতে হবে।")
            
        current_time = int(time.time())
        current_premium = max(user.get("premium_until", current_time), current_time)
        new_premium = current_premium + doc["duration"]
        
        # প্রিমিয়াম আপডেট এবং কোড ডিলিট
        await users_db.update_one({"_id": user_id}, {"$set": {"premium_until": new_premium}})
        await redeem_db.delete_one({"_id": code}) 
        
        valid_date = datetime.fromtimestamp(new_premium).strftime('%Y-%m-%d %I:%M %p')
        await message.reply(f"🎉 **অভিনন্দন!** রিডিম সফল হয়েছে।\n\nআপনি এখন থেকে **{valid_date}** পর্যন্ত কোনো প্রকার লিংক শর্টনার ছাড়াই সরাসরি ফাইল ডাউনলোড করতে পারবেন।")
    except Exception as e:
        logger.error(f"Redeem error: {e}")
        await message.reply("❌ রিডিম করার সময় কোনো সমস্যা হয়েছে।")

@app.on_message(filters.command("brodcast") & filters.user(ADMIN_ID) & filters.reply)
async def cmd_broadcast(client, message):
    msg_to_broadcast = message.reply_to_message
    
    status = await message.reply("📢 ব্রডকাস্ট প্রসেস শুরু হয়েছে... দয়া করে অপেক্ষা করুন। ⏳")
    
    success_count = 0
    failed_count = 0
    
    # সকল ইউজার লোড করা হচ্ছে
    users_cursor = users_db.find({}, {"_id": 1})
    
    async for u in users_cursor:
        try:
            await msg_to_broadcast.copy(u["_id"])
            success_count += 1
        except FloodWait as e:
            logger.warning(f"Broadcast FloodWait for {e.value} seconds.")
            await asyncio.sleep(e.value)
            await msg_to_broadcast.copy(u["_id"])
            success_count += 1
        except Exception:
            failed_count += 1
            
        await asyncio.sleep(0.05) # সার্ভারের উপর চাপ কমাতে 
        
    await status.edit(
        f"✅ **ব্রডকাস্ট সফলভাবে সম্পন্ন হয়েছে!**\n\n"
        f"✔️ সফলভাবে পাঠানো হয়েছে: `{success_count}` জনকে\n"
        f"❌ ব্যর্থ হয়েছে/ব্লক করেছে: `{failed_count}` জন"
    )


if __name__ == "__main__":
    logger.info("Ultimate Production Bot is Starting...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
