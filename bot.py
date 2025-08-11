import os
import json
import datetime
import schedule
import time
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

# -----------------------
# Config from Railway env vars
# -----------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Please set TOKEN environment variable in Railway.")

# Persistent storage path
TASK_FILE = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "tasks.json")

bot = Bot(token=TOKEN)

# -----------------------
# Data storage helpers
# -----------------------
def load_data():
    try:
        with open(TASK_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}  # Each key will be a chat_id

def save_data(data):
    os.makedirs(os.path.dirname(TASK_FILE), exist_ok=True)
    with open(TASK_FILE, "w") as f:
        json.dump(data, f, indent=2)

def ensure_user_exists(chat_id):
    data = load_data()
    if str(chat_id) not in data:
        data[str(chat_id)] = {"tasks": [], "done": []}
        save_data(data)

# -----------------------
# UI: checklist with buttons
# -----------------------
def get_checklist_markup(chat_id):
    data = load_data()
    user_data = data.get(str(chat_id), {"tasks": [], "done": []})
    buttons = []
    for i, task in enumerate(user_data["tasks"]):
        done = i in user_data["done"]
        label = f"✅ {task}" if done else f"⬜ {task}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"toggle_{i}")])
    return InlineKeyboardMarkup(buttons)

def send_checklist_message(chat_id):
    today = datetime.datetime.now().strftime("%A, %d %B %Y")
    bot.send_message(chat_id=chat_id,
                     text=f"📋 *Today's Checklist* — {today}",
                     parse_mode="Markdown",
                     reply_markup=get_checklist_markup(chat_id))

# -----------------------
# Bot Commands
# -----------------------
def start(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    ensure_user_exists(chat_id)
    update.message.reply_text("Hi! Use /add to add tasks, /checklist to view them.")

def add_task(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    ensure_user_exists(chat_id)

    task = " ".join(context.args)
    if not task:
        update.message.reply_text("Usage: /add <task>")
        return

    data = load_data()
    data[str(chat_id)]["tasks"].append(task)
    save_data(data)
    update.message.reply_text(f"✅ Task added: {task}")

def show_checklist(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    ensure_user_exists(chat_id)
    send_checklist_message(chat_id)

# -----------------------
# Button handler
# -----------------------
def toggle_task(update: Update, context: CallbackContext):
    query = update.callback_query
    chat_id = query.message.chat_id
    ensure_user_exists(chat_id)

    index = int(query.data.split("_")[1])
    data = load_data()

    if index in data[str(chat_id)]["done"]:
        data[str(chat_id)]["done"].remove(index)
    else:
        data[str(chat_id)]["done"].append(index)

    save_data(data)
    query.answer()
    query.edit_message_reply_markup(reply_markup=get_checklist_markup(chat_id))

# -----------------------
# Daily reset
# -----------------------
def reset_tasks():
    data = load_data()
    for user_id in list(data.keys()):
        data[user_id]["done"] = []
        save_data(data)
        send_checklist_message(user_id)

schedule.every().day.at("08:00").do(reset_tasks)

# -----------------------
# Main bot runner
# -----------------------
updater = Updater(TOKEN)
dp = updater.dispatcher

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("add", add_task))
dp.add_handler(CommandHandler("checklist", show_checklist))
dp.add_handler(CallbackQueryHandler(toggle_task, pattern=r"toggle_\d+"))

updater.start_polling()

# Keep schedule running in background
while True:
    schedule.run_pending()
    time.sleep(60)
