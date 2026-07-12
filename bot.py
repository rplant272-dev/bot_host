# -*- coding: utf-8 -*-
import sys
import os
import random
import sqlite3
import re
import time
import threading
import json
import logging

# ======================== НАСТРОЙКА ПУТЕЙ ДЛЯ ХОСТИНГА ============================
# Определяем папку для хранения данных (БД, файлы настроек, если нужно)
DATA_DIR = os.environ.get('DATA_DIR', '/app/data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# Основная БД и префикс для аудиторий теперь лежат в DATA_DIR
MAIN_DB = os.path.join(DATA_DIR, "assistant.db")
AUDIENCE_DB_PREFIX = "audience_"
# =================================================================================

# Импорт переменных из config.py или из переменных окружения
try:
    from config import *
except (ImportError, NameError):
    # Если config отсутствует или переменные не определены, читаем из окружения
    GROUP_TOKEN = os.environ.get('GROUP_TOKEN')
    GROUP_ID = os.environ.get('GROUP_ID')
    OWNER_ID = os.environ.get('OWNER_ID')

# Проверяем, что все переменные определены
if not GROUP_TOKEN or not GROUP_ID or not OWNER_ID:
    print("❌ Ошибка: не заданы GROUP_TOKEN, GROUP_ID, OWNER_ID!")
    print("Убедитесь, что они есть в config.py или в переменных окружения.")
    sys.exit(1)

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================== РАБОТА С БАЗАМИ ДАННЫХ ============================

DB_LOCK = threading.RLock()

def get_db_path(peer_id):
    if peer_id is None or peer_id == 0:
        return MAIN_DB
    return os.path.join(DATA_DIR, f"{AUDIENCE_DB_PREFIX}{peer_id}.db")

def create_audience_schema(conn):
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS creative (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            variant INTEGER,
            task_text TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            template TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            variant INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            correct_option_index INTEGER NOT NULL,
            order_num INTEGER NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS test_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            option_label TEXT NOT NULL,
            option_text TEXT NOT NULL,
            FOREIGN KEY (question_id) REFERENCES test_questions(id) ON DELETE CASCADE
        )
    ''')
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('st1_text', '')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('st2_text', '')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_template', '')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_time_limit', '30')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_fail_threshold', '5')")
    conn.commit()

def get_db_connection(peer_id=None):
    if peer_id is not None:
        dc = get_datacenter_peer_id()
        if dc is not None and peer_id == dc:
            peer_id = None

    if peer_id is None:
        conn = sqlite3.connect(MAIN_DB, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    db_file = get_db_path(peer_id)
    is_new = not os.path.exists(db_file)
    conn = sqlite3.connect(db_file, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if is_new:
        create_audience_schema(conn)
    return conn

def init_main_db():
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id TEXT PRIMARY KEY,
                added_by TEXT,
                added_at INTEGER,
                role TEXT DEFAULT 'admin'
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS co_owners (
                user_id TEXT PRIMARY KEY,
                added_by TEXT,
                added_at INTEGER
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS audiences (
                peer_id INTEGER PRIMARY KEY,
                owner_id TEXT,
                confirmed INTEGER DEFAULT 0,
                request_time INTEGER,
                request_message_id INTEGER,
                is_datacenter INTEGER DEFAULT 0,
                last_activity INTEGER
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS creative (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                variant INTEGER,
                task_text TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                template TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS test_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                variant INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                correct_option_index INTEGER NOT NULL,
                order_num INTEGER NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS test_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL,
                option_label TEXT NOT NULL,
                option_text TEXT NOT NULL,
                FOREIGN KEY (question_id) REFERENCES test_questions(id) ON DELETE CASCADE
            )
        ''')
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('datacenter_peer_id', '')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('st1_text', '')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('st2_text', '')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_template', '')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_time_limit', '30')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_fail_threshold', '5')")
        conn.commit()
    finally:
        conn.close()

def delete_audience_db(peer_id):
    if is_datacenter(peer_id):
        return True
    db_file = get_db_path(peer_id)
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
            logger.info(f"База данных аудитории {peer_id} удалена.")
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления БД {peer_id}: {e}")
            return False
    return True

# -------------------- ФУНКЦИИ ДОСТУПА К ДАННЫМ --------------------

def get_setting(key, default=None, peer_id=None):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row['value'] if row else default
    finally:
        conn.close()

def set_setting(key, value, peer_id=None):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()

def get_report_template(peer_id=None):
    return get_setting("report_template", "", peer_id)

def set_report_template(text, peer_id=None):
    set_setting("report_template", text, peer_id)

def get_creative_text(ctype, variant, peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT task_text FROM creative WHERE type=? AND variant=?", (ctype, variant))
        return cur.fetchone()
    finally:
        conn.close()

def set_creative_text(ctype, variant, text, peer_id):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("REPLACE INTO creative (type, variant, task_text) VALUES (?, ?, ?)", (ctype, variant, text))
        conn.commit()
    finally:
        conn.close()

def delete_creative_text(ctype, variant, peer_id):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("DELETE FROM creative WHERE type=? AND variant=?", (ctype, variant))
        conn.commit()
    finally:
        conn.close()

def get_topic_by_id(topic_id, peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, text, template FROM topics WHERE id=?", (topic_id,))
        return cur.fetchone()
    finally:
        conn.close()

def get_all_topics(peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, text, template FROM topics ORDER BY id")
        return cur.fetchall()
    finally:
        conn.close()

def add_topic(text, template, peer_id):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("INSERT INTO topics (text, template) VALUES (?, ?)", (text, template))
        conn.commit()
    finally:
        conn.close()

def delete_topic(topic_id, peer_id):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        conn.commit()
    finally:
        conn.close()

def delete_all_topics(peer_id):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("DELETE FROM topics")
        conn.commit()
    finally:
        conn.close()

# -------------------- ФУНКЦИИ ДЛЯ ТЕСТОВ ПО ОДНОМУ --------------------

def get_test_questions(topic, variant, peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, question_text, correct_option_index, order_num FROM test_questions WHERE topic=? AND variant=? ORDER BY order_num", (topic, variant))
        return cur.fetchall()
    finally:
        conn.close()

def get_test_options(question_id, peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, option_label, option_text FROM test_options WHERE question_id=? ORDER BY id", (question_id,))
        return cur.fetchall()
    finally:
        conn.close()

def add_test_question(peer_id, topic, variant, question_text, correct_option_index, order_num, options):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO test_questions (topic, variant, question_text, correct_option_index, order_num) VALUES (?, ?, ?, ?, ?)",
                    (topic, variant, question_text, correct_option_index, order_num))
        qid = cur.lastrowid
        for label, text in options:
            cur.execute("INSERT INTO test_options (question_id, option_label, option_text) VALUES (?, ?, ?)", (qid, label, text))
        conn.commit()
        return qid
    finally:
        conn.close()

def update_test_question(question_id, new_question_text, new_correct_index, new_options, peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE test_questions SET question_text=?, correct_option_index=? WHERE id=?", (new_question_text, new_correct_index, question_id))
        cur.execute("DELETE FROM test_options WHERE question_id=?", (question_id,))
        for label, text in new_options:
            cur.execute("INSERT INTO test_options (question_id, option_label, option_text) VALUES (?, ?, ?)", (question_id, label, text))
        conn.commit()
    finally:
        conn.close()

def delete_test_question(question_id, peer_id):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("DELETE FROM test_questions WHERE id=?", (question_id,))
        conn.commit()
    finally:
        conn.close()

def delete_test_questions(peer_id, topic, variant):
    conn = get_db_connection(peer_id)
    try:
        conn.execute("DELETE FROM test_questions WHERE topic=? AND variant=?", (topic, variant))
        conn.commit()
    finally:
        conn.close()

def get_test_time_limit(peer_id):
    val = get_setting("test_time_limit", "30", peer_id)
    try:
        return int(val)
    except:
        return 30

def set_test_time_limit(peer_id, seconds):
    set_setting("test_time_limit", str(seconds), peer_id)

def get_test_fail_threshold(peer_id):
    val = get_setting("test_fail_threshold", "5", peer_id)
    try:
        return int(val)
    except:
        return 5

def set_test_fail_threshold(peer_id, threshold):
    set_setting("test_fail_threshold", str(threshold), peer_id)

def has_one_by_one_test(topic, variant, peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM test_questions WHERE topic=? AND variant=? LIMIT 1", (topic, variant))
        return cur.fetchone() is not None
    finally:
        conn.close()

# -------------------- УПРАВЛЕНИЕ ДАТАЦЕНТРОМ И КОПИРОВАНИЕ --------------------

def get_datacenter_peer_id():
    val = get_setting("datacenter_peer_id", None)
    return int(val) if val else None

def set_datacenter_peer_id(peer_id):
    set_setting("datacenter_peer_id", str(peer_id) if peer_id else "")

def copy_global_to_audience(target_peer):
    source_conn = get_db_connection(None)
    target_conn = get_db_connection(target_peer)
    try:
        # Отключаем проверку внешних ключей на время копирования
        target_conn.execute("PRAGMA foreign_keys=OFF")
        
        # Копируем таблицы
        tables = ['creative', 'topics', 'test_questions']
        for table in tables:
            target_conn.execute(f"DELETE FROM {table}")
            cur = source_conn.cursor()
            cur.execute(f"SELECT * FROM {table}")
            rows = cur.fetchall()
            if not rows:
                logger.info(f"Таблица {table} пуста, пропускаем")
                continue
            # Получаем список колонок (кроме id)
            cur.execute(f"PRAGMA table_info({table})")
            cols = [row['name'] for row in cur.fetchall() if row['name'] != 'id']
            placeholders = ', '.join(['?' for _ in cols])
            insert_sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
            
            # Для test_questions сохраняем маппинг старых ID на новые
            if table == 'test_questions':
                qid_map = {}
                for row in rows:
                    cur2 = target_conn.cursor()
                    cur2.execute(insert_sql, [row[col] for col in cols])
                    new_id = cur2.lastrowid
                    qid_map[row['id']] = new_id
                    cur2.close()
                # После вставки вопросов, копируем test_options
                cur_opts = source_conn.cursor()
                cur_opts.execute("SELECT * FROM test_options")
                opts = cur_opts.fetchall()
                if opts:
                    target_conn.execute("DELETE FROM test_options")
                    for opt in opts:
                        new_qid = qid_map.get(opt['question_id'])
                        if new_qid:
                            target_conn.execute(
                                "INSERT INTO test_options (question_id, option_label, option_text) VALUES (?, ?, ?)",
                                (new_qid, opt['option_label'], opt['option_text'])
                            )
                    logger.info(f"Скопировано {len(opts)} вариантов ответов")
                else:
                    logger.info("Нет вариантов для копирования")
                cur_opts.close()
            else:
                for row in rows:
                    target_conn.execute(insert_sql, [row[col] for col in cols])
            logger.info(f"Скопировано {len(rows)} записей из {table}")
        
        # Копируем настройки
        source_cur = source_conn.cursor()
        source_cur.execute("SELECT key, value FROM settings")
        settings = source_cur.fetchall()
        target_conn.execute("DELETE FROM settings")
        for s in settings:
            target_conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (s['key'], s['value']))
        logger.info(f"Скопировано {len(settings)} настроек")
        source_cur.close()
        
        # Включаем проверку внешних ключей обратно
        target_conn.execute("PRAGMA foreign_keys=ON")
        target_conn.commit()
        logger.info("Копирование данных в аудиторию завершено успешно")
    except Exception as e:
        logger.error(f"Ошибка при копировании данных: {e}")
        raise
    finally:
        source_conn.close()
        target_conn.close()

def copy_datacenter_to_audience(target_peer_id):
    dc = get_datacenter_peer_id()
    if dc is None:
        return False
    copy_global_to_audience(target_peer_id)
    return True

def ensure_audience_initialized(peer_id):
    conn = get_db_connection(peer_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM creative LIMIT 1")
        if cur.fetchone() is not None:
            return True
    finally:
        conn.close()
    return copy_datacenter_to_audience(peer_id)

# -------------------- УПРАВЛЕНИЕ АУДИТОРИЯМИ --------------------

def is_datacenter(peer_id):
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_datacenter FROM audiences WHERE peer_id=? AND confirmed=1", (peer_id,))
        row = cur.fetchone()
        return row is not None and row['is_datacenter'] == 1
    finally:
        conn.close()

def is_audience_confirmed(peer_id):
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT confirmed FROM audiences WHERE peer_id=?", (peer_id,))
        row = cur.fetchone()
        return row is not None and row['confirmed'] == 1
    finally:
        conn.close()

def get_audience_owner(peer_id):
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT owner_id FROM audiences WHERE peer_id=?", (peer_id,))
        row = cur.fetchone()
        return row['owner_id'] if row else None
    finally:
        conn.close()

def set_audience_request(peer_id, owner_id=None, request_msg_id=None):
    conn = get_db_connection(None)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO audiences (peer_id, owner_id, confirmed, request_time, request_message_id, is_datacenter, last_activity) VALUES (?, ?, 0, ?, ?, 0, ?)",
            (peer_id, str(owner_id) if owner_id else None, int(time.time()), request_msg_id, int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()

def update_audience_activity(peer_id):
    conn = get_db_connection(None)
    try:
        conn.execute("UPDATE audiences SET last_activity=? WHERE peer_id=?", (int(time.time()), peer_id))
        conn.commit()
    finally:
        conn.close()

def get_all_audiences():
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT peer_id, owner_id, last_activity FROM audiences WHERE confirmed=1 AND is_datacenter=0 ORDER BY last_activity DESC")
        return cur.fetchall()
    finally:
        conn.close()

# -------------------- ОЧИСТКА СОСТОЯНИЯ БЕСЕДЫ --------------------

def cleanup_peer_state(peer_id):
    """Принудительно завершает все процессы, связанные с беседой, и очищает глобальные состояния."""
    # Очистка активного теста
    if peer_id in active_tests:
        test = active_tests.pop(peer_id)
        if test.get('timer'):
            try:
                test['timer'].cancel()
            except:
                pass
        logger.info(f"Принудительно завершён тест в беседе {peer_id}")
    
    # Очистка состояний меню для всех пользователей этой беседы
    keys_to_remove = []
    for key in list(menu_state.keys()):
        if isinstance(key, tuple) and len(key) == 2 and key[0] == peer_id:
            keys_to_remove.append(key)
    for key in keys_to_remove:
        menu_state.pop(key, None)
    if keys_to_remove:
        logger.info(f"Удалено {len(keys_to_remove)} состояний меню для беседы {peer_id}")

def delete_audience_by_owner(peer_id):
    if is_datacenter(peer_id):
        return False, "Это датацентр, его нельзя удалить этой командой."
    # Принудительно завершаем все процессы
    cleanup_peer_state(peer_id)
    # Удаляем файл БД
    delete_audience_db(peer_id)
    # Удаляем запись из главной БД
    conn = get_db_connection(None)
    try:
        conn.execute("DELETE FROM audiences WHERE peer_id=?", (peer_id,))
        conn.commit()
    finally:
        conn.close()
    return True, "Аудитория удалена."

# -------------------- СОЗДАНИЕ И УДАЛЕНИЕ АУДИТОРИЙ --------------------

def init_global_materials():
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM topics")
        if cur.fetchone()[0] == 0:
            default_topics = [
                "Понятие преступления и пределы уголовной ответственности",
                "Роль адвоката в системе правосудия",
                "Презумпция невиновности как основа судебного процесса",
                "Соотношение уголовной и административной ответственности",
                "Юридическая квалификация преступлений: ошибки следствия",
                "Права обвиняемого при задержании и допросе",
                "Законность доказательств в уголовном процессе",
                "Тактика защиты подозреваемого на стадии расследования",
                "Работа адвоката при избрании меры пресечения",
                "Оспаривание незаконного задержания",
                "Стратегия защиты при обвинении в тяжких преступлениях",
                "Адвокатская линия защиты при соучастии",
                "Переговоры с прокуратурой и досудебное урегулирование",
                "Роль адвоката при заключении процессуальных соглашений",
                "Границы полномочий правоохранительных органов",
                "Типичные процессуальные нарушения сотрудников LSPD/FBI",
                "Защита граждан от превышения должностных полномочий",
                "Правомерность применения силы сотрудниками государства",
                "Обжалование действий государственных служащих",
                "Подготовка адвоката к судебному заседанию",
                "Искусство перекрёстного допроса",
                "Оценка доказательств судом",
                "Построение убедительной защитительной речи",
                "Тактика поведения адвоката в суде",
                "Судебные ошибки и основания для апелляции"
            ]
            for topic in default_topics:
                conn.execute("INSERT INTO topics (text, template) VALUES (?, ?)", (topic, ""))
            conn.commit()
    finally:
        conn.close()

def create_datacenter(peer_id, owner_id, request_msg_id=None):
    old_dc = get_datacenter_peer_id()
    if old_dc is not None and old_dc != peer_id:
        conn = get_db_connection(None)
        try:
            conn.execute("UPDATE audiences SET is_datacenter=0 WHERE peer_id=?", (old_dc,))
            conn.commit()
        finally:
            conn.close()
        logger.info(f"Старый датацентр {old_dc} стал аудиторией")
    init_global_materials()

    set_datacenter_peer_id(peer_id)
    conn = get_db_connection(None)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO audiences (peer_id, owner_id, confirmed, request_time, request_message_id, is_datacenter, last_activity) VALUES (?, ?, 1, ?, ?, 1, ?)",
            (peer_id, str(owner_id), int(time.time()), request_msg_id, int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"✅ Датацентр создан: {peer_id}")

def create_audience(peer_id, owner_id, request_msg_id=None):
    dc = get_datacenter_peer_id()
    if dc is None:
        raise Exception("Нет активного датацентра. Сначала создайте датацентр.")

    get_db_connection(peer_id)
    copy_global_to_audience(peer_id)

    conn = get_db_connection(None)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO audiences (peer_id, owner_id, confirmed, request_time, request_message_id, is_datacenter, last_activity) VALUES (?, ?, 1, ?, ?, 0, ?)",
            (peer_id, str(owner_id), int(time.time()), request_msg_id, int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"✅ Аудитория создана: {peer_id}")

def delete_audience(peer_id):
    if is_datacenter(peer_id):
        conn = get_db_connection(None)
        try:
            conn.execute("UPDATE audiences SET is_datacenter=0 WHERE peer_id=?", (peer_id,))
            conn.commit()
        finally:
            conn.close()
        dc = get_datacenter_peer_id()
        if dc == peer_id:
            set_datacenter_peer_id(None)
        logger.info(f"🗑 Датацентр {peer_id} стал обычной аудиторией (данные сохранены в глобальной БД).")
        return

    delete_audience_db(peer_id)
    conn = get_db_connection(None)
    try:
        conn.execute("DELETE FROM audiences WHERE peer_id=?", (peer_id,))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"🗑 Аудитория {peer_id} удалена полностью.")

# -------------------- ЗАПРОС ПОДТВЕРЖДЕНИЯ --------------------

def request_audience_confirmation(peer_id):
    if is_audience_confirmed(peer_id):
        send_message(peer_id, "✅ Эта беседа уже является аудиторией.")
        return

    if not bot_is_admin_in_chat(peer_id):
        send_message(peer_id, "❌ Бот не является администратором этой беседы. Для создания аудитории или датацентра необходимы права администратора.")
        return

    conn = get_db_connection(None)
    try:
        conn.execute("DELETE FROM audiences WHERE peer_id=?", (peer_id,))
        conn.commit()
    finally:
        conn.close()

    keyboard = VkKeyboard(inline=True)
    keyboard.add_callback_button(
        label="✅ Создать аудиторию",
        color=VkKeyboardColor.POSITIVE,
        payload={"cmd": "confirm_audience"}
    )
    keyboard.add_callback_button(
        label="⭐ Создать датацентр",
        color=VkKeyboardColor.PRIMARY,
        payload={"cmd": "confirm_datacenter"}
    )
    resp = send_message(peer_id,
                        "📢 Эта беседа может стать аудиторией обучения.\n"
                        "Выберите тип создания:\n"
                        "• «Создать аудиторию» – обычная группа со своей копией базы (требуется наличие датацентра).\n"
                        "• «Создать датацентр» – центральная база (доступно только владельцу или совладельцу).\n"
                        "Время на подтверждение: 5 минут.",
                        keyboard=keyboard)
    msg_id = resp.get('conversation_message_id') if resp else None
    set_audience_request(peer_id, owner_id=None, request_msg_id=msg_id)

# -------------------- ПРАВА ДОСТУПА --------------------

def is_owner(vk_id):
    return str(vk_id) == str(OWNER_ID)

def is_co_owner(vk_id):
    if is_owner(vk_id):
        return True
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM co_owners WHERE user_id=?", (str(vk_id),))
        return cur.fetchone() is not None
    finally:
        conn.close()

def is_full_access(vk_id):
    return is_owner(vk_id) or is_co_owner(vk_id)

def is_allowed(vk_id):
    if is_full_access(vk_id):
        return True
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (str(vk_id),))
        return cur.fetchone() is not None
    finally:
        conn.close()

def can_create_audience(vk_id):
    return is_full_access(vk_id) or is_allowed(vk_id)

def can_manage_materials(vk_id, peer_id):
    if is_full_access(vk_id):
        return True
    owner = get_audience_owner(peer_id)
    return owner == str(vk_id)

def add_allowed_user(vk_id, added_by):
    conn = get_db_connection(None)
    try:
        conn.execute("INSERT OR REPLACE INTO allowed_users (user_id, added_by, added_at, role) VALUES (?, ?, ?, 'admin')",
                     (str(vk_id), str(added_by), int(time.time())))
        conn.commit()
    finally:
        conn.close()

def remove_allowed_user(vk_id):
    conn = get_db_connection(None)
    try:
        conn.execute("DELETE FROM allowed_users WHERE user_id=?", (str(vk_id),))
        conn.commit()
    finally:
        conn.close()

def get_allowed_users():
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, added_by, added_at FROM allowed_users ORDER BY added_at")
        return cur.fetchall()
    finally:
        conn.close()

def add_co_owner(user_id, added_by):
    conn = get_db_connection(None)
    try:
        conn.execute("INSERT OR REPLACE INTO co_owners (user_id, added_by, added_at) VALUES (?, ?, ?)",
                     (str(user_id), str(added_by), int(time.time())))
        conn.commit()
    finally:
        conn.close()

def remove_co_owner(user_id):
    conn = get_db_connection(None)
    try:
        conn.execute("DELETE FROM co_owners WHERE user_id=?", (str(user_id),))
        conn.commit()
    finally:
        conn.close()

def get_co_owners():
    conn = get_db_connection(None)
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, added_by, added_at FROM co_owners ORDER BY added_at")
        return cur.fetchall()
    finally:
        conn.close()

# -------------------- ПРОВЕРКИ VK --------------------

def bot_is_admin_in_chat(peer_id):
    try:
        members = vk.messages.getConversationMembers(peer_id=peer_id)
        bot_id = -int(GROUP_ID)
        for item in members.get("items", []):
            if item.get("member_id") == bot_id:
                return item.get("is_admin", False)
        return False
    except Exception as e:
        print(f"⚠️ Ошибка проверки прав администратора в беседе {peer_id}: {e}")
        return False

def get_chat_name(peer_id):
    if peer_id < 2000000000:
        return None
    try:
        info = vk.messages.getConversationsById(peer_ids=[peer_id])
        items = info.get("items", [])
        if items:
            settings = items[0].get("chat_settings")
            if settings:
                return settings.get("title")
        return None
    except Exception as e:
        print(f"⚠️ Ошибка получения названия беседы {peer_id}: {e}")
        return None

# -------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------

def read_text_file(filename):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(script_dir, filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except:
        return None

def write_text_file(filename, text):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(script_dir, filename)
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(text)
        return True
    except:
        return False

def send_message(peer_id, text, attachment=None, keyboard=None, retries=2):
    if not isinstance(peer_id, int) or peer_id <= 0:
        logger.error(f"Некорректный peer_id: {peer_id}")
        return None
    for attempt in range(retries):
        try:
            params = {"peer_id": peer_id, "message": text, "random_id": random.getrandbits(31)}
            if attachment:
                params["attachment"] = attachment
            if keyboard:
                if hasattr(keyboard, 'get_keyboard'):
                    params["keyboard"] = keyboard.get_keyboard()
                else:
                    params["keyboard"] = keyboard
            response = vk.messages.send(**params)
            if isinstance(response, dict):
                conv_id = response.get('conversation_message_id')
                msg_id = response.get('response') or response.get('message_id')
                if msg_id is None and conv_id is not None:
                    msg_id = conv_id
                return {'message_id': msg_id, 'conversation_message_id': conv_id}
            else:
                return {'message_id': response, 'conversation_message_id': None}
        except Exception as e:
            logger.warning(f"Ошибка отправки (попытка {attempt+1}): {e}")
            if "[15]" in str(e):
                logger.error(f"Access denied, прекращаем попытки")
                return None
            if attempt == retries-1:
                logger.error(f"Не удалось отправить сообщение в {peer_id}: {e}")
                return None
            time.sleep(1)
    return None

def edit_message(peer_id, cmid, text, keyboard=None):
    if cmid is None:
        return False
    try:
        params = {"peer_id": peer_id, "message": text, "conversation_message_id": cmid}
        if keyboard:
            if hasattr(keyboard, 'get_keyboard'):
                params["keyboard"] = keyboard.get_keyboard()
            else:
                params["keyboard"] = keyboard
        vk.messages.edit(**params)
        return True
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения {cmid}: {e}")
        return False

def delete_message(peer_id, conversation_message_id, retries=2, force=False):
    if conversation_message_id is None:
        return False
    if not force and peer_id >= 2000000000 and not bot_is_admin_in_chat(peer_id):
        logger.warning(f"Бот не администратор в беседе {peer_id}, удаление пропущено")
        return False
    for attempt in range(retries):
        try:
            vk.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[conversation_message_id],
                delete_for_all=1
            )
            return True
        except Exception as e:
            logger.warning(f"Ошибка удаления сообщения {conversation_message_id} (попытка {attempt+1}): {e}")
            if "[15]" in str(e):
                return False
            if attempt == retries-1:
                return False
            time.sleep(0.5)
    return False

def send_long_message(peer_id, text, keyboard=None):
    MAX_LEN = 4000
    msg_ids = []
    if not text:
        resp = send_message(peer_id, "📭 Содержимое отсутствует.", keyboard=keyboard)
        if resp and isinstance(resp, dict) and resp.get('conversation_message_id'):
            msg_ids.append(resp['conversation_message_id'])
        return msg_ids
    if len(text) <= MAX_LEN:
        resp = send_message(peer_id, text, keyboard=keyboard)
        if resp and isinstance(resp, dict) and resp.get('conversation_message_id'):
            msg_ids.append(resp['conversation_message_id'])
        return msg_ids
    parts = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > MAX_LEN:
            if current:
                parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    for i, part in enumerate(parts):
        kb = keyboard if i == len(parts)-1 else None
        resp = send_message(peer_id, part, keyboard=kb)
        if resp and isinstance(resp, dict) and resp.get('conversation_message_id'):
            msg_ids.append(resp['conversation_message_id'])
    return msg_ids

def kick_from_chat(peer_id, user_id):
    try:
        vk.messages.removeChatUser(chat_id=peer_id - 2000000000, user_id=user_id)
    except Exception as e:
        print(f"⚠️ Ошибка удаления {user_id}: {e}")

def add_user_to_chat(peer_id, user_id):
    try:
        vk.messages.addChatUser(chat_id=peer_id - 2000000000, user_id=user_id)
        return True
    except Exception as e:
        print(f"⚠️ Ошибка добавления пользователя {user_id}: {e}")
        return False

def get_user_nickname(vk_id):
    try:
        user = vk.users.get(user_ids=vk_id)[0]
        return f"{user['first_name']} {user['last_name']}"
    except:
        return f"id{vk_id}"

def delete_message_later(peer_id, msg_id, delay=1):
    if msg_id is None:
        return
    def _delete():
        time.sleep(delay)
        delete_message(peer_id, msg_id)
    threading.Thread(target=_delete, daemon=True).start()

# ======================== ПРОВЕРКА ПРАВ ДЛЯ УПРАВЛЕНИЯ ТЕСТОМ ============================

def can_control_test(user_id, peer_id):
    if is_full_access(user_id):
        return True
    owner = get_audience_owner(peer_id)
    if owner and str(owner) == str(user_id):
        return True
    return False

# ======================== КЛАВИАТУРЫ ============================

HODAITSTVA_NAMES = {
    1: "Вызов эксперта",
    2: "Истребование",
    3: "Отвод",
    4: "Отложение",
    5: "Привлечение специалиста",
    6: "Приобщение"
}

def get_main_menu_keyboard(has_full_access=False, can_manage=False, is_datacenter=False):
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("📚 1 этап (собеседование)", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("📖 2 этап (лекция)", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("📝 3 этап (тесты)", color=VkKeyboardColor.SECONDARY)
    keyboard.add_button("🎨 4 этап (творческое)", color=VkKeyboardColor.SECONDARY)
    if can_manage:
        keyboard.add_line()
        keyboard.add_button("🛠 Управление материалами", color=VkKeyboardColor.PRIMARY)
    if is_datacenter:
        keyboard.add_line()
        keyboard.add_button("⭐ Датацентр", color=VkKeyboardColor.POSITIVE)
    keyboard.add_line()
    keyboard.add_button("🔒 Скрыть панель", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_stage3_topics_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    topics = ["Конституция", "Устав адвокатуры", "Уголовный кодекс", "Федеральное постановление", "Процессуальный кодекс"]
    for topic in topics:
        keyboard.add_button(topic, color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_stage3_variants_keyboard(topic):
    keyboard = VkKeyboard(one_time=False, inline=False)
    display_topic = topic.replace('_', ' ')
    for v in [1, 2, 3]:
        keyboard.add_button(f"{display_topic} вариант {v}", color=VkKeyboardColor.PRIMARY)
        if v % 2 == 0:
            keyboard.add_line()
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_stage4_types_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("Ходатайства", color=VkKeyboardColor.SECONDARY)
    keyboard.add_button("Обращение в прокуратуру/Иск", color=VkKeyboardColor.SECONDARY)
    keyboard.add_button("Доклад", color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_stage4_variants_keyboard(ctype):
    keyboard = VkKeyboard(one_time=False, inline=False)
    if ctype == "Доклад":
        max_variants = 1
    elif ctype == "Ходатайства":
        max_variants = 6
    else:
        max_variants = 3
    for v in range(1, max_variants + 1):
        if ctype == "Ходатайства":
            label = HODAITSTVA_NAMES.get(v, f"Ходатайства вариант {v}")
        else:
            if ctype == "Обращение_в_прокуратуру_Иск":
                display_name = "Обращение в прокуратуру/Иск"
            else:
                display_name = ctype.replace('_', ' ')
            label = f"{display_name} вариант {v}"
        keyboard.add_button(label, color=VkKeyboardColor.PRIMARY)
        if v % 2 == 0 and v < max_variants:
            keyboard.add_line()
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_empty_keyboard():
    return VkKeyboard.get_empty_keyboard()

def get_manage_main_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("🗣 Собеседование", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("📚 Лекция", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("❓ Тесты (по одному)", color=VkKeyboardColor.SECONDARY)
    keyboard.add_button("🎨 Творческое", color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button("⚙️ Настройки тестирования", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("🏛 Главное меню", color=VkKeyboardColor.PRIMARY)
    return keyboard.get_keyboard()

def get_manage_simple_action_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("➕ Изменить текст", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_manage_action_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("🔍 Посмотреть", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("➕ Добавить/Заменить", color=VkKeyboardColor.POSITIVE)
    keyboard.add_button("🗑 Удалить", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

def get_buffer_keyboard(next_step=False):
    keyboard = VkKeyboard(one_time=False, inline=False)
    if next_step:
        keyboard.add_button("➡️ Далее", color=VkKeyboardColor.POSITIVE)
    else:
        keyboard.add_button("💾 Сохранить", color=VkKeyboardColor.POSITIVE)
    return keyboard.get_keyboard()

def get_creative_topics_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("➕ Добавить тему", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("✏️ Изменить форму доклада", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("🗑 Очистить все темы", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

def get_creative_topic_action_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("✏️ Изменить шаблон", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("🗑 Удалить тему", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

def get_test_question_keyboard(options, labels):
    keyboard = VkKeyboard(inline=True)
    for i, (label, text) in enumerate(zip(labels, options)):
        if i % 2 == 0 and i > 0:
            keyboard.add_line()
        keyboard.add_callback_button(label, color=VkKeyboardColor.PRIMARY, payload={"cmd": "test_answer", "index": i})
    keyboard.add_line()
    keyboard.add_callback_button("⏸ Остановить", color=VkKeyboardColor.NEGATIVE, payload={"cmd": "test_pause"})
    return keyboard.get_keyboard()

def get_test_pause_keyboard():
    keyboard = VkKeyboard(inline=True)
    keyboard.add_callback_button("▶️ Продолжить", color=VkKeyboardColor.POSITIVE, payload={"cmd": "test_resume"})
    keyboard.add_callback_button("⏹ Завершить", color=VkKeyboardColor.NEGATIVE, payload={"cmd": "test_end"})
    return keyboard.get_keyboard()

def get_test_start_keyboard():
    keyboard = VkKeyboard(inline=True)
    keyboard.add_callback_button("✅ Готов", color=VkKeyboardColor.POSITIVE, payload={"cmd": "test_ready"})
    keyboard.add_callback_button("❌ Отмена", color=VkKeyboardColor.NEGATIVE, payload={"cmd": "test_cancel"})
    return keyboard.get_keyboard()

def get_test_settings_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("⏱ Время на вопрос", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("❌ Порог ошибок", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_manage_test_questions_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("➕ Добавить вопрос", color=VkKeyboardColor.POSITIVE)
    keyboard.add_button("✏️ Редактировать вопрос", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("🗑 Удалить вопрос", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("🗑 Удалить все вопросы", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

def get_add_option_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("➕ Ещё вариант", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("✅ Готово", color=VkKeyboardColor.POSITIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_question_list_keyboard(questions):
    keyboard = VkKeyboard(one_time=False, inline=False)
    for i, q in enumerate(questions, 1):
        keyboard.add_button(str(i), color=VkKeyboardColor.SECONDARY)
        if i % 5 == 0:
            keyboard.add_line()
    if len(questions) % 5 != 0:
        keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

def get_edit_question_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("✏️ Редактировать вопрос", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("✏️ Редактировать варианты", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("🗑 Удалить вопрос", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

def get_edit_options_keyboard():
    keyboard = VkKeyboard(one_time=False, inline=False)
    keyboard.add_button("➕ Добавить вариант", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("🗑 Удалить вариант", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("✏️ Изменить вариант", color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button("✅ Готово", color=VkKeyboardColor.POSITIVE)
    keyboard.add_line()
    keyboard.add_button("🔙 Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

# ======================== ОЧИСТКА ТЕКСТА ============================

def clean_text_from_mentions(text):
    cleaned = re.sub(r'\[[^\]]+\]', '', text)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)
    return cleaned.strip()

def is_panel_command(text):
    clean = clean_text_from_mentions(text)
    if not clean:
        return False
    panel_texts = [
        "🔙 Назад",
        "📚 1 этап (собеседование)",
        "📖 2 этап (лекция)",
        "📝 3 этап (тесты)",
        "🎨 4 этап (творческое)",
        "🔒 Скрыть панель",
        "🛠 Управление материалами",
        "Конституция",
        "Устав адвокатуры",
        "Уголовный кодекс",
        "Федеральное постановление",
        "Процессуальный кодекс",
        "Конституция вариант 1",
        "Конституция вариант 2",
        "Конституция вариант 3",
        "Устав адвокатуры вариант 1",
        "Устав адвокатуры вариант 2",
        "Устав адвокатуры вариант 3",
        "Уголовный кодекс вариант 1",
        "Уголовный кодекс вариант 2",
        "Уголовный кодекс вариант 3",
        "Федеральное постановление вариант 1",
        "Федеральное постановление вариант 2",
        "Федеральное постановление вариант 3",
        "Процессуальный кодекс вариант 1",
        "Процессуальный кодекс вариант 2",
        "Процессуальный кодекс вариант 3",
        "Ходатайства",
        "Обращение в прокуратуру/Иск",
        "Доклад",
        "Ходатайства вариант 1",
        "Ходатайства вариант 2",
        "Ходатайства вариант 3",
        "Ходатайства вариант 4",
        "Ходатайства вариант 5",
        "Ходатайства вариант 6",
        "Обращение в прокуратуру/Иск вариант 1",
        "Обращение в прокуратуру/Иск вариант 2",
        "Обращение в прокуратуру/Иск вариант 3",
        "Доклад вариант 1",
        "📝 Изменить шаблоны",
        "📋 Список творческих",
        "🗣 Собеседование",
        "📚 Лекция",
        "📝 Экзамены",
        "🎨 Творческое",
        "🛠 Управление материалами",
        "🏛 Главное меню",
        "✏️ Изменить форму доклада",
        "❓ Тесты (по одному)",
        "⚙️ Настройки тестирования",
        "⏱ Время на вопрос",
        "❌ Порог ошибок",
        "➕ Ещё вариант",
        "✅ Готово",
        "✏️ Редактировать вопрос",
        "✏️ Редактировать варианты",
        "🗑 Удалить вопрос",
        "➕ Добавить вариант",
        "🗑 Удалить вариант",
        "✏️ Изменить вариант"
    ]
    panel_texts.extend(HODAITSTVA_NAMES.values())
    panel_texts.extend([
        "Обращение_в_прокуратуру_Иск вариант 1",
        "Обращение_в_прокуратуру_Иск вариант 2",
        "Обращение_в_прокуратуру_Иск вариант 3"
    ])
    return clean in panel_texts

def send_menu(peer_id, user_id, text, keyboard):
    send_message(peer_id, text, keyboard=keyboard)

# ======================== ГЛОБАЛЬНОЕ СОСТОЯНИЕ ============================
menu_state = {}
menu_state_locks = {}
active_tests = {}
test_timers = {}

def get_menu_state_lock(key):
    if key not in menu_state_locks:
        menu_state_locks[key] = threading.Lock()
    return menu_state_locks[key]

def safe_menu_state_set(key, value):
    with get_menu_state_lock(key):
        menu_state[key] = value

def safe_menu_state_get(key):
    with get_menu_state_lock(key):
        return menu_state.get(key)

def safe_menu_state_pop(key):
    with get_menu_state_lock(key):
        return menu_state.pop(key, None)

# ======================== ОБРАБОТЧИК ГЛАВНОГО МЕНЮ ============================

def handle_main_menu(text, peer_id, sender_id, conversation_message_id, can_manage=False):
    clean_text = clean_text_from_mentions(text)
    key = (peer_id, sender_id)

    state_data = safe_menu_state_get(key)
    if state_data and isinstance(state_data, dict) and state_data.get('mode') == 'manage':
        return False

    if not state_data or not isinstance(state_data, dict):
        state_data = {'mode': 'main', 'state': 'main'}
        safe_menu_state_set(key, state_data)

    state = state_data.get('state', 'main')
    is_dc = is_datacenter(peer_id)
    has_full = is_full_access(sender_id)

    def delete_original():
        if conversation_message_id:
            delete_message(peer_id, conversation_message_id)

    if clean_text == "🔙 Назад":
        delete_original()
        if state.startswith('stage3_mode_'):
            state_data['state'] = 'stage3_topics'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "Выберите тему теста:", get_stage3_topics_keyboard())
        else:
            state_data['state'] = 'main'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🏛 Главное меню:", get_main_menu_keyboard(has_full, can_manage, is_dc))
        return True

    if clean_text == "🔒 Скрыть панель":
        delete_original()
        safe_menu_state_pop(key)
        send_message(peer_id, "🔒 Панель скрыта.", keyboard=get_empty_keyboard())
        return True

    if clean_text == "📚 1 этап (собеседование)":
        delete_original()
        st1_text = get_setting("st1_text", None, peer_id)
        if not st1_text:
            st1_text = "📝 Текст собеседования не задан."
        send_long_message(peer_id, st1_text)
        return True

    if clean_text == "📖 2 этап (лекция)":
        delete_original()
        st2_text = get_setting("st2_text", None, peer_id)
        if not st2_text:
            st2_text = "📚 Текст лекции не задан."
        send_long_message(peer_id, st2_text)
        return True

    if clean_text == "📝 3 этап (тесты)":
        delete_original()
        state_data['state'] = 'stage3_topics'
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "Выберите тему теста:", get_stage3_topics_keyboard())
        return True

    if clean_text == "🎨 4 этап (творческое)":
        delete_original()
        state_data['state'] = 'stage4_types'
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "Выберите тип творческого задания:", get_stage4_types_keyboard())
        return True

    if clean_text == "🛠 Управление материалами" and can_manage:
        delete_original()
        state_data = {
            'mode': 'manage',
            'state': 'manage_main',
            'buffer': ''
        }
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "🛠 Панель управления материалами:", get_manage_main_keyboard())
        return True

    # ---------- ВЫБОР ТЕМЫ (3 ЭТАП) ----------
    if state == 'stage3_topics':
        topics_map = {
            "Конституция": "Конституция",
            "Устав адвокатуры": "Устав_адвокатуры",
            "Уголовный кодекс": "Уголовный_кодекс",
            "Федеральное постановление": "Федеральное_постановление",
            "Процессуальный кодекс": "Процессуальный_кодекс"
        }
        if clean_text in topics_map:
            delete_original()
            topic = topics_map[clean_text]
            state_data['state'] = f'stage3_variants_{topic}'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {clean_text}:", get_stage3_variants_keyboard(topic))
            return True

    # ---------- ВЫБОР ВАРИАНТА ТЕСТА ----------
    if state.startswith('stage3_variants_'):
        topic = state.replace('stage3_variants_', '')
        display_topic = topic.replace('_', ' ')
        for v in [1, 2, 3]:
            if clean_text == f"{display_topic} вариант {v}":
                delete_original()
                has_one = has_one_by_one_test(topic, v, peer_id)
                if has_one:
                    start_one_by_one_test(peer_id, topic, v, sender_id)
                else:
                    send_message(peer_id, f"❓ Для {display_topic} вариант {v} нет вопросов. Добавьте их в управлении материалами.")
                return True

    # ---------- ВЫБОР ТИПА (4 ЭТАП) ----------
    if state == 'stage4_types':
        type_map = {
            "Ходатайства": "Ходатайства",
            "Обращение в прокуратуру/Иск": "Обращение_в_прокуратуру_Иск",
            "Доклад": "Доклад"
        }
        if clean_text in type_map:
            delete_original()
            ctype = type_map[clean_text]
            state_data['state'] = f'stage4_variants_{ctype}'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {clean_text}:", get_stage4_variants_keyboard(ctype))
            return True

    # ---------- ВЫБОР ВАРИАНТА ТВОРЧЕСКОГО ----------
    if state.startswith('stage4_variants_'):
        ctype = state.replace('stage4_variants_', '')
        display_type = ctype.replace('_', ' ')
        if ctype == "Обращение_в_прокуратуру_Иск":
            display_type = "Обращение в прокуратуру/Иск"

        if ctype == "Доклад":
            max_var = 1
        elif ctype == "Ходатайства":
            max_var = 6
        else:
            max_var = 3

        for v in range(1, max_var + 1):
            if ctype == "Ходатайства":
                expected_label = HODAITSTVA_NAMES.get(v)
                if clean_text == expected_label:
                    delete_original()
                    row = get_creative_text(ctype, v, peer_id)
                    if row and row['task_text']:
                        send_long_message(peer_id, f"📎 Творческое задание: {expected_label}\n\n{row['task_text']}")
                    else:
                        send_message(peer_id, f"📂 Творческое задание «{expected_label}» не найдено или пусто.")
                    return True
            else:
                expected_spaces = f"{display_type} вариант {v}"
                expected_underscores = f"{ctype} вариант {v}"
                if clean_text == expected_spaces or clean_text == expected_underscores:
                    delete_original()
                    row = get_creative_text(ctype, v, peer_id)
                    if ctype == "Доклад":
                        conn = get_db_connection(peer_id)
                        try:
                            cur = conn.cursor()
                            cur.execute("SELECT id, text, template FROM topics ORDER BY RANDOM() LIMIT 1")
                            topic_row = cur.fetchone()
                        finally:
                            conn.close()
                        report_template = get_report_template(peer_id)
                        if topic_row:
                            topic_text = topic_row['text']
                            topic_template = topic_row['template'] or ""
                            msg_text = ""
                            if report_template:
                                msg_text += f"📎 Форма доклада:\n{report_template}\n\n"
                            msg_text += f"Тема: {topic_text}"
                            if topic_template:
                                msg_text += f"\n\nШаблон темы:\n{topic_template}"
                        else:
                            msg_text = "📎 Творческое задание: Доклад\n\n⚠️ Темы для докладов не добавлены."
                        if row and row['task_text']:
                            msg_text += f"\n\n{row['task_text']}"
                        send_long_message(peer_id, msg_text)
                    else:
                        if row and row['task_text']:
                            send_long_message(peer_id, f"📎 Творческое задание: {display_type}\n\n{row['task_text']}")
                        else:
                            send_message(peer_id, f"📂 Творческое задание типа «{display_type}» вариант {v} не найдено или пусто.")
                    return True

    return False

# ======================== ФУНКЦИИ ТЕСТИРОВАНИЯ (общие для всей беседы) ============================

def start_one_by_one_test(peer_id, topic, variant, sender_id):
    if peer_id in active_tests:
        send_message(peer_id, "⏳ В этой беседе уже запущен тест. Дождитесь его завершения.")
        return

    questions = get_test_questions(topic, variant, peer_id)
    if not questions:
        send_message(peer_id, "❓ Нет вопросов для этого варианта в режиме по одному.")
        return

    questions_with_options = []
    for q in questions:
        options = get_test_options(q['id'], peer_id)
        questions_with_options.append({
            'id': q['id'],
            'question_text': q['question_text'],
            'correct_option_index': q['correct_option_index'],
            'order_num': q['order_num'],
            'options': options
        })

    start_text = f"📝 Тест по теме: {topic} (вариант {variant})\n\nНажмите «Готов», чтобы начать, или «Отмена», чтобы отменить."
    keyboard = get_test_start_keyboard()
    send_message(peer_id, start_text, keyboard=keyboard)

    active_tests[peer_id] = {
        'topic': topic,
        'variant': variant,
        'questions': questions_with_options,
        'current_index': 0,
        'errors': 0,
        'results': [],
        'answers': [],
        'total': len(questions_with_options),
        'finished': False,
        'timer': None,
        'cmid': None,
        'started': False,
        'paused': False,
        'initiator': sender_id
    }

def begin_test(peer_id, cmid):
    test = active_tests.get(peer_id)
    if not test or test.get('started', False):
        return

    test['cmid'] = cmid
    test['started'] = True
    send_next_question(peer_id)

def cancel_test(peer_id, cmid):
    test = active_tests.pop(peer_id, None)
    if not test:
        return

    if cmid:
        edit_message(peer_id, cmid, "❌ Тест отменён.", keyboard=get_empty_keyboard())

def send_next_question(peer_id):
    test = active_tests.get(peer_id)
    if not test or test.get('finished', False):
        return

    if not test.get('started', False):
        return

    if test.get('paused', False):
        return

    idx = test['current_index']
    questions = test['questions']
    if idx >= len(questions):
        finish_test(peer_id, success=True)
        return

    q = questions[idx]
    options = q['options']
    if not options:
        test['results'].append(False)
        test['answers'].append({
            'question_text': q['question_text'],
            'chosen_text': 'Нет вариантов',
            'correct_text': 'Нет вариантов',
            'correct': False
        })
        test['errors'] += 1
        test['current_index'] += 1
        if not check_fail(peer_id):
            send_next_question(peer_id)
        return

    question_text = f"❓ {idx+1}. {q['question_text']}"
    option_labels = [opt['option_label'] for opt in options]
    option_texts = [opt['option_text'] for opt in options]
    for label, text in zip(option_labels, option_texts):
        question_text += f"\n{label} {text}"

    keyboard = get_test_question_keyboard(option_texts, option_labels)
    cmid = test.get('cmid')

    if cmid:
        edit_message(peer_id, cmid, question_text, keyboard=keyboard)
    else:
        resp = send_message(peer_id, question_text, keyboard=keyboard)
        if resp and resp.get('conversation_message_id'):
            test['cmid'] = resp['conversation_message_id']

    time_limit = get_test_time_limit(peer_id)
    if test.get('timer'):
        test['timer'].cancel()
    timer = threading.Timer(time_limit, on_test_timeout, args=[peer_id])
    timer.daemon = True
    timer.start()
    test['timer'] = timer

def pause_test(peer_id, cmid):
    test = active_tests.get(peer_id)
    if not test or test.get('finished', False):
        return

    if test.get('timer'):
        test['timer'].cancel()
        test['timer'] = None

    test['paused'] = True

    text = "⏸ Тест приостановлен. Выберите действие:"
    keyboard = get_test_pause_keyboard()
    if cmid:
        edit_message(peer_id, cmid, text, keyboard=keyboard)
    else:
        send_message(peer_id, text, keyboard=keyboard)

def resume_test(peer_id, cmid):
    test = active_tests.get(peer_id)
    if not test or test.get('finished', False):
        return

    if not test.get('paused', False):
        return

    test['paused'] = False
    send_next_question(peer_id)

def end_test_early(peer_id, cmid):
    test = active_tests.get(peer_id)
    if not test or test.get('finished', False):
        return

    if test.get('timer'):
        test['timer'].cancel()
        test['timer'] = None

    finish_test(peer_id, success=False, reason='user_cancelled')

def on_test_timeout(peer_id):
    test = active_tests.get(peer_id)
    if not test or test.get('finished', False):
        return

    q = test['questions'][test['current_index']]
    test['results'].append(False)
    test['answers'].append({
        'question_text': q['question_text'],
        'chosen_text': 'Время вышло',
        'correct_text': q['options'][q['correct_option_index']]['option_text'] if q['options'] else 'Нет вариантов',
        'correct': False
    })
    test['errors'] += 1
    test['current_index'] += 1

    if check_fail(peer_id):
        return

    cmid = test.get('cmid')
    if cmid:
        edit_message(peer_id, cmid, "⏰ Время вышло! Засчитано как ошибка.", keyboard=get_empty_keyboard())
        def show_next():
            time.sleep(1.5)
            if test.get('finished', False):
                return
            if cmid:
                edit_message(peer_id, cmid, "⏳ Следующий вопрос...", keyboard=get_empty_keyboard())
            time.sleep(1.0)
            if test.get('finished', False):
                return
            send_next_question(peer_id)
        threading.Thread(target=show_next, daemon=True).start()
    else:
        send_next_question(peer_id)

def check_fail(peer_id):
    test = active_tests.get(peer_id)
    if not test:
        return True
    threshold = get_test_fail_threshold(peer_id)
    if test['errors'] >= threshold:
        finish_test(peer_id, success=False)
        return True
    return False

def finish_test(peer_id, success, reason=None):
    test = active_tests.pop(peer_id, None)
    if not test:
        return

    if test.get('timer'):
        test['timer'].cancel()

    total = test['total']
    results = test['results']
    correct = sum(results)
    errors = test['errors']

    report_lines = []
    for i, res in enumerate(results, 1):
        report_lines.append(f"{i}. {'+' if res else '-'}")
    report = "\n".join(report_lines)

    if reason == 'user_cancelled':
        msg = f"⏹ Тест завершён досрочно.\nПравильных: {correct}/{total}\nОшибок: {errors}\n\n{report}"
    elif success:
        msg = f"✅ Тест пройден!\nПравильных: {correct}/{total}\nОшибок: {errors}\n\n{report}"
    else:
        msg = f"❌ Тест провален! Превышен порог ошибок ({get_test_fail_threshold(peer_id)}).\nПравильных: {correct}/{total}\nОшибок: {errors}\n\n{report}"

    cmid = test.get('cmid')
    if cmid:
        edit_message(peer_id, cmid, msg, keyboard=get_stage3_topics_keyboard())
    else:
        send_message(peer_id, msg, keyboard=get_stage3_topics_keyboard())

    initiator = test.get('initiator')
    if initiator:
        key = (peer_id, initiator)
        safe_menu_state_set(key, {'mode': 'main', 'state': 'stage3_topics'})

    datacenter = get_datacenter_peer_id()
    if datacenter and datacenter != peer_id:
        audience_name = get_chat_name(peer_id) or f"Беседа {peer_id}"
        header = f"📝 ДЕТАЛЬНЫЙ ОТЧЁТ по тесту от аудитории: {audience_name}\nТема: {test['topic']} (вариант {test['variant']})\n\n"
        detail_lines = []
        for i, ans in enumerate(test.get('answers', []), 1):
            status = "✅" if ans.get('correct', False) else "❌"
            question = ans.get('question_text', 'Вопрос')
            chosen = ans.get('chosen_text', 'Нет ответа')
            correct_ans = ans.get('correct_text', 'Нет правильного')
            detail_lines.append(f"{i}. {question}\n   Выбран: {chosen}\n   Правильный: {correct_ans}\n   {status}\n")
        if not detail_lines:
            detail_lines.append("Нет данных по ответам.")
        detail_report = header + "\n".join(detail_lines)
        send_long_message(int(datacenter), detail_report)

def handle_test_answer_callback(event):
    peer_id = event.object.peer_id
    test = active_tests.get(peer_id)
    if not test or test.get('finished', False):
        return

    if test.get('timer'):
        test['timer'].cancel()
        test['timer'] = None

    payload = event.object.payload
    if not payload or 'index' not in payload:
        return
    chosen_index = payload['index']

    q = test['questions'][test['current_index']]
    options = q.get('options', [])
    correct_index = q.get('correct_option_index', 0)
    correct_text = options[correct_index]['option_text'] if options else 'Нет вариантов'
    chosen_text = options[chosen_index]['option_text'] if options and chosen_index < len(options) else 'Неизвестно'

    correct = (chosen_index == correct_index)
    test['results'].append(correct)
    test['answers'].append({
        'question_text': q['question_text'],
        'chosen_text': chosen_text,
        'correct_text': correct_text,
        'correct': correct
    })
    if not correct:
        test['errors'] += 1

    test['current_index'] += 1

    if check_fail(peer_id):
        return

    cmid = test.get('cmid')
    if cmid:
        edit_message(peer_id, cmid, "⏳ Следующий вопрос...", keyboard=get_empty_keyboard())
        def show_next():
            time.sleep(1.5)
            if test.get('finished', False):
                return
            send_next_question(peer_id)
        threading.Thread(target=show_next, daemon=True).start()
    else:
        send_next_question(peer_id)

# ======================== ОБРАБОТЧИК КОМАНД ============================

def handle_command(text, peer_id, sender_id):
    if not text.startswith('/'):
        return
    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []

    if peer_id >= 2000000000 and not is_audience_confirmed(peer_id):
        if cmd not in ['/init', '/help']:
            send_message(peer_id, "❌ Беседа не активирована. Используйте /init для активации.")
            return

    owner_only_commands = ["/addcoowner", "/removecoowner", "/listcoowners", "/listaudiences", "/deleteaudience", "/settext", "/settime", "/setthreshold"]
    if cmd in owner_only_commands and not is_owner(sender_id):
        send_message(peer_id, "❌ Эта команда доступна только владельцу.")
        return

    if cmd == "/init":
        if not can_create_audience(sender_id):
            send_message(peer_id, "❌ У вас нет прав на создание аудиторий. Обратитесь к владельцу.")
            return
        if peer_id < 2000000000:
            send_message(peer_id, "❌ /init работает только в беседах.")
            return
        request_audience_confirmation(peer_id)
        return

    if not is_owner(sender_id) and not is_allowed(sender_id):
        send_message(peer_id, "❌ У вас нет прав для использования бота.")
        return

    if cmd == "/menu":
        key = (peer_id, sender_id)
        if key in menu_state:
            safe_menu_state_pop(key)
        is_dc = is_datacenter(peer_id)
        can_manage = can_manage_materials(sender_id, peer_id)
        state_data = {'mode': 'main', 'state': 'main'}
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "🏛 Главное меню:", get_main_menu_keyboard(is_full_access(sender_id), can_manage, is_dc))
        return

    if cmd == "/panel":
        key = (peer_id, sender_id)
        if key in menu_state:
            safe_menu_state_pop(key)
        is_dc = is_datacenter(peer_id)
        can_manage = can_manage_materials(sender_id, peer_id)
        state_data = {'mode': 'main', 'state': 'main'}
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "🏛 Главное меню:", get_main_menu_keyboard(is_full_access(sender_id), can_manage, is_dc))
        return

    if cmd == "/manage":
        if not can_manage_materials(sender_id, peer_id):
            send_message(peer_id, "❌ У вас нет прав на управление материалами в этой аудитории.")
            return
        key = (peer_id, sender_id)
        state_data = {
            'mode': 'manage',
            'state': 'manage_main',
            'buffer': ''
        }
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "🛠 Панель управления материалами:", get_manage_main_keyboard())
        return

    if cmd == "/clearmenu":
        key = (peer_id, sender_id)
        safe_menu_state_pop(key)
        send_message(peer_id, "✅ Меню сброшено.")
        return

    # ==================== НОВАЯ КОМАНДА /restart ====================
    if cmd == "/restart":
        if not is_full_access(sender_id):
            send_message(peer_id, "❌ Команда доступна только владельцу или совладельцу.")
            return
        send_message(peer_id, "🔄 Перезапуск бота...")
        logger.info(f"Бот перезапущен пользователем {sender_id} из беседы {peer_id}")
        # Завершаем процесс с кодом 0, менеджер процессов перезапустит
        sys.exit(0)

    if cmd == "/help":
        help_text = (
            "⚙️ УПРАВЛЕНИЕ БОТОМ\n\n"
            "📄 ТЕСТЫ И МАТЕРИАЛЫ\n"
            "/st 1 — собеседование\n"
            "/st 2 — лекция\n"
            "/st 3 <тема> <вариант> — тест (по одному)\n"
            "/st 4 <тип> <вариант> — творческое\n\n"
            "🔒 ПРАВА ДОСТУПА\n"
            "/allow @user — выдать доступ на создание аудиторий (только владелец)\n"
            "/disallow @user — забрать доступ\n"
            "/listallowed — список администраторов (могут создавать аудитории)\n"
            "/addcoowner @user — добавить совладельца (полный доступ, только владелец)\n"
            "/removecoowner @user — убрать совладельца\n"
            "/listcoowners — список совладельцев\n\n"
            "📝 ШАБЛОНЫ ТЕКСТОВ (только владелец/совладелец)\n"
            "/settext st1|st2|st3|st4|graduation <текст>\n\n"
            "🔧 НАСТРОЙКИ ТЕСТИРОВАНИЯ (по одному)\n"
            "/settime <сек> — время на вопрос\n"
            "/setthreshold <число> — порог ошибок\n\n"
            "🔧 ДРУГОЕ\n"
            "/menu — открыть главное меню\n"
            "/clearmenu — сбросить состояние меню\n"
            "/mypeer — показать ID беседы\n"
            "/addto @user — добавить в беседу\n"
            "/end @user 1|2 — завершить обучение\n\n"
            "🏛 АУДИТОРИИ\n"
            "/init — запросить подтверждение аудитории (только для тех, у кого есть право создавать)\n"
            "/sync — синхронизировать с датацентром (копировать данные)\n"
            "/setowner @user — сменить владельца аудитории\n"
            "/listaudiences — список всех аудиторий (названия, владелец, последняя активность)\n"
            "/deleteaudience <peer_id> — удалить аудиторию (только владелец)\n\n"
            "🔄 /restart — перезапустить бота (только владелец/совладелец)"
        )
        send_message(peer_id, help_text)
        return

    if cmd == "/mypeer":
        send_message(peer_id, f"📌 Peer ID: {peer_id}")
        return

    if cmd == "/addto":
        if not args:
            send_message(peer_id, "⚠️ /addto @user")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        user_id = match.group(1)
        if add_user_to_chat(peer_id, int(user_id)):
            send_message(peer_id, f"✅ Пользователь {get_user_nickname(user_id)} добавлен в беседу.")
        else:
            send_message(peer_id, "❌ Не удалось добавить пользователя. Проверьте права бота.")
        return

    if cmd == "/allow":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может выдавать права.")
            return
        if not args:
            send_message(peer_id, "⚠️ /allow @user")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        user_id = match.group(1)
        add_allowed_user(user_id, sender_id)
        send_message(peer_id, f"✅ Права на создание аудиторий выданы пользователю {get_user_nickname(user_id)}.")
        return

    if cmd == "/disallow":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может забирать права.")
            return
        if not args:
            send_message(peer_id, "⚠️ /disallow @user")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        user_id = match.group(1)
        remove_allowed_user(user_id)
        send_message(peer_id, f"✅ Права на создание аудиторий отозваны у {get_user_nickname(user_id)}.")
        return

    if cmd == "/listallowed":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может просматривать список.")
            return
        rows = get_allowed_users()
        if not rows:
            send_message(peer_id, "📭 Список пользователей с правом создания аудиторий пуст.")
            return
        text = "📋 СПИСОК АДМИНИСТРАТОРОВ (могут создавать аудитории)\n\n"
        for row in rows:
            nick = get_user_nickname(row['user_id'])
            added_by = get_user_nickname(row['added_by'])
            date = time.strftime("%d.%m.%Y", time.localtime(row['added_at']))
            text += f"• {nick} (ID {row['user_id']}) — добавлен {added_by} {date}\n"
        send_message(peer_id, text)
        return

    if cmd == "/addcoowner":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может назначать совладельцев.")
            return
        if not args:
            send_message(peer_id, "⚠️ /addcoowner @user")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        user_id = match.group(1)
        add_co_owner(user_id, sender_id)
        send_message(peer_id, f"✅ Пользователь {get_user_nickname(user_id)} назначен совладельцем.")
        return

    if cmd == "/removecoowner":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может снимать совладельцев.")
            return
        if not args:
            send_message(peer_id, "⚠️ /removecoowner @user")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        user_id = match.group(1)
        remove_co_owner(user_id)
        send_message(peer_id, f"✅ Пользователь {get_user_nickname(user_id)} больше не совладелец.")
        return

    if cmd == "/listcoowners":
        rows = get_co_owners()
        if not rows:
            send_message(peer_id, "📭 Список совладельцев пуст.")
            return
        text = "📋 СПИСОК СОВЛАДЕЛЬЦЕВ (полный доступ)\n\n"
        for row in rows:
            nick = get_user_nickname(row['user_id'])
            added_by = get_user_nickname(row['added_by'])
            date = time.strftime("%d.%m.%Y", time.localtime(row['added_at']))
            text += f"• {nick} (ID {row['user_id']}) — добавлен {added_by} {date}\n"
        send_message(peer_id, text)
        return

    if cmd == "/listaudiences":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может просматривать список аудиторий.")
            return
        rows = get_all_audiences()
        if not rows:
            send_message(peer_id, "📭 Список аудиторий пуст.")
            return
        text = "📋 СПИСОК АУДИТОРИЙ\n\n"
        for row in rows:
            peer = row['peer_id']
            owner = row['owner_id']
            last_activity = row['last_activity']
            owner_name = get_user_nickname(owner) if owner else "Неизвестно"
            chat_name = get_chat_name(peer) or f"Беседа {peer}"
            last_time = time.strftime("%d.%m.%Y %H:%M", time.localtime(last_activity))
            text += f"• {chat_name}\n   ID: {peer}\n   Владелец: {owner_name}\n   Последняя активность: {last_time}\n\n"
        send_message(peer_id, text)
        return

    if cmd == "/deleteaudience":
        if not is_owner(sender_id):
            send_message(peer_id, "❌ Только владелец может удалять аудитории.")
            return
        if len(args) < 1:
            send_message(peer_id, "⚠️ /deleteaudience <peer_id>")
            return
        try:
            target_peer = int(args[0])
        except ValueError:
            send_message(peer_id, "❌ peer_id должен быть числом.")
            return
        if target_peer == peer_id:
            send_message(peer_id, "❌ Нельзя удалить текущую беседу.")
            return
        success, msg = delete_audience_by_owner(target_peer)
        send_message(peer_id, f"{'✅' if success else '❌'} {msg}")
        return

    if cmd == "/sync":
        if peer_id < 2000000000:
            send_message(peer_id, "❌ /sync работает только в беседах.")
            return
        if not can_manage_materials(sender_id, peer_id):
            send_message(peer_id, "❌ У вас нет прав на синхронизацию этой аудитории.")
            return
        if not is_audience_confirmed(peer_id):
            send_message(peer_id, "❌ Аудитория не подтверждена. Используйте /init.")
            return
        if is_datacenter(peer_id):
            send_message(peer_id, "⚠️ Эта беседа является датацентром. Синхронизация не требуется.")
            return
        try:
            if copy_datacenter_to_audience(peer_id):
                send_message(peer_id, "✅ Данные аудитории синхронизированы с датацентром.")
            else:
                send_message(peer_id, "❌ Не удалось синхронизировать: датацентр не найден.")
        except Exception as e:
            send_message(peer_id, f"❌ Ошибка синхронизации: {e}")
        return

    if cmd == "/setowner":
        if peer_id < 2000000000:
            send_message(peer_id, "❌ /setowner работает только в беседах.")
            return
        if not can_manage_materials(sender_id, peer_id):
            send_message(peer_id, "❌ У вас нет прав на смену владельца этой аудитории.")
            return
        if not args:
            send_message(peer_id, "⚠️ /setowner @user")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        new_owner = match.group(1)
        conn = get_db_connection(None)
        try:
            conn.execute("UPDATE audiences SET owner_id=? WHERE peer_id=?", (new_owner, peer_id))
            conn.commit()
        finally:
            conn.close()
        send_message(peer_id, f"✅ Владельцем аудитории теперь является {get_user_nickname(new_owner)}.")
        return

    if cmd == "/end":
        if len(args) < 2:
            send_message(peer_id, "⚠️ /end @user 1|2")
            return
        mention = args[0]
        match = re.search(r'\[id(\d+)\|', mention)
        if not match:
            send_message(peer_id, "⚠️ Не удалось распознать пользователя.")
            return
        user_id = match.group(1)
        result = args[1]
        if result not in ("1", "2"):
            send_message(peer_id, "⚠️ Результат: 1 — успешно, 2 — не прошёл")
            return
        if result == "1":
            text = read_text_file("graduation.txt")
            if text is None:
                text = "🎉 Поздравляем! Вы успешно окончили университет!"
            send_message(peer_id, text)
        else:
            send_message(peer_id, "❌ Студент не прошёл университет.")
        kick_from_chat(peer_id, int(user_id))
        return

    if cmd == "/settext":
        if len(args) < 2:
            send_message(peer_id, "⚠️ /settext st1|st2|st3|st4|graduation <текст>")
            return
        name = args[0]
        if name not in ("st1", "st2", "st3", "st4", "graduation"):
            send_message(peer_id, "❌ Имя должно быть st1, st2, st3, st4 или graduation.")
            return
        new_text = ' '.join(args[1:])
        set_setting(name, new_text, peer_id)
        send_message(peer_id, f"✅ Шаблон «{name}» обновлён.")
        return

    if cmd == "/settime":
        if not args:
            send_message(peer_id, "⚠️ /settime <секунды>")
            return
        try:
            seconds = int(args[0])
            if seconds < 1:
                raise ValueError
            set_test_time_limit(peer_id, seconds)
            send_message(peer_id, f"✅ Время на вопрос установлено: {seconds} сек.")
        except:
            send_message(peer_id, "❌ Введите положительное число.")
        return

    if cmd == "/setthreshold":
        if not args:
            send_message(peer_id, "⚠️ /setthreshold <число>")
            return
        try:
            threshold = int(args[0])
            if threshold < 0:
                raise ValueError
            set_test_fail_threshold(peer_id, threshold)
            send_message(peer_id, f"✅ Порог ошибок установлен: {threshold}.")
        except:
            send_message(peer_id, "❌ Введите неотрицательное число.")
        return

    send_message(peer_id, "⚠️ Неизвестная команда. Введите /help для списка.")

# ======================== ОБРАБОТЧИК ПАНЕЛИ УПРАВЛЕНИЯ ============================

def handle_manage_message(text, peer_id, sender_id, conversation_message_id):
    clean_text = clean_text_from_mentions(text)
    key = (peer_id, sender_id)
    state_data = safe_menu_state_get(key)
    if not state_data or not isinstance(state_data, dict) or state_data.get('mode') != 'manage':
        return False

    current_state = state_data.get('state', 'manage_main')

    def delete_original():
        if conversation_message_id:
            delete_message(peer_id, conversation_message_id)

    # ----- АККУМУЛЯЦИЯ ТЕКСТА -----
    if current_state in ['wait_st1_text', 'wait_st2_text', 'wait_creative_text', 'wait_new_topic', 'wait_template_text', 'wait_report_template',
                         'wait_edit_question_text', 'wait_edit_option_text', 'wait_add_question_text', 'wait_enter_options_text', 'wait_enter_correct']:
        if clean_text not in ["💾 Сохранить", "➡️ Далее", "🔙 Назад", "✅ Готово", "➕ Ещё вариант"]:
            if 'buffer' not in state_data:
                state_data['buffer'] = ""
            if state_data['buffer']:
                state_data['buffer'] += "\n"
            state_data['buffer'] += clean_text
            safe_menu_state_set(key, state_data)
            return True

    # ----- НАВИГАЦИЯ НАЗАД -----
    if clean_text == "🔙 Назад":
        delete_original()
        if current_state in ['manage_st1', 'manage_st2', 'manage_st3_topics', 'manage_st4_types']:
            state_data['state'] = 'manage_main'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🛠 Панель управления материалами:", get_manage_main_keyboard())
        elif current_state == 'manage_st3_variants':
            state_data['state'] = 'manage_st3_topics'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📝 Выберите тему теста:", get_stage3_topics_keyboard())
        elif current_state == 'manage_st3_action':
            state_data['state'] = 'manage_st3_variants'
            topic = state_data.get('selected_topic')
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {topic}:", get_stage3_variants_keyboard(topic))
        elif current_state == 'manage_edit_one_by_one':
            state_data['state'] = 'manage_st3_variants'
            topic = state_data.get('selected_topic')
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {topic}:", get_stage3_variants_keyboard(topic))
        elif current_state in ['manage_add_question', 'manage_enter_options_type', 'manage_enter_options_text', 'manage_enter_correct', 'manage_edit_question', 'manage_edit_options', 'manage_edit_options_text',
                               'manage_select_question_to_edit', 'manage_select_question_to_delete']:
            state_data['state'] = 'manage_edit_one_by_one'
            topic = state_data['selected_topic']
            variant = state_data['selected_variant']
            safe_menu_state_set(key, state_data)
            questions = get_test_questions(topic, variant, peer_id)
            if questions:
                msg = "❓ Режим по одному. Вопросов: {}\n\n".format(len(questions))
                for q in questions:
                    msg += f"{q['order_num']}. {q['question_text']}\n"
                msg += "\nВыберите действие:"
                send_menu(peer_id, sender_id, msg, get_manage_test_questions_keyboard())
            else:
                send_menu(peer_id, sender_id, "❓ Режим по одному. Вопросов пока нет.\nДобавьте вопросы:", get_manage_test_questions_keyboard())
        elif current_state == 'manage_st4_variants':
            state_data['state'] = 'manage_st4_types'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🎨 Выберите тип творческого задания:", get_stage4_types_keyboard())
        elif current_state == 'manage_st4_action':
            state_data['state'] = 'manage_st4_variants'
            ctype = state_data.get('selected_ctype')
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {ctype}:", get_stage4_variants_keyboard(ctype))
        elif current_state == 'manage_st4_topics':
            state_data['state'] = 'manage_st4_types'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🎨 Выберите тип творческого задания:", get_stage4_types_keyboard())
        elif current_state == 'manage_st4_topic_action':
            state_data['state'] = 'manage_st4_topics'
            safe_menu_state_set(key, state_data)
            topics = get_all_topics(peer_id)
            topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
            send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
            return True
        elif current_state == 'manage_test_settings':
            state_data['state'] = 'manage_main'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🛠 Панель управления материалами:", get_manage_main_keyboard())
        elif current_state in ['manage_set_time', 'manage_set_threshold']:
            state_data['state'] = 'manage_test_settings'
            safe_menu_state_set(key, state_data)
            time_limit = get_test_time_limit(peer_id)
            threshold = get_test_fail_threshold(peer_id)
            msg = f"⚙️ НАСТРОЙКИ ТЕСТИРОВАНИЯ (по одному)\n\n⏱ Время на вопрос: {time_limit} сек\n❌ Порог ошибок: {threshold}\n\nИспользуйте команды для изменения:\n/settime <сек>\n/setthreshold <число>"
            send_menu(peer_id, sender_id, msg, get_test_settings_keyboard())
        else:
            state_data['state'] = 'manage_main'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🛠 Панель управления материалами:", get_manage_main_keyboard())
        return True

    # ----- ОСНОВНОЕ МЕНЮ УПРАВЛЕНИЯ -----
    if current_state == 'manage_main':
        if clean_text == "🗣 Собеседование":
            state_data['state'] = 'manage_st1'
            safe_menu_state_set(key, state_data)
            current_txt = get_setting("st1_text", None, peer_id)
            if not current_txt:
                current_txt = "Текст не задан."
            send_menu(peer_id, sender_id, f"📝 ТЕКУЩИЙ ТЕКСТ СОБЕСЕДОВАНИЯ:\n\n{current_txt}", get_manage_simple_action_keyboard())
        elif clean_text == "📚 Лекция":
            state_data['state'] = 'manage_st2'
            safe_menu_state_set(key, state_data)
            current_txt = get_setting("st2_text", None, peer_id)
            if not current_txt:
                current_txt = "Текст не задан."
            send_menu(peer_id, sender_id, f"📝 ТЕКУЩИЙ ТЕКСТ ЛЕКЦИИ:\n\n{current_txt}", get_manage_simple_action_keyboard())
        elif clean_text == "❓ Тесты (по одному)":
            state_data['state'] = 'manage_st3_topics'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📝 Выберите тему теста:", get_stage3_topics_keyboard())
        elif clean_text == "🎨 Творческое":
            state_data['state'] = 'manage_st4_types'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🎨 Выберите тип творческого задания:", get_stage4_types_keyboard())
        elif clean_text == "⚙️ Настройки тестирования":
            state_data['state'] = 'manage_test_settings'
            safe_menu_state_set(key, state_data)
            time_limit = get_test_time_limit(peer_id)
            threshold = get_test_fail_threshold(peer_id)
            msg = f"⚙️ НАСТРОЙКИ ТЕСТИРОВАНИЯ (по одному)\n\n⏱ Время на вопрос: {time_limit} сек\n❌ Порог ошибок: {threshold}\n\nИспользуйте команды для изменения:\n/settime <сек>\n/setthreshold <число>"
            send_menu(peer_id, sender_id, msg, get_test_settings_keyboard())
        elif clean_text == "🏛 Главное меню":
            safe_menu_state_pop(key)
            is_dc = is_datacenter(peer_id)
            can_manage = can_manage_materials(sender_id, peer_id)
            state_data = {'mode': 'main', 'state': 'main'}
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🏛 Главное меню:", get_main_menu_keyboard(is_full_access(sender_id), can_manage, is_dc))
        return True

    # ----- УПРАВЛЕНИЕ СОБЕСЕДОВАНИЕМ -----
    if current_state == 'manage_st1':
        if clean_text == "➕ Изменить текст":
            state_data['state'] = 'wait_st1_text'
            state_data['buffer'] = ""
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📥 Отправьте текст СОБЕСЕДОВАНИЯ (можно несколькими сообщениями).\nПо окончании нажмите «💾 Сохранить».", get_buffer_keyboard())
        return True

    if current_state == 'wait_st1_text' and clean_text == "💾 Сохранить":
        final_text = state_data.get('buffer', '').strip()
        set_setting("st1_text", final_text, peer_id)
        state_data['state'] = 'manage_st1'
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, f"✅ Текст собеседования обновлён!\n\n{final_text if final_text else '(пусто)'}", get_manage_simple_action_keyboard())
        delete_message_later(peer_id, conversation_message_id)
        return True

    # ----- УПРАВЛЕНИЕ ЛЕКЦИЕЙ -----
    if current_state == 'manage_st2':
        if clean_text == "➕ Изменить текст":
            state_data['state'] = 'wait_st2_text'
            state_data['buffer'] = ""
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📥 Отправьте текст ЛЕКЦИИ (можно несколькими сообщениями).\nПо окончании нажмите «💾 Сохранить».", get_buffer_keyboard())
        return True

    if current_state == 'wait_st2_text' and clean_text == "💾 Сохранить":
        final_text = state_data.get('buffer', '').strip()
        set_setting("st2_text", final_text, peer_id)
        state_data['state'] = 'manage_st2'
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, f"✅ Текст лекции обновлён!\n\n{final_text if final_text else '(пусто)'}", get_manage_simple_action_keyboard())
        delete_message_later(peer_id, conversation_message_id)
        return True

    # ----- УПРАВЛЕНИЕ ТЕСТАМИ (по одному) -----
    if current_state == 'manage_st3_topics':
        topics_map = {
            "Конституция": "Конституция",
            "Устав адвокатуры": "Устав_адвокатуры",
            "Уголовный кодекс": "Уголовный_кодекс",
            "Федеральное постановление": "Федеральное_постановление",
            "Процессуальный кодекс": "Процессуальный_кодекс"
        }
        if clean_text in topics_map:
            state_data['selected_topic'] = topics_map[clean_text]
            state_data['state'] = 'manage_st3_variants'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {clean_text}:", get_stage3_variants_keyboard(topics_map[clean_text]))
        return True

    if current_state == 'manage_st3_variants':
        match = re.search(r'вариант (\d+)', clean_text)
        if match:
            variant = int(match.group(1))
            state_data['selected_variant'] = variant
            state_data['state'] = 'manage_edit_one_by_one'
            safe_menu_state_set(key, state_data)
            topic = state_data['selected_topic']
            questions = get_test_questions(topic, variant, peer_id)
            if questions:
                msg = "❓ Режим по одному. Вопросов: {}\n\n".format(len(questions))
                for q in questions:
                    msg += f"{q['order_num']}. {q['question_text']}\n"
                msg += "\nВыберите действие:"
                send_menu(peer_id, sender_id, msg, get_manage_test_questions_keyboard())
            else:
                send_menu(peer_id, sender_id, "❓ Режим по одному. Вопросов пока нет.\nДобавьте вопросы:", get_manage_test_questions_keyboard())
        return True

    # ----- РЕДАКТИРОВАНИЕ ВОПРОСОВ ПО ОДНОМУ -----
    if current_state == 'manage_edit_one_by_one':
        if clean_text == "➕ Добавить вопрос":
            state_data['state'] = 'manage_add_question'
            state_data['question_data'] = {}
            state_data['option_list'] = []
            state_data['option_type'] = None
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "Введите текст вопроса (можно несколькими словами).\nДля отмены введите 'стоп'.", None)
            return True
        elif clean_text == "✏️ Редактировать вопрос":
            questions = get_test_questions(state_data['selected_topic'], state_data['selected_variant'], peer_id)
            if not questions:
                send_message(peer_id, "Нет вопросов для редактирования.")
                return True
            state_data['state'] = 'manage_select_question_to_edit'
            safe_menu_state_set(key, state_data)
            msg = "Выберите номер вопроса для редактирования:\n"
            for i, q in enumerate(questions, 1):
                msg += f"{i}. {q['question_text']}\n"
            send_menu(peer_id, sender_id, msg, get_question_list_keyboard(questions))
            return True
        elif clean_text == "🗑 Удалить вопрос":
            questions = get_test_questions(state_data['selected_topic'], state_data['selected_variant'], peer_id)
            if not questions:
                send_message(peer_id, "Нет вопросов для удаления.")
                return True
            state_data['state'] = 'manage_select_question_to_delete'
            safe_menu_state_set(key, state_data)
            msg = "Выберите номер вопроса для удаления:\n"
            for i, q in enumerate(questions, 1):
                msg += f"{i}. {q['question_text']}\n"
            send_menu(peer_id, sender_id, msg, get_question_list_keyboard(questions))
            return True
        elif clean_text == "🗑 Удалить все вопросы":
            topic = state_data['selected_topic']
            variant = state_data['selected_variant']
            delete_test_questions(peer_id, topic, variant)
            send_message(peer_id, "✅ Все вопросы удалены.")
            state_data['state'] = 'manage_edit_one_by_one'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "❓ Режим по одному. Вопросов пока нет.\nДобавьте вопросы:", get_manage_test_questions_keyboard())
            return True
        elif clean_text == "🔙 Назад":
            state_data['state'] = 'manage_st3_variants'
            topic = state_data['selected_topic']
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {topic}:", get_stage3_variants_keyboard(topic))
            return True

    # ----- ВЫБОР ВОПРОСА ДЛЯ РЕДАКТИРОВАНИЯ -----
    if current_state == 'manage_select_question_to_edit':
        try:
            num = int(clean_text)
            questions = get_test_questions(state_data['selected_topic'], state_data['selected_variant'], peer_id)
            if 1 <= num <= len(questions):
                q = questions[num-1]
                state_data['edit_question_id'] = q['id']
                state_data['edit_question_order'] = q['order_num']
                state_data['state'] = 'manage_edit_question'
                safe_menu_state_set(key, state_data)
                send_menu(peer_id, sender_id, f"Редактируем вопрос #{num}:\nТекущий текст: {q['question_text']}\nВведите новый текст вопроса (или 'стоп' для отмены):", None)
            else:
                send_message(peer_id, "Введите корректный номер.")
        except ValueError:
            send_message(peer_id, "Введите число.")
        return True

    if current_state == 'manage_edit_question':
        if clean_text.lower() == 'стоп':
            state_data['state'] = 'manage_edit_one_by_one'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "❓ Редактирование отменено.", get_manage_test_questions_keyboard())
            return True
        state_data['edit_question_text'] = clean_text
        state_data['state'] = 'manage_edit_options_type'
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "Выберите тип меток для вариантов (новые варианты перезапишут старые):\n1 - A, B, C...\n2 - 1, 2, 3...\n3 - А, Б, В...", None)
        return True

    if current_state == 'manage_edit_options_type':
        if clean_text in ['1', '2', '3']:
            state_data['edit_option_type'] = clean_text
            state_data['state'] = 'manage_edit_options_text'
            state_data['edit_option_list'] = []
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "Введите текст первого варианта ответа (новые варианты перезапишут старые):", get_add_option_keyboard())
            return True
        else:
            send_message(peer_id, "Пожалуйста, выберите 1, 2 или 3.")
            return True

    if current_state == 'manage_edit_options_text':
        if clean_text == "✅ Готово":
            if not state_data.get('edit_option_list'):
                send_message(peer_id, "Вы не ввели ни одного варианта. Введите хотя бы один.")
                return True
            state_data['state'] = 'manage_edit_correct'
            safe_menu_state_set(key, state_data)
            options = state_data['edit_option_list']
            labels = get_option_labels(len(options), state_data['edit_option_type'])
            msg = "Введите номер правильного варианта (1-{}):\n".format(len(options))
            for i, (label, text) in enumerate(zip(labels, options), 1):
                msg += f"{i}. {label}) {text}\n"
            send_menu(peer_id, sender_id, msg, None)
            return True
        elif clean_text == "➕ Ещё вариант":
            send_message(peer_id, "Введите текст следующего варианта:")
            return True
        elif clean_text == "🔙 Назад":
            state_data['state'] = 'manage_edit_one_by_one'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "❓ Редактирование отменено.", get_manage_test_questions_keyboard())
            return True
        else:
            if 'edit_option_list' not in state_data:
                state_data['edit_option_list'] = []
            state_data['edit_option_list'].append(clean_text)
            send_message(peer_id, f"Вариант {len(state_data['edit_option_list'])} сохранён.\nВведите следующий вариант или нажмите «✅ Готово».", keyboard=get_add_option_keyboard())
            safe_menu_state_set(key, state_data)
            return True

    if current_state == 'manage_edit_correct':
        try:
            correct_num = int(clean_text)
            options = state_data['edit_option_list']
            if 1 <= correct_num <= len(options):
                correct_index = correct_num - 1
                qid = state_data['edit_question_id']
                topic = state_data['selected_topic']
                variant = state_data['selected_variant']
                question_text = state_data['edit_question_text']
                labels = get_option_labels(len(options), state_data['edit_option_type'])
                order = state_data['edit_question_order']
                update_test_question(qid, question_text, correct_index, list(zip(labels, options)), peer_id)
                send_message(peer_id, "✅ Вопрос обновлён!")
                state_data['state'] = 'manage_edit_one_by_one'
                safe_menu_state_set(key, state_data)
                questions = get_test_questions(topic, variant, peer_id)
                if questions:
                    msg = "❓ Режим по одному. Вопросов: {}\n\n".format(len(questions))
                    for q in questions:
                        msg += f"{q['order_num']}. {q['question_text']}\n"
                    msg += "\nВыберите действие:"
                    send_menu(peer_id, sender_id, msg, get_manage_test_questions_keyboard())
                else:
                    send_menu(peer_id, sender_id, "❓ Режим по одному. Вопросов пока нет.\nДобавьте вопросы:", get_manage_test_questions_keyboard())
                return True
            else:
                send_message(peer_id, f"Введите число от 1 до {len(options)}.")
        except ValueError:
            send_message(peer_id, "Пожалуйста, введите число.")
        return True

    # ----- ВЫБОР ВОПРОСА ДЛЯ УДАЛЕНИЯ -----
    if current_state == 'manage_select_question_to_delete':
        try:
            num = int(clean_text)
            questions = get_test_questions(state_data['selected_topic'], state_data['selected_variant'], peer_id)
            if 1 <= num <= len(questions):
                q = questions[num-1]
                qid = q['id']
                delete_test_question(qid, peer_id)
                send_message(peer_id, "✅ Вопрос удалён.")
                state_data['state'] = 'manage_edit_one_by_one'
                safe_menu_state_set(key, state_data)
                questions = get_test_questions(state_data['selected_topic'], state_data['selected_variant'], peer_id)
                if questions:
                    msg = "❓ Режим по одному. Вопросов: {}\n\n".format(len(questions))
                    for q in questions:
                        msg += f"{q['order_num']}. {q['question_text']}\n"
                    msg += "\nВыберите действие:"
                    send_menu(peer_id, sender_id, msg, get_manage_test_questions_keyboard())
                else:
                    send_menu(peer_id, sender_id, "❓ Режим по одному. Вопросов пока нет.\nДобавьте вопросы:", get_manage_test_questions_keyboard())
            else:
                send_message(peer_id, "Введите корректный номер.")
        except ValueError:
            send_message(peer_id, "Введите число.")
        return True

    # ----- ДОБАВЛЕНИЕ НОВОГО ВОПРОСА -----
    if current_state == 'manage_add_question':
        if clean_text.lower() == 'стоп':
            state_data['state'] = 'manage_edit_one_by_one'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "❓ Добавление отменено.", get_manage_test_questions_keyboard())
            return True
        state_data['question_data']['question'] = clean_text
        state_data['state'] = 'manage_enter_options_type'
        safe_menu_state_set(key, state_data)
        send_menu(peer_id, sender_id, "Выберите тип меток для вариантов:\n1 - A, B, C...\n2 - 1, 2, 3...\n3 - А, Б, В...", None)
        return True

    if current_state == 'manage_enter_options_type':
        if clean_text in ['1', '2', '3']:
            state_data['option_type'] = clean_text
            state_data['state'] = 'manage_enter_options_text'
            state_data['option_list'] = []
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "Введите текст первого варианта ответа:", get_add_option_keyboard())
            return True
        else:
            send_message(peer_id, "Пожалуйста, выберите 1, 2 или 3.")
            return True

    if current_state == 'manage_enter_options_text':
        if clean_text == "✅ Готово":
            if not state_data.get('option_list'):
                send_message(peer_id, "Вы не ввели ни одного варианта. Введите хотя бы один.")
                return True
            state_data['state'] = 'manage_enter_correct'
            safe_menu_state_set(key, state_data)
            options = state_data['option_list']
            labels = get_option_labels(len(options), state_data['option_type'])
            msg = "Введите номер правильного варианта (1-{}):\n".format(len(options))
            for i, (label, text) in enumerate(zip(labels, options), 1):
                msg += f"{i}. {label}) {text}\n"
            send_menu(peer_id, sender_id, msg, None)
            return True
        elif clean_text == "➕ Ещё вариант":
            send_message(peer_id, "Введите текст следующего варианта:")
            return True
        elif clean_text == "🔙 Назад":
            state_data['state'] = 'manage_edit_one_by_one'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "❓ Редактирование отменено.", get_manage_test_questions_keyboard())
            return True
        else:
            if 'option_list' not in state_data:
                state_data['option_list'] = []
            state_data['option_list'].append(clean_text)
            send_message(peer_id, f"Вариант {len(state_data['option_list'])} сохранён.\nВведите следующий вариант или нажмите «✅ Готово».", keyboard=get_add_option_keyboard())
            safe_menu_state_set(key, state_data)
            return True

    if current_state == 'manage_enter_correct':
        try:
            correct_num = int(clean_text)
            options = state_data['option_list']
            if 1 <= correct_num <= len(options):
                correct_index = correct_num - 1
                topic = state_data['selected_topic']
                variant = state_data['selected_variant']
                question_text = state_data['question_data']['question']
                labels = get_option_labels(len(options), state_data['option_type'])
                conn = get_db_connection(peer_id)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT MAX(order_num) FROM test_questions WHERE topic=? AND variant=?", (topic, variant))
                    row = cur.fetchone()
                    order = (row[0] or 0) + 1
                finally:
                    conn.close()
                add_test_question(peer_id, topic, variant, question_text, correct_index, order, list(zip(labels, options)))
                send_message(peer_id, "✅ Вопрос добавлен!")
                state_data['state'] = 'manage_edit_one_by_one'
                safe_menu_state_set(key, state_data)
                questions = get_test_questions(topic, variant, peer_id)
                if questions:
                    msg = "❓ Режим по одному. Вопросов: {}\n\n".format(len(questions))
                    for q in questions:
                        msg += f"{q['order_num']}. {q['question_text']}\n"
                    msg += "\nВыберите действие:"
                    send_menu(peer_id, sender_id, msg, get_manage_test_questions_keyboard())
                else:
                    send_menu(peer_id, sender_id, "❓ Режим по одному. Вопросов пока нет.\nДобавьте вопросы:", get_manage_test_questions_keyboard())
                return True
            else:
                send_message(peer_id, f"Введите число от 1 до {len(options)}.")
        except ValueError:
            send_message(peer_id, "Пожалуйста, введите число.")
        return True

    # ----- УПРАВЛЕНИЕ ТВОРЧЕСКИМИ =====
    if current_state == 'manage_st4_variants':
        ctype = state_data.get('selected_ctype')
        if not ctype:
            return True

        # Определяем выбранный вариант
        variant = None
        if ctype == "Ходатайства":
            for v, name in HODAITSTVA_NAMES.items():
                if clean_text == name:
                    variant = v
                    break
        elif ctype == "Обращение_в_прокуратуру_Иск":
            match = re.match(r'^Обращение в прокуратуру/Иск вариант (\d+)$', clean_text)
            if match:
                variant = int(match.group(1))
        elif ctype == "Доклад":
            if clean_text == "Доклад вариант 1":
                variant = 1

        if variant is not None:
            state_data['selected_variant'] = variant
            state_data['state'] = 'manage_st4_action'
            safe_menu_state_set(key, state_data)
            row = get_creative_text(ctype, variant, peer_id)
            if row and row['task_text']:
                msg = f"📝 Текущий текст для {ctype} вариант {variant}:\n\n{row['task_text']}"
            else:
                msg = f"📭 Текст для {ctype} вариант {variant} не задан."
            send_menu(peer_id, sender_id, msg, get_manage_action_keyboard())
        return True

    if current_state == 'manage_st4_action':
        ctype = state_data.get('selected_ctype')
        variant = state_data.get('selected_variant')
        if not ctype or not variant:
            return True

        if clean_text == "🔍 Посмотреть":
            row = get_creative_text(ctype, variant, peer_id)
            if row and row['task_text']:
                send_long_message(peer_id, f"📎 Текст творческого задания:\n\n{row['task_text']}")
            else:
                send_message(peer_id, "📭 Текст не задан.")
            return True

        elif clean_text == "➕ Добавить/Заменить":
            state_data['state'] = 'wait_creative_text'
            state_data['buffer'] = ""
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📥 Отправьте текст творческого задания (можно несколькими сообщениями).\nПо окончании нажмите «💾 Сохранить».", get_buffer_keyboard())
            return True

        elif clean_text == "🗑 Удалить":
            delete_creative_text(ctype, variant, peer_id)
            send_message(peer_id, "🗑 Текст творческого задания удалён.")
            state_data['state'] = 'manage_st4_variants'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {ctype}:", get_stage4_variants_keyboard(ctype))
            return True

        elif clean_text == "🔙 Назад":
            state_data['state'] = 'manage_st4_variants'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, f"Выберите вариант для {ctype}:", get_stage4_variants_keyboard(ctype))
            return True

    if current_state == 'wait_creative_text' and clean_text == "💾 Сохранить":
        final_text = state_data.get('buffer', '').strip()
        ctype = state_data.get('selected_ctype')
        variant = state_data.get('selected_variant')
        if ctype and variant:
            set_creative_text(ctype, variant, final_text, peer_id)
            send_message(peer_id, f"✅ Текст для {ctype} вариант {variant} сохранён.")
            state_data['state'] = 'manage_st4_action'
            safe_menu_state_set(key, state_data)
            row = get_creative_text(ctype, variant, peer_id)
            if row and row['task_text']:
                msg = f"📝 Текущий текст для {ctype} вариант {variant}:\n\n{row['task_text']}"
            else:
                msg = f"📭 Текст для {ctype} вариант {variant} не задан."
            send_menu(peer_id, sender_id, msg, get_manage_action_keyboard())
            delete_message_later(peer_id, conversation_message_id)
        return True

    # ----- УПРАВЛЕНИЕ ТЕМАМИ ДОКЛАДОВ -----
    if current_state == 'manage_st4_types':
        type_map = {
            "Ходатайства": "Ходатайства",
            "Обращение в прокуратуру/Иск": "Обращение_в_прокуратуру_Иск",
            "Доклад": "Доклад"
        }
        if clean_text in type_map:
            state_data['selected_ctype'] = type_map[clean_text]
            if clean_text == "Доклад":
                state_data['state'] = 'manage_st4_topics'
                safe_menu_state_set(key, state_data)
                topics = get_all_topics(peer_id)
                topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
                send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
            else:
                state_data['state'] = 'manage_st4_variants'
                safe_menu_state_set(key, state_data)
                send_menu(peer_id, sender_id, f"Выберите вариант для {clean_text}:", get_stage4_variants_keyboard(type_map[clean_text]))
        return True

    if current_state == 'manage_st4_topics':
        if clean_text == "➕ Добавить тему":
            state_data['state'] = 'wait_new_topic'
            state_data['buffer'] = ""
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📥 Отправьте текст ТЕМЫ доклада.", None)
            return True
        if clean_text == "✏️ Изменить форму доклада":
            state_data['state'] = 'wait_report_template'
            state_data['buffer'] = ""
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📥 Отправьте новый текст ФОРМЫ ДОКЛАДА (общий шаблон).\nПо окончании нажмите «💾 Сохранить».", get_buffer_keyboard())
            return True
        if clean_text == "🗑 Очистить все темы":
            delete_all_topics(peer_id)
            send_message(peer_id, "🗑 Все темы удалены.")
            state_data['state'] = 'manage_st4_topics'
            safe_menu_state_set(key, state_data)
            topics = get_all_topics(peer_id)
            topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
            send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
            return True
        match = re.match(r'^(\d+)\.', clean_text)
        if match:
            topic_id = int(match.group(1))
            row = get_topic_by_id(topic_id, peer_id)
            if row:
                state_data['selected_topic_id'] = topic_id
                state_data['selected_topic_text'] = row['text']
                state_data['state'] = 'manage_st4_topic_action'
                safe_menu_state_set(key, state_data)
                template_status = "есть" if row['template'] else "нет"
                send_menu(peer_id, sender_id, f"📌 Тема: {row['text']}\nШаблон: {template_status}", get_creative_topic_action_keyboard())
                return True

    if current_state == 'wait_report_template' and clean_text == "💾 Сохранить":
        template = state_data.get('buffer', '').strip()
        set_report_template(template, peer_id)
        state_data['state'] = 'manage_st4_topics'
        safe_menu_state_set(key, state_data)
        topics = get_all_topics(peer_id)
        topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
        send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}\n\n✅ Общий шаблон обновлён.", get_creative_topics_keyboard())
        delete_message_later(peer_id, conversation_message_id)
        return True

    if current_state == 'manage_st4_topic_action':
        if clean_text == "✏️ Изменить шаблон":
            state_data['state'] = 'wait_template_text'
            state_data['buffer'] = ""
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "📥 Отправьте новый текст ШАБЛОНА для этой темы.\nПо окончании нажмите «💾 Сохранить».", get_buffer_keyboard())
            return True
        if clean_text == "🗑 Удалить тему":
            topic_id = state_data.get('selected_topic_id')
            if topic_id:
                delete_topic(topic_id, peer_id)
                send_message(peer_id, "🗑 Тема удалена.")
                state_data['state'] = 'manage_st4_topics'
                safe_menu_state_set(key, state_data)
                topics = get_all_topics(peer_id)
                topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
                send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
                return True
        if clean_text == "🔙 Назад":
            state_data['state'] = 'manage_st4_topics'
            safe_menu_state_set(key, state_data)
            topics = get_all_topics(peer_id)
            topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
            send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
            return True

    if current_state == 'wait_new_topic':
        if clean_text not in ["🔙 Назад"]:
            new_topic = clean_text.strip()
            if new_topic:
                state_data['temp_topic'] = new_topic
                state_data['state'] = 'wait_template_text'
                state_data['buffer'] = ""
                safe_menu_state_set(key, state_data)
                send_menu(peer_id, sender_id, "📥 Отправьте текст ШАБЛОНА для доклада (можно пропустить, отправив пустое сообщение).\nПо окончании нажмите «💾 Сохранить».", get_buffer_keyboard())
            return True

    if current_state == 'wait_template_text' and clean_text == "💾 Сохранить":
        template = state_data.get('buffer', '').strip()
        topic_text = state_data.get('temp_topic', '')
        if topic_text:
            add_topic(topic_text, template, peer_id)
            send_message(peer_id, f"✅ Тема и шаблон добавлены.")
        state_data['state'] = 'manage_st4_topics'
        safe_menu_state_set(key, state_data)
        topics = get_all_topics(peer_id)
        topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
        send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
        delete_message_later(peer_id, conversation_message_id)
        return True

    if current_state == 'wait_template_text' and clean_text == "🔙 Назад":
        state_data['state'] = 'manage_st4_topics'
        safe_menu_state_set(key, state_data)
        topics = get_all_topics(peer_id)
        topics_list = "\n".join([f"{t['id']}. {t['text']} (шаблон: {'есть' if t['template'] else 'нет'})" for t in topics]) if topics else "Список тем пуст."
        send_menu(peer_id, sender_id, f"📋 ТЕМЫ ДОКЛАДОВ:\n\n{topics_list}", get_creative_topics_keyboard())
        return True

    # ----- НАСТРОЙКИ ТЕСТИРОВАНИЯ -----
    if current_state == 'manage_test_settings':
        if clean_text == "⏱ Время на вопрос":
            state_data['state'] = 'manage_set_time'
            safe_menu_state_set(key, state_data)
            send_message(peer_id, "Введите время в секундах (число):")
            return True
        elif clean_text == "❌ Порог ошибок":
            state_data['state'] = 'manage_set_threshold'
            safe_menu_state_set(key, state_data)
            send_message(peer_id, "Введите допустимое количество ошибок (число):")
            return True
        elif clean_text == "🔙 Назад":
            state_data['state'] = 'manage_main'
            safe_menu_state_set(key, state_data)
            send_menu(peer_id, sender_id, "🛠 Панель управления материалами:", get_manage_main_keyboard())
            return True

    if current_state == 'manage_set_time':
        try:
            val = int(clean_text)
            if val < 1:
                raise ValueError
            set_test_time_limit(peer_id, val)
            send_message(peer_id, f"✅ Время на вопрос установлено: {val} сек.")
        except:
            send_message(peer_id, "❌ Введите положительное число.")
        state_data['state'] = 'manage_test_settings'
        safe_menu_state_set(key, state_data)
        time_limit = get_test_time_limit(peer_id)
        threshold = get_test_fail_threshold(peer_id)
        msg = f"⚙️ НАСТРОЙКИ ТЕСТИРОВАНИЯ (по одному)\n\n⏱ Время на вопрос: {time_limit} сек\n❌ Порог ошибок: {threshold}\n\nИспользуйте команды для изменения:\n/settime <сек>\n/setthreshold <число>"
        send_menu(peer_id, sender_id, msg, get_test_settings_keyboard())
        return True

    if current_state == 'manage_set_threshold':
        try:
            val = int(clean_text)
            if val < 0:
                raise ValueError
            set_test_fail_threshold(peer_id, val)
            send_message(peer_id, f"✅ Порог ошибок установлен: {val}.")
        except:
            send_message(peer_id, "❌ Введите неотрицательное число.")
        state_data['state'] = 'manage_test_settings'
        safe_menu_state_set(key, state_data)
        time_limit = get_test_time_limit(peer_id)
        threshold = get_test_fail_threshold(peer_id)
        msg = f"⚙️ НАСТРОЙКИ ТЕСТИРОВАНИЯ (по одному)\n\n⏱ Время на вопрос: {time_limit} сек\n❌ Порог ошибок: {threshold}\n\nИспользуйте команды для изменения:\n/settime <сек>\n/setthreshold <число>"
        send_menu(peer_id, sender_id, msg, get_test_settings_keyboard())
        return True

    return False

def get_option_labels(count, type_choice):
    if type_choice == '1':
        return [chr(65+i) for i in range(count)]
    elif type_choice == '2':
        return [str(i+1) for i in range(count)]
    elif type_choice == '3':
        return [chr(1040+i) for i in range(count)]
    else:
        return [str(i+1) for i in range(count)]

# ======================== ОБРАБОТЧИК CALLBACK ============================

def handle_callback(event):
    payload = event.object.payload
    if not payload:
        return
    cmd = payload.get('cmd')

    def safe_answer():
        try:
            vk.messages.sendMessageEventAnswer(
                event_id=event.object.event_id,
                user_id=event.object.user_id,
                peer_id=event.object.peer_id
            )
        except Exception:
            pass

    if cmd == "confirm_audience":
        peer_id = event.object.peer_id
        user_id = str(event.object.user_id)

        if not can_create_audience(user_id):
            send_message(peer_id, "❌ У вас нет прав на создание аудиторий.")
            safe_answer()
            return

        if not bot_is_admin_in_chat(peer_id):
            send_message(peer_id, "❌ Бот не является администратором этой беседы. Создание аудитории невозможно.")
            safe_answer()
            return

        conn = get_db_connection(None)
        try:
            cur = conn.cursor()
            cur.execute("SELECT request_time FROM audiences WHERE peer_id=? AND confirmed=0", (peer_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            send_message(peer_id, "❌ Запрос на подтверждение не найден. Используйте /init.")
            safe_answer()
            return
        request_time = row['request_time']
        if time.time() - request_time > 300:
            send_message(peer_id, "⏰ Время подтверждения истекло. Используйте /init для нового запроса.")
            conn = get_db_connection(None)
            try:
                conn.execute("DELETE FROM audiences WHERE peer_id=?", (peer_id,))
                conn.commit()
            finally:
                conn.close()
            safe_answer()
            return

        dc = get_datacenter_peer_id()
        if dc is None:
            send_message(peer_id, "❌ Нет активного датацентра. Сначала создайте датацентр (доступно владельцу или совладельцу).")
            safe_answer()
            return

        try:
            create_audience(peer_id, user_id)
            send_message(peer_id, f"✅ Аудитория создана! Владелец: {get_user_nickname(user_id)}.\n"
                                  f"Теперь вы можете управлять материалами через /manage или меню.\n"
                                  f"Датацентр: {dc}")
        except Exception as e:
            send_message(peer_id, f"❌ Ошибка создания аудитории: {e}")
        safe_answer()

    elif cmd == "confirm_datacenter":
        peer_id = event.object.peer_id
        user_id = str(event.object.user_id)

        if not is_full_access(user_id):
            send_message(peer_id, "❌ Создание датацентра доступно только владельцу или совладельцу.")
            safe_answer()
            return

        if not bot_is_admin_in_chat(peer_id):
            send_message(peer_id, "❌ Бот не является администратором этой беседы. Создание датацентра невозможно.")
            safe_answer()
            return

        conn = get_db_connection(None)
        try:
            cur = conn.cursor()
            cur.execute("SELECT request_time FROM audiences WHERE peer_id=? AND confirmed=0", (peer_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            send_message(peer_id, "❌ Запрос на подтверждение не найден. Используйте /init.")
            safe_answer()
            return
        request_time = row['request_time']
        if time.time() - request_time > 300:
            send_message(peer_id, "⏰ Время подтверждения истекло. Используйте /init для нового запроса.")
            conn = get_db_connection(None)
            try:
                conn.execute("DELETE FROM audiences WHERE peer_id=?", (peer_id,))
                conn.commit()
            finally:
                conn.close()
            safe_answer()
            return

        try:
            create_datacenter(peer_id, user_id)
            send_message(peer_id, f"⭐ Датацентр создан! Владелец: {get_user_nickname(user_id)}.\n"
                                  f"Теперь эта беседа использует мастер-базу.\n"
                                  f"Все ответы на тесты из аудиторий будут приходить сюда.\n"
                                  f"Другие аудитории могут синхронизироваться с этим датацентром через /sync.")
        except Exception as e:
            send_message(peer_id, f"❌ Ошибка создания датацентра: {e}")
        safe_answer()

    elif cmd == "test_ready":
        peer_id = event.object.peer_id
        cmid = event.object.conversation_message_id
        safe_answer()
        begin_test(peer_id, cmid)

    elif cmd == "test_cancel":
        peer_id = event.object.peer_id
        cmid = event.object.conversation_message_id
        safe_answer()
        cancel_test(peer_id, cmid)

    elif cmd == "test_pause":
        peer_id = event.object.peer_id
        user_id = event.object.user_id
        if not can_control_test(user_id, peer_id):
            send_message(peer_id, "❌ Только владелец аудитории может управлять тестом.")
            safe_answer()
            return
        cmid = event.object.conversation_message_id
        safe_answer()
        pause_test(peer_id, cmid)

    elif cmd == "test_resume":
        peer_id = event.object.peer_id
        user_id = event.object.user_id
        if not can_control_test(user_id, peer_id):
            send_message(peer_id, "❌ Только владелец аудитории может управлять тестом.")
            safe_answer()
            return
        cmid = event.object.conversation_message_id
        safe_answer()
        resume_test(peer_id, cmid)

    elif cmd == "test_end":
        peer_id = event.object.peer_id
        user_id = event.object.user_id
        if not can_control_test(user_id, peer_id):
            send_message(peer_id, "❌ Только владелец аудитории может управлять тестом.")
            safe_answer()
            return
        cmid = event.object.conversation_message_id
        safe_answer()
        end_test_early(peer_id, cmid)

    elif cmd == "test_answer":
        handle_test_answer_callback(event)
        safe_answer()
    else:
        safe_answer()

# -------------------- ФОНОВАЯ ОЧИСТКА --------------------

def background_cleanup():
    while True:
        time.sleep(86400)

# ======================== ОСНОВНОЙ ЦИКЛ ============================

def main():
    global vk, longpoll
    vk_session = vk_api.VkApi(token=GROUP_TOKEN)
    vk = vk_session.get_api()
    try:
        longpoll = VkBotLongPoll(vk_session, GROUP_ID, wait=45)
        print("✅ Бот запущен")
    except Exception as e:
        print(f"❌ Ошибка LongPoll: {e}")
        sys.exit(1)

    init_main_db()

    cleanup_thread = threading.Thread(target=background_cleanup, daemon=True)
    cleanup_thread.start()

    bot_id = -int(GROUP_ID)

    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_EVENT:
                    handle_callback(event)
                    continue

                if event.type == VkBotEventType.MESSAGE_NEW:
                    msg = event.object.message
                    peer_id = msg['peer_id']
                    text = msg['text'].strip()
                    sender_id = str(msg['from_id'])

                    if peer_id >= 2000000000 and is_audience_confirmed(peer_id):
                        update_audience_activity(peer_id)

                    if peer_id >= 2000000000 and not is_audience_confirmed(peer_id):
                        if not text.startswith('/') or (text.startswith('/') and not text.lower().startswith(('/init', '/help'))):
                            continue

                    action = msg.get('action')
                    if action and peer_id >= 2000000000:
                        action_type = action.get('type')
                        member_id = action.get('member_id')
                        if action_type == 'chat_invite_user' and member_id == bot_id:
                            logger.info(f"✅ Бот добавлен в беседу {peer_id}")
                            if is_audience_confirmed(peer_id):
                                send_message(peer_id, "✅ Бот уже настроен для этой аудитории.")
                            else:
                                request_audience_confirmation(peer_id)
                            continue
                        elif action_type == 'chat_kick_user' and member_id == bot_id:
                            logger.info(f"❌ Бот удалён из беседы {peer_id}")

                    if text.startswith('/'):
                        handle_command(text, peer_id, sender_id)
                        continue

                    key = (peer_id, sender_id)
                    can_manage = can_manage_materials(sender_id, peer_id)

                    state_data = safe_menu_state_get(key)
                    if state_data and isinstance(state_data, dict) and state_data.get('mode') == 'manage':
                        handled = handle_manage_message(text, peer_id, sender_id, msg.get('conversation_message_id'))
                        if handled:
                            continue

                    if can_manage:
                        handled = handle_main_menu(text, peer_id, sender_id, msg.get('conversation_message_id'), True)
                        if handled:
                            continue

                elif event.type == VkBotEventType.GROUP_JOIN:
                    peer_id = event.object.peer_id
                    user_id = event.object.user_id
                    if peer_id >= 2000000000 and user_id == bot_id:
                        logger.info(f"✅ (запасное) Бот добавлен в беседу {peer_id}")
                        if is_audience_confirmed(peer_id):
                            send_message(peer_id, "✅ Бот уже настроен для этой аудитории.")
                        else:
                            request_audience_confirmation(peer_id)

                elif event.type == VkBotEventType.GROUP_LEAVE:
                    peer_id = event.object.peer_id
                    user_id = event.object.user_id
                    if peer_id >= 2000000000 and user_id == bot_id:
                        logger.info(f"❌ (запасное) Бот удалён из беседы {peer_id}")
        except Exception as e:
            logger.error(f"Ошибка в основном цикле: {e}")
            time.sleep(5)
            try:
                longpoll = VkBotLongPoll(vk_session, GROUP_ID, wait=45)
            except:
                pass

if __name__ == "__main__":
    main()