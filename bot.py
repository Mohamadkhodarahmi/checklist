import os
import json
import datetime
import schedule
import time
import logging
import threading
import uuid
from typing import Dict, List, Optional
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
from telegram.error import TelegramError, BadRequest

# -----------------------
# Setup Enhanced Logging
# -----------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# -----------------------
# Config from Railway env vars
# -----------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Please set TOKEN environment variable in Railway.")

# For Telegram Stars, this should be empty
PROVIDER_TOKEN = ""

# Persistent storage path
TASK_FILE = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "tasks.json")

bot = Bot(token=TOKEN)

# -----------------------
# Enhanced Data Models
# -----------------------
class Task:
    def __init__(self, text: str, task_id: str = None, created_at: str = None):
        self.id = task_id or str(uuid.uuid4())
        self.text = text
        self.created_at = created_at or datetime.datetime.now().isoformat()
        self.completed = False

    def to_dict(self):
        return {
            "id": self.id,
            "text": self.text,
            "created_at": self.created_at,
            "completed": self.completed
        }

    @classmethod
    def from_dict(cls, data):
        task = cls(data["text"], data["id"], data["created_at"])
        task.completed = data.get("completed", False)
        return task

class Checklist:
    def __init__(self, name: str, tasks: List[Task] = None):
        self.name = name
        self.tasks = tasks or []

    def add_task(self, text: str) -> Task:
        task = Task(text)
        self.tasks.append(task)
        return task

    def get_task_by_id(self, task_id: str) -> Optional[Task]:
        return next((task for task in self.tasks if task.id == task_id), None)

    def toggle_task(self, task_id: str) -> bool:
        task = self.get_task_by_id(task_id)
        if task:
            task.completed = not task.completed
            return True
        return False

    def remove_task(self, task_id: str) -> bool:
        task = self.get_task_by_id(task_id)
        if task:
            self.tasks.remove(task)
            return True
        return False

    def get_progress(self) -> tuple:
        completed = sum(1 for task in self.tasks if task.completed)
        total = len(self.tasks)
        return completed, total

    def reset_all(self):
        for task in self.tasks:
            task.completed = False

    def to_dict(self):
        return {
            "name": self.name,
            "tasks": [task.to_dict() for task in self.tasks]
        }

    @classmethod
    def from_dict(cls, data):
        checklist = cls(data["name"])
        checklist.tasks = [Task.from_dict(task_data) for task_data in data.get("tasks", [])]
        return checklist

# -----------------------
# Enhanced Data Storage
# -----------------------
def load_data():
    """Loads bot data from the JSON file with error handling."""
    try:
        with open(TASK_FILE, "r", encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"Data loaded successfully for {len(data)} users")
            return data
    except FileNotFoundError:
        logger.info("No existing data file found, starting fresh")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON: {e}")
        # Backup corrupted file and start fresh
        backup_name = f"{TASK_FILE}.backup.{int(time.time())}"
        os.rename(TASK_FILE, backup_name)
        logger.info(f"Corrupted file backed up as {backup_name}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error loading data: {e}")
        return {}

def save_data(data):
    """Saves bot data to the JSON file with atomic write."""
    try:
        # Atomic write: write to temp file first, then rename
        temp_file = f"{TASK_FILE}.tmp"
        os.makedirs(os.path.dirname(TASK_FILE), exist_ok=True)
        
        with open(temp_file, "w", encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        os.rename(temp_file, TASK_FILE)
        logger.debug(f"Data saved successfully for {len(data)} users")
    except Exception as e:
        logger.error(f"Error saving data: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

def ensure_user_exists(chat_id):
    """Ensures a user has an entry in the data file with migration support."""
    data = load_data()
    chat_id_str = str(chat_id)
    
    if chat_id_str not in data:
        # New user
        data[chat_id_str] = {
            "is_premium": False,
            "premium_expires": None,
            "checklists": {
                "Daily": Checklist("Daily").to_dict()
            },
            "settings": {
                "daily_reset_time": "08:00",
                "timezone": "UTC",
                "notifications_enabled": True
            }
        }
    else:
        # Migrate old format if needed
        user_data = data[chat_id_str]
        
        # Add missing keys
        if "is_premium" not in user_data:
            user_data["is_premium"] = False
        if "premium_expires" not in user_data:
            user_data["premium_expires"] = None
        if "settings" not in user_data:
            user_data["settings"] = {
                "daily_reset_time": "08:00",
                "timezone": "UTC",
                "notifications_enabled": True
            }
        
        # Migrate old task format
        if "checklists" not in user_data:
            old_tasks = user_data.get("tasks", [])
            old_done = user_data.get("done", [])
            
            # Convert old format to new format
            daily_checklist = Checklist("Daily")
            for i, task_text in enumerate(old_tasks):
                task = daily_checklist.add_task(task_text)
                if i in old_done:
                    task.completed = True
            
            user_data["checklists"] = {
                "Daily": daily_checklist.to_dict()
            }
            
            # Clean up old keys
            for old_key in ["tasks", "done"]:
                if old_key in user_data:
                    del user_data[old_key]
        else:
            # Ensure all checklists are in new format
            for name, checklist_data in user_data["checklists"].items():
                if "tasks" in checklist_data and isinstance(checklist_data["tasks"], list):
                    # Check if tasks are in old format (list of strings with separate done list)
                    if (checklist_data["tasks"] and 
                        isinstance(checklist_data["tasks"][0], str)):
                        # Convert old format
                        old_tasks = checklist_data["tasks"]
                        old_done = checklist_data.get("done", [])
                        
                        checklist = Checklist(name)
                        for i, task_text in enumerate(old_tasks):
                            task = checklist.add_task(task_text)
                            if i in old_done:
                                task.completed = True
                        
                        user_data["checklists"][name] = checklist.to_dict()
    
    save_data(data)
    return data[chat_id_str]

def is_user_premium(chat_id) -> bool:
    """Check if user has active premium subscription."""
    user_data = ensure_user_exists(chat_id)
    if not user_data["is_premium"]:
        return False
    
    # Check if premium has expired
    if user_data.get("premium_expires"):
        expiry = datetime.datetime.fromisoformat(user_data["premium_expires"])
        if datetime.datetime.now() > expiry:
            # Premium expired, update status
            data = load_data()
            data[str(chat_id)]["is_premium"] = False
            data[str(chat_id)]["premium_expires"] = None
            save_data(data)
            return False
    
    return True

# -----------------------
# Enhanced UI Functions
# -----------------------
def get_checklist_markup(chat_id, checklist_name="Daily"):
    """Generates an enhanced inline keyboard markup for a specific checklist."""
    user_data = ensure_user_exists(chat_id)
    checklist_data = user_data["checklists"].get(checklist_name)
    
    if not checklist_data:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Checklist not found", callback_data="noop")]])
    
    checklist = Checklist.from_dict(checklist_data)
    buttons = []
    
    # Progress bar
    completed, total = checklist.get_progress()
    progress_text = f"Progress: {completed}/{total}"
    if total > 0:
        percentage = int((completed / total) * 100)
        progress_bar = "▓" * (percentage // 10) + "░" * (10 - percentage // 10)
        progress_text = f"{progress_bar} {percentage}%"
    
    buttons.append([InlineKeyboardButton(progress_text, callback_data="noop")])
    
    # Task buttons
    for task in checklist.tasks:
        icon = "✅" if task.completed else "⬜️"
        label = f"{icon} {task.text}"
        # Truncate long task names
        if len(label) > 35:
            label = label[:32] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"toggle_{checklist_name}_{task.id}")])
    
    # Action buttons row
    action_buttons = []
    if is_user_premium(chat_id):
        action_buttons.append(InlineKeyboardButton("➕ Add", callback_data=f"add_{checklist_name}"))
        action_buttons.append(InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_mode_{checklist_name}"))
    
    action_buttons.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{checklist_name}"))
    action_buttons.append(InlineKeyboardButton("❌ Close", callback_data=f"close_{checklist_name}"))
    
    if action_buttons:
        # Split into rows if too many buttons
        if len(action_buttons) > 2:
            buttons.append(action_buttons[:2])
            buttons.append(action_buttons[2:])
        else:
            buttons.append(action_buttons)
    
    return InlineKeyboardMarkup(buttons)

def get_checklist_list_markup(chat_id):
    """Generates an enhanced list of all checklists for a user."""
    user_data = ensure_user_exists(chat_id)
    buttons = []
    
    for name, checklist_data in user_data["checklists"].items():
        checklist = Checklist.from_dict(checklist_data)
        completed, total = checklist.get_progress()
        
        # Add progress indicator
        if total > 0:
            percentage = int((completed / total) * 100)
            progress_emoji = "🟢" if percentage == 100 else "🟡" if percentage > 0 else "🔴"
            label = f"{progress_emoji} {name} ({completed}/{total})"
        else:
            label = f"📋 {name} (empty)"
        
        buttons.append([InlineKeyboardButton(label, callback_data=f"showlist_{name}")])
    
    # Add management buttons for premium users
    if is_user_premium(chat_id):
        buttons.append([
            InlineKeyboardButton("➕ New List", callback_data="create_new_list"),
            InlineKeyboardButton("⚙️ Settings", callback_data="settings")
        ])
    
    return InlineKeyboardMarkup(buttons)

def send_checklist_message(chat_id, checklist_name="Daily"):
    """Sends an enhanced checklist message to the user."""
    today = datetime.datetime.now().strftime("%A, %d %B %Y")
    user_data = ensure_user_exists(chat_id)
    checklist_data = user_data["checklists"].get(checklist_name)
    
    if not checklist_data:
        bot.send_message(chat_id=chat_id, text=f"❌ Checklist '{checklist_name}' not found.")
        return
    
    checklist = Checklist.from_dict(checklist_data)
    completed, total = checklist.get_progress()
    
    # Create a more informative header
    header = f"📋 *{checklist_name}* — {today}\n"
    if total > 0:
        percentage = int((completed / total) * 100)
        header += f"Progress: {completed}/{total} ({percentage}%)\n"
    else:
        header += "No tasks yet. Add some tasks to get started!\n"
    
    try:
        bot.send_message(
            chat_id=chat_id,
            text=header,
            parse_mode="Markdown",
            reply_markup=get_checklist_markup(chat_id, checklist_name)
        )
    except TelegramError as e:
        logger.error(f"Error sending checklist message: {e}")
        bot.send_message(chat_id=chat_id, text="❌ Error displaying checklist. Please try again.")

def send_premium_prompt(chat_id):
    """Sends an enhanced prompt to non-premium users to upgrade."""
    premium_features = [
        "• Multiple named checklists",
        "• Task deletion and editing",
        "• Custom daily reset times",
        "• Progress statistics",
        "• Export/import checklists"
    ]
    
    message = (
        "🌟 *Premium Features:*\n\n"
        + "\n".join(premium_features) +
        "\n\nUpgrade now with `/upgrade` for just 1-5 Telegram Stars!"
    )
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Upgrade Now", callback_data="upgrade_prompt")
    ]])
    
    bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# -----------------------
# Enhanced Bot Commands
# -----------------------
def start(update: Update, context: CallbackContext):
    """Handles the /start command with enhanced welcome message."""
    chat_id = update.message.chat_id
    ensure_user_exists(chat_id)
    
    welcome_text = (
        "🎯 *Welcome to Advanced Checklist Bot!*\n\n"
        "📝 *Basic Commands:*\n"
        "• `/add <task>` - Add a task\n"
        "• `/show` - View your checklist\n"
        "• `/help` - Show all commands\n\n"
        "⭐ *Premium Features:*\n"
        "• Multiple checklists\n"
        "• Task management\n"
        "• Custom settings\n\n"
        "Type `/upgrade` to unlock premium features!"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Checklist", callback_data="showlist_Daily")],
        [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade_prompt")]
    ])
    
    update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

def help_command(update: Update, context: CallbackContext):
    """Provides comprehensive help."""
    chat_id = update.message.chat_id
    is_premium = is_user_premium(chat_id)
    
    basic_commands = [
        "`/start` - Welcome message and quick access",
        "`/add <task>` - Add a new task to your default list",
        "`/show` - Display your checklist(s)",
        "`/help` - Show this help message"
    ]
    
    premium_commands = [
        "`/add <list> <task>` - Add task to specific list",
        "`/new_checklist <name>` - Create a new checklist",
        "`/delete_checklist <name>` - Delete a checklist",
        "`/settings` - Manage your preferences",
        "`/stats` - View your productivity statistics"
    ]
    
    help_text = "📚 *Bot Commands:*\n\n*Basic Commands:*\n" + "\n".join(basic_commands)
    
    if is_premium:
        help_text += "\n\n*Premium Commands:*\n" + "\n".join(premium_commands)
    else:
        help_text += "\n\n*Premium Commands:*\n(Available with `/upgrade`)\n" + "\n".join(premium_commands)
        help_text += "\n\n⭐ Upgrade for just 1-5 Telegram Stars to unlock all features!"
    
    update.message.reply_text(help_text, parse_mode="Markdown")

def add_task(update: Update, context: CallbackContext):
    """Enhanced task addition with better argument parsing."""
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)
    args = context.args

    if not args:
        update.message.reply_text(
            "❌ *Usage:*\n"
            "• `/add <task>` - Add to default list\n" +
            ("• `/add <checklist> <task>` - Add to specific list (Premium)" if is_user_premium(chat_id) else "")
            , parse_mode="Markdown"
        )
        return

    data = load_data()

    if is_user_premium(chat_id):
        if len(args) >= 2:
            # Try to interpret first arg as checklist name
            potential_checklist = args[0]
            if potential_checklist in user_data["checklists"]:
                checklist_name = potential_checklist
                task_text = " ".join(args[1:])
            else:
                # First arg is not a checklist, treat whole thing as task for Daily
                checklist_name = "Daily"
                task_text = " ".join(args)
        else:
            checklist_name = "Daily"
            task_text = " ".join(args)
    else:
        checklist_name = "Daily"
        task_text = " ".join(args)

    # Add the task
    checklist_data = user_data["checklists"].get(checklist_name)
    if not checklist_data:
        update.message.reply_text(f"❌ Checklist '{checklist_name}' does not exist.")
        return
    
    checklist = Checklist.from_dict(checklist_data)
    task = checklist.add_task(task_text)
    
    # Save back to data
    data[str(chat_id)]["checklists"][checklist_name] = checklist.to_dict()
    save_data(data)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📋 View {checklist_name}", callback_data=f"showlist_{checklist_name}")
    ]])

    update.message.reply_text(
        f"✅ Task added to *{checklist_name}*:\n`{task_text}`",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

def show_checklist(update: Update, context: CallbackContext):
    """Enhanced checklist display."""
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)
    args = context.args
    
    if not is_user_premium(chat_id):
        send_checklist_message(chat_id, "Daily")
        return
        
    if not args:
        # Show checklist selection menu
        update.message.reply_text(
            "📋 *Select a checklist to view:*",
            parse_mode="Markdown",
            reply_markup=get_checklist_list_markup(chat_id)
        )
        return
    
    checklist_name = " ".join(args)
    if checklist_name in user_data["checklists"]:
        send_checklist_message(chat_id, checklist_name)
    else:
        update.message.reply_text(f"❌ Checklist '{checklist_name}' not found.")

def new_checklist(update: Update, context: CallbackContext):
    """Enhanced checklist creation."""
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)

    if not is_user_premium(chat_id):
        send_premium_prompt(chat_id)
        return

    if not context.args:
        update.message.reply_text(
            "❌ *Usage:* `/new_checklist <name>`\n\n"
            "*Examples:*\n"
            "• `/new_checklist Work Tasks`\n"
            "• `/new_checklist Shopping`\n"
            "• `/new_checklist Weekly Goals`",
            parse_mode="Markdown"
        )
        return

    checklist_name = " ".join(context.args)
    
    # Validate checklist name
    if len(checklist_name) > 50:
        update.message.reply_text("❌ Checklist name must be 50 characters or less.")
        return
    
    if checklist_name in user_data["checklists"]:
        update.message.reply_text(f"❌ Checklist '{checklist_name}' already exists.")
        return
    
    # Check premium limit
    if len(user_data["checklists"]) >= 10:  # Reasonable limit
        update.message.reply_text("❌ Maximum of 10 checklists allowed. Delete some first.")
        return

    # Create new checklist
    data = load_data()
    new_checklist = Checklist(checklist_name)
    data[str(chat_id)]["checklists"][checklist_name] = new_checklist.to_dict()
    save_data(data)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 View {checklist_name}", callback_data=f"showlist_{checklist_name}")],
        [InlineKeyboardButton("➕ Add Tasks", callback_data=f"add_{checklist_name}")]
    ])

    update.message.reply_text(
        f"🎉 Checklist '*{checklist_name}*' created successfully!",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

def delete_checklist(update: Update, context: CallbackContext):
    """Delete a checklist (premium feature)."""
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)

    if not is_user_premium(chat_id):
        send_premium_prompt(chat_id)
        return

    if not context.args:
        # Show deletion interface
        buttons = []
        for name in user_data["checklists"]:
            if name != "Daily":  # Protect the Daily checklist
                buttons.append([InlineKeyboardButton(f"🗑️ {name}", callback_data=f"confirm_delete_{name}")])
        
        if not buttons:
            update.message.reply_text("❌ No checklists available for deletion (Daily checklist is protected).")
            return
            
        update.message.reply_text(
            "🗑️ *Select checklist to delete:*\n\n⚠️ This action cannot be undone!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    checklist_name = " ".join(context.args)
    
    if checklist_name == "Daily":
        update.message.reply_text("❌ Cannot delete the Daily checklist.")
        return
        
    if checklist_name not in user_data["checklists"]:
        update.message.reply_text(f"❌ Checklist '{checklist_name}' not found.")
        return

    # Confirmation step
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete_confirmed_{checklist_name}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete")]
    ])

    update.message.reply_text(
        f"⚠️ Are you sure you want to delete '*{checklist_name}*'?\n\nThis will permanently remove all tasks in this checklist.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

def upgrade_premium(update: Update, context: CallbackContext):
    """Fixed premium upgrade with proper Telegram Stars pricing."""
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)

    if is_user_premium(chat_id):
        expiry = user_data.get("premium_expires")
        if expiry:
            expiry_date = datetime.datetime.fromisoformat(expiry).strftime("%B %d, %Y")
            update.message.reply_text(f"⭐ You already have premium access until {expiry_date}!")
        else:
            update.message.reply_text("⭐ You already have premium access!")
        return

    # Fixed pricing for Telegram Stars (prices in stars, not cents)
    prices = [
        LabeledPrice("1 Month Premium", 1),  # 1 star
        LabeledPrice("1 Year Premium", 5),   # 5 stars
    ]
    
    try:
        context.bot.send_invoice(
            chat_id=chat_id,
            title="🌟 Premium Checklist Access",
            description=(
                "Unlock premium features:\n"
                "• Multiple named checklists\n"
                "• Task management tools\n"
                "• Custom settings\n"
                "• Progress statistics"
            ),
            payload="premium_subscription",
            provider_token=PROVIDER_TOKEN,
            currency="XTR",  # Telegram Stars currency
            prices=prices,
            start_parameter="premium-upgrade",
            is_flexible=False
        )
    except BadRequest as e:
        logger.error(f"Payment error: {e}")
        if "Stars_invoice_invalid" in str(e):
            update.message.reply_text(
                "❌ Payment system temporarily unavailable. Please try again later.\n\n"
                "If this persists, please contact support."
            )
        else:
            update.message.reply_text(
                f"❌ Payment error: {str(e)}\n\nPlease try again or contact support."
            )
    except Exception as e:
        logger.error(f"Unexpected payment error: {e}")
        update.message.reply_text("❌ An unexpected error occurred. Please try again later.")

def stats_command(update: Update, context: CallbackContext):
    """Show user productivity statistics (premium feature)."""
    chat_id = update.message.chat_id
    user_data = ensure_user_exists(chat_id)
    
    if not is_user_premium(chat_id):
        send_premium_prompt(chat_id)
        return
    
    total_tasks = 0
    completed_tasks = 0
    total_checklists = len(user_data["checklists"])
    
    checklist_stats = []
    
    for name, checklist_data in user_data["checklists"].items():
        checklist = Checklist.from_dict(checklist_data)
        completed, total = checklist.get_progress()
        total_tasks += total
        completed_tasks += completed
        
        if total > 0:
            percentage = int((completed / total) * 100)
            checklist_stats.append(f"• *{name}*: {completed}/{total} ({percentage}%)")
        else:
            checklist_stats.append(f"• *{name}*: Empty")
    
    overall_percentage = int((completed_tasks / total_tasks) * 100) if total_tasks > 0 else 0
    
    stats_text = (
        "📊 *Your Productivity Stats:*\n\n"
        f"📋 Total Checklists: {total_checklists}\n"
        f"📝 Total Tasks: {total_tasks}\n"
        f"✅ Completed: {completed_tasks}\n"
        f"📈 Overall Progress: {overall_percentage}%\n\n"
        "*Checklist Breakdown:*\n" + "\n".join(checklist_stats)
    )
    
    update.message.reply_text(stats_text, parse_mode="Markdown")

# -----------------------
# Enhanced Payment Handlers
# -----------------------
def pre_checkout_callback(update: Update, context: CallbackContext):
    """Enhanced pre-checkout validation."""
    query = update.pre_checkout_query
    
    try:
        if query.invoice_payload != "premium_subscription":
            logger.warning(f"Invalid payload in pre-checkout: {query.invoice_payload}")
            query.answer(ok=False, error_message="Invalid payment request.")
            return
        
        # Additional validation could be added here
        query.answer(ok=True)
        logger.info(f"Pre-checkout approved for user {query.from_user.id}")
        
    except Exception as e:
        logger.error(f"Error in pre-checkout: {e}")
        query.answer(ok=False, error_message="Payment processing error.")

def successful_payment_callback(update: Update, context: CallbackContext):
    """Enhanced payment success handler with proper premium activation."""
    chat_id = update.message.chat_id
    payment = update.message.successful_payment
    
    try:
        # Determine subscription length based on amount paid
        total_amount = payment.total_amount
        if total_amount == 1:  # 1 star
            duration_days = 30
            plan_name = "1 Month"
        elif total_amount == 5:  # 5 stars
            duration_days = 365
            plan_name = "1 Year"
        else:
            logger.warning(f"Unexpected payment amount: {total_amount}")
            duration_days = 30
            plan_name = "1 Month"
        
        # Calculate expiry date
        expiry_date = datetime.datetime.now() + datetime.timedelta(days=duration_days)
        
        # Update user data
        data = load_data()
        user_data = data.get(str(chat_id), {})
        user_data["is_premium"] = True
        user_data["premium_expires"] = expiry_date.isoformat()
        data[str(chat_id)] = user_data
        save_data(data)
        
        # Send success message with premium features info
        success_message = (
            f"🎉 *Payment Successful!*\n\n"
            f"⭐ Plan: {plan_name} Premium\n"
            f"📅 Expires: {expiry_date.strftime('%B %d, %Y')}\n\n"
            f"🌟 *Premium Features Unlocked:*\n"
            f"• Multiple named checklists\n"
            f"• Task deletion and editing\n"
            f"• Custom daily reset times\n"
            f"• Progress statistics\n"
            f"• Advanced task management\n\n"
            f"Type `/help` to see all available commands!"
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Checklists", callback_data="show_all_lists")],
            [InlineKeyboardButton("➕ Create New List", callback_data="create_new_list")]
        ])
        
        update.message.reply_text(
            success_message,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        logger.info(f"Premium activated for user {chat_id}: {plan_name} plan")
        
    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")
        update.message.reply_text(
            "✅ Payment received! However, there was an issue activating premium features. "
            "Please contact support with your payment details."
        )

# -----------------------
# Enhanced Button Handlers
# -----------------------
def button_handler(update: Update, context: CallbackContext):
    """Enhanced callback query handler with comprehensive functionality."""
    query = update.callback_query
    chat_id = query.message.chat_id
    callback_data = query.data
    
    try:
        query.answer()  # Always answer the callback query
        
        data = load_data()
        user_data = data.get(str(chat_id), {})
        
        if callback_data == "noop":
            return  # Do nothing for progress bars, etc.
        
        elif callback_data.startswith("toggle_"):
            # Toggle task completion: toggle_ChecklistName_TaskID
            parts = callback_data.split("_", 2)
            if len(parts) < 3:
                return
                
            checklist_name = parts[1]
            task_id = parts[2]
            
            checklist_data = user_data["checklists"].get(checklist_name)
            if checklist_data:
                checklist = Checklist.from_dict(checklist_data)
                if checklist.toggle_task(task_id):
                    # Save updated data
                    data[str(chat_id)]["checklists"][checklist_name] = checklist.to_dict()
                    save_data(data)
                    
                    # Update the message
                    query.edit_message_reply_markup(
                        reply_markup=get_checklist_markup(chat_id, checklist_name)
                    )
        
        elif callback_data.startswith("showlist_"):
            # Show specific checklist
            checklist_name = callback_data.split("_", 1)[1]
            send_checklist_message(chat_id, checklist_name)
        
        elif callback_data.startswith("refresh_"):
            # Refresh checklist display
            checklist_name = callback_data.split("_", 1)[1]
            query.edit_message_reply_markup(
                reply_markup=get_checklist_markup(chat_id, checklist_name)
            )
        
        elif callback_data.startswith("close_"):
            # Close checklist
            checklist_name = callback_data.split("_", 1)[1]
            query.edit_message_text(f"📋 Checklist '{checklist_name}' closed.")
        
        elif callback_data.startswith("delete_mode_"):
            # Enter task deletion mode (premium)
            if not is_user_premium(chat_id):
                send_premium_prompt(chat_id)
                return
                
            checklist_name = callback_data.split("_", 2)[2]
            checklist_data = user_data["checklists"].get(checklist_name)
            
            if checklist_data:
                checklist = Checklist.from_dict(checklist_data)
                if not checklist.tasks:
                    query.answer("No tasks to delete!", show_alert=True)
                    return
                
                # Show tasks for deletion
                buttons = []
                for task in checklist.tasks:
                    label = f"🗑️ {task.text[:30]}{'...' if len(task.text) > 30 else ''}"
                    buttons.append([InlineKeyboardButton(
                        label, 
                        callback_data=f"delete_task_{checklist_name}_{task.id}"
                    )])
                
                buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"refresh_{checklist_name}")])
                
                query.edit_message_text(
                    f"🗑️ *Delete Task from {checklist_name}*\n\nSelect a task to delete:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
        
        elif callback_data.startswith("delete_task_"):
            # Delete specific task (premium)
            if not is_user_premium(chat_id):
                return
                
            parts = callback_data.split("_", 3)
            checklist_name = parts[2]
            task_id = parts[3]
            
            checklist_data = user_data["checklists"].get(checklist_name)
            if checklist_data:
                checklist = Checklist.from_dict(checklist_data)
                task = checklist.get_task_by_id(task_id)
                
                if task and checklist.remove_task(task_id):
                    # Save updated data
                    data[str(chat_id)]["checklists"][checklist_name] = checklist.to_dict()
                    save_data(data)
                    
                    query.answer(f"Task '{task.text}' deleted!", show_alert=True)
                    send_checklist_message(chat_id, checklist_name)
        
        elif callback_data.startswith("confirm_delete_"):
            # Confirm checklist deletion
            checklist_name = callback_data.split("_", 2)[2]
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Delete Forever", callback_data=f"delete_confirmed_{checklist_name}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete")]
            ])
            
            query.edit_message_text(
                f"⚠️ *Final Confirmation*\n\n"
                f"Delete checklist '*{checklist_name}*' and all its tasks?\n\n"
                f"This action cannot be undone!",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        
        elif callback_data.startswith("delete_confirmed_"):
            # Actually delete the checklist
            if not is_user_premium(chat_id):
                return
                
            checklist_name = callback_data.split("_", 2)[2]
            
            if checklist_name in user_data["checklists"] and checklist_name != "Daily":
                del data[str(chat_id)]["checklists"][checklist_name]
                save_data(data)
                
                query.edit_message_text(f"✅ Checklist '{checklist_name}' has been deleted.")
            else:
                query.edit_message_text("❌ Error: Could not delete checklist.")
        
        elif callback_data == "cancel_delete":
            query.edit_message_text("❌ Deletion cancelled.")
        
        elif callback_data == "create_new_list":
            # Prompt for new checklist creation
            if not is_user_premium(chat_id):
                send_premium_prompt(chat_id)
                return
                
            query.edit_message_text(
                "➕ *Create New Checklist*\n\n"
                "Use the command: `/new_checklist <n>`\n\n"
                "*Examples:*\n"
                "• `/new_checklist Work Tasks`\n"
                "• `/new_checklist Shopping List`\n"
                "• `/new_checklist Weekly Goals`",
                parse_mode="Markdown"
            )
        
        elif callback_data == "show_all_lists":
            # Show all checklists
            bot.send_message(
                chat_id=chat_id,
                text="📋 *Your Checklists:*",
                parse_mode="Markdown",
                reply_markup=get_checklist_list_markup(chat_id)
            )
        
        elif callback_data == "upgrade_prompt":
            # Trigger upgrade process
            context.bot.send_message(
                chat_id=chat_id,
                text="Preparing upgrade options..."
            )
            upgrade_premium(update, context)
        
        elif callback_data == "settings":
            # Show settings (premium feature)
            if not is_user_premium(chat_id):
                send_premium_prompt(chat_id)
                return
            
            settings = user_data.get("settings", {})
            settings_text = (
                "⚙️ *Your Settings:*\n\n"
                f"🕐 Daily Reset Time: {settings.get('daily_reset_time', '08:00')}\n"
                f"🌍 Timezone: {settings.get('timezone', 'UTC')}\n"
                f"🔔 Notifications: {'Enabled' if settings.get('notifications_enabled', True) else 'Disabled'}\n\n"
                f"Use `/settings` commands to modify these settings."
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕐 Change Reset Time", callback_data="change_reset_time")],
                [InlineKeyboardButton("🔔 Toggle Notifications", callback_data="toggle_notifications")]
            ])
            
            query.edit_message_text(
                settings_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        
        elif callback_data == "toggle_notifications":
            # Toggle notification settings
            if not is_user_premium(chat_id):
                return
                
            settings = user_data.get("settings", {})
            current = settings.get("notifications_enabled", True)
            settings["notifications_enabled"] = not current
            
            data[str(chat_id)]["settings"] = settings
            save_data(data)
            
            status = "enabled" if not current else "disabled"
            query.answer(f"Notifications {status}!", show_alert=True)
            
            # Refresh settings display
            button_handler(update, context)  # Recursively call to refresh settings
        
    except Exception as e:
        logger.error(f"Error in button handler: {e}")
        query.answer("An error occurred. Please try again.", show_alert=True)

# -----------------------
# Enhanced Daily Reset with Threading
# -----------------------
def reset_tasks():
    """Enhanced daily task reset with better user experience."""
    logger.info("Starting daily task reset...")
    
    try:
        data = load_data()
        reset_count = 0
        
        for user_id, user_data in data.items():
            try:
                # Reset all checklists
                for checklist_name, checklist_data in user_data.get("checklists", {}).items():
                    checklist = Checklist.from_dict(checklist_data)
                    checklist.reset_all()
                    user_data["checklists"][checklist_name] = checklist.to_dict()
                
                reset_count += 1
                
                # Send notification if enabled
                settings = user_data.get("settings", {})
                if settings.get("notifications_enabled", True):
                    try:
                        if is_user_premium(int(user_id)):
                            message = (
                                "🌅 *Good morning!*\n\n"
                                "Your checklists have been reset for a new day.\n"
                                "Use `/show` to view your checklists."
                            )
                            keyboard = InlineKeyboardMarkup([[
                                InlineKeyboardButton("📋 View Checklists", callback_data="show_all_lists")
                            ]])
                        else:
                            message = (
                                "🌅 *Good morning!*\n\n"
                                "Your daily checklist has been reset.\n"
                                "Ready for a productive day?"
                            )
                            keyboard = InlineKeyboardMarkup([[
                                InlineKeyboardButton("📋 View Checklist", callback_data="showlist_Daily")
                            ]])
                        
                        bot.send_message(
                            chat_id=int(user_id),
                            text=message,
                            parse_mode="Markdown",
                            reply_markup=keyboard
                        )
                    except Exception as e:
                        logger.warning(f"Could not send reset notification to user {user_id}: {e}")
                        
            except Exception as e:
                logger.error(f"Error resetting tasks for user {user_id}: {e}")
                continue
        
        save_data(data)
        logger.info(f"Daily reset completed for {reset_count} users")
        
    except Exception as e:
        logger.error(f"Critical error in daily reset: {e}")

def run_scheduler():
    """Run the scheduler in a separate thread."""
    logger.info("Scheduler thread started")
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
        except Exception as e:
            logger.error(f"Error in scheduler thread: {e}")
            time.sleep(60)

# Schedule the daily reset
schedule.every().day.at("08:00").do(reset_tasks)

# -----------------------
# Error Handler
# -----------------------
def error_handler(update: object, context: CallbackContext) -> None:
    """Log errors caused by updates."""
    logger.error(f"Exception while handling an update: {context.error}")
    
    # Try to inform the user
    if update and hasattr(update, 'effective_chat') and update.effective_chat:
        try:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ An error occurred while processing your request. Please try again."
            )
        except:
            pass  # If we can't send error message, just log it

# -----------------------
# Enhanced Main Function
# -----------------------
def main():
    """Enhanced bot startup with better error handling and threading."""
    logger.info("Starting Enhanced Checklist Bot...")
    
    try:
        updater = Updater(TOKEN)
        dp = updater.dispatcher

        # Register error handler
        dp.add_error_handler(error_handler)

        # Command handlers
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("help", help_command))
        dp.add_handler(CommandHandler("add", add_task))
        dp.add_handler(CommandHandler("show", show_checklist))
        dp.add_handler(CommandHandler("new_checklist", new_checklist))
        dp.add_handler(CommandHandler("delete_checklist", delete_checklist))
        dp.add_handler(CommandHandler("upgrade", upgrade_premium))
        dp.add_handler(CommandHandler("stats", stats_command))
        
        # Callback query handler for buttons
        dp.add_handler(CallbackQueryHandler(button_handler))
        
        # Payment handlers
        dp.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
        dp.add_handler(MessageHandler(Filters.successful_payment, successful_payment_callback))

        # Start scheduler in background thread
        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()
        logger.info("Scheduler thread started")

        # Start the bot
        logger.info("Bot is starting...")
        updater.start_polling(drop_pending_updates=True)
        logger.info("Bot is now running! Press Ctrl+C to stop.")
        
        # Keep the main thread alive
        updater.idle()
        
    except Exception as e:
        logger.critical(f"Critical error starting bot: {e}")
        raise

if __name__ == '__main__':
    main()