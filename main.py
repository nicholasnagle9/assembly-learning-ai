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

# --- System-Wide Persona Prompt ---
# This is the core instruction given to the AI in almost every prompt.
SYSTEM_PERSONA_PROMPT = """
You are "Asmby," an expert, encouraging, and friendly math tutor.
Your entire focus is on teaching mathematics.
You must NEVER discuss or ask questions about computer programming, science, history, or other non-math subjects unless it's a direct, real-world analogy to explain a math concept.
You should always be patient and guide the user. When a user is vague, your goal is to help them narrow down their interest by asking clarifying questions or providing relevant suggestions.
"""

# --- Configuration & Initialization ---
load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://ai-tutor.local", "*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    request_options = {"timeout": 120}
    generative_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Google AI: {e}")
    generative_model = None

# --- Pydantic Models & DB Helpers ---
class ChatRequest(BaseModel): message: str; access_code: Optional[str] = None
class ChatResponse(BaseModel): reply: str; access_code: str

def get_db_connection():
    try:
        conn = mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))
        return conn
    except mysql.connector.Error as e:
        print(f"DB Connection Error: {e}"); return None

def get_or_create_user(cursor, access_code: Optional[str]) -> Dict:
    if access_code:
        cursor.execute("SELECT * FROM Users WHERE access_code = %s", (access_code,))
        user_record = cursor.fetchone()
        if user_record: return user_record
    while True:
        new_code = f"{random.choice(['wise', 'happy', 'clever', 'brave', 'shiny'])}-{random.choice(['fox', 'river', 'stone', 'star', 'moon'])}-{secrets.randbelow(100)}"
        cursor.execute("SELECT user_id FROM Users WHERE access_code = %s", (new_code,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO Users (access_code) VALUES (%s)", (new_code,)); user_id = cursor.lastrowid
            cursor.execute("SELECT * FROM Users WHERE user_id = %s", (user_id,)); return cursor.fetchone()

# --- Knowledge Graph & AI Helpers ---
def get_all_skills_by_subject(cursor):
    skills_by_subject = {}
    cursor.execute("SELECT skill_id, skill_name, subject FROM Skills ORDER BY subject, skill_id")
    for row in cursor.fetchall():
        subject = row.get('subject', 'General')
        if subject not in skills_by_subject: skills_by_subject[subject] = []
        skills_by_subject[subject].append({"id": row['skill_id'], "name": row['skill_name']})
    return skills_by_subject

def get_mastered_skills(cursor, user_id):
    cursor.execute("SELECT skill_id FROM User_Skills WHERE user_id = %s", (user_id,)); return {row['skill_id'] for row in cursor.fetchall()}

def get_direct_prerequisites(cursor, skill_id: int) -> List[Dict]:
    query = """
        SELECT p.prerequisite_id, s.skill_name 
        FROM Prerequisites p
        JOIN Skills s ON p.prerequisite_id = s.skill_id
        WHERE p.skill_id = %s
    """
    cursor.execute(query, (skill_id,))
    return cursor.fetchall()

def mark_skill_as_mastered(cursor, user_id, skill_id):
    cursor.execute("INSERT IGNORE INTO User_Skills (user_id, skill_id) VALUES (%s, %s)", (user_id, skill_id))

def ask_ai(prompt):
    # Prepend the system persona to every single AI call
    full_prompt = f"{SYSTEM_PERSONA_PROMPT}\n\n--- TASK ---\n\n{prompt}"
    if not generative_model: return "AI model not configured."
    try: return generative_model.generate_content(full_prompt, request_options=request_options).text.strip()
    except Exception as e: print(f"AI Error: {e}"); return "Sorry, I had trouble thinking."

def evaluate_answer_with_ai(question, user_answer):
    prompt = f'A user was asked: "{question}". They responded: "{user_answer}". Is this correct? Respond ONLY with JSON: {{"is_correct": boolean, "feedback": "A short, one-sentence piece of feedback."}}'
    response_text = ask_ai(prompt)
    try:
        if "```json" in response_text: response_text = response_text.split("```json")[1].split("```")[0].strip()
        return json.loads(response_text)
    except (json.JSONDecodeError, IndexError): return {"is_correct": False, "feedback": "I had trouble evaluating that."}

# --- Main Chat Endpoint ---
@app.post("/chat", response_model=ChatResponse)
async def chat_handler(req: ChatRequest):
    db = get_db_connection()
    if not db: raise HTTPException(status_code=500, detail="Database connection failed.")
    cursor = db.cursor(dictionary=True)
    
    try:
        user_record = get_or_create_user(cursor, req.access_code); db.commit()
        access_code, user_id = user_record['access_code'], user_record['user_id']
        session = json.loads(user_record.get('session_state') or '{}') or {"phase": "Awaiting_Goal"}
        user_message, ai_response = req.message, "I'm not sure how to respond."

        if user_message == "##INITIALIZE##":
            ai_response = "Hello! I'm your personal AI Tutor, Asmby. What would you like to learn today?" if session.get("phase") == "Awaiting_Goal" else f"[Resuming Session]\n\n{session.get('last_ai_reply', 'Welcome back!')}"
        else:
            continue_loop = True
            while continue_loop:
                phase = session.get("phase", "Awaiting_Goal")
                continue_loop = False 

                if phase == "Awaiting_Goal":
                    all_skills = get_all_skills_by_subject(cursor)
                    prompt = f"""A user wants to learn about: "{user_message}". Determine the single most advanced, relevant skill ID from the list below they should start with.
                    Available skills: {json.dumps(all_skills, indent=2)}
                    Respond ONLY with the numeric skill ID."""
                    target_id_str = ask_ai(prompt).strip()
                    try:
                        target_id = int(target_id_str)
                        cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (target_id,))
                        skill_record = cursor.fetchone()
                        if skill_record:
                            session.update({
                                "original_goal_id": target_id,
                                "current_skill_record": skill_record,
                                "phase": "Dive_In_Crawl"
                            })
                            continue_loop = True # Immediately transition to the crawl phase
                        else: ai_response = "I couldn't find a skill for that. Could you be more specific?"
                    except (ValueError, TypeError): ai_response = "I'm having trouble pinpointing a topic for that. Can you be more specific?"

                elif phase == "Dive_In_Crawl":
                    skill_record = session['current_skill_record']
                    prompt = f"Explain the concept of '{skill_record['skill_name']}'. Use this core idea as a guide, but create your own unique, detailed explanation with fresh analogies and real-world examples: '{skill_record['crawl_prompt']}'"
                    explanation = ask_ai(prompt)
                    ai_response = f"{explanation}\n\n---\n**Does this initial explanation make sense, or would you prefer we back up and cover some of the foundational 'assembly' concepts first?**"
                    session['phase'] = "Awaiting_Checkpoint_Response"

                elif phase == "Awaiting_Checkpoint_Response":
                    prompt = f"A user was asked if a concept made sense or if they wanted to learn prerequisites. They responded: '{user_message}'. Is their answer affirmative (they understand) or negative (they are confused/want prerequisites)? Respond ONLY with 'affirmative' or 'negative'."
                    verdict = ask_ai(prompt).lower()
                    if "affirmative" in verdict:
                        ai_response = "Great! Let's try a practice question."
                        session['phase'] = 'Walk_Ask'
                        continue_loop = True
                    else:
                        ai_response = "No problem at all! Let's find a better starting point."
                        mastered_skills = get_mastered_skills(cursor, user_id)
                        prereqs = get_direct_prerequisites(cursor, session['current_skill_record']['skill_id'])
                        unmastered_prereqs = [p for p in prereqs if p['prerequisite_id'] not in mastered_skills]
                        
                        if unmastered_prereqs:
                            path_str = "\n".join([f"- {p['skill_name']}" for p in unmastered_prereqs])
                            ai_response += f"\n\nTo really grasp **{session['current_skill_record']['skill_name']}**, it helps to be solid on these topics. Which sounds like the best place to start?\n\n{path_str}"
                            session['unmastered_prereqs'] = unmastered_prereqs
                            session['phase'] = 'Awaiting_Prerequisite_Choice'
                        else:
                            ai_response = "It seems you already know all the prerequisites for this! Let's try a practice question to pinpoint the confusion."
                            session['phase'] = 'Walk_Ask'
                            continue_loop = True

                elif phase == "Awaiting_Prerequisite_Choice":
                    prereq_options = session.get('unmastered_prereqs', [])
                    prompt = f"""A user was given this list of topics to choose from: {json.dumps(prereq_options)}. Their response was: '{user_message}'. Which topic are they choosing? Respond ONLY with the corresponding skill_id."""
                    target_id_str = ask_ai(prompt).strip()
                    try:
                        target_id = int(target_id_str)
                        cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (target_id,))
                        skill_record = cursor.fetchone()
                        # Start a normal learning plan now
                        session['learning_plan'] = [target_id, session['original_goal_id']] # Simple plan for now
                        session['current_skill_index'] = 0
                        session['current_skill_record'] = skill_record
                        session['phase'] = "Crawl"
                        continue_loop = True
                    except (ValueError, TypeError):
                        ai_response = "Sorry, I didn't catch that. Please choose one of the topics from the list."

                elif phase == "Crawl":
                    plan = session.get('learning_plan', [session['original_goal_id']])
                    index = session.get('current_skill_index', 0)
                    if index < len(plan):
                        cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (plan[index],))
                        skill_record = cursor.fetchone()
                        session['current_skill_record'] = skill_record
                        prompt = f"Explain the concept of '{skill_record['skill_name']}'. Use this as a guide, but create your own detailed explanation: '{skill_record['crawl_prompt']}'"
                        ai_response = ask_ai(prompt)
                        session['phase'] = 'Walk_Ask'
                    else:
                        ai_response, session = "You've finished your plan! What's next?", {"phase": "Awaiting_Goal"}
                
                elif phase == "Walk_Ask":
                    skill_record = session['current_skill_record']
                    prompt = f"The user is learning about '{skill_record['skill_name']}'. Create a single, simple, guided practice question. Inspiration: '{skill_record['walk_prompt']}'"
                    question = ask_ai(prompt)
                    ai_response, session['last_question'], session['phase'] = question, question, 'Walk_Evaluate'

                elif phase == "Walk_Evaluate":
                    evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
                    ai_response = evaluation.get('feedback')
                    if evaluation.get('is_correct'): session['phase'], continue_loop = 'Run_Ask', True
                    else: ai_response += "\n\nLet's try breaking that down again."; session['phase'] = 'Crawl'

                elif phase == "Run_Ask":
                    skill_record = session['current_skill_record']
                    prompt = f"The user needs to be tested on '{skill_record['skill_name']}'. Create a single, direct assessment question. Inspiration: '{skill_record['run_prompt']}'"
                    question = ask_ai(prompt)
                    ai_response, session['last_question'], session['phase'] = question, question, 'Run_Evaluate'

                elif phase == "Run_Evaluate":
                    evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
                    ai_response = evaluation.get('feedback', 'Got it.')
                    if evaluation.get('is_correct'): session['phase'], continue_loop = 'Summary', True
                    else: ai_response += "\n\nLet's review this concept one more time."; session['phase'] = 'Crawl'

                elif phase == "Summary":
                    skill_record = session['current_skill_record']
                    mark_skill_as_mastered(cursor, user_id, skill_record['skill_id'])
                    ai_response = f"Excellent! You've mastered **{skill_record['skill_name']}**."
                    session['current_skill_index'] = session.get('current_skill_index', -1) + 1
                    plan, index = session.get('learning_plan', []), session.get('current_skill_index', 0)
                    if index < len(plan):
                        session['phase'] = 'Crawl'; continue_loop = True 
                    else:
                        ai_response += "\n\nCongratulations, you've completed your entire learning plan! What would you like to learn next?"
                        session = {"phase": "Awaiting_Goal"}
        
        # Final Step: Commit session and return
        session['last_ai_reply'] = ai_response
        cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id))
        db.commit()
        return ChatResponse(reply=ai_response, access_code=access_code)

    except Exception as e:
        print(f"--- ERROR IN HANDLER ---\n{traceback.format_exc()}--- END ERROR ---")
        raise HTTPException(status_code=500, detail=f"An internal error occurred.")
    finally:
        if db and db.is_connected(): db.close()
