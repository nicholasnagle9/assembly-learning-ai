import os
import json
import random
import secrets
import mysql.connector
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import Optional, Set, List, Dict
import traceback

# --- Configuration & Initialization ---
load_dotenv()
app = FastAPI()

# --- FIX: More permissive CORS for debugging ---
# This allows requests from your specific local domain AND a wildcard for testing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://ai-tutor.local", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    request_options = {"timeout": 120}
    generative_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Google AI: {e}")
    generative_model = None

# --- Pydantic Models ---
class ChatRequest(BaseModel):
    message: str
    access_code: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    access_code: str

# --- Database & User Helpers ---
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

def generate_access_code() -> str:
    adjectives = ['wise', 'happy', 'clever', 'brave', 'shiny', 'calm', 'eager', 'quick', 'deep', 'vast']
    nouns = ['fox', 'river', 'stone', 'star', 'moon', 'tree', 'lion', 'ocean', 'sky', 'peak']
    return f"{random.choice(adjectives)}-{random.choice(nouns)}-{secrets.randbelow(100)}"

def get_or_create_user(cursor, access_code: Optional[str]) -> Dict:
    print(f"Attempting to find user with access_code: {access_code}")
    if access_code:
        cursor.execute("SELECT * FROM Users WHERE access_code = %s", (access_code,))
        user_record = cursor.fetchone()
        if user_record:
            print(f"Found user_id: {user_record['user_id']}")
            return user_record

    print("No user found or no code provided. Creating new user.")
    while True:
        new_code = generate_access_code()
        cursor.execute("SELECT user_id FROM Users WHERE access_code = %s", (new_code,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO Users (access_code) VALUES (%s)", (new_code,))
            user_id = cursor.lastrowid
            cursor.execute("SELECT * FROM Users WHERE user_id = %s", (user_id,))
            new_user_record = cursor.fetchone()
            print(f"Created new user_id: {new_user_record['user_id']} with code: {new_user_record['access_code']}")
            return new_user_record

# --- Knowledge Graph Helpers ---
def get_all_skills(cursor) -> List[Dict]:
    cursor.execute("SELECT skill_id, skill_name FROM Skills")
    return cursor.fetchall()

def get_mastered_skills(cursor, user_id: int) -> Set[int]:
    cursor.execute("SELECT skill_id FROM User_Skills WHERE user_id = %s", (user_id,))
    return {row['skill_id'] for row in cursor.fetchall()}

def get_all_prerequisites_recursive(cursor, skill_id: int) -> Set[int]:
    visited, to_visit = set(), {skill_id}
    all_prereqs = set()
    while to_visit:
        current_id = to_visit.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
        cursor.execute(query, (current_id,))
        prereqs = {row['prerequisite_id'] for row in cursor.fetchall()}
        all_prereqs.update(prereqs)
        to_visit.update(prereqs)
    return all_prereqs

def mark_skill_as_mastered(cursor, user_id: int, skill_id: int):
    cursor.execute(
        "INSERT IGNORE INTO User_Skills (user_id, skill_id) VALUES (%s, %s)",
        (user_id, skill_id)
    )

# --- AI Interaction Helpers ---
def ask_ai(prompt: str) -> str:
    if not generative_model: return "AI model not configured."
    try:
        response = generative_model.generate_content(prompt, request_options=request_options)
        return response.text.strip()
    except Exception as e:
        print(f"AI generation error: {e}")
        return "Sorry, I had trouble thinking of a response."

def evaluate_answer_with_ai(question: str, user_answer: str) -> Dict:
    prompt = f"""A user was asked: "{question}". They responded: "{user_answer}". Is this response correct? Respond ONLY with a single, minified JSON object: {{"is_correct": boolean, "feedback": "A short, one-sentence piece of feedback."}}"""
    response_text = ask_ai(prompt)
    try:
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        return json.loads(response_text)
    except (json.JSONDecodeError, IndexError):
        print(f"Failed to parse AI JSON response: {response_text}")
        return {"is_correct": False, "feedback": "I'm having trouble evaluating that answer, let's try moving on."}

# --- Main Chat Endpoint ---
@app.post("/chat", response_model=ChatResponse)
async def chat_handler(req: ChatRequest):
    db = get_db_connection()
    if not db: raise HTTPException(status_code=500, detail="Database connection failed.")
    cursor = db.cursor(dictionary=True)
    
    try:
        user_record = get_or_create_user(cursor, req.access_code)
        db.commit()
        access_code = user_record['access_code']
        user_id = user_record['user_id']
        
        session_json = user_record.get('session_state')
        session = json.loads(session_json) if session_json else {"phase": "Awaiting_Goal"}
        
        user_message = req.message
        ai_response = "I'm not sure how to respond."

        print(f"\n--- NEW REQUEST ---")
        print(f"Access Code: {access_code}, User ID: {user_id}")
        print(f"Incoming Message: '{user_message}'")
        print(f"Session BEFORE processing: {session}")

        if user_message == "##INITIALIZE##":
            if session.get("phase") == "Awaiting_Goal":
                ai_response = "Hello! I'm your personal AI Tutor. What would you like to learn today?"
            else:
                last_reply = session.get("last_ai_reply", "Welcome back! Let's continue where you left off.")
                ai_response = f"[Resuming Session]\n\n{last_reply}"
            session['last_ai_reply'] = ai_response
            
            cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id))
            db.commit()
            return ChatResponse(reply=ai_response, access_code=access_code)

        while True:
            phase = session.get("phase", "Awaiting_Goal")
            continue_loop = False 

            if phase == "Awaiting_Goal":
                all_skills = get_all_skills(cursor)
                skill_list_str = "\n".join([f"- ID: {s['skill_id']}, Name: {s['skill_name']}" for s in all_skills])
                prompt = f"""A user wants to learn: "{user_message}". Which skill is the best match? Respond ONLY with the numeric skill_id.\n\nSkills:\n{skill_list_str}"""
                target_id_str = ask_ai(prompt).strip()
                try:
                    target_id = int(target_id_str)
                    cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (target_id,))
                    skill_record = cursor.fetchone()
                    if skill_record:
                        session.update({
                            "target_skill_id": target_id,
                            "target_skill_name": skill_record['skill_name'],
                            "phase": "Confirming_Goal"
                        })
                        ai_response = f"Got it. It sounds like your main goal is to learn **{skill_record['skill_name']}**. Is that right? (yes/no)"
                    else:
                        ai_response = "I couldn't find that exact skill. Could you please rephrase?"
                except (ValueError, TypeError):
                    ai_response = "I'm having trouble understanding that goal. Could you be more specific?"

            elif phase == "Confirming_Goal":
                if "yes" in user_message.lower():
                    target_skill_id = session['target_skill_id']
                    mastered_skills = get_mastered_skills(cursor, user_id)
                    
                    if target_skill_id in mastered_skills:
                        ai_response = "Looks like you've already mastered that! What would you like to learn next?"
                        session = {"phase": "Awaiting_Goal"}
                    else:
                        prereqs = get_all_prerequisites_recursive(cursor, target_skill_id)
                        unmastered_prereqs = sorted(list(prereqs - mastered_skills))
                        full_path_ids = unmastered_prereqs + ([target_skill_id] if target_skill_id not in unmastered_prereqs else [])

                        if not full_path_ids:
                             ai_response = "It seems you know all the prerequisites for this topic already. Let's start with your main goal!"
                             full_path_ids = [target_skill_id]

                        cursor.execute(f"SELECT skill_id, skill_name FROM Skills WHERE skill_id IN ({','.join(map(str, full_path_ids)) or 'NULL'})")
                        path_skills = {s['skill_id']: s['skill_name'] for s in cursor.fetchall()}
                        
                        learning_path = [path_skills.get(sid) for sid in full_path_ids if path_skills.get(sid)]
                        path_str = "\n".join([f"{i+1}. {name}" for i, name in enumerate(learning_path)])
                        ai_response = f"Great! Here is the personalized learning path I've built for you:\n\n{path_str}\n\nPress enter or say 'ok' to start with **{learning_path[0]}**."
                        session.update({
                            "learning_plan": full_path_ids,
                            "current_skill_index": 0,
                            "phase": "Crawl"
                        })
                else:
                    ai_response = "My mistake. Please tell me what you'd like to learn, and I'll try again."
                    session['phase'] = 'Awaiting_Goal'

            elif phase == "Crawl":
                plan = session.get('learning_plan', [])
                index = session.get('current_skill_index', 0)
                if index < len(plan):
                    current_skill_id = plan[index]
                    cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (current_skill_id,))
                    skill_record = cursor.fetchone()
                    
                    session['current_skill_record'] = skill_record
                    ai_response = skill_record.get('crawl_prompt', 'Let''s begin.')
                    session['phase'] = 'Walk_Ask'
                else:
                    ai_response = "You've finished your learning plan! What's next?"
                    session = {"phase": "Awaiting_Goal"}

            elif phase == "Walk_Ask":
                skill_record = session['current_skill_record']
                question = skill_record.get('walk_prompt')
                if question:
                    ai_response = question
                    session['last_question'] = question
                    session['phase'] = 'Walk_Evaluate'
                else:
                    session['phase'] = 'Run_Ask'
                    continue_loop = True

            elif phase == "Walk_Evaluate":
                evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
                ai_response = evaluation.get('feedback')
                if evaluation.get('is_correct'):
                    session['phase'] = 'Run_Ask'
                    continue_loop = True
                else:
                    session['phase'] = 'Crawl'

            elif phase == "Run_Ask":
                skill_record = session['current_skill_record']
                question = skill_record.get('run_prompt')
                if question:
                    ai_response = question
                    session['last_question'] = question
                    session['phase'] = 'Run_Evaluate'
                else:
                    ai_response = "This topic doesn't have a final question, so we'll mark it as complete."
                    session['phase'] = 'Summary'
                    continue_loop = True

            elif phase == "Run_Evaluate":
                evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
                ai_response = evaluation.get('feedback', 'Got it.')
                if evaluation.get('is_correct'):
                    session['phase'] = 'Summary'
                    continue_loop = True
                else:
                    ai_response += "\n\nLet's review this concept one more time before we move on."
                    session['phase'] = 'Crawl'

            elif phase == "Summary":
                skill_record = session['current_skill_record']
                mark_skill_as_mastered(cursor, user_id, skill_record['skill_id'])
                
                ai_response = f"Excellent! You've mastered **{skill_record['skill_name']}**."
                session['current_skill_index'] += 1
                
                plan = session.get('learning_plan', [])
                index = session.get('current_skill_index', 0)
                if index < len(plan):
                    next_skill_id = plan[index]
                    cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                    next_skill_record = cursor.fetchone()
                    ai_response += f"\n\nNext up: **{next_skill_record['skill_name']}**. Ready?"
                    session['phase'] = 'Crawl'
                else:
                    ai_response += "\n\nCongratulations, you've completed your entire learning plan! What would you like to learn next?"
                    session = {"phase": "Awaiting_Goal"}
            
            if not continue_loop:
                break
        
        session['last_ai_reply'] = ai_response
        cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id))
        db.commit()
        print(f"Session AFTER processing: {session}")
        print(f"Final AI Response: '{ai_response[:100]}...'")
        print(f"--- END REQUEST ---\n")
        return ChatResponse(reply=ai_response, access_code=access_code)

    except Exception as e:
        print(f"--- ERROR IN HANDLER ---")
        traceback.print_exc()
        print(f"--- END ERROR ---")
        raise HTTPException(status_code=500, detail=f"An internal error occurred: {e}")
    finally:
        if db and db.is_connected():
            cursor.close()
            db.close()
