import telebot
import google.generativeai as genai
import subprocess
import json
import os
from config import GEMINI_API_KEY  # Берем ключ из твоего локального конфига

# Твои данные
TG_TOKEN = "8909396898:AAE0OtE0lamhaMPyOW6_Ys1iN1cwBOYnd-c"
USER_ID = 7285099714

bot = telebot.TeleBot(TG_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Словарь для хранения запущенных процессов, чтобы мы могли их останавливать
active_processes = {}

def parse_command_with_ai(user_text):
    prompt = f"""
    Проанализируй команду пользователя: "{user_text}".
    Пойми, что он хочет сделать: запустить публикацию (start) или остановить (stop).
    Найди все номера вилл, которые он упоминает.
    Верни ответ СТРОГО в формате JSON: {{"action": "start" или "stop", "villas": ["номер1", "номер2"]}}.
    Никакого лишнего текста, только JSON.
    """
    try:
        response = model.generate_content(prompt)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    except Exception as e:
        print("Ошибка Gemini:", e)
        return None

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if message.chat.id != USER_ID:
        bot.send_message(message.chat.id, "Доступ запрещен.")
        return

    bot.send_message(message.chat.id, "🧠 Думаю...")
    
    parsed_data = parse_command_with_ai(message.text)
    
    if not parsed_data:
        bot.send_message(message.chat.id, "❌ Не понял команду. Напиши проще, например: 'старт 326' или 'останови 308'.")
        return

    action = parsed_data.get("action")
    villas = parsed_data.get("villas", [])

    if not villas:
        bot.send_message(message.chat.id, "⚠️ Не нашел номера вилл в твоем сообщении.")
        return

    if action == "start":
        for villa in villas:
            if villa in active_processes:
                bot.send_message(message.chat.id, f"Вилла {villa} уже в процессе публикации!")
                continue
            
            # ЗАМЕНИ "ТВОЙ_ID_ПРОФИЛЯ_MORELOGIN" НА РЕАЛЬНЫЙ ID ИЗ MORELOGIN
            profile_id = "ТВОЙ_ID_ПРОФИЛЯ_MORELOGIN" 
            
            process = subprocess.Popen(["python3", "test.py", profile_id, f"{villa}:bali"])
            active_processes[villa] = process
            
        bot.send_message(message.chat.id, f"✅ Дал команду на старт вилл: {', '.join(villas)}")

    elif action == "stop":
        for villa in villas:
            if villa in active_processes:
                process = active_processes[villa]
                process.terminate()
                del active_processes[villa]
                bot.send_message(message.chat.id, f"🛑 Публикация виллы {villa} остановлена.")
            else:
                bot.send_message(message.chat.id, f"Вилла {villa} сейчас не публикуется.")

print("Бот-пульт запущен. Ожидаю команд...")
bot.polling(none_stop=True)
