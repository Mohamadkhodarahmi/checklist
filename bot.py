import os
import json
import datetime
import schedule
import time
import logging
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
    PreCheckoutQueryHandler,
    MessageHandler,
    Filters
)

# -----------------------
# Setup Logging
# -----------------------
# This is a good practice to see what your bot is doing.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------
# Config from Railway env vars
# -----------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Please set TOKEN environment variable in Railway.")

# The bot's provider token for Telegram Stars
# This is a TEST token from Telegram. For a live bot, you must get your own.
# You can get a test token from @BotFather
PROVIDER_TOKEN = "381764678:TEST:25838" 

# Persistent storage path
TASK_FILE = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "tasks.json")

bot = Bot(token=TOKEN)

# -----------------------
# Data storage helpers
# -----------------------
def load_data():
    """Loads bot data from the JSON file."""
    try:
        with open(TASK_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_data(data):
    """Saves bot data to the JSON file."""
    os.makedirs(os.path.dirname(TASK_FILE), exist_ok=True)
    with open(TASK_FILE, "w") as f:
        json.dump(data, f, indent=2)

def ensure_user_exists(chat_id):
    """
    Ensures a user has an entry in the data file.
    Initializes a default "Daily" checklist for new users.
    Adds missing keys for old users.
    """
    data = load_data()
    chat_id_str = str(chat_id)
    if chat_id_str not in data:
        data[chat_id_str] = {
            "is_premium": False,
            "checklists": {
                "Daily": {"tasks": [], "done": []}
            }
        }
    else:
        # Check for and add missing keys for existing users
        if "is_premium" not in data[chat_id_str]:
            data[chat_id_str]["is_premium"] = False
        if "checklists" not in data[chat_id_str]:
            # Move old tasks to a new "Daily" checklist
            old_tasks = data[chat_id_str].get("tasks", [])
            old_done = data[chat_id_str].get("done", [])
            data[chat_id_str]["checklists"] = {
                "Daily": {"tasks": old_tasks, "done": old_done}
            }
            # Remove the old top-level keys
            if "tasks" in data[chat_id_str]:
                del data[chat_id_str]["tasks"]
            if "done" in data[chat_id_str]:
                del data[chat_id_str]["done"]
    
    save_data(data)
    return data[chat_id_str]


# -----------------------
# UI: Checklist with buttons
# -----------------------
def get_checklist_markup(chat_id, checklist_name="Daily"):
    """
    Generates an inline keyboard markup for a specific checklist.
    Returns a blank markup if the checklist doesn't exist.
    """
    user_data = ensure_user_exists(chat_id)
    checklist = user_data["checklists"].get(checklist_name, {"tasks": [], "done": []})
    
    buttons = []
    for i, task in enumerate(checklist["tasks"]):
        # Check if the task is done by its index
        done = i in checklist["done"]
        label = f"✅ {task}" if done else f"⬜️ {task}"
        
        # The callback data needs to include the checklist name to be specific
        buttons.append([InlineKeyboardButton(label, callback_data=f"toggle_{checklist_name}_{i}")])
    
    # Add a close button to dismiss the checklist
    if buttons:
        buttons.append([InlineKeyboardButton("Close", callback_data=f"close_{checklist_name}")])

    return InlineKeyboardMarkup(buttons)

def get_checklist_list_markup(chat_id):
    """Generates a list of all checklists for a user."""
    user_data = ensure_user_exists(chat_id)
    buttons = []
    
    for name in user_data["checklists"]:
        buttons.append([InlineKeyboardButton(name, callback_data=f"showlist_{name}")])
        
    return InlineKeyboardMarkup(buttons)


def send_checklist_message(chat_id, checklist_name="Daily"):
    """Sends a checklist message to the user."""
    today = datetime.datetime.now().strftime("%A, %d %B %Y")
    bot.send_message(
        chat_id=chat_id,
        text=f"📋 *Today's Checklist* — {today}\n\nList: *{checklist_name}*",
        parse_mode="Markdown",
        reply_markup=get_checklist_markup(chat_id, checklist_name)
    )

def send_premium_prompt(chat_id):
    """Sends a prompt to non-premium users to upgrade."""
    bot.send_message(
        chat_id=chat_id,
        text="This is a premium feature. To unlock it, please use the /upgrade command!",
    )

# -----------------------
# Bot Commands
# -----------------------
def start(update: Update, context: CallbackContext):
    """Handles the /start command."""
    chat_id = update.message.chat_id
    ensure_user_exists(chat_id)
    update.message.reply_text(
        "Hi! Welcome to the Checklist Bot. Use /add <task> to add a task, "
        "/show to view your default checklist, and /upgrade to see premium options."
    )

def add_task(update: Update, context: CallbackContext):
    """
    Adds a task to a specific checklist.
    For non-premium users, it adds to the "Daily" list.
    Premium users can specify the checklist name.
    """
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)

    args = context.args
    if not args:
        update.message.reply_text("Usage: /add <task>")
        return

    # Check if the user is premium
    if user_data["is_premium"]:
        if len(args) < 2:
            update.message.reply_text("Usage: /add <checklist_name> <task>")
            return
        
        checklist_name = args[0]
        task = " ".join(args[1:])
        
        # Ensure the specified checklist exists before adding the task
        if checklist_name not in user_data["checklists"]:
            update.message.reply_text(f"Checklist '{checklist_name}' does not exist. Please create it with /new_checklist first.")
            return

        user_data["checklists"][checklist_name]["tasks"].append(task)
        save_data({str(chat_id): user_data})
        update.message.reply_text(f"✅ Task '{task}' added to list '{checklist_name}'.")

    else:
        # Non-premium user, add to the default "Daily" list
        task = " ".join(args)
        user_data["checklists"]["Daily"]["tasks"].append(task)
        save_data({str(chat_id): user_data})
        update.message.reply_text(
            f"✅ Task added to your default list: {task}\n"
            f"Unlock multiple checklists with /upgrade!"
        )

def show_checklist(update: Update, context: CallbackContext):
    """
    Shows a specific checklist.
    Non-premium users are shown the "Daily" list.
    Premium users can specify the list name.
    """
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)
    
    args = context.args
    
    if not user_data["is_premium"]:
        send_checklist_message(chat_id, "Daily")
        return
        
    if not args:
        # If no argument, show a list of all checklists
        update.message.reply_text(
            "Please select a checklist to view:",
            reply_markup=get_checklist_list_markup(chat_id)
        )
        return
    
    checklist_name = args[0]
    if checklist_name in user_data["checklists"]:
        send_checklist_message(chat_id, checklist_name)
    else:
        update.message.reply_text(f"Checklist '{checklist_name}' not found.")

def new_checklist(update: Update, context: CallbackContext):
    """
    Creates a new checklist. This is a premium feature.
    """
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)

    if not user_data["is_premium"]:
        send_premium_prompt(chat_id)
        return

    if not context.args:
        update.message.reply_text("Usage: /new_checklist <name>")
        return

    checklist_name = " ".join(context.args)
    if checklist_name in user_data["checklists"]:
        update.message.reply_text(f"A checklist named '{checklist_name}' already exists.")
        return
    
    user_data["checklists"][checklist_name] = {"tasks": [], "done": []}
    save_data({str(chat_id): user_data})
    update.message.reply_text(f"🎉 New checklist '{checklist_name}' created!")


def upgrade_premium(update: Update, context: CallbackContext):
    """
    Handles the /upgrade command and sends an invoice for premium access.
    The payment tiers are:
    1 star for the first option, 5 for the second, and so on.
    """
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)

    if user_data["is_premium"]:
        update.message.reply_text("You already have premium access!")
        return

    # Define the payment tiers. Prices are in the smallest currency unit.
    # XTR is the currency code for Telegram Stars.
    prices = [
        LabeledPrice("Premium Access (1 Month)", 1 * 100),
        LabeledPrice("Premium Access (1 Year)", 5 * 100)
    ]
    
    # We can add more options here
    
    context.bot.send_invoice(
        chat_id=chat_id,
        title="Premium Checklist Access",
        description="Unlock multiple named checklists and other premium features!",
        payload="premium_subscription",  # A unique string to identify this transaction
        provider_token=PROVIDER_TOKEN,
        currency="XTR",
        prices=prices,
        start_parameter="premium-bot",
        is_flexible=False
    )

# -----------------------
# Payment Handlers
# -----------------------
def pre_checkout_callback(update: Update, context: CallbackContext):
    """
    Handles pre-checkout queries.
    This is called when a user clicks the "Pay" button.
    You can use this to perform final checks before payment.
    """
    query = update.pre_checkout_query
    if query.invoice_payload != "premium_subscription":
        query.answer(ok=False, error_message="Something went wrong with the payment.")
    else:
        query.answer(ok=True) # All good, let the user proceed

def successful_payment_callback(update: Update, context: CallbackContext):
    """
    Handles successful payments.
    This is called when a payment is completed.
    We grant premium access here.
    """
    chat_id = update.message.chat_id
    data = load_data()
    user_data = data.get(str(chat_id), {})
    
    user_data["is_premium"] = True
    data[str(chat_id)] = user_data
    save_data(data)
    
    update.message.reply_text("🎉 Thank you for your payment! You now have premium access.")

# -----------------------
# Button Handlers
# -----------------------
def button_handler(update: Update, context: CallbackContext):
    """Handles all inline keyboard button presses."""
    query = update.callback_query
    chat_id = query.message.chat_id
    query.answer()  # Always answer the callback query

    data = load_data()
    user_data = data.get(str(chat_id), {})

    callback_data = query.data
    
    if callback_data.startswith("toggle_"):
        # Example callback: toggle_Daily_0
        parts = callback_data.split("_")
        checklist_name = parts[1]
        index = int(parts[2])
        
        checklist = user_data["checklists"].get(checklist_name)
        if checklist:
            if index in checklist["done"]:
                checklist["done"].remove(index)
            else:
                checklist["done"].append(index)
            save_data(data)
            
            # Update the message with the new checklist state
            query.edit_message_reply_markup(
                reply_markup=get_checklist_markup(chat_id, checklist_name)
            )
            
    elif callback_data.startswith("showlist_"):
        # Show a specific checklist from the list of options
        checklist_name = callback_data.split("_")[1]
        send_checklist_message(chat_id, checklist_name)
        
    elif callback_data.startswith("close_"):
        # Close the checklist message
        query.edit_message_text(f"Checklist '{callback_data.split('_')[1]}' closed.")


# -----------------------
# Daily Reset
# -----------------------
def reset_tasks():
    """Resets all 'done' statuses for all users' checklists daily."""
    logger.info("Running daily task reset.")
    data = load_data()
    for user_id, user_data in data.items():
        for checklist_name in user_data["checklists"]:
            user_data["checklists"][checklist_name]["done"] = []
            
        save_data(data)
        
        # Send a new checklist message to all users
        # For premium users, we can't guess which checklist to show.
        # So we'll just send a general greeting.
        # For non-premium users, we show the daily checklist.
        if user_data.get("is_premium"):
            bot.send_message(
                chat_id=user_id,
                text="A new day has begun! Use /show to see your checklists."
            )
        else:
            send_checklist_message(user_id, "Daily")

# Schedule the daily reset at 8 AM.
schedule.every().day.at("08:00").do(reset_tasks)

# -----------------------
# Main bot runner
# -----------------------
def main():
    """Starts the bot."""
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # Command handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add_task))
    dp.add_handler(CommandHandler("show", show_checklist))
    dp.add_handler(CommandHandler("new_checklist", new_checklist))
    dp.add_handler(CommandHandler("upgrade", upgrade_premium))
    
    # Callback query handler for buttons
    dp.add_handler(CallbackQueryHandler(button_handler))
    
    # Payment handlers
    dp.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    dp.add_handler(MessageHandler(Filters.successful_payment, successful_payment_callback))

    # Start the Bot
    updater.start_polling()

    # Keep schedule running in background
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == '__main__':
    main()

