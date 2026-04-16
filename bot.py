import os
import random
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import asyncpg
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

load_dotenv()

TOKEN = os.environ[\"TELEGRAM_TOKEN\"]
DATABASE_URL = os.environ[\"DATABASE_URL\"]
STRETCH_RATIO = float(os.environ.get(\"STRETCH_RATIO\", \"0.65\"))
ADMIN_ID = int(os.environ.get(\"ADMIN_USER_ID\", \"0\"))



async def get_db():
    return await asyncpg.connect(DATABASE_URL)

async def setup_db():
    db = await get_db()
    await db.execute(\"\"\"
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            username TEXT,
            hole_size FLOAT DEFAULT 0,
            biggest_ever FLOAT DEFAULT 0,
            last_stretch TIMESTAMPTZ,
            last_attack TIMESTAMPTZ,
            last_hit TIMESTAMPTZ,
            pvp_wins INT DEFAULT 0,
            pvp_losses INT DEFAULT 0,
            cm_stolen FLOAT DEFAULT 0,
            cm_lost FLOAT DEFAULT 0,
            stretch_bonus BOOLEAN DEFAULT FALSE,
            banned BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (user_id, chat_id)
        )
    \"\"\")
    await db.execute(\"\"\"
        CREATE TABLE IF NOT EXISTS stretch_of_day (
            chat_id BIGINT PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            record_cm FLOAT,
            record_date DATE
        )
    \"\"\")
    await db.execute(\"\"\"
        CREATE TABLE IF NOT EXISTS hall_of_fame (
            chat_id BIGINT PRIMARY KEY,
            biggest_stretch_user TEXT,
            biggest_stretch_cm FLOAT DEFAULT 0,
            biggest_hole_user TEXT,
            biggest_hole_cm FLOAT DEFAULT 0
        )
    \"\"\")
    await db.close()



async def get_or_create_user(db, user_id, chat_id, username):
    row = await db.fetchrow(
        \"SELECT * FROM users WHERE user_id=$1 AND chat_id=$2\",
        user_id, chat_id
    )
    if not row:
        await db.execute(
            \"\"\"INSERT INTO users (user_id, chat_id, username)
               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING\"\"\",
            user_id, chat_id, username
        )
        row = await db.fetchrow(
            \"SELECT * FROM users WHERE user_id=$1 AND chat_id=$2\",
            user_id, chat_id
        )
    else:
        await db.execute(
            \"UPDATE users SET username=$3 WHERE user_id=$1 AND chat_id=$2\",
            user_id, chat_id, username
        )
    return row

def on_cooldown(last_time, hours):
    if last_time is None:
        return False
    now = datetime.now(timezone.utc)
    return now - last_time < timedelta(hours=hours)

def format_time_left(last_time, hours):
    now = datetime.now(timezone.utc)
    delta = timedelta(hours=hours) - (now - last_time)
    total = int(delta.total_seconds())
    h, m = divmod(total
    return f\"{h}h {m}m\"



async def stretch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db = await get_db()

    row = await get_or_create_user(db, user.id, chat_id, user.username)

    if row[\"banned\"]:
        await update.message.reply_text(\"You are banned from playing.\")
        await db.close()
        return

    if on_cooldown(row[\"last_stretch\"], 8):
        left = format_time_left(row[\"last_stretch\"], 8)
        await update.message.reply_text(f\"⏰ Cooldown! Come back in {left}.\")
        await db.close()
        return

    growing = random.random() < STRETCH_RATIO
    amount = round(random.uniform(0.1, 15.0), 2)
    if row[\"stretch_bonus\"] and growing:
        amount = round(amount * 1.1, 2)
    if not growing:
        amount = -amount

    new_size = max(0, round(row[\"hole_size\"] + amount, 2))
    new_biggest = max(row[\"biggest_ever\"], new_size)

    await db.execute(
        \"\"\"UPDATE users SET
            hole_size=$3, biggest_ever=$4,
            last_stretch=NOW(), stretch_bonus=FALSE
           WHERE user_id=$1 AND chat_id=$2\"\"\",
        user.id, chat_id, new_size, new_biggest
    )


    sotd = await db.fetchrow(
        \"SELECT * FROM stretch_of_day WHERE chat_id=$1\", chat_id
    )
    today = datetime.now(timezone.utc).date()
    is_new_record = False

    if growing:
        if not sotd or sotd[\"record_date\"] != today or amount > sotd[\"record_cm\"]:
            await db.execute(
                \"\"\"INSERT INTO stretch_of_day
                    (chat_id, user_id, username, record_cm, record_date)
                   VALUES ($1,$2,$3,$4,$5)
                   ON CONFLICT (chat_id) DO UPDATE SET
                    user_id=$2, username=$3,
                    record_cm=$4, record_date=$5\"\"\",
                chat_id, user.id, user.username or \"Anonymous\",
                amount, today
            )
            await db.execute(
                \"UPDATE users SET stretch_bonus=TRUE WHERE user_id=$1 AND chat_id=$2\",
                user.id, chat_id
            )
            is_new_record = True

    emoji = \"🎯\" if growing else \"😬\"
    sign = \"+\" if growing else \"\"
    name = user.username or user.first_name
    msg = f\"{emoji} {name} {'stretched' if growing else 'shrunk'} {sign}{amount}cm!\\nCurrent size: {new_size}cm\"

    if is_new_record:
        msg += \"\\n\\n🏆 NEW STRETCH OF THE DAY! +10% bonus on your next stretch!\"

    await update.message.reply_text(msg)
    await db.close()


async def top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = await get_db()

    rows = await db.fetch(
        \"\"\"SELECT username, hole_size FROM users
           WHERE chat_id=$1 ORDER BY hole_size DESC LIMIT 10\"\"\",
        chat_id
    )

    if not rows:
        await update.message.reply_text(\"No players yet! Use /stretch to start.\")
        await db.close()
        return

    medals = [\"🥇\", \"🥈\", \"🥉\"]
    lines = [\"🏆 *TOP HOLES* 🏆\\n\"]
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else \"▫️\"
        name = row[\"username\"] or \"Anonymous\"
        lines.append(f\"{medal} {i+1}. {name} — {row['hole_size']}cm\")

    await update.message.reply_text(\"\\n\".join(lines), parse_mode=\"Markdown\")
    await db.close()


async def shrink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(\"Usage: /shrink @username\")
        return

    attacker = update.effective_user
    chat_id = update.effective_chat.id
    target_name = ctx.args[0].lstrip(\"@\")
    db = await get_db()

    attacker_row = await get_or_create_user(db, attacker.id, chat_id, attacker.username)

    if attacker_row[\"banned\"]:
        await update.message.reply_text(\"You are banned.\")
        await db.close()
        return

    if on_cooldown(attacker_row[\"last_attack\"], 8):
        left = format_time_left(attacker_row[\"last_attack\"], 8)
        await update.message.reply_text(f\"⏰ Attack cooldown! Come back in {left}.\")
        await db.close()
        return

    target_row = await db.fetchrow(
        \"SELECT * FROM users WHERE chat_id=$1 AND username=$2\",
        chat_id, target_name
    )

    if not target_row:
        await update.message.reply_text(f\"@{target_name} not found! They need to use /stretch first.\")
        await db.close()
        return

    if target_row[\"user_id\"] == attacker.id:
        await update.message.reply_text(\"You can't attack yourself!\")
        await db.close()
        return

    if on_cooldown(target_row[\"last_hit\"], 1):
        await update.message.reply_text(f\"@{target_name} is protected for 1 hour after being attacked!\")
        await db.close()
        return

    attacker_power = random.randint(1, 100)
    defender_power = random.randint(1, 100)
    attacker_wins = attacker_power > defender_power

    shrink_pct = random.uniform(0.10, 0.30)
    shrink_amount = round(target_row[\"hole_size\"] * shrink_pct, 2)

    if attacker_wins:
        new_target_size = max(0, round(target_row[\"hole_size\"] - shrink_amount, 2))
        await db.execute(
            \"\"\"UPDATE users SET last_attack=NOW(), pvp_wins=pvp_wins+1,
               cm_stolen=cm_stolen+$3 WHERE user_id=$1 AND chat_id=$2\"\"\",
            attacker.id, chat_id, shrink_amount
        )
        await db.execute(
            \"\"\"UPDATE users SET hole_size=$3, last_hit=NOW(),
               pvp_losses=pvp_losses+1, cm_lost=cm_lost+$4
               WHERE user_id=$1 AND chat_id=$2\"\"\",
            target_row[\"user_id\"], chat_id, new_target_size, shrink_amount
        )
        a_name = attacker.username or attacker.first_name
        msg = (
            f\"⚔️ *PVP BATTLE* ⚔️\\n\\n\"
            f\"{a_name} ({attacker_power}) VS @{target_name} ({defender_power})\\n\\n\"
            f\"🎯 {a_name} WINS!\\n\"
            f\"@{target_name} shrunk by {shrink_amount}cm → now {new_target_size}cm\"
        )
    else:
        await db.execute(
            \"UPDATE users SET last_attack=NOW(), pvp_losses=pvp_losses+1 WHERE user_id=$1 AND chat_id=$2\",
            attacker.id, chat_id
        )
        await db.execute(
            \"UPDATE users SET last_hit=NOW(), pvp_wins=pvp_wins+1 WHERE user_id=$1 AND chat_id=$2\",
            target_row[\"user_id\"], chat_id
        )
        a_name = attacker.username or attacker.first_name
        msg = (
            f\"⚔️ *PVP BATTLE* ⚔️\\n\\n\"
            f\"{a_name} ({attacker_power}) VS @{target_name} ({defender_power})\\n\\n\"
            f\"🛡️ @{target_name} DEFENDS! No damage dealt.\"
        )

    await update.message.reply_text(msg, parse_mode=\"Markdown\")
    await db.close()


async def mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db = await get_db()

    row = await get_or_create_user(db, user.id, chat_id, user.username)
    total = row[\"pvp_wins\"] + row[\"pvp_losses\"]
    rate = round((row[\"pvp_wins\"] / total) * 100, 1) if total > 0 else 0

    msg = (
        f\"📊 *YOUR STATS*\\n\\n\"
        f\"🕳️ Hole size: {row['hole_size']}cm\\n\"
        f\"🏆 Biggest ever: {row['biggest_ever']}cm\\n\\n\"
        f\"⚔️ PvP: {row['pvp_wins']}W / {row['pvp_losses']}L — {rate}% win rate\\n\"
        f\"📈 CM stolen: {row['cm_stolen']}\\n\"
        f\"📉 CM lost: {row['cm_lost']}\"
    )
    await update.message.reply_text(msg, parse_mode=\"Markdown\")
    await db.close()


async def sotd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = await get_db()

    row = await db.fetchrow(
        \"SELECT * FROM stretch_of_day WHERE chat_id=$1\", chat_id
    )

    if not row:
        await update.message.reply_text(\"👑 No record today yet! Use /stretch to be first.\")
    else:
        today = datetime.now(timezone.utc).date()
        date_str = row[\"record_date\"].strftime(\"%B %d, %Y\")
        msg = (
            f\"👑 *STRETCH OF THE DAY*\\n\\n\"
            f\"🏆 {row['username']} — {row['record_cm']}cm\\n\"
            f\"📅 {date_str}\"
        )
        await update.message.reply_text(msg, parse_mode=\"Markdown\")
    await db.close()


async def halloffame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = await get_db()

    row = await db.fetchrow(
        \"SELECT * FROM hall_of_fame WHERE chat_id=$1\", chat_id
    )

    if not row:
        await update.message.reply_text(\"🏛️ Hall of Fame is empty — make history with /stretch!\")
        await db.close()
        return

    msg = (
        f\"🏛️ *HALL OF FAME*\\n\\n\"
        f\"🎯 Biggest stretch: {row['biggest_stretch_user']} — {row['biggest_stretch_cm']}cm\\n\"
        f\"🕳️ Biggest hole: {row['biggest_hole_user']} — {row['biggest_hole_cm']}cm\"
    )
    await update.message.reply_text(msg, parse_mode=\"Markdown\")
    await db.close()


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if not ctx.args:
        await update.message.reply_text(\"Usage: /admin ban|unban|resetsize|clearcd @username [value]\")
        return

    cmd = ctx.args[0]
    db = await get_db()

    if cmd in (\"ban\", \"unban\") and len(ctx.args) >= 2:
        target = ctx.args[1].lstrip(\"@\")
        banned = cmd == \"ban\"
        await db.execute(
            \"UPDATE users SET banned=$1 WHERE username=$2 AND chat_id=$3\",
            banned, target, update.effective_chat.id
        )
        await update.message.reply_text(f\"{'Banned' if banned else 'Unbanned'} @{target}.\")

    elif cmd == \"resetsize\" and len(ctx.args) >= 2:
        target = ctx.args[1].lstrip(\"@\")
        value = float(ctx.args[2]) if len(ctx.args) >= 3 else 0.0
        await db.execute(
            \"UPDATE users SET hole_size=$1 WHERE username=$2 AND chat_id=$3\",
            value, target, update.effective_chat.id
        )
        await update.message.reply_text(f\"Reset @{target}'s hole to {value}cm.\")

    elif cmd == \"clearcd\" and len(ctx.args) >= 2:
        target = ctx.args[1].lstrip(\"@\")
        await db.execute(
            \"\"\"UPDATE users SET last_stretch=NULL, last_attack=NULL, last_hit=NULL
               WHERE username=$1 AND chat_id=$2\"\"\",
            target, update.effective_chat.id
        )
        await update.message.reply_text(f\"Cleared cooldowns for @{target}.\")

    else:
        await update.message.reply_text(\"Unknown admin command.\")

    await db.close()




async def main():
    await setup_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(\"stretch\", stretch))
    app.add_handler(CommandHandler(\"top\", top))
    app.add_handler(CommandHandler(\"shrink\", shrink))
    app.add_handler(CommandHandler(\"mystats\", mystats))
    app.add_handler(CommandHandler(\"sotd\", sotd))
    app.add_handler(CommandHandler(\"halloffame\", halloffame))
    app.add_handler(CommandHandler(\"admin\", admin))

    print(\"Bot is running...\")
    await app.run_polling()

if __name__ == \"__main__\":
    asyncio.run(main())
