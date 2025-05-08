import os
import logging
import random
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest
from aiohttp import web

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('airtime_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Bot configuration from environment variables
CONFIG = {
    'token': os.getenv('TELEGRAM_BOT_TOKEN', ''),
    'admin_id': int(os.getenv('ADMIN_ID', '')),
    'required_channels': os.getenv('REQUIRED_CHANNELS', 'megahubbots').split(','),
    'channel_links': os.getenv('CHANNEL_LINKS', 'https://t.me/megahubbots').split(',')
}

# MongoDB connection
try:
    mongodb_uri = os.getenv('MONGODB_URI')
    if not mongodb_uri:
        raise ValueError("MONGODB_URI environment variable not set")
    
    # Add retryWrites and SSL parameters if not already in URI
    if "retryWrites" not in mongodb_uri:
        if "?" in mongodb_uri:
            mongodb_uri += "&retryWrites=true&w=majority"
        else:
            mongodb_uri += "?retryWrites=true&w=majority"
    
    # Force SSL/TLS connection
    if "ssl=true" not in mongodb_uri.lower():
        if "?" in mongodb_uri:
            mongodb_uri += "&ssl=true"
        else:
            mongodb_uri += "?ssl=true"
    
    client = MongoClient(
        mongodb_uri,
        tls=True,
        tlsAllowInvalidCertificates=False,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        serverSelectionTimeoutMS=30000
    )
    
    # Test the connection immediately
    client.admin.command('ping')
    logger.info("Successfully connected to MongoDB")
    
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    raise

db = client[os.getenv('DATABASE_NAME', 'airtime_bot')]

# Collections
users_collection = db['users']
airtime_requests_collection = db['airtime_requests']
transactions_collection = db['transactions']

# Webhook configuration
PORT = int(os.getenv('PORT', 10000))
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '') + WEBHOOK_PATH

# Animation Frames
PROCESSING_FRAMES = [
    "📱 Processing Airtime Request [▓▓░░░░░░░░░░░] 15%",
    "📱 Processing Airtime Request [▓▓▓░░░░░░░░░] 30%",
    "📱 Processing Airtime Request [▓▓▓▓▓░░░░░░░] 45%",
    "📱 Processing Airtime Request [▓▓▓▓▓▓▓░░░░░] 60%",
    "📱 Processing Airtime Request [▓▓▓▓▓▓▓▓▓░░░] 80%",
    "📱 Processing Airtime Request [▓▓▓▓▓▓▓▓▓▓▓] 100%",
]

# === DATABASE FUNCTIONS ===
def add_user(user):
    """Add user to database if not exists"""
    users_collection.update_one(
        {'user_id': user.id},
        {'$set': {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'balance': 0,
            'requests': 0,
            'join_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }},
        upsert=True
    )

def add_airtime_request(user_id, network, phone_number, amount):
    """Add an airtime request record"""
    airtime_requests_collection.insert_one({
        'user_id': user_id,
        'network': network,
        'phone_number': phone_number,
        'amount': amount,
        'status': 'pending',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

def add_transaction(user_id, transaction_type, amount, status='completed'):
    """Add a transaction record"""
    transactions_collection.insert_one({
        'user_id': user_id,
        'type': transaction_type,
        'amount': amount,
        'status': status,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

def get_user_stats(user_id):
    """Get user statistics"""
    user = users_collection.find_one({'user_id': user_id})
    request_count = airtime_requests_collection.count_documents({'user_id': user_id})
    return user, request_count

# === FORCE JOIN FUNCTIONALITY ===
async def is_user_member(user_id, bot):
    """Check if user is member of all required channels"""
    for channel in CONFIG['required_channels']:
        try:
            chat_member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            if chat_member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            logger.error(f"Error checking membership for {user_id} in {channel}: {e}")
            return False
    return True

async def ask_user_to_join(update):
    """Send message with join buttons"""
    buttons = [
        [InlineKeyboardButton(f"Join {CONFIG['required_channels'][i]}", url=CONFIG['channel_links'][i])] 
        for i in range(len(CONFIG['required_channels']))
    ]
    buttons.append([InlineKeyboardButton("✅ Verify", callback_data="verify_membership")])
    
    await update.message.reply_text(
        "🪬 Verification Status: ⚠️ You must join the following channels to use this bot and verify you're not a robot 🚨\n\n"
        "Click the buttons below to join, then press *'✅ Verify'*.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def verify_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle membership verification"""
    query = update.callback_query
    user_id = query.from_user.id

    if await is_user_member(user_id, context.bot):
        await query.message.edit_text("✅ You are verified! You can now use the bot.")
        await start(update, context)
    else:
        await query.answer("⚠️ You haven't joined all the required channels yet!", show_alert=True)

# === AIRTIME FUNCTIONS ===
async def show_processing_animation(query):
    """Show processing animation"""
    message = await query.message.reply_text(PROCESSING_FRAMES[0])
    for frame in PROCESSING_FRAMES[1:]:
        await asyncio.sleep(1)
        await message.edit_text(frame)
    return message

async def handle_airtime_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle airtime request"""
    query = update.callback_query
    await query.answer()

    # Show processing animation
    processing_message = await show_processing_animation(query)

    # Get network from callback data
    network = query.data.split('_')[1]
    
    # Generate random airtime amount (for demo purposes)
    amount = random.randint(10, 50) * 10  # Random amount between 100 and 500 in increments of 10
    
    # Save request to database
    add_airtime_request(query.from_user.id, network, "DEMO_PHONE", amount)
    add_transaction(query.from_user.id, "airtime", amount)
    
    # Create response
    keyboard = [
        [InlineKeyboardButton("🔄 Request Again", callback_data=f"airtime_{network}")],
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")]
    ]
    
    await processing_message.delete()
    await query.message.reply_text(
        f"🎉 Airtime Request Successful!\n\n"
        f"📱 Network: {network}\n"
        f"💰 Amount: {amount} Naira\n"
        f"📞 Phone: DEMO_PHONE (for demonstration)\n\n"
        "⚠️ Note: This is a demo. No actual airtime will be sent.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# === COMMAND HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    add_user(user)

    if not await is_user_member(user.id, context.bot):
        await ask_user_to_join(update)
        return

    keyboard = [
        [InlineKeyboardButton("📱 MTN Airtime", callback_data="airtime_MTN")],
        [InlineKeyboardButton("📱 Airtel Airtime", callback_data="airtime_AIRTEL")],
        [InlineKeyboardButton("📱 Glo Airtime", callback_data="airtime_GLO")],
        [InlineKeyboardButton("📱 9mobile Airtime", callback_data="airtime_9MOBILE")],
    ]
    await update.message.reply_text(
        "📱 Welcome to the Airtime Request Bot!\n\n"
        "This bot allows you to request free airtime (demo version).\n\n"
        "Select your network below to get started:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /profile command"""
    user = update.effective_user
    if not await is_user_member(user.id, context.bot):
        await ask_user_to_join(update)
        return

    user_data, request_count = get_user_stats(user.id)
    await update.message.reply_text(
        f"👤 User Info:\n\n"
        f"🆔 User ID: {user.id}\n"
        f"🤵 Name: {user.first_name}\n"
        f"👤 Username: {user.username or 'N/A'}\n"
        f"📊 Airtime Requests: {request_count}\n"
        f"⏳ Joined: {user_data.get('join_date', 'N/A')}"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /stats command."""
    if update.effective_user.id != CONFIG['admin_id']:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    user_count = users_collection.count_documents({})
    total_requests = airtime_requests_collection.count_documents({})
    text = f"📊 *Bot Statistics*\n\n"
    text += f"👥 Total Users: {user_count}\n"
    text += f"📱 Total Airtime Requests: {total_requests}\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast command (admin only)"""
    if update.effective_user.id != CONFIG['admin_id']:
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    if not context.args:
        await update.message.reply_text("❌ Please provide a message to broadcast.")
        return

    message = " ".join(context.args)
    users = users_collection.find({}, {'user_id': 1})
    success = 0

    for user in users:
        try:
            await context.bot.send_message(user['user_id'], message)
            success += 1
        except Exception as e:
            logger.error(f"Failed to send to {user['user_id']}: {e}")

    await update.message.reply_text(f"✅ Broadcast sent to {success} users.")

# === WEBHOOK SETUP ===
async def health_check(request):
    """Health check endpoint"""
    return web.Response(text="OK")

async def telegram_webhook(request):
    """Handle incoming webhook requests"""
    update = Update.de_json(await request.json(), application.bot)
    await application.update_queue.put(update)
    return web.Response(text="OK")

def main():
    """Run the bot"""
    global application
    application = Application.builder().token(CONFIG['token']).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # Add callback handlers for all networks
    application.add_handler(CallbackQueryHandler(handle_airtime_request, pattern="^airtime_MTN$"))
    application.add_handler(CallbackQueryHandler(handle_airtime_request, pattern="^airtime_AIRTEL$"))
    application.add_handler(CallbackQueryHandler(handle_airtime_request, pattern="^airtime_GLO$"))
    application.add_handler(CallbackQueryHandler(handle_airtime_request, pattern="^airtime_9MOBILE$"))
    application.add_handler(CallbackQueryHandler(verify_membership, pattern="^verify_membership$"))

    # Start the bot with webhook if running on Render
    if os.getenv('RENDER'):
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL
        )
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
