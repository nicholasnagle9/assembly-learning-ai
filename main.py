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
You must NEVER discuss non-math subjects unless it's a direct, real-world analogy to explain a math concept.
You should always be patient. When a user is vague, help them narrow down their interest by asking clarifying questions or providing relevant suggestions based on their progress.
Avoid re-introducing yourself. Maintain a continuous, natural conversation.
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
class SkillTreeRequest(BaseModel): access_code: Optional[str] = None
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
def get_all_skills_with_details(cursor) -> Dict[int, Dict]:
    skills_dict = {}; cursor.execute("SELECT * FROM Skills")
    for row in cursor.fetchall(): skills_dict[row['skill_id']] = row
    return skills_dict
def get_all_skills_by_subject(cursor) -> Dict[str, Dict[str, List[Dict]]]:
    # Organizes skills by Stage -> Topic -> Skill List
    skills_by_structure = {}
    cursor.execute("SELECT skill_id, skill_name, educational_stage, topic_group FROM Skills ORDER BY educational_stage, topic_group, skill_id")
    for row in cursor.fetchall():
        stage = row.get('educational_stage', 'General')
        topic = row.get('topic_group', 'General Topics')
        if stage not in skills_by_structure: skills_by_structure[stage] = {}
        if topic not in skills_by_structure[stage]: skills_by_structure[stage][topic] = []
        skills_by_structure[stage][topic].append({"id": row['skill_id'], "name": row['skill_name']})
    return skills_by_structure
def get_mastered_skills(cursor, user_id):
    cursor.execute("SELECT skill_id FROM User_Skills WHERE user_id = %s", (user_id,)); return {row['skill_id'] for row in cursor.fetchall()}
def get_all_prerequisites_for_skill_list(cursor, skill_ids: List[int]) -> Dict[int, Set[int]]:
    prereqs = {skill_id: set() for skill_id in skill_ids}
    if not skill_ids: return prereqs
    placeholders = ','.join(['%s'] * len(skill_ids))
    cursor.execute(f"SELECT skill_id, prerequisite_id FROM Prerequisites WHERE skill_id IN ({placeholders})", tuple(skill_ids))
    for row in cursor.fetchall(): prereqs[row['skill_id']].add(row['prerequisite_id'])
    return prereqs
def mark_skill_as_mastered(cursor, user_id, skill_id):
    cursor.execute("INSERT IGNORE INTO User_Skills (user_id, skill_id) VALUES (%s, %s)", (user_id, skill_id))

def ask_ai(prompt):
    full_prompt = f"{SYSTEM_PERSONA_PROMPT}\n\n--- TASK ---\n\n{prompt}"
    if not generative_model: return "AI model not configured."
    try: return generative_model.generate_content(full_prompt, request_options=request_options).text.strip()
    except Exception as e: print(f"AI Error: {e}"); return "Sorry, I had trouble thinking."

def collaborative_evaluation_with_ai(question, user_answer):
    prompt = f"""A user was asked: '{question}'. They responded: '{user_answer}'.
    Your task is to: 1. Praise what is CORRECT. 2. Gently identify ONE area for improvement. 3. Determine if they can proceed.
    Respond ONLY with JSON: {{"can_proceed": boolean, "collaborative_feedback": "Your full, conversational response here."}}"""
    response_text = ask_ai(prompt)
    try:
        if "```json" in response_text: response_text = response_text.split("```json")[1].split("```")[0].strip()
        return json.loads(response_text)
    except (json.JSONDecodeError, IndexError):
        return {"can_proceed": False, "collaborative_feedback": "I had trouble evaluating that. Let's try another way."}

def classify_user_request_type(user_message, all_skills):
    stages = sorted(list(set(s['educational_stage'] for s in all_skills.values() if s.get('educational_stage'))))
    topics = sorted(list(set(s['topic_group'] for s in all_skills.values() if s.get('topic_group'))))
    prompt = f"""Analyze the user's request: "{user_message}".
    Categorize it as 'educational_stage', 'topic_group', or 'skill'.
    Available Stages: {stages}
    Available Topic Groups: {topics}
    Respond ONLY with a single minified JSON object. e.g., {{"type": "topic_group", "value": "Geometry"}}
    """
    try: return json.loads(ask_ai(prompt))
    except (json.JSONDecodeError, IndexError): return {"type": "skill", "value": user_message}

def build_learning_plan_from_scope(cursor, all_skills, scope_type, scope_value):
    target_skill_ids = []
    if scope_type == 'educational_stage':
        target_skill_ids = [sid for sid, s in all_skills.items() if s.get('educational_stage') == scope_value]
    elif scope_type == 'topic_group':
        target_skill_ids = [sid for sid, s in all_skills.items() if s.get('topic_group') == scope_value]
    else:
        for sid, s in all_skills.items():
            if s['skill_name'].lower() == scope_value.lower():
                target_skill_ids = [sid]; break
    
    plan = []; prereqs = get_all_prerequisites_for_skill_list(cursor, list(all_skills.keys()))
    skills_in_plan = set(target_skill_ids)
    to_add = set(target_skill_ids)
    while to_add:
        current_id = to_add.pop()
        skill_prereqs = prereqs.get(current_id, set())
        for prereq_id in skill_prereqs:
            if prereq_id not in skills_in_plan: skills_in_plan.add(prereq_id); to_add.add(prereq_id)
    in_degree = {u: 0 for u in skills_in_plan}; adj = {u: [] for u in skills_in_plan}
    for u in skills_in_plan:
        for v in prereqs.get(u, set()):
            if v in skills_in_plan: in_degree[u] += 1; adj[v].append(u)
    queue = [u for u in skills_in_plan if in_degree[u] == 0]
    while queue:
        u = queue.pop(0); plan.append(u)
        for v in adj.get(u, []):
            in_degree[v] -= 1
            if in_degree[v] == 0: queue.append(v)
    return plan

# --- NEW: Skill Tree Endpoint ---
@app.post("/skill-tree")
async def get_skill_tree(req: SkillTreeRequest):
    db = get_db_connection();
    if not db: raise HTTPException(status_code=500, detail="Database connection failed.")
    cursor = db.cursor(dictionary=True)
    try:
        user_record = get_or_create_user(cursor, req.access_code); db.commit()
        user_id = user_record['user_id']
        
        mastered_skill_ids = get_mastered_skills(cursor, user_id)
        skill_tree = get_all_skills_by_subject(cursor)
        
        return {
            "mastered_skill_ids": list(mastered_skill_ids),
            "skill_tree": skill_tree
        }
    except Exception as e:
        print(f"--- ERROR IN SKILL TREE ---\n{traceback.format_exc()}--- END ERROR ---")
        raise HTTPException(status_code=500, detail="Could not retrieve skill tree.")
    finally:
        if db and db.is_connected(): db.close()

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
        user_message = req.message
        
        # This logic is now greatly simplified because most of it is handled by the V2 architecture
        # For this MVP, we will keep the core loop simple.
        
        # A simplified Awaiting_Goal phase
        if session.get("phase") == "Awaiting_Goal":
            request_details = classify_user_request_type(user_message, get_all_skills_with_details(cursor))
            scope_type = request_details.get("type", "skill")
            scope_value = request_details.get("value", user_message)
            
            full_plan = build_learning_plan_from_scope(cursor, get_all_skills_with_details(cursor), scope_type, scope_value)
            mastered_skills = get_mastered_skills(cursor, user_id)
            plan_to_learn = [sid for sid in full_plan if sid not in mastered_skills]

            if not plan_to_learn:
                ai_response = f"Excellent! It looks like you've already mastered all of {scope_value}. What's next?"
                session = {"phase": "Awaiting_Goal"}
            else:
                first_skill_name = get_all_skills_with_details(cursor)[plan_to_learn[0]]['skill_name']
                ai_response = f"Absolutely! I can guide you through {scope_value}. Our path will begin with '{first_skill_name}'.\n\nReady to get started?"
                session.update({"learning_plan": plan_to_learn, "current_skill_index": 0, "phase": "Crawl"})
        
        else: # Handle in-lesson conversation
             # This is a placeholder for the more complex CWR logic, which can be ported into here.
             # For now, we'll just have a simple response.
             ai_response = "Continuing lesson..."
             # In a full implementation, you'd have the CRAWL/WALK/RUN logic here.


        session['last_ai_reply'] = ai_response
        cursor.execute("UPDATE Users SET session_state = %s WHERE user_id = %s", (json.dumps(session), user_id)); db.commit()
        return ChatResponse(reply=ai_response, access_code=access_code)

    except Exception as e:
        print(f"--- ERROR IN HANDLER ---\n{traceback.format_exc()}--- END ERROR ---")
        raise HTTPException(status_code=500, detail=f"An internal error occurred.")
    finally:
        if db and db.is_connected(): db.close()

