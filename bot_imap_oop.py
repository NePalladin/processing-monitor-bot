import telebot
import threading
import time
import re
import imaplib
import email
import email.utils
from datetime import datetime, timedelta
import json
import os
import logging
import schedule
from dotenv import load_dotenv

# --- 1. Настройка логгера ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class ProcessingMonitorBot:
    def __init__(self):
        # Загрузка секретов
        load_dotenv()
        self.bot_token = os.getenv("BOT_TOKEN")
        self.chat_id = os.getenv("CHAT_ID")
        self.email_acc = os.getenv("EMAIL_ACCOUNT")
        self.email_pass = os.getenv("APP_PASSWORD")
        self.imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")

        # Защита от NDA: скрытые клиенты и целевой БИН для отчетов
        self.hidden_batches = os.getenv("HIDDEN_BATCHES", "CLIENT2").split(",")
        self.target_bin = os.getenv("TARGET_BIN", "0000000")

        self.bot = telebot.TeleBot(self.bot_token)

        # Загрузка конфигурации
        with open("config.json", "r", encoding="utf-8") as f:
            self.config = json.load(f)

        # Маппер для специфичных тем писем EPIN
        self.epin_subjects = self.config.get("EPIN_SUBJECTS", {
            "CLIENT1 VCAPF": "CLIENT1 EPIN VCAPF COMPLETED",
            "CLIENT3": "CLIENT3 EPIN UNDIF COMPLETED"
        })

        # Состояния
        self.is_paused = False
        self.checked_epouts = set()
        self.received_epins = set()
        self.batch_memory = {key: 0 for key in self.config["EPOUT_SCHEDULE"].keys()}

        self.setup_handlers()

    # ==========================================
    # ВНУТРЕННИЕ IMAP-ФУНКЦИИ
    # ==========================================
    def _get_shift_start(self):
        now = datetime.now()
        shift = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now < shift:
            shift -= timedelta(days=1)
        return shift

    def _extract_body(self, msg):
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                    return part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='replace')
        else:
            return msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='replace')
        return ""

    def _connect_and_search(self, subject_filter, folder_name):
        found_emails = []
        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.email_acc, self.email_pass)

            status, _ = mail.select(f'"{folder_name}"')
            if status != "OK":
                mail.select("INBOX")

            shift_start = self._get_shift_start()
            since_date = (shift_start - timedelta(days=1)).strftime("%d-%b-%Y")

            status, data = mail.search(None, f'(SINCE "{since_date}" SUBJECT "{subject_filter}")')

            if status == "OK" and data[0]:
                for m_id in reversed(data[0].split()):
                    status, msg_data = mail.fetch(m_id, '(RFC822)')
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            date_tuple = email.utils.parsedate_tz(msg['Date'])
                            if date_tuple:
                                local_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))
                                if local_date >= shift_start:
                                    found_emails.append((self._extract_body(msg), local_date))
            mail.logout()
        except Exception as e:
            logging.error(f"IMAP Error: {e}")
        return found_emails

    def _get_latest_email(self, subject, folder):
        emails = self._connect_and_search(subject, folder)
        return emails[0] if emails else None

    # ==========================================
    # БИЗНЕС-ЛОГИКА
    # ==========================================
    def daily_reset(self):
        self.checked_epouts.clear()
        self.received_epins.clear()
        self.batch_memory = {key: 0 for key in self.config["EPOUT_SCHEDULE"].keys()}
        self.is_paused = False
        logging.info("🔄 Дневной сброс выполнен.")

    def update_batches(self):
        logging.info("⏳ Запущен парсинг батчей...")
        batch_emails = [e[0] for e in self._connect_and_search("Batch is exported", "BATCH")]
        for body in batch_emails:
            body = body.replace('\n', ' ').replace('\r', '')
            for inst in self.config["EPOUT_SCHEDULE"].keys():
                match = re.search(fr"VISA_{inst}_.*Transaction count (\d+)", body)
                if match:
                    self.batch_memory[inst] = int(match.group(1))
        logging.info(f"✅ Батчи: {self.batch_memory}")
        return self.batch_memory

    def check_epout(self, inst):
        if self.is_paused: return
        config = self.config["EPOUT_SCHEDULE"][inst]
        warnings = []
        epout_subj = f"{inst} EPOUT Job successfully completed!"
        epout_data = self._get_latest_email(epout_subj, "Epout")

        if not epout_data:
            if config.get("check_date_and_batch") and self.batch_memory.get(inst) == 0:
                self.bot.send_message(self.chat_id, f"💬 *{inst} EPOUT:* Пропущено (0 транзакций).", parse_mode="Markdown")
            else:
                self.bot.send_message(self.chat_id, f"🚨 *Письмо {epout_subj} не найдено!*", parse_mode="Markdown")
            return

        body = epout_data[0]
        ret_match = re.search(r"Return Transactions\.+ (\d+)", body)
        if not ret_match or int(ret_match.group(1)) != 0:
            warnings.append("⚠️ Найдены Return Transactions!")

        if config["check_date_and_batch"]:
            yest_str = self._get_shift_start().strftime("%d_%m_%Y")
            date_match = re.search(fr"VISA_{inst}_(\d{{2}}_\d{{2}}_\d{{4}})", body)
            if date_match and date_match.group(1) != yest_str:
                warnings.append(f"🚨 Ахтунг! Файл за {date_match.group(1).replace('_', '.')}!")

            reports = [e[0] for e in self._connect_and_search(f"CMD_FTFRPT Report {datetime.now().strftime('%Y-%m-%d')}", "VCX")]
            total_mon = sum(int(re.search(r"Monetary tran count \d+ (\d+)", r).group(1)) for r in reports if config["cib"] in r and re.search(r"Monetary tran count \d+ (\d+)", r))

            if total_mon != self.batch_memory.get(inst, 0):
                warnings.append(f"🚨 Транзакции не сходятся: Обработано {total_mon}, в батче {self.batch_memory.get(inst, 0)}!")

        msg = f"🚨 *{inst} EPOUT ошибки!*\n\n" + "\n\n".join(warnings) if warnings else f"✅ *{inst} EPOUT complete*"
        self.bot.send_message(self.chat_id, msg, parse_mode="Markdown")

    def check_epin_deadline(self, inst):
        if self.is_paused: return
        if inst not in self.received_epins:
            self.bot.send_message(self.chat_id, f"🚨 *EPIN по {inst} не получен в срок!*", parse_mode="Markdown")

    def check_evening_report(self, time_label):
        if self.is_paused: return
        data = self._get_latest_email("EAS Stats Report", "VCX status reports")
        body = data[0] if data else ""

        if time_label == "19:30":
            missing = []
            for bin_code, d in self.config["INSTITUTES"].items():
                for req in d["requires"]:
                    if not any(bin_code in line and req in line for line in body.split('\n')):
                        missing.append(f"{d['name']} (нет {req})")
            if missing:
                self.is_paused = True
                self.bot.send_message(self.chat_id, f"⚠️ *Нет пакетов в 19:30!*\n{', '.join(missing)}\n_Пауза._", parse_mode="Markdown")
            else:
                self.bot.send_message(self.chat_id, "✅ Отчет за 19:30 проверен.")
        elif time_label == "23:00":
            if not any(self.target_bin in line and "VCAPF" in line for line in body.split('\n')):
                self.bot.send_message(self.chat_id, "🚨 *В 23:00 нет VCAPF по CLIENT1!*", parse_mode="Markdown")
        elif time_label == "23:30":
            if not body:
                self.bot.send_message(self.chat_id, "🚨 *Письмо за 23:30 не найдено!*", parse_mode="Markdown")
            elif "ALL FILES ARE IN PRG*** STATUS" not in " ".join(body.split()):
                self.bot.send_message(self.chat_id, "⚠️ *В 23:30 отчет не пустой!*", parse_mode="Markdown")

    def _check_grace_period(self, client_id, expected_subj, info_subj, target_hour, min_start, min_end):
        """Универсальный метод проверки оконных отчетов"""
        now = datetime.now()
        client_key = f"{client_id}_CONLY"

        if now.hour == target_hour and min_start <= now.minute <= min_end and client_key not in self.checked_epouts:
            ok_data = self._get_latest_email(expected_subj, "Epout")
            info_data = self._get_latest_email(info_subj, "Epout")

            if ok_data:
                match = re.search(r"Return Transactions\.+ (\d+)", ok_data[0].replace('\n', ' '))
                if match and int(match.group(1)) == 0:
                    self.bot.send_message(self.chat_id, f"✅ *{client_id} EPOUT CONLY complete*", parse_mode="Markdown")
                else:
                    self.bot.send_message(self.chat_id, f"⚠️ *{client_id} EPOUT CONLY с возвратами!*", parse_mode="Markdown")
                self.checked_epouts.add(client_key)
            elif info_data:
                self.bot.send_message(self.chat_id, f"💬 *{client_id} EPOUT CONLY:* No CTF files.", parse_mode="Markdown")
                self.checked_epouts.add(client_key)
            elif now.minute >= min_end:
                self.bot.send_message(self.chat_id, f"🚨 *{client_id} EPOUT CONLY не найдено (ожидание истекло)!*", parse_mode="Markdown")
                self.checked_epouts.add(client_key)

    def scan_frequent_tasks(self):
        """Плавающие проверки (раз в минуту)"""
        if self.is_paused: return
        now = datetime.now()

        # 1. Вызов универсального Grace Period
        self._check_grace_period(
            client_id="CLIENT2",
            expected_subj="CLIENT2 EPOUT CONLY Job successfully completed!",
            info_subj="[INFO] CLIENT2 EPOUT CollectionOnly Job finished",
            target_hour=7,
            min_start=50,
            min_end=55
        )

        # 2. Ловля EPIN
        for inst in self.config["EPIN_DEADLINES"].keys():
            if inst not in self.received_epins:
                subj = self.epin_subjects.get(inst, f"{inst} EPIN COMPLETED")
                ep_data = self._get_latest_email(subj, "Epin")
                if ep_data:
                    dt = ep_data[1]
                    if dt.date() < now.date() and now.hour >= 4:
                        self.received_epins.add(inst)
                        continue

                    warn = "" if "RETURN.TRANS.CTF - The file does not contain any records" in ep_data[0].replace('\n', ' ') else "\n⚠️ *В RETURN TRANSACTIONS варнинг!*"
                    self.bot.send_message(self.chat_id, f'✅ *{subj}* в {dt.strftime("%H:%M")}{warn}', parse_mode="Markdown")
                    self.received_epins.add(inst)

    # ==========================================
    # ПЛАНИРОВЩИК И ЗАПУСК
    # ==========================================
    def setup_schedules(self):
        schedule.every().day.at("12:00").do(self.daily_reset)
        schedule.every().day.at("03:05").do(self.update_batches)

        schedule.every().day.at("19:32").do(self.check_evening_report, time_label="19:30")
        schedule.every().day.at("23:02").do(self.check_evening_report, time_label="23:00")
        schedule.every().day.at("23:32").do(self.check_evening_report, time_label="23:30")

        for inst, data in self.config["EPOUT_SCHEDULE"].items():
            schedule.every().day.at(f"{data['time'][0]:02d}:{data['time'][1]:02d}").do(self.check_epout, inst=inst)

        for inst, t_arr in self.config["EPIN_DEADLINES"].items():
            schedule.every().day.at(f"{t_arr[0]:02d}:{t_arr[1]:02d}").do(self.check_epin_deadline, inst=inst)

        schedule.every(1).minutes.do(self.scan_frequent_tasks)

    def setup_handlers(self):
        @self.bot.message_handler(commands=['get_batches'])
        def manual_batches(message):
            self.bot.reply_to(message, "⏳ Собираю батчи...")
            self.update_batches()
            batches_str = ", ".join([f"{k}: {v}" for k, v in self.batch_memory.items() if k not in self.hidden_batches])
            self.bot.send_message(self.chat_id, f"✅ Батчи: {batches_str}")

        @self.bot.message_handler(commands=['pause'])
        def manual_pause(message):
            self.is_paused = True
            self.bot.reply_to(message, "⏸ Пауза включена.")

        @self.bot.message_handler(commands=['resume'])
        def manual_resume(message):
            self.is_paused = False
            self.bot.reply_to(message, "▶️ Работа возобновлена.")

        @self.bot.message_handler(commands=['status'])
        def check_status(message):
            status = "⏸ НА ПАУЗЕ" if self.is_paused else "▶️ АКТИВЕН"
            batches_str = ", ".join([f"{k}: {v}" for k, v in self.batch_memory.items() if k not in self.hidden_batches])
            self.bot.reply_to(message, f"Тестовый бот. Статус: {status}\nФоновый мониторинг: ✅ Планировщик работает\nБатчи: {batches_str}")

    def run(self):
        self.setup_schedules()
        logging.info("🚀 Бот запущен, планировщик активен!")
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logging.error(f"Сбой: {e}")
                time.sleep(10)

if __name__ == "__main__":
    app = ProcessingMonitorBot()
    threading.Thread(target=app.run, daemon=True).start()
    app.bot.infinity_polling(skip_pending=True)
