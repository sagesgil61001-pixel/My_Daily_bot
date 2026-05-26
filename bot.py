import os
import json
import logging
from datetime import datetime, date, timedelta, time as dtime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import pytz

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN    = os.environ['TELEGRAM_TOKEN']
CHAT_ID  = int(os.environ['CHAT_ID'])
TZ       = pytz.timezone('Asia/Jerusalem')
TASKS_FILE = 'tasks.json'

# isoweekday: 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 7=Sun
DAY_NAMES = {1: 'שני', 2: 'שלישי', 3: 'רביעי', 4: 'חמישי', 5: 'שישי', 6: 'שבת', 7: 'ראשון'}

# ─── Storage ─────────────────────────────────────────────────────────────────

def load():
    if not os.path.exists(TASKS_FILE):
        data = {"recurring": [], "one_time": [], "carried_over": [], "today_session": None}
        save(data)
        return data
    with open(TASKS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save(data):
    with open(TASKS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def next_id(lst):
    return max((t['id'] for t in lst), default=0) + 1

# ─── Task Logic ───────────────────────────────────────────────────────────────

def build_day_tasks(today: date) -> list:
    data  = load()
    dow   = today.isoweekday()
    today_str = today.isoformat()
    tasks = []

    for t in data['recurring']:
        if dow in t['days']:
            tasks.append({'id': f"r{t['id']}", 'task': t['task'], 'done': False, 'type': 'recurring'})

    for t in data['one_time']:
        if t['date'] == today_str and not t.get('done', False):
            tasks.append({'id': f"o{t['id']}", 'task': t['task'], 'done': False, 'type': 'one_time'})

    for t in data['carried_over']:
        tasks.append({'id': f"c{t['id']}", 'task': t['task'], 'done': False, 'type': 'carried_over'})

    return tasks

# ─── Message Builders ─────────────────────────────────────────────────────────

def morning_text(tasks: list, today: date) -> str:
    day = DAY_NAMES[today.isoweekday()]
    dt  = today.strftime('%d/%m/%Y')
    lines = [f"🌅 <b>בוקר טוב! יום {day}, {dt}</b>\n"]

    if not tasks:
        lines.append("אין משימות להיום ✨")
    else:
        lines.append("<b>המשימות שלך להיום:</b>")
        for i, t in enumerate(tasks, 1):
            prefix = "🔄 " if t['type'] == 'carried_over' else ""
            lines.append(f"{i}. {prefix}{t['task']}")
        lines.append(f"\n<i>סה״כ {len(tasks)} משימות</i>")

    return '\n'.join(lines)

def evening_keyboard(tasks: list) -> InlineKeyboardMarkup:
    buttons = []
    for t in tasks:
        icon = "✅" if t['done'] else "⬜"
        buttons.append([InlineKeyboardButton(f"{icon} {t['task']}", callback_data=f"tog_{t['id']}")])
    buttons.append([InlineKeyboardButton("✔️  סיים את היום", callback_data="finish")])
    return InlineKeyboardMarkup(buttons)

# ─── Scheduled Jobs ───────────────────────────────────────────────────────────

async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    tasks = build_day_tasks(today)

    data = load()
    data['today_session'] = {'date': today.isoformat(), 'tasks': tasks, 'msg_id': None}
    data['carried_over']  = []   # absorbed into session

    # Clean old done one_time tasks (older than 7 days)
    cutoff = (today - timedelta(days=7)).isoformat()
    data['one_time'] = [t for t in data['one_time'] if not (t.get('done') and t['date'] < cutoff)]
    save(data)

    await context.bot.send_message(chat_id=CHAT_ID, text=morning_text(tasks, today), parse_mode='HTML')

async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    data    = load()
    session = data.get('today_session')

    if not session or not session['tasks']:
        await context.bot.send_message(chat_id=CHAT_ID, text="🌙 לא היו משימות להיום.")
        return

    msg = await context.bot.send_message(
        chat_id=CHAT_ID,
        text="🌙 <b>סיום יום — סמן מה עשית:</b>",
        parse_mode='HTML',
        reply_markup=evening_keyboard(session['tasks'])
    )
    data['today_session']['msg_id'] = msg.message_id
    save(data)

# ─── Callback Handlers ────────────────────────────────────────────────────────

async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    task_id = q.data[4:]   # strip "tog_"
    data    = load()
    session = data.get('today_session')
    if not session:
        return

    for t in session['tasks']:
        if t['id'] == task_id:
            t['done'] = not t['done']
            break

    save(data)
    await q.edit_message_reply_markup(evening_keyboard(session['tasks']))

async def cb_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data    = load()
    session = data.get('today_session')
    if not session:
        return

    tasks  = session['tasks']
    done   = [t for t in tasks if t['done']]
    undone = [t for t in tasks if not t['done']]

    # Carry over undone tasks
    co_id = next_id(data.get('carried_over', []))
    for t in undone:
        data['carried_over'].append({'id': co_id, 'task': t['task']})
        co_id += 1

    # Mark one_time tasks as done
    for t in done:
        if t['id'].startswith('o'):
            ot_id = int(t['id'][1:])
            for ot in data['one_time']:
                if ot['id'] == ot_id:
                    ot['done'] = True

    data['today_session'] = None
    save(data)

    lines = ["<b>✅ סיכום יום:</b>", f"הושלמו: {len(done)} משימות"]
    if undone:
        lines.append(f"עוברות למחר: {len(undone)} 🔄")
        for t in undone:
            lines.append(f"  • {t['task']}")
    else:
        lines.append("כל המשימות הושלמו! 🎉")

    await q.edit_message_text('\n'.join(lines), parse_mode='HTML')

# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"✅ הבוט פעיל!\n\nה-Chat ID שלך הוא:\n<code>{cid}</code>\n\nהוסף אותו ל-Railway כ-CHAT_ID",
        parse_mode='HTML'
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    tasks = build_day_tasks(today)
    await update.message.reply_text(morning_text(tasks, today), parse_mode='HTML')

async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("שימוש: /tomorrow משימה כלשהי")
        return
    task     = ' '.join(context.args)
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    data     = load()
    data['one_time'].append({'id': next_id(data['one_time']), 'task': task, 'date': tomorrow, 'done': False})
    save(data)
    await update.message.reply_text(f"✅ נוסף למחר:\n{task}")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("שימוש: /add DD/MM משימה כלשהי")
        return
    try:
        d, m = context.args[0].split('/')
        y    = datetime.now(TZ).year
        td   = date(y, int(m), int(d))
        if td < datetime.now(TZ).date():
            td = date(y + 1, int(m), int(d))
    except Exception:
        await update.message.reply_text("❌ פורמט שגוי. שימוש: /add DD/MM משימה")
        return
    task = ' '.join(context.args[1:])
    data = load()
    data['one_time'].append({'id': next_id(data['one_time']), 'task': task, 'date': td.isoformat(), 'done': False})
    save(data)
    await update.message.reply_text(f"✅ נוסף ל-{context.args[0]}:\n{task}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 <b>פקודות:</b>\n\n"
        "/today — הצג משימות של היום\n"
        "/tomorrow [משימה] — הוסף משימה למחר\n"
        "/add DD/MM [משימה] — הוסף משימה לתאריך ספציפי\n"
        "/help — הצג עזרה זו"
    )
    await update.message.reply_text(text, parse_mode='HTML')

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("today",    cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("add",      cmd_add))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CallbackQueryHandler(cb_toggle, pattern=r'^tog_'))
    app.add_handler(CallbackQueryHandler(cb_finish, pattern=r'^finish$'))

    jq = app.job_queue
    jq.run_daily(job_morning, time=dtime(9,  30, tzinfo=TZ), name="morning")
    jq.run_daily(job_evening, time=dtime(22, 30, tzinfo=TZ), name="evening")

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
