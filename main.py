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
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
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
    if access_code:
        cursor.execute("SELECT * FROM Users WHERE access_code = %s", (access_code,))
        user_record = cursor.fetchone()
        if user_record: return user_record
    while True:
        new_code = generate_access_code()
        cursor.execute("SELECT user_id FROM Users WHERE access_code = %s", (new_code,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO Users (access_code) VALUES (%s)", (new_code,))
            user_id = cursor.lastrowid
            cursor.execute("SELECT * FROM Users WHERE user_id = %s", (user_id,))
            return cursor.fetchone()

# --- Knowledge Graph Helpers ---
def get_all_skills_by_subject(cursor) -> Dict[str, List[Dict]]:
    skills_by_subject = {}
    cursor.execute("SELECT skill_id, skill_name, subject FROM Skills ORDER BY subject, skill_id")
    for row in cursor.fetchall():
        subject = row.get('subject', 'General')
        if subject not in skills_by_subject:
            skills_by_subject[subject] = []
        skills_by_subject[subject].append({"id": row['skill_id'], "name": row['skill_name']})
    return skills_by_subject

def get_mastered_skills(cursor, user_id: int) -> Set[int]:
    cursor.execute("SELECT skill_id FROM User_Skills WHERE user_id = %s", (user_id,))
    return {row['skill_id'] for row in cursor.fetchall()}

def get_all_prerequisites_recursive(cursor, skill_id: int) -> Set[int]:
    visited, to_visit, all_prereqs = set(), {skill_id}, set()
    while to_visit:
        current_id = to_visit.pop()
        if current_id in visited: continue
        visited.add(current_id)
        cursor.execute("SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s", (current_id,))
        prereqs = {row['prerequisite_id'] for row in cursor.fetchall()}
        all_prereqs.update(prereqs)
        to_visit.update(prereqs)
    return all_prereqs

def mark_skill_as_mastered(cursor, user_id: int, skill_id: int):
    cursor.execute("INSERT IGNORE INTO User_Skills (user_id, skill_id) VALUES (%s, %s)", (user_id, skill_id))

# --- AI Interaction Helpers ---
def ask_ai(prompt: str) -> str:
    if not generative_model: return "AI model not configured."
    try:
        response = generative_model.generate_content(prompt, request_options=request_options)
        return response.text.strip()
    except Exception as e:
        print(f"AI generation error: {e}")
        return "Sorry, I had trouble thinking."

def evaluate_answer_with_ai(question: str, user_answer: str) -> Dict:
    prompt = f"""A user was asked: "{question}". They responded: "{user_answer}". Is this response correct? Respond ONLY with a single, minified JSON object: {{"is_correct": boolean, "feedback": "A short, one-sentence piece of feedback."}}"""
    response_text = ask_ai(prompt)
    try:
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        return json.loads(response_text)
    except (json.JSONDecodeError, IndexError):
        return {"is_correct": False, "feedback": "I had trouble evaluating that, let's try moving on."}

def classify_intent_with_ai(session: Dict, user_message: str) -> str:
    phase = session.get("phase", "Awaiting_Goal")
    if phase == "Confirming_Goal" or phase.endswith("_Evaluate"):
        return "Answering_Question"

    context = f"The user is in the '{phase}' phase of a lesson on '{session.get('current_skill_record', {}).get('skill_name', 'an unknown topic')}'. The last thing the AI said was: '{session.get('last_ai_reply', '...')}'"
    prompt = f"""Analyze the user's message to determine their intent, given the conversation context.
    Context: {context}
    User message: "{user_message}"
    Categorize the user's primary intent (Answering_Question, Asking_Clarification, Changing_Topic). Respond ONLY with the category name."""
    intent = ask_ai(prompt)
    valid_intents = ["Answering_Question", "Asking_Clarification", "Changing_Topic"]
    for valid in valid_intents:
        if valid in intent:
            print(f"Classified intent as: {valid}")
            return valid
    return "Answering_Question"

# --- Main Chat Endpoint ---
@app.post("/chat", response_model=ChatResponse)
async def chat_handler(req: ChatRequest):
    db = get_db_connection()
    if not db: raise HTTPException(status_code=500, detail="Database connection failed.")
    cursor = db.cursor(dictionary=True)
    
    try:
        user_record = get_or_create_user(cursor, req.access_code)
        db.commit()
        access_code, user_id = user_record['access_code'], user_record['user_id']
        session = json.loads(user_record.get('session_state') or '{}') or {"phase": "Awaiting_Goal"}
        user_message = req.message
        ai_response = "I'm not sure how to respond."

        if user_message == "##INITIALIZE##":
            ai_response = "Hello! I'm your personal AI Tutor. What would you like to learn today?" if session.get("phase") == "Awaiting_Goal" else f"[Resuming Session]\n\n{session.get('last_ai_reply', 'Welcome back!')}"
            session['last_ai_reply'] = ai_response
            cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id))
            db.commit()
            return ChatResponse(reply=ai_response, access_code=access_code)

        intent = classify_intent_with_ai(session, user_message)
        
        if intent == "Asking_Clarification":
            topic = session.get('current_skill_record', {}).get('skill_name', 'the current topic')
            prompt = f"The user is learning about '{topic}' and asked for clarification: '{user_message}'. Provide a helpful, concise explanation with real-world examples."
            ai_response = ask_ai(prompt)
        
        elif intent == "Changing_Topic":
            mastered_skills = get_mastered_skills(cursor, user_id)
            cursor.execute(f"SELECT DISTINCT s.skill_id, s.skill_name FROM Skills s JOIN Prerequisites p ON s.skill_id = p.skill_id WHERE p.prerequisite_id IN ({','.join(map(str, mastered_skills)) or 'NULL'})")
            next_logical_skills = [s for s in cursor.fetchall() if s['skill_id'] not in mastered_skills]
            
            if next_logical_skills:
                skill_list_str = "\n".join([f"- {s['skill_name']}" for s in next_logical_skills[:5]]) # Limit to 5 suggestions
                ai_response = f"Based on what you've learned, here are some good next steps:\n\n{skill_list_str}\n\nOr you can tell me something else you're interested in!"
            else:
                ai_response = "Of course. What topic would you like to learn about?"
            session = {"phase": "Awaiting_Goal"}

        elif intent == "Answering_Question":
            continue_loop = True
            while continue_loop:
                phase = session.get("phase", "Awaiting_Goal")
                continue_loop = False 

                if phase == "Awaiting_Goal":
                    all_skills_by_subject = get_all_skills_by_subject(cursor)
                    mastered_skills = get_mastered_skills(cursor, user_id)
                    prompt = f"""You are a curriculum expert. A user wants to learn about: "{user_message}".
                    Your task is to select the single best starting skill for them.
                    
                    Here are the available skills, grouped by subject:
                    {json.dumps(all_skills_by_subject, indent=2)}

                    The user has already mastered these skill IDs: {list(mastered_skills)}

                    Based on all this, determine the most logical, unmastered skill ID for the user to start with.
                    If the user mentions a broad subject, pick the first unmastered skill in that subject.
                    Respond ONLY with the numeric skill ID.
                    """
                    target_id_str = ask_ai(prompt).strip()
                    try:
                        target_id = int(target_id_str)
                        # --- FIX: Check for mastery BEFORE confirming ---
                        if target_id in mastered_skills:
                            ai_response = "It looks like you've already mastered that topic! What else would you like to explore?"
                            session = {"phase": "Awaiting_Goal"}
                        else:
                            cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (target_id,))
                            skill_record = cursor.fetchone()
                            if skill_record:
                                session.update({"target_skill_id": target_id, "target_skill_name": skill_record['skill_name'], "phase": "Confirming_Goal"})
                                ai_response = f"Got it. A great place to start would be **{skill_record['skill_name']}**. Sound good?"
                            else: ai_response = "I couldn't find a skill for that. Could you rephrase?"
                    except (ValueError, TypeError): ai_response = "I'm having trouble pinpointing a topic for that. Can you be more specific?"

                elif phase == "Confirming_Goal":
                    affirmatives = ["yes", "yep", "sure", "ok", "correct", "right", "yup", "yeah", "good"]
                    if any(word in user_message.lower() for word in affirmatives):
                        target_skill_id = session['target_skill_id']
                        mastered_skills = get_mastered_skills(cursor, user_id)
                        prereqs = get_all_prerequisites_recursive(cursor, target_skill_id)
                        full_path_ids = sorted(list(prereqs - mastered_skills)) + [target_skill_id]
                        cursor.execute(f"SELECT skill_id, skill_name FROM Skills WHERE skill_id IN ({','.join(map(str, full_path_ids)) or 'NULL'})")
                        path_skills = {s['skill_id']: s['skill_name'] for s in cursor.fetchall()}
                        learning_path = [path_skills.get(sid) for sid in full_path_ids if path_skills.get(sid)]
                        path_str = "\n".join([f"{i+1}. {name}" for i, name in enumerate(learning_path)])
                        ai_response = f"Great! Here is the learning path I've built for you:\n\n{path_str}\n\nPress enter or say 'ok' to start with **{learning_path[0]}**."
                        session.update({"learning_plan": full_path_ids, "current_skill_index": 0, "phase": "Crawl"})
                    else:
                        ai_response = "No problem. What would you like to learn instead?"
                        session['phase'] = 'Awaiting_Goal'

                elif phase == "Crawl":
                    plan, index = session.get('learning_plan', []), session.get('current_skill_index', 0)
                    if index < len(plan):
                        cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (plan[index],))
                        skill_record = cursor.fetchone()
                        session['current_skill_record'] = skill_record
                        ai_response = skill_record.get('crawl_prompt', 'Let''s begin.')
                        session['phase'] = 'Walk_Ask'
                    else:
                        ai_response = "You've finished your plan! What's next?"
                        session = {"phase": "Awaiting_Goal"}
                
                elif phase == "Walk_Ask":
                    question = session.get('current_skill_record', {}).get('walk_prompt')
                    if question:
                        ai_response, session['last_question'], session['phase'] = question, question, 'Walk_Evaluate'
                    else: session['phase'], continue_loop = 'Run_Ask', True
                
                elif phase == "Walk_Evaluate":
                    evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
                    ai_response = evaluation.get('feedback')
                    if evaluation.get('is_correct'):
                        session['phase'], continue_loop = 'Run_Ask', True
                    else:
                        ai_response += "\n\nLet's try breaking that down again."
                        session['phase'] = 'Crawl'

                elif phase == "Run_Ask":
                    question = session.get('current_skill_record', {}).get('run_prompt')
                    if question:
                        ai_response, session['last_question'], session['phase'] = question, question, 'Run_Evaluate'
                    else:
                        ai_response, session['phase'], continue_loop = "No final question, we'll mark it as complete!", 'Summary', True
                
                elif phase == "Run_Evaluate":
                    evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
                    ai_response = evaluation.get('feedback', 'Got it.')
                    if evaluation.get('is_correct'):
                        session['phase'], continue_loop = 'Summary', True
                    else:
                        ai_response += "\n\nLet's review this concept one more time."
                        session['phase'] = 'Crawl'

                elif phase == "Summary":
                    skill_record = session['current_skill_record']
                    mark_skill_as_mastered(cursor, user_id, skill_record['skill_id'])
                    ai_response = f"Excellent! You've mastered **{skill_record['skill_name']}**."
                    session['current_skill_index'] += 1
                    plan, index = session.get('learning_plan', []), session.get('current_skill_index', 0)
                    if index < len(plan):
                        cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (plan[index],))
                        next_skill_record = cursor.fetchone()
                        ai_response += f"\n\nNext up: **{next_skill_record['skill_name']}**. Ready?"
                        session['phase'] = 'Crawl'
                    else:
                        ai_response += "\n\nCongratulations, you've completed your entire learning plan! What would you like to learn next?"
                        session = {"phase": "Awaiting_Goal"}
        
        session['last_ai_reply'] = ai_response
        cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id))
        db.commit()
        return ChatResponse(reply=ai_response, access_code=access_code)

    except Exception as e:
        print(f"--- ERROR IN HANDLER ---\n{traceback.format_exc()}--- END ERROR ---")
        raise HTTPException(status_code=500, detail=f"An internal error occurred.")
    finally:
        if db and db.is_connected():
            cursor.close()
            db.close()
