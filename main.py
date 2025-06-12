import os
import json 
import mysql.connector
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# --- Configuration & Initialization ---
load_dotenv()
app = FastAPI()

try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    request_options = {"timeout": 300} 
    generative_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Google AI: {e}")
    generative_model = None

# --- In-Memory State Management ---
# Format: { "username": {"numeric_user_id": 1, "current_skill_id": 1, "skill_name": "...", "phase": "Crawl", "last_question": "..."} }
user_session_state = {}

# --- Database & Helper Functions (no changes) ---
def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        return conn
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        return None

def get_mastered_skills(cursor, numeric_user_id):
    query = "SELECT skill_id FROM User_Skills WHERE user_id = %s"
    cursor.execute(query, (numeric_user_id,))
    return set(map(lambda row: row['skill_id'], cursor.fetchall()))

def find_next_skill(cursor, mastered_skills, goal_skill_id):
    if goal_skill_id in mastered_skills: return None
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (goal_skill_id,))
    prerequisites = cursor.fetchall()
    if not prerequisites: return goal_skill_id
    for prereq in prerequisites:
        skill_to_learn = find_next_skill(cursor, mastered_skills, prereq['prerequisite_id'])
        if skill_to_learn is not None: return skill_to_learn
    return goal_skill_id

# --- FastAPI App & Middleware ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- Main Chat Endpoint (The Upgraded Brain) ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    username = chat_request.user_id
    user_message = chat_request.message
    ai_response = "An unexpected error occurred."
    db_connection = get_db_connection()
    if not db_connection: return {"reply": "Error: Could not connect to the database."}

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

        # --- State Machine Logic ---
        if username not in user_session_state:
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            goal_skill_id = 7
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
            if next_skill_id:
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                skill_record = cursor.fetchone()
                user_session_state[username] = {
                    "numeric_user_id": numeric_user_id, "current_skill_id": next_skill_id,
                    "skill_name": skill_record['skill_name'], "phase": "Crawl", "last_question": None
                }
            else:
                return {"reply": "### Congratulations!\n\nYou have mastered the entire Algebra path!"}

        current_session = user_session_state[username]
        skill_name = current_session['skill_name']
        phase = current_session['phase']
        last_question = current_session.get('last_question')

        # --- Dynamic Prompt Generation & AI Call ---
        system_prompt = ""
        # The AI's only job is to EXPLAIN
        if phase == "Crawl":
            system_prompt = f"You are a teacher. Your ONLY task is to EXPLAIN the concept of '{skill_name}'. Use Markdown: use bolding for key terms, lists, and wrap math in `code blocks`. Keep it simple. End by asking if the user understands."
            user_session_state[username]['phase'] = 'Walk_Ask'
        # The AI's only job is to ASK a guided question
        elif phase == "Walk_Ask":
            system_prompt = f"You are a friendly tutor. Your ONLY task is to ask a single, simple, leading question to help the user begin practicing '{skill_name}'. Do not solve it for them."
            user_session_state[username]['phase'] = 'Walk_Evaluate'
        # The AI's only job is to EVALUATE the user's answer to the guided question
        elif phase == "Walk_Evaluate":
            system_prompt = f"A user was asked: '{last_question}'. They responded: '{user_message}'. Is this a correct and logical step forward? Respond ONLY with a JSON object: {{\"is_correct\": boolean, \"feedback\": \"A short, one-sentence piece of feedback.\"}}"
            # If they get it right, we move to the real test. If not, we try another guided question.
            user_session_state[username]['phase'] = 'Run_Ask' # Assume correct for now, can add logic later
        # The AI's only job is to ASK an assessment question
        elif phase == "Run_Ask":
            system_prompt = f"You are an examiner. Your ONLY task is to ask one, direct assessment question to test mastery of '{skill_name}'. Do not include the answer."
            user_session_state[username]['phase'] = 'Run_Evaluate'
        # The AI's only job is to EVALUATE the assessment question
        elif phase == "Run_Evaluate":
            system_prompt = f"You are an AI grader. A student was asked the question: '{last_question}'. The student responded: '{user_message}'. First, in a <thinking> block, reason step-by-step if the answer is correct and complete. Second, based on your reasoning, respond ONLY with a single, minified JSON object in the format: {{\"is_correct\": boolean, \"feedback\": \"Your short, encouraging feedback here.\"}}"
        # The AI's only job is to SUMMARIZE and introduce the next topic
        elif phase == "Summary":
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            goal_skill_id = 7
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
            if next_skill_id:
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                next_skill_record = cursor.fetchone()
                system_prompt = f"The user just mastered '{skill_name}'. Briefly congratulate them and introduce the next topic: '{next_skill_record['skill_name']}'. Explain why it's the next logical step. End by asking if they are ready."
            else:
                system_prompt = "The user has just mastered the final skill. Congratulate them on completing the entire learning path."
            del user_session_state[username]

        # --- AI Call and State Handling ---
        try:
            print(f"--- Calling AI for {phase} phase ---")
            response = generative_model.generate_content(system_prompt, request_options=request_options)
            response_text = response.text
            print(f"--- AI Response: ---\n{response_text}\n--------------------")

            if phase.endswith("_Evaluate"):
                start_index = response_text.find('{')
                end_index = response_text.rfind('}')
                if start_index != -1 and end_index != -1:
                    json_string = response_text[start_index:end_index+1]
                    assessment = json.loads(json_string)
                    ai_response = assessment.get("feedback", "Evaluation error.")
                    if assessment.get("is_correct"):
                        if phase == "Run_Evaluate": # Mastered!
                            skill_id = current_session['current_skill_id']
                            user_id = current_session['numeric_user_id']
                            cursor.execute("INSERT INTO User_Skills (user_id, skill_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE skill_id=skill_id", (user_id, skill_id))
                            db_connection.commit()
                            user_session_state[username]['phase'] = 'Summary'
                            ai_response += f"\n\n**Excellent! You've mastered: {skill_name}!**"
                    else: # If evaluation is incorrect
                        ai_response += "\nLet's try that another way."
                        if phase == "Run_Evaluate": # Failed the test, go back to guided practice
                           user_session_state[username]['phase'] = 'Walk_Ask'
                else:
                    ai_response = "My evaluation response was malformed. Let's try again."
            else:
                ai_response = response_text
                if phase.endswith("_Ask"):
                    user_session_state[username]['last_question'] = response_text
        except Exception as e:
            ai_response = f"An API or logic error occurred: {e}"
            print(e)
            if username in user_session_state: del user_session_state[username] # Reset state on error
            
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

    return {"reply": ai_response}

@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}