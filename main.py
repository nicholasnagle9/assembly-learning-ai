# main.py (Full MVP Logic)

import os
import mysql.connector
import google.generativeai as genai
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# --- Configuration & Initialization ---
load_dotenv()
app = FastAPI()

# Configure the Google AI client
try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    generative_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Google AI: {e}")
    generative_model = None

# --- In-Memory State Management (for this MVP) ---
# This dictionary will hold the current learning state for each user.
# In a production app, you might use a faster cache like Redis.
# Format: { "user_id": {"current_skill_id": 1, "skill_name": "...", "phase": "Crawl"} }
user_session_state = {}


# --- Database Connection Function ---
def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME")
        )
        return conn
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        return None

# --- Helper Function: Get User's Mastered Skills ---
def get_mastered_skills(cursor, user_id):
    query = "SELECT skill_id FROM User_Skills WHERE user_id = %s"
    cursor.execute(query, (user_id,))
    mastered_ids = set(map(lambda row: row['skill_id'], cursor.fetchall()))
    print(f"User {user_id} has mastered skill IDs: {mastered_ids}")
    return mastered_ids

# --- Helper Function: The Assembly Logic ---
def find_next_skill(cursor, mastered_skills, goal_skill_id):
    if goal_skill_id in mastered_skills:
        return None
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (goal_skill_id,))
    prerequisites = cursor.fetchall()
    if not prerequisites:
        return goal_skill_id
    for prereq in prerequisites:
        skill_to_learn = find_next_skill(cursor, mastered_skills, prereq['prerequisite_id'])
        if skill_to_learn is not None:
            return skill_to_learn
    return goal_skill_id

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Model ---
class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- Main Chat Endpoint (The Full Brain) ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    user_id = chat_request.user_id
    user_message = chat_request.message
    ai_response = "An unexpected error occurred."

    db_connection = get_db_connection()
    if not db_connection:
        return {"reply": "Error: Could not connect to the database."}

    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # --- Check if user exists, if not, create them ---
        cursor.execute("SELECT user_id FROM Users WHERE user_id = %s", (user_id,))
        if not cursor.fetchone():
            # For this MVP, we'll use the user_id as username and a placeholder email/password
            cursor.execute("INSERT INTO Users (user_id, username, email, password_hash) VALUES (%s, %s, %s, %s)",
                           (user_id, user_id, f"{user_id}@example.com", "placeholder"))
            db_connection.commit()
            print(f"Created new user: {user_id}")

        # --- State Machine Logic ---
        # 1. Check if the user is already in the middle of a lesson
        if user_id not in user_session_state:
            # If not, find their next skill
            mastered_skills = get_mastered_skills(cursor, user_id)
            goal_skill_id = 7 # Hardcoded goal: "Solving Two-Step Equations"
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)

            if next_skill_id:
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                skill_record = cursor.fetchone()
                # Start a new learning session for this user
                user_session_state[user_id] = {
                    "current_skill_id": next_skill_id,
                    "skill_name": skill_record['skill_name'],
                    "phase": "Crawl"
                }
                print(f"Starting new session for {user_id}: Learning '{skill_record['skill_name']}'")
            else:
                return {"reply": "Congratulations! You have mastered the entire Algebra path!"}

        # 2. Handle the current phase of the lesson
        current_session = user_session_state[user_id]
        skill_name = current_session['skill_name']
        phase = current_session['phase']

        # --- Dynamic Prompt Generation & AI Call ---
        system_prompt = ""
        if phase == "Crawl":
            system_prompt = f"You are a teacher. Your task is to EXPLAIN the concept of '{skill_name}'. Keep it simple and clear. End by asking if the user understands."
            # After explanation, we move to the next phase
            user_session_state[user_id]['phase'] = 'Walk'

        elif phase == "Walk":
            system_prompt = f"You are a friendly tutor. The user has just learned the definition of '{skill_name}'. Your task is to GUIDE them through a simple, interactive example of it. Ask leading questions."
            # For simplicity, we'll assume one guided practice is enough.
            user_session_state[user_id]['phase'] = 'Run'

        elif phase == "Run":
            system_prompt = f"You are an examiner. Assess if the user has mastered '{skill_name}'. Ask them a direct question or problem to solve. Then, analyze their answer. If their answer is correct, FINISH your response with the single word: CORRECT. Otherwise, do not use that word."
            
            # This is where the AI call happens
            if generative_model:
                full_prompt = f"{system_prompt}\n\nUSER'S ANSWER: {user_message}"
                response = generative_model.generate_content(full_prompt)
                ai_response = response.text
                
                # Check if the AI thinks the user was correct
                if "CORRECT" in ai_response.upper():
                    # MASTERED! Save to database.
                    skill_id = current_session['current_skill_id']
                    cursor.execute("INSERT INTO User_Skills (user_id, skill_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE skill_id=skill_id", (user_id, skill_id))
                    db_connection.commit()
                    print(f"User {user_id} mastered skill ID: {skill_id}")
                    # End the session
                    del user_session_state[user_id]
                    # Add a concluding remark
                    ai_response += "\n\nGreat job! You've mastered this topic."
                return {"reply": ai_response}

            else: # Fallback if AI is not configured
                 return {"reply": "AI model not configured."}

        # For Crawl and Walk phases, we just need to send the teaching prompt
        if generative_model:
            full_prompt = f"{system_prompt}\n\nUSER'S PREVIOUS MESSAGE FOR CONTEXT: {user_message}"
            response = generative_model.generate_content(full_prompt)
            ai_response = response.text
        else:
            ai_response = "AI model not configured."

    except Exception as e:
        ai_response = f"An error occurred: {e}"
        print(e)
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

    return {"reply": ai_response}

@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}