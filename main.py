# main.py (Upgraded with API Timeouts and better logging)

import os
import json
import mysql.connector
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions # <--- NEW: Import for specific exceptions
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# --- Configuration & Initialization ---
load_dotenv()
app = FastAPI()

try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    # NEW: Configure the API call with a timeout of 60 seconds
    request_options = {"timeout": 60} 
    generative_model = genai.GenerativeModel(
        'gemini-1.5-flash',
        request_options=request_options
    )
except Exception as e:
    print(f"Error configuring Google AI: {e}")
    generative_model = None

# ... (All other helper functions like get_db_connection, get_mastered_skills, find_next_skill remain the same) ...
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

def get_mastered_skills(cursor, numeric_user_id):
    query = "SELECT skill_id FROM User_Skills WHERE user_id = %s"
    cursor.execute(query, (numeric_user_id,))
    mastered_ids = set(map(lambda row: row['skill_id'], cursor.fetchall()))
    print(f"User ID {numeric_user_id} has mastered skill IDs: {mastered_ids}")
    return mastered_ids

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
# --- End of helper functions ---


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    message: str
    user_id: str

@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    username = chat_request.user_id
    user_message = chat_request.message
    ai_response = "An unexpected error occurred."

    db_connection = get_db_connection()
    if not db_connection:
        return {"reply": "Error: Could not connect to the database."}

    try:
        cursor = db_connection.cursor(dictionary=True)
        
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (username,))
        user_record = cursor.fetchone()
        
        if not user_record:
            cursor.execute("INSERT INTO Users (username, email, password_hash) VALUES (%s, %s, %s)",
                           (username, f"{username}@example.com", "placeholder"))
            db_connection.commit()
            numeric_user_id = cursor.lastrowid
        else:
            numeric_user_id = user_record['user_id']

        if username not in user_session_state:
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            goal_skill_id = 7
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)

            if next_skill_id:
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                skill_record = cursor.fetchone()
                user_session_state[username] = {
                    "current_skill_id": next_skill_id,
                    "numeric_user_id": numeric_user_id,
                    "skill_name": skill_record['skill_name'],
                    "phase": "Crawl"
                }
            else:
                return {"reply": "### Congratulations!\n\nYou have mastered the entire Algebra path! Great work."}

        current_session = user_session_state[username]
        skill_name = current_session['skill_name']
        phase = current_session['phase']

        # --- Dynamic Prompt Generation ---
        system_prompt = ""
        # ... (Crawl, Walk, Run, Summary prompt definitions are the same) ...
        if phase == "Crawl":
            system_prompt = f"You are a teacher. Your task is to EXPLAIN the concept of '{skill_name}'. Use Markdown for formatting: use bolding for key terms, use lists for steps, and wrap all mathematical examples in code blocks. Keep it simple and clear. End by asking if the user understands."
            user_session_state[username]['phase'] = 'Walk'

        elif phase == "Walk":
            system_prompt = f"You are a friendly tutor. The user has learned the definition of '{skill_name}'. Your task is to GUIDE them through a simple, interactive example of it. Use Markdown for formatting and wrap all math in code blocks. Ask leading questions."
            user_session_state[username]['phase'] = 'Run'

        elif phase == "Run":
            system_prompt = f"You are an examiner. Your task is to assess if the user has mastered '{skill_name}'. Give them a direct question or problem to solve. Then, analyze their answer. Respond ONLY with a single, minified JSON object in the format: {{\"is_correct\": boolean, \"feedback\": \"Your short, encouraging feedback here.\"}}. For example: {{\"is_correct\":true,\"feedback\":\"Perfect! That's exactly right.\"}} or {{\"is_correct\":false,\"feedback\":\"Not quite. Remember to distribute the negative sign.\"}}"
            
            print("--- Entering RUN phase ---")
            full_prompt = f"{system_prompt}\n\nHere is the user's answer to your previous question: '{user_message}'"
            
            try:
                print("Attempting to call Google AI API...")
                response = generative_model.generate_content(full_prompt)
                response_text = response.text
                print(f"Received from AI: {response_text}")

                assessment = json.loads(response_text)
                ai_response = assessment.get("feedback", "I had trouble parsing the assessment.")
                
                if assessment.get("is_correct") == True:
                    skill_id = current_session['current_skill_id']
                    user_id = current_session['numeric_user_id']
                    cursor.execute("INSERT INTO User_Skills (user_id, skill_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE skill_id=skill_id", (user_id, skill_id))
                    db_connection.commit()
                    print(f"User ID {user_id} mastered skill ID: {skill_id}")
                    user_session_state[username]['phase'] = 'Summary'
                    ai_response += f"\n\n**You've mastered: {skill_name}!**"
            
            # --- NEW: Catch specific API errors ---
            except (google_exceptions.DeadlineExceeded, google_exceptions.ServiceUnavailable) as e:
                print(f"API Timeout or Service Unavailable: {e}")
                ai_response = "Sorry, I'm having trouble connecting to my core brain right now. The service may be temporarily unavailable. Please try again in a moment."
            except json.JSONDecodeError:
                print(f"AI did not return valid JSON. Response: {response_text}")
                ai_response = "I'm having a little trouble evaluating that response. Let's try that again."
            
            return {"reply": ai_response}

        elif phase == "Summary":
             mastered_skills = get_mastered_skills(cursor, numeric_user_id)
             goal_skill_id = 7
             next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
             if next_skill_id:
                 cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                 next_skill_record = cursor.fetchone()
                 system_prompt = f"The user has just mastered '{skill_name}'. Briefly congratulate them and introduce the next topic, which is '{next_skill_record['skill_name']}'. Explain why it's the next logical step. End by asking if they are ready to continue."
             else:
                 system_prompt = "The user has just mastered the final skill. Congratulate them on completing the entire learning path."
             del user_session_state[username]

        # --- AI Call for Crawl, Walk, Summary Phases ---
        try:
            print(f"--- Calling AI for {phase} phase ---")
            response = generative_model.generate_content(system_prompt)
            ai_response = response.text
        except (google_exceptions.DeadlineExceeded, google_exceptions.ServiceUnavailable) as e:
            print(f"API Timeout or Service Unavailable: {e}")
            ai_response = "Sorry, I'm having trouble connecting to my core brain right now. Please try again in a moment."
        
    except Exception as e:
        ai_response = f"A critical error occurred: {e}"
        print(e)
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

    return {"reply": ai_response}

@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}