# data_pipeline/local_cache.py
import os
import json

BASE_DATA_DIR = "data"
STRESS_DATA_FILE = os.path.join(BASE_DATA_DIR, "stress_records.json")
CALENDAR_DATA_DIR = os.path.join(BASE_DATA_DIR, "calendar_data")

def ensure_directory(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)

def get_calendar_file_path(date_str):
    file_date_str = date_str.replace('-', '')
    return os.path.join(CALENDAR_DATA_DIR, f"calendar_{file_date_str}.json")

def load_calendar_events(date_str):
    file_path = get_calendar_file_path(date_str)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ 读取日历文件出错: {str(e)}")
    return None

def save_calendar_events(date_str, events):
    try:
        ensure_directory(CALENDAR_DATA_DIR)
        file_path = get_calendar_file_path(date_str)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
    except OSError:
        pass  

def load_stress_records():
    try:
        ensure_directory(BASE_DATA_DIR)
        if os.path.exists(STRESS_DATA_FILE):
            with open(STRESS_DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_stress_records(records):
    try:
        ensure_directory(BASE_DATA_DIR)
        with open(STRESS_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except OSError:
        pass