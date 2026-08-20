import os
import io
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import FastAPI, Form, File, UploadFile, HTTPException, Depends, status
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import Column, Integer, String, Float, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from jose import JWTError, jwt
import bcrypt
from google import genai
from google.genai import types
import PIL.Image


# --- 1. DATABASE SETUP ---
DATABASE_URL = "sqlite:///./fitmentor.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- 2. DATABASE MODELS ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    age = Column(Integer, default=18)
    height_cm = Column(Float, default=175.0)
    weight_kg = Column(Float, default=70.0)
    gender = Column(String, default="male")

class WorkoutLog(Base):
    __tablename__ = "workout_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    goal = Column(String)
    day = Column(String)
    completed_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(String, nullable=True)

class WeightLog(Base):
    __tablename__ = "weight_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    weight_kg = Column(Float)
    logged_at = Column(DateTime, default=datetime.utcnow)

class MealLog(Base):
    __tablename__ = "meal_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    meal_name = Column(String)
    calories = Column(Float)
    protein_g = Column(Float, default=0.0)
    carbs_g = Column(Float, default=0.0)
    fat_g = Column(Float, default=0.0)
    logged_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)


# --- 3. AUTHENTICATION & SECURITY ---
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY .env faylında təyin olunmayıb!")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

def hash_password(password: str) -> str:
    pwd_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    pwd_bytes = plain_password.encode('utf-8')[:72]
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token keçərsizdir və ya müddəti bitib.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


# --- 4. FASTAPI APP & GEMINI CLIENT ---
app = FastAPI()

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_index():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return {"message": "FitMentor AI Backend işləyir!"}

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_KEY:
    raise RuntimeError("GEMINI_API_KEY .env faylında təyin olunmayıb!")
client = genai.Client(api_key=GEMINI_KEY)

# Yalnız aktiv söhbət tarixçəsi üçün (istəsən sonra bazaya köçürülə bilər)
user_chats: Dict[int, Dict[str, List[dict]]] = {}


# --- 5. ENDPOINTS ---
@app.post("/auth/register")
async def register(
    email: str = Form(...),
    password: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    db: Session = Depends(get_db)
):
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        return JSONResponse(status_code=400, content={"detail": "Bu e-poçt ilə artıq qeydiyyatdan keçilib."})

    new_user = User(
        email=email,
        hashed_password=hash_password(password),
        first_name=first_name,
        last_name=last_name,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    user_chats[new_user.id] = {}

    return {"message": "Qeydiyyat uğurla tamamlandı!"}

@app.post("/auth/login")
async def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        return JSONResponse(status_code=400, content={"detail": "E-poçt və ya şifrə yanlışdır."})

    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer", "email": email}


@app.get("/profile")
async def get_profile(current_user: User = Depends(get_current_user)):
    height_m = current_user.height_cm / 100
    bmi = round(current_user.weight_kg / (height_m ** 2), 1) if height_m > 0 else 0
    daily_calories = int(
        10 * current_user.weight_kg + 6.25 * current_user.height_cm - 5 * current_user.age
        + (5 if current_user.gender == "male" else -161)
    )

    return {
        "profile": {
            "first_name": current_user.first_name,
            "last_name": current_user.last_name,
            "email": current_user.email,
            "age": current_user.age,
            "gender": current_user.gender,
            "height_cm": current_user.height_cm,
            "weight_kg": current_user.weight_kg,
        },
        "bmi": bmi,
        "daily_calories": daily_calories
    }

@app.post("/profile/update")
async def update_profile(
    first_name: str = Form(...),
    last_name: str = Form(...),
    age: int = Form(...),
    gender: str = Form(...),
    height_cm: float = Form(...),
    weight_kg: float = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    current_user.first_name = first_name
    current_user.last_name = last_name
    current_user.age = age
    current_user.gender = gender
    current_user.height_cm = height_cm
    current_user.weight_kg = weight_kg
    db.commit()

    return {"message": "Profil uğurla yeniləndi!"}


@app.get("/chats")
async def get_chats(current_user: User = Depends(get_current_user)):
    chats = user_chats.get(current_user.id, {})
    return [{"id": chat_id, "title": chat_id} for chat_id in chats.keys()]

@app.get("/chats/{chat_id}")
async def get_chat_messages(chat_id: str, current_user: User = Depends(get_current_user)):
    chats = user_chats.get(current_user.id, {})
    return {"messages": chats.get(chat_id, [])}


@app.post("/ai-chat")
async def ai_chat(
    chat_id: Optional[str] = Form(None),
    message: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user)
):
    if current_user.id not in user_chats:
        user_chats[current_user.id] = {}

    if not chat_id:
        chat_id = f"Söhbət {len(user_chats[current_user.id]) + 1}"

    if chat_id not in user_chats[current_user.id]:
        user_chats[current_user.id][chat_id] = []

    contents = []
    if image:
        image_bytes = await image.read()
        pil_image = PIL.Image.open(io.BytesIO(image_bytes))
        contents.append(pil_image)

    if message:
        contents.append(message)
    else:
        contents.append("Bu şəkli analiz et.")

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction="Sən peşəkar fitnes və qidalanma məsləhətçisisən. İstifadəçi hansı dildə yazırsa yazsın, həmişə səlis və aydın Azərbaycan dilində cavab ver."
            )
        )
        reply_text = response.text
    except Exception as e:
        reply_text = f"Xəta baş verdi: {str(e)}"

    user_chats[current_user.id][chat_id].append({"sender": "user", "text": message or "Şəkil göndərildi", "image": bool(image)})
    user_chats[current_user.id][chat_id].append({"sender": "api", "text": reply_text, "image": False})

    return {"chat_id": chat_id, "reply": reply_text}


@app.post("/generate-workout")
async def generate_workout(
    location: str = Form(...),
    days_per_week: int = Form(...),
    current_user: User = Depends(get_current_user)
):
    prompt = f"""
    Mənə peşəkar fitnes məşqçisi kimi fərdiləşdirilmiş həftəlik məşq proqramı yaz.
    İstifadəçi məlumatları:
    - Yaş: {current_user.age}
    - Cins: {current_user.gender}
    - Çəki: {current_user.weight_kg} kq
    - Boy: {current_user.height_cm} sm
    - Məşq yeri: {location}
    - Həftədə məşq tezliyi: {days_per_week} dəfə

    Proqramı günlərə bölərək, hər gün üçün hərəkətləri, təkrar saylarını və qısa məsləhətləri səlis Azərbaycan dilində yaz.
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="Sən peşəkar fitnes məşqçisisən. Həmişə səlis və aydın Azərbaycan dilində cavab ver."
            )
        )
        workout_plan = response.text
    except Exception as e:
        workout_plan = f"Xəta baş verdi: {str(e)}"

    return {"workout_plan": workout_plan}


@app.post("/generate-meal-plan")
async def generate_meal_plan(
    goal: str = Form("Çəki saxlamaq"),
    current_user: User = Depends(get_current_user)
):
    daily_calories = int(
        10 * current_user.weight_kg + 6.25 * current_user.height_cm - 5 * current_user.age
        + (5 if current_user.gender == "male" else -161)
    )

    prompt = f"""
    Mənə peşəkar diyetoloq kimi fərdiləşdirilmiş gündəlik qida proqramı (səhər, nahar, şam və qəlyanaltı) yaz.
    İstifadəçi məlumatları:
    - Yaş: {current_user.age}
    - Cins: {current_user.gender}
    - Çəki: {current_user.weight_kg} kq
    - Boy: {current_user.height_cm} sm
    - Gündəlik Kalori Ehtiyacı: təxminən {daily_calories} kcal
    - Əsas Məqsəd: {goal}

    Hər yemək üzrə kalori miqdarını, qida tərkibini qeyd et və səlis Azərbaycan dilində yaz.
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="Sən peşəkar diyetoloqsan. Həmişə səlis və aydın Azərbaycan dilində cavab ver."
            )
        )
        meal_plan = response.text
    except Exception as e:
        meal_plan = f"Xəta baş verdi: {str(e)}"

    return {"meal_plan": meal_plan, "target_calories": daily_calories}
