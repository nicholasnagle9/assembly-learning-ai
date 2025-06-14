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
SYSTEM_PERSONA_PROMPT = """
You are "Asmby," an expert, encouraging, and friendly math tutor. Your entire focus is on teaching mathematics.
You must NEVER discuss or ask questions about non-math subjects unless it's a direct, real-world analogy to explain a math concept.
You should always be patient and guide the user. When a user is vague, help them narrow down their interest by asking clarifying questions or providing relevant suggestions.
Avoid re-introducing yourself in the middle of a lesson. Maintain a continuous, natural conversation.
"""

# --- Configuration & Initialization ---
load_dotenv(); app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["http://ai-tutor.local", "*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    request_options = {"timeout": 120}; generative_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Google AI: {e}"); generative_model = None

# --- Pydantic Models & DB Helpers ---
class ChatRequest(BaseModel): message: str; access_code: Optional[str] = None
class ChatResponse(BaseModel): reply: str; access_code: str
def get_db_connection():
    try: return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))
    except mysql.connector.Error as e: print(f"DB Connection Error: {e}"); return None
def get_or_create_user(cursor, access_code: Optional[str]) -> Dict:
    if access_code:
        cursor.execute("SELECT * FROM Users WHERE access_code = %s", (access_code,)); user_record = cursor.fetchone()
        if user_record: return user_record
    while True:
        new_code = f"{random.choice(['wise', 'happy', 'clever', 'brave', 'shiny'])}-{random.choice(['fox', 'river', 'stone', 'star', 'moon'])}-{secrets.randbelow(100)}"
        cursor.execute("SELECT user_id FROM Users WHERE access_code = %s", (new_code,));
        if not cursor.fetchone():
            cursor.execute("INSERT INTO Users (access_code) VALUES (%s)", (new_code,)); user_id = cursor.lastrowid
            cursor.execute("SELECT * FROM Users WHERE user_id = %s", (user_id,)); return cursor.fetchone()

# --- Knowledge Graph & AI Helpers ---
def get_all_skills_by_subject(cursor):
    skills = {}; cursor.execute("SELECT skill_id, skill_name, subject FROM Skills ORDER BY subject, skill_id")
    for row in cursor.fetchall():
        subject = row.get('subject', 'General')
        if subject not in skills: skills[subject] = []
        skills[subject].append({"id": row['skill_id'], "name": row['skill_name']})
    return skills
def get_mastered_skills(cursor, user_id):
    cursor.execute("SELECT skill_id FROM User_Skills WHERE user_id = %s", (user_id,)); return {row['skill_id'] for row in cursor.fetchall()}
def get_direct_prerequisites(cursor, skill_id):
    query = "SELECT p.prerequisite_id, s.skill_name FROM Prerequisites p JOIN Skills s ON p.prerequisite_id = s.skill_id WHERE p.skill_id = %s"
    cursor.execute(query, (skill_id,)); return cursor.fetchall()
def mark_skill_as_mastered(cursor, user_id, skill_id):
    cursor.execute("INSERT IGNORE INTO User_Skills (user_id, skill_id) VALUES (%s, %s)", (user_id, skill_id))

def ask_ai(prompt):
    full_prompt = f"{SYSTEM_PERSONA_PROMPT}\n\n--- TASK ---\n\n{prompt}"
    if not generative_model: return "AI model not configured."
    try: return generative_model.generate_content(full_prompt, request_options=request_options).text.strip()
    except Exception as e: print(f"AI Error: {e}"); return "Sorry, I had trouble thinking."

def collaborative_evaluation_with_ai(question, user_answer):
    prompt = f"""A user was asked: '{question}'. They responded: '{user_answer}'.
    Your task is to:
    1. Start by explicitly acknowledging and praising what is CORRECT in their answer.
    2. Then, gently identify ONE key area for improvement. If there are no mistakes, just give positive reinforcement.
    3. Based on their overall grasp, determine if they are ready to proceed.
    Respond ONLY with a JSON object: {{"can_proceed": boolean, "collaborative_feedback": "Your full, conversational response here."}}"""
    response_text = ask_ai(prompt)
    try:
        if "```json" in response_text: response_text = response_text.split("```json")[1].split("```")[0].strip()
        return json.loads(response_text)
    except (json.JSONDecodeError, IndexError):
        return {"can_proceed": False, "collaborative_feedback": "I had some trouble evaluating that. Let's try approaching it a different way."}

# --- Intent Classification Helpers ---
def classify_main_intent(session, user_message):
    phase = session.get("phase", "Awaiting_Goal")
    if phase.endswith("_Evaluate") or session.get("is_awaiting_feedback_response"): return "Answering_Question"
    context = f"The user is in the '{phase}' phase. Last AI message: '{session.get('last_ai_reply', '...')}'"
    prompt = f'Analyze the user message to determine their intent given the context.\nContext: {context}\nUser message: "{user_message}"\nCategorize intent as Answering_Question, Asking_Clarification, or Changing_Topic. Respond ONLY with the category name.'
    intent = ask_ai(prompt)
    for valid in ["Answering_Question", "Asking_Clarification", "Changing_Topic"]:
        if valid in intent: return valid
    return "Answering_Question"

def is_exiting_clarification_loop(session, user_message):
    context = f"The user asked a clarifying question. The AI's last response was: '{session.get('last_clarification_reply', '...')}'"
    prompt = f"Does their new message: '{user_message}' indicate they understand and are ready to return to the main lesson? (e.g., 'ok that makes sense', 'got it', 'continue'). Respond ONLY with 'yes' or 'no'."
    return "yes" in ask_ai(prompt).lower()

# --- Main Chat Endpoint ---
@app.post("/chat", response_model=ChatResponse)
async def chat_handler(req: ChatRequest):
    db = get_db_connection();
    if not db: raise HTTPException(status_code=500, detail="Database connection failed.")
    cursor = db.cursor(dictionary=True)
    
    try:
        user_record = get_or_create_user(cursor, req.access_code); db.commit()
        access_code, user_id = user_record['access_code'], user_record['user_id']
        session = json.loads(user_record.get('session_state') or '{}') or {"phase": "Awaiting_Goal"}
        user_message, ai_response = req.message, "I'm not sure how to respond."

        if user_message == "##INITIALIZE##":
            ai_response = "Hello! I'm your personal AI Tutor, Asmby. What would you like to learn today?" if session.get("phase") == "Awaiting_Goal" else f"[Resuming Session]\n\n{session.get('last_ai_reply', 'Welcome back!')}"
        
        elif session.get("phase") == "Clarification_Mode":
            if is_exiting_clarification_loop(session, user_message):
                return_point = session.get("return_point", {})
                session = return_point
                last_question = session.get("last_question", "Let's try that again.")
                # --- FIX: Graceful return in one message ---
                ai_response = f"Great, glad that's clearer!\n\n---\n**Let's get back to our original question:**\n\n{last_question}"
                # The state is now restored, ready for the user's answer to the original question.
            else:
                topic = session.get('return_point', {}).get('current_skill_record', {}).get('skill_name', 'the current topic')
                prompt = f"The user is in a clarification loop about '{topic}'. They have a follow-up question: '{user_message}'. Provide a helpful, concise answer."
                ai_response = ask_ai(prompt)
                session['last_clarification_reply'] = ai_response

        else:
            intent = classify_main_intent(session, user_message)
            
            if intent == "Asking_Clarification":
                session['return_point'] = session.copy()
                session['phase'] = "Clarification_Mode"
                topic = session.get('current_skill_record', {}).get('skill_name', 'the current topic')
                prompt = f"The user is learning about '{topic}' and asked for clarification: '{user_message}'. Provide a helpful explanation with real-world examples."
                ai_response = ask_ai(prompt)
                session['last_clarification_reply'] = ai_response
            
            elif intent == "Changing_Topic":
                ai_response, session = "Of course. What topic would you like to learn about?", {"phase": "Awaiting_Goal"}

            elif intent == "Answering_Question":
                continue_loop = True
                while continue_loop:
                    phase = session.get("phase", "Awaiting_Goal"); continue_loop = False 

                    if phase == "Awaiting_Goal":
                        all_skills = get_all_skills_by_subject(cursor)
                        prompt = f"""A user wants to learn about: "{user_message}". Determine the single most advanced, relevant skill ID they should start with.
                        Available skills: {json.dumps(all_skills, indent=2)}
                        Respond ONLY with the numeric skill ID."""
                        try:
                            target_id = int(ask_ai(prompt).strip())
                            cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (target_id,)); skill_record = cursor.fetchone()
                            if skill_record:
                                session.update({"current_skill_record": skill_record, "phase": "Dive_In_Crawl"}); continue_loop = True
                            else: ai_response = "I couldn't find a skill for that. Could you be more specific?"
                        except (ValueError, TypeError): ai_response = "I'm having trouble pinpointing a topic for that."

                    elif phase == "Dive_In_Crawl":
                        skill_record = session['current_skill_record']
                        # --- FIX: Removed repetitive intro from prompt ---
                        prompt = f"Explain the concept of '{skill_record['skill_name']}'. Use the following as a guide, but create your own detailed explanation with fresh analogies: '{skill_record['crawl_prompt']}'"
                        explanation = ask_ai(prompt)
                        ai_response = f"{explanation}\n\n---\n**Does this make sense, or should we cover the 'assembly' concepts first?**"
                        session['phase'] = "Awaiting_Checkpoint_Response"

                    elif phase == "Awaiting_Checkpoint_Response":
                        prompt = f"A user was asked if a concept made sense. They responded: '{user_message}'. Is their answer affirmative (they understand) or negative (they are confused)? Respond ONLY with 'affirmative' or 'negative'."
                        if "affirmative" in ask_ai(prompt).lower():
                            ai_response, session['phase'], continue_loop = "Great! Let's try a practice question.", 'Walk_Ask', True
                        else:
                            mastered = get_mastered_skills(cursor, user_id)
                            prereqs = [p for p in get_direct_prerequisites(cursor, session['current_skill_record']['skill_id']) if p['prerequisite_id'] not in mastered]
                            if prereqs:
                                path_str = "\n".join([f"- {p['skill_name']}" for p in prereqs])
                                ai_response = f"No problem! To grasp **{session['current_skill_record']['skill_name']}**, it helps to know these topics. Which sounds best to start with?\n\n{path_str}"
                                session['unmastered_prereqs'], session['phase'] = prereqs, 'Awaiting_Prerequisite_Choice'
                            else:
                                ai_response, session['phase'], continue_loop = "It seems you know the prerequisites! Let's try a practice question to pinpoint the confusion.", 'Walk_Ask', True
                    
                    elif phase == "Awaiting_Prerequisite_Choice":
                         try:
                            prompt = f"A user was given this list: {json.dumps(session.get('unmastered_prereqs', []))}. Their response was: '{user_message}'. Which topic are they choosing? Respond ONLY with the corresponding skill_id."
                            target_id = int(ask_ai(prompt).strip())
                            cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (target_id,)); skill_record = cursor.fetchone()
                            session.update({"learning_plan": [target_id], "current_skill_index": 0, "current_skill_record": skill_record, "phase": "Crawl"}); continue_loop = True
                         except (ValueError, TypeError): ai_response = "Sorry, I didn't catch that. Please choose a topic from the list."

                    elif phase == "Crawl":
                        plan, index = session.get('learning_plan', []), session.get('current_skill_index', 0)
                        if index < len(plan):
                            cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (plan[index],)); skill_record = cursor.fetchone()
                            session['current_skill_record'] = skill_record
                            prompt = f"Explain '{skill_record['skill_name']}'. Guide: '{skill_record['crawl_prompt']}'"
                            ai_response, session['phase'] = ask_ai(prompt), 'Walk_Ask'
                        else: ai_response, session = "You've finished your learning plan! What's next?", {"phase": "Awaiting_Goal"}
                    
                    elif phase == "Walk_Ask":
                        # --- FIX: Simpler, non-repetitive prompt ---
                        prompt = f"Create a simple, focused, guided practice question for the concept: '{session['current_skill_record']['skill_name']}'."
                        question = ask_ai(prompt)
                        ai_response, session['last_question'], session['phase'] = question, question, 'Walk_Evaluate'

                    elif phase == "Walk_Evaluate":
                        # --- NEW: "Trust but Verify" Logic ---
                        if session.get("is_awaiting_feedback_response"):
                            affirmatives = ["yes", "ok", "makes sense", "got it", "continue", "sure", "i understand"]
                            if any(word in user_message.lower() for word in affirmatives):
                                prompt = f"The user says they now understand '{session['current_skill_record']['skill_name']}'. Generate a new, slightly different practice question to verify their understanding. It must be different from the last question: '{session['last_question']}'"
                                new_question = ask_ai(prompt)
                                ai_response = f"Great! Let's try a slightly different question to be sure.\n\n{new_question}"
                                session['last_question'] = new_question
                                session.pop("is_awaiting_feedback_response", None) # Clear the flag
                                session['phase'] = 'Walk_Evaluate' # Re-evaluate with the new question
                            else:
                                ai_response, session['phase'] = "No problem, let's go over the main concept again.", 'Crawl'
                        else:
                            evaluation = collaborative_evaluation_with_ai(session['last_question'], user_message)
                            ai_response = evaluation.get('collaborative_feedback')
                            if evaluation.get('can_proceed'):
                                session['phase'], continue_loop = 'Run_Ask', True
                            else:
                                session['is_awaiting_feedback_response'] = True
                                ai_response += "\n\nDoes that explanation help clarify things for you?"

                    elif phase == "Run_Ask":
                        # --- FIX: Simpler, non-repetitive prompt ---
                        prompt = f"Create one direct, single-concept assessment question to test mastery of '{session['current_skill_record']['skill_name']}'."
                        question = ask_ai(prompt)
                        ai_response, session['last_question'], session['phase'] = question, question, 'Run_Evaluate'

                    elif phase == "Run_Evaluate":
                        evaluation = collaborative_evaluation_with_ai(session['last_question'], user_message)
                        ai_response = evaluation.get('collaborative_feedback', 'Got it.')
                        if evaluation.get('can_proceed'): 
                            session['phase'], continue_loop = 'Summary', True
                        else: 
                            ai_response += "\n\nLet's review this concept one more time."; session['phase'] = 'Crawl'

                    elif phase == "Summary":
                        skill_record = session['current_skill_record']
                        mark_skill_as_mastered(cursor, user_id, skill_record['skill_id'])
                        ai_response = f"Excellent! You've mastered **{skill_record['skill_name']}**."
                        session['current_skill_index'] = session.get('current_skill_index', -1) + 1
                        plan, index = session.get('learning_plan', []), session.get('current_skill_index', 0)
                        if index < len(plan): session['phase'], continue_loop = 'Crawl', True 
                        else:
                            ai_response += "\n\nCongratulations! You've completed your learning plan. What's next?"
                            session = {"phase": "Awaiting_Goal"}
        
        session['last_ai_reply'] = ai_response
        cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id)); db.commit()
        return ChatResponse(reply=ai_response, access_code=access_code)
    except Exception as e:
        print(f"--- ERROR IN HANDLER ---\n{traceback.format_exc()}--- END ERROR ---")
        raise HTTPException(status_code=500, detail=f"An internal error occurred.")
    finally:
        if db and db.is_connected(): db.close()
