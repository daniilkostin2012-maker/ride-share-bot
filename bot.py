import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_TOKEN")
# Вставь сюда свой токен от BotFather

# Включим логирование (чтобы видеть ошибки)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот для поиска попутчиков на склад.\n"
                                    "Напиши /driver — если ты водитель\n"
                                    "Напиши /passenger — если ты пассажир")

async def driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вы выбрали роль водителя. Скоро я спрошу у вас детали поездки.")

async def passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вы выбрали роль пассажира. Скоро я спрошу у вас точку отправления.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("driver", driver))
    app.add_handler(CommandHandler("passenger", passenger))
    app.run_polling()

if __name__ == '__main__':
    main()