import logging
import os
import threading
import datetime
from flask import Flask, render_template, jsonify, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
import pymongo
from bson.objectid import ObjectId
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# 1. CONFIGURATION & DATABASE
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    MONGO_URI = os.getenv("MONGO_URI")
    ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
    ADMIN_ID = os.getenv("ADMIN_ID") # Sirf ye ID delete/upload kar sakti hai
except Exception as e:
    logger.error(f"Config Error: {e}")

# Database Connection (Same pattern as your code)
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client["StudyMaterialDB"]
    files_col = db["files"] # Stores folders and files
    logger.info("✅ MongoDB Connected!")
except Exception as e:
    logger.error(f"❌ DB Failed: {e}")

# ==============================================================================
# 2. FLASK SERVER & ADMIN API
# ==============================================================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Study Bot is Online 🟢 <br> Go to /admin?pass=YOUR_PASS", 200

@app.route('/admin')
def admin_page():
    if request.args.get('pass') != ADMIN_PASS: return "<h1>❌ ACCESS DENIED</h1>"
    return render_template('admin.html')

# --- API: Get Content of a Folder ---
@app.route('/api/get_nodes', methods=['GET'])
def get_nodes():
    if request.args.get('pass') != ADMIN_PASS: return jsonify({"error": "Auth Failed"})
    parent_id = request.args.get('parent_id')
    
    query = {"parent_id": parent_id} if parent_id and parent_id != "root" else {"parent_id": None}
    nodes = list(files_col.find(query))
    
    # Convert ObjectId to string for JSON
    for node in nodes:
        node['_id'] = str(node['_id'])
    
    return jsonify(nodes)

# --- API: Create Folder/File ---
@app.route('/api/create_node', methods=['POST'])
def create_node():
    data = request.json
    if data.get('pass') != ADMIN_PASS: return jsonify({"error": "Auth Failed"})
    
    new_node = {
        "name": data['name'],
        "type": data['type'], # 'folder' or 'file'
        "file_id": data.get('file_id', None), # Only for files
        "parent_id": data.get('parent_id') if data.get('parent_id') != "root" else None,
        "created_at": datetime.datetime.now()
    }
    result = files_col.insert_one(new_node)
    return jsonify({"status": "success", "id": str(result.inserted_id)})

# --- API: Delete Item ---
@app.route('/api/delete_node', methods=['POST'])
def delete_node():
    data = request.json
    if data.get('pass') != ADMIN_PASS: return jsonify({"error": "Auth Failed"})
    
    node_id = data['id']
    # Agar folder hai to uske andar ka sab delete karo (Recursive)
    delete_recursive(node_id)
    return jsonify({"status": "deleted"})

def delete_recursive(node_id):
    # Find children
    children = files_col.find({"parent_id": node_id})
    for child in children:
        delete_recursive(str(child['_id']))
    files_col.delete_one({"_id": ObjectId(node_id)})

# Background Thread for Flask
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def start_background_server():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ==============================================================================
# 3. TELEGRAM BOT HANDLERS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Root folders dikhao
    await show_folder(update, context, None, edit=False)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    action = data[0]
    
    if action == "open":
        folder_id = data[1] if data[1] != "root" else None
        await show_folder(update, context, folder_id, edit=True)
        
    elif action == "file":
        file_db_id = data[1]
        file_doc = files_col.find_one({"_id": ObjectId(file_db_id)})
        if file_doc:
            await context.bot.send_document(chat_id=query.message.chat_id, document=file_doc['file_id'], caption=f"📄 {file_doc['name']}")
        else:
            await query.message.reply_text("File not found or deleted.")

    elif action == "back":
        current_id = data[1]
        if current_id == "None" or current_id == "root":
            await show_folder(update, context, None, edit=True) # Go to Root
        else:
            # Find parent of current folder
            curr_node = files_col.find_one({"_id": ObjectId(current_id)})
            parent = curr_node.get('parent_id')
            await show_folder(update, context, parent, edit=True)

async def show_folder(update: Update, context: ContextTypes.DEFAULT_TYPE, parent_id, edit=False):
    # Database se children fetch karo
    query = {"parent_id": str(parent_id)} if parent_id else {"parent_id": None}
    items = list(files_col.find(query))
    
    keyboard = []
    # Items Buttons
    for item in items:
        if item['type'] == 'folder':
            btn_text = f"📁 {item['name']}"
            callback = f"open:{item['_id']}"
        else:
            btn_text = f"📄 {item['name']}"
            callback = f"file:{item['_id']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
    
    # Back Button Logic
    if parent_id:
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"back:{parent_id}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "📚 **Study Material Library**\nSelect a folder or file:"
    
    if edit:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# --- HELPER: Get File ID ---
# Admin jab bot ko file bhejega, bot uska File ID dega
async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != str(ADMIN_ID): return # Security
    
    doc = update.message.document or update.message.video or update.message.photo[-1]
    if doc:
        file_id = doc.file_id
        file_name = getattr(doc, 'file_name', 'Unknown')
        msg = f"✅ **File Detected!**\n\n**Name:** `{file_name}`\n**File ID:** `{file_id}`\n\nCopy this ID and add to Admin Panel."
        await update.message.reply_text(msg, parse_mode="Markdown")

# ==============================================================================
# 4. LAUNCH
# ==============================================================================
if __name__ == '__main__':
    print("🚀 Starting Admin Server...")
    start_background_server()

    print("🚀 Starting Bot...")
    t_req = HTTPXRequest(connection_pool_size=8, read_timeout=60, write_timeout=60, connect_timeout=60)
    app_bot = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_req).build()

    app_bot.add_handler(CommandHandler('start', start))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    # File ID nikalne ke liye handler
    app_bot.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO, get_file_id))

    app_bot.run_polling()
