import sqlite3
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
import uvicorn
from datetime import datetime, timedelta
import uuid
import threading

app = FastAPI()

# 동시성 문제를 완화하기 위한 락 (특히 라이선스 업데이트 시)
credit_lock = threading.Lock()

# SQLite 연결 (멀티스레드 환경 대응을 위해 check_same_thread=False)
def get_db_connection():
    conn = sqlite3.connect('license_system.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# 데이터베이스 초기화: 테이블 생성
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            license_code TEXT NOT NULL UNIQUE,
            valid_from DATETIME NOT NULL,
            valid_until DATETIME NOT NULL,
            next_payment_date DATETIME,
            usage_time INTEGER DEFAULT 0,
            usage_count INTEGER DEFAULT 0,
            remaining_usage INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id INTEGER NOT NULL,
            session_start DATETIME NOT NULL,
            session_end DATETIME,
            duration INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (license_id) REFERENCES licenses(id) ON DELETE CASCADE
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            license_id INTEGER,
            order_code TEXT NOT NULL UNIQUE,
            amount REAL NOT NULL,
            order_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            payment_status TEXT NOT NULL,
            details TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (license_id) REFERENCES licenses(id) ON DELETE SET NULL
        );
    ''')
    conn.commit()
    conn.close()

init_db()

# 새로운 라이선스 코드를 생성하는 함수
def generate_license_code():
    return str(uuid.uuid4())

# 주문(결제) 정보를 처리하는 함수
def process_order(email: str, order_code: str, amount: float):
    """
    주문 처리 시:
    - 이메일로 사용자를 확인하여 신규라면 Users 테이블에 추가.
    - 기존 라이선스가 없으면 새 라이선스 생성 (유효기간 30일, 크레딧은 결제액에 따른 변환값 예시로 amount*60초)
    - 기존 라이선스가 있으면 기존 만료일 기준 +30일 연장 및 크레딧 추가.
    - 주문 내역은 중복 주문번호 방지를 위해 INSERT OR IGNORE 처리.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (email) VALUES (?)", (email,))
        conn.commit()
        user_id = cur.lastrowid
    else:
        user_id = user["id"]

    cur.execute("SELECT * FROM licenses WHERE user_id = ?", (user_id,))
    license_record = cur.fetchone()
    now = datetime.now()
    if not license_record:
        license_code = generate_license_code()
        valid_from = now
        valid_until = now + timedelta(days=30)
        remaining_usage = int(amount * 60)  # 예시: 결제액 * 60초의 크레딧
        cur.execute("""
            INSERT INTO licenses (user_id, license_code, valid_from, valid_until, remaining_usage)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, license_code, valid_from, valid_until, remaining_usage))
        license_id = cur.lastrowid
    else:
        license_id = license_record["id"]
        # 기존 만료일 기준으로 연장 (+30일). 만료일이 지난 경우는 now로 대체.
        valid_until_str = license_record["valid_until"]
        valid_until = datetime.strptime(valid_until_str, "%Y-%m-%d %H:%M:%S")
        if valid_until < now:
            valid_until = now
        new_valid_until = valid_until + timedelta(days=30)
        remaining_usage = license_record["remaining_usage"] if license_record["remaining_usage"] is not None else 0
        additional_usage = int(amount * 60)
        remaining_usage += additional_usage
        cur.execute("""
            UPDATE licenses SET valid_until = ?, remaining_usage = ?
            WHERE id = ?
        """, (new_valid_until.strftime("%Y-%m-%d %H:%M:%S"), remaining_usage, license_id))

    cur.execute("""
        INSERT OR IGNORE INTO orders (user_id, license_id, order_code, amount, payment_status)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, license_id, order_code, amount, "completed"))
    conn.commit()
    conn.close()

# 테스트용: 주문(결제) 시뮬레이션 엔드포인트
@app.post("/simulate_order")
def simulate_order(email: str = Form(...), order_code: str = Form(...), amount: float = Form(...)):
    try:
        process_order(email, order_code, amount)
        return {"status": "order processed"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# /transcribe 엔드포인트: 파일 처리 시 라이선스 검증 및 크레딧 차감
@app.post("/transcribe")
async def transcribe(license_key: str = Form(...), file: UploadFile = File(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_code = ?", (license_key,))
    license_record = cur.fetchone()
    if not license_record:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid license key")
    
    valid_until = datetime.strptime(license_record["valid_until"], "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    if now > valid_until:
        conn.close()
        raise HTTPException(status_code=400, detail="License expired")
    
    # 파일의 재생 길이(초)를 추출하는 부분. 여기서는 데모를 위해 60초로 가정.
    file_duration = 60

    # 동시 접근에 대비하여 락을 걸고 크레딧 차감 처리
    with credit_lock:
        remaining_usage = license_record["remaining_usage"] if license_record["remaining_usage"] is not None else 0
        if remaining_usage < file_duration:
            conn.close()
            raise HTTPException(status_code=400, detail="Insufficient credit")
        new_remaining = remaining_usage - file_duration
        cur.execute("""
            UPDATE licenses 
            SET remaining_usage = ?, usage_count = usage_count + 1, usage_time = usage_time + ?
            WHERE id = ?
        """, (new_remaining, file_duration, license_record["id"]))
        # 사용 기록 기록
        session_start = now.strftime("%Y-%m-%d %H:%M:%S")
        session_end = (now + timedelta(seconds=file_duration)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO usage_logs (license_id, session_start, session_end, duration)
            VALUES (?, ?, ?, ?)
        """, (license_record["id"], session_start, session_end, file_duration))
        conn.commit()
    conn.close()

    # 파일 처리는 실제 STT나 요약 기능 대신 파일 크기를 반환하는 것으로 대체 (프로토타입)
    content = await file.read()
    return {
        "status": "file processed",
        "file_size": len(content),
        "deducted_seconds": file_duration,
        "remaining_credit": new_remaining
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)