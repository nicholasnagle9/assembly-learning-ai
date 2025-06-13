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

# --- Configuration & Initialization ---
load_dotenv()
app = FastAPI()

# IMPORTANT: Add your frontend URL to allow requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, change "*" to your actual frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- AI Configuration ---
try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    request_options = {"timeout": 120}
    generative_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Google AI: {e}")
    generative_model = None

# --- In-Memory State Management (for multi-turn conversation state) ---
# This holds the temporary state for a user's session.
user_session_state: Dict[str, Dict] = {}

# --- Pydantic Models for API ---
class ChatRequest(BaseModel):
    message: str
    access_code: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    access_code: Optional[str] = None

# --- Database Connection ---
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

# --- "Codeword" User Persistence ---
def generate_access_code() -> str:
    """Generates a memorable, unique access code."""
    # In a real app, you might check for collisions, but this is fine for an MVP.
    adjectives = ['wise', 'happy', 'clever', 'brave', 'shiny', 'calm', 'eager']
    nouns = ['fox', 'river', 'stone', 'star', 'moon', 'tree', 'lion']
    return f"{random.choice(adjectives)}-{random.choice(nouns)}-{secrets.randbelow(100)}"

def get_or_create_user(cursor, access_code: Optional[str]) -> (Dict, Optional[str]):
    """
    Finds a user by access_code or creates a new one if code is None.
    Returns the user record and the access_code (new or existing).
    """
    new_code_generated = None
    if access_code:
        cursor.execute("SELECT * FROM Users WHERE access_code = %s", (access_code,))
        user_record = cursor.fetchone()
        if user_record:
            return user_record, access_code

    # If no code provided or code not found, create a new user
    new_code = generate_access_code()
    new_code_generated = new_code
    cursor.execute("INSERT INTO Users (access_code) VALUES (%s)", (new_code,))
    user_id = cursor.lastrowid
    cursor.execute("SELECT * FROM Users WHERE user_id = %s", (user_id,))
    user_record = cursor.fetchone()
    return user_record, new_code_generated


# --- Knowledge Graph & Learning Path Helpers ---
def get_all_skills(cursor) -> List[Dict]:
    cursor.execute("SELECT skill_id, skill_name FROM Skills")
    return cursor.fetchall()

def get_mastered_skills(cursor, user_id: int) -> Set[int]:
    cursor.execute("SELECT skill_id FROM User_Skills WHERE user_id = %s", (user_id,))
    return {row['skill_id'] for row in cursor.fetchall()}

def get_all_prerequisites_recursive(cursor, skill_id: int, visited=None) -> Set[int]:
    if visited is None:
        visited = set()
    if skill_id in visited:
        return set()
    visited.add(skill_id)
    
    prerequisites = set()
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (skill_id,))
    for row in cursor.fetchall():
        prereq_id = row['prerequisite_id']
        prerequisites.add(prereq_id)
        prerequisites.update(get_all_prerequisites_recursive(cursor, prereq_id, visited))
    return prerequisites

def mark_skill_as_mastered(cursor, user_id: int, skill_id: int):
    cursor.execute(
        "INSERT IGNORE INTO User_Skills (user_id, skill_id) VALUES (%s, %s)",
        (user_id, skill_id)
    )

# --- AI Interaction Helpers ---
def ask_ai(prompt: str) -> str:
    """Sends a prompt to the generative model and gets a clean text response."""
    if not generative_model:
        return "AI model is not configured."
    try:
        response = generative_model.generate_content(prompt, request_options=request_options)
        return response.text.strip()
    except Exception as e:
        print(f"AI generation error: {e}")
        return "Sorry, I had trouble thinking of a response."

def evaluate_answer_with_ai(question: str, user_answer: str) -> Dict:
    """Asks the AI to evaluate an answer and return a structured JSON object."""
    prompt = f"""
    A user was asked the following question: "{question}"
    The user responded: "{user_answer}"
    Is the user's response correct?
    Respond ONLY with a single, minified JSON object with two keys:
    - "is_correct": a boolean (true or false).
    - "feedback": a short, one-sentence piece of remedial feedback if the answer is wrong, or encouraging feedback if it's right.
    Example of a correct response: {{"is_correct": true, "feedback": "That's exactly right, great job!"}}
    Example of an incorrect response: {{"is_correct": false, "feedback": "Not quite, remember to combine the terms with the same variable."}}
    """
    response_text = ask_ai(prompt)
    try:
        # Clean up potential markdown code blocks
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
    if not db:
        raise HTTPException(status_code=500, detail="Database connection failed.")
    cursor = db.cursor(dictionary=True)
    
    try:
        # Step 1: Get or Create User
        user_record, new_access_code = get_or_create_user(cursor, req.access_code)
        db.commit() # Commit user creation
        
        user_id = user_record['user_id']
        access_code = user_record['access_code']
        session = user_session_state.get(access_code, {"phase": "Awaiting_Goal"})
        
        # Step 2: The State Machine
        phase = session.get("phase")
        user_message = req.message
        ai_response = "I'm not sure how to respond to that."

        if phase == "Awaiting_Goal":
            all_skills = get_all_skills(cursor)
            skill_list_str = "\n".join([f"- ID: {s['skill_id']}, Name: {s['skill_name']}" for s in all_skills])
            prompt = f"""
            A user said they want to learn: "{user_message}".
            Based on this, which of the following skills is the best match?
            Respond ONLY with the numeric skill_id of the best match.
            
            Available Skills:
            {skill_list_str}
            """
            target_id_str = ask_ai(prompt)
            try:
                target_id = int(target_id_str)
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (target_id,))
                skill_record = cursor.fetchone()
                if skill_record:
                    session['target_skill_id'] = target_id
                    session['target_skill_name'] = skill_record['skill_name']
                    session['phase'] = "Confirming_Goal"
                    ai_response = f"Got it. It sounds like you want to learn about **{skill_record['skill_name']}**. Is that correct? (yes/no)"
                else:
                    ai_response = "I couldn't find a skill matching that. Could you please rephrase your goal?"
            except (ValueError, TypeError):
                ai_response = "I'm having trouble understanding that goal. Could you be more specific?"

        elif phase == "Confirming_Goal":
            if "yes" in user_message.lower():
                target_skill_id = session['target_skill_id']
                mastered_skills = get_mastered_skills(cursor, user_id)
                prereqs = get_all_prerequisites_recursive(cursor, target_skill_id)
                full_path_ids = sorted(list(prereqs - mastered_skills)) + [target_skill_id]
                
                cursor.execute(f"SELECT skill_id, skill_name FROM Skills WHERE skill_id IN ({','.join(map(str, full_path_ids))})")
                path_skills = {s['skill_id']: s['skill_name'] for s in cursor.fetchall()}

                learning_path = [path_skills.get(sid) for sid in full_path_ids if path_skills.get(sid)]
                
                session['learning_plan'] = full_path_ids
                session['current_skill_index'] = 0
                
                path_str = "\n".join([f"{i+1}. {name}" for i, name in enumerate(learning_path)])
                ai_response = f"Great! Here is the personalized learning path I've built for you to reach your goal:\n\n{path_str}\n\nWe'll start with **{learning_path[0]}**. Ready to begin?"
                session['phase'] = 'Presenting_Path'
            else:
                ai_response = "My mistake. Please tell me what you'd like to learn, and I'll try again."
                session['phase'] = 'Awaiting_Goal'
        
        elif phase in ["Presenting_Path", "Summary"]: # User is ready to start a skill
            plan = session.get('learning_plan', [])
            index = session.get('current_skill_index', 0)
            if index < len(plan):
                current_skill_id = plan[index]
                cursor.execute("SELECT * FROM Skills WHERE skill_id = %s", (current_skill_id,))
                skill_record = cursor.fetchone()
                
                session['current_skill_record'] = skill_record
                session['phase'] = 'Crawl'
                ai_response = skill_record.get('crawl_prompt', 'Let''s begin.')
                session['phase'] = 'Walk_Ask' # Transition to asking the first question
            else: # Should not happen from 'Presenting_Path'
                ai_response = "Looks like you've completed your learning plan! What's next?"
                session = {"phase": "Awaiting_Goal"}
        
        elif phase == "Walk_Ask":
            skill_record = session['current_skill_record']
            question = skill_record.get('walk_prompt')
            if question:
                ai_response = question
                session['last_question'] = question
                session['phase'] = 'Walk_Evaluate'
            else: # If no walk question, skip to run
                session['phase'] = 'Run_Ask'
                # Re-call the handler to immediately process the Run_Ask phase
                user_session_state[access_code] = session
                return await chat_handler(req)

        elif phase == "Walk_Evaluate":
            evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
            ai_response = evaluation.get('feedback')
            if evaluation.get('is_correct'):
                session['phase'] = 'Run_Ask'
                # Re-call the handler to immediately process the Run_Ask phase
                user_session_state[access_code] = session
                return await chat_handler(req)
            else:
                session['phase'] = 'Walk_Ask' # Ask another guided question
        
        elif phase == "Run_Ask":
            skill_record = session['current_skill_record']
            question = skill_record.get('run_prompt')
            if question:
                ai_response = question
                session['last_question'] = question
                session['phase'] = 'Run_Evaluate'
            else: # If no run question, auto-master and move on
                ai_response = "This skill doesn't have a final question, so we'll mark it as complete!"
                session['phase'] = 'Summary'
                user_session_state[access_code] = session
                return await chat_handler(req)
        
        elif phase == "Run_Evaluate":
            evaluation = evaluate_answer_with_ai(session['last_question'], user_message)
            ai_response = evaluation.get('feedback')
            if evaluation.get('is_correct'):
                skill_record = session['current_skill_record']
                mark_skill_as_mastered(cursor, user_id, skill_record['skill_id'])
                db.commit()
                
                ai_response += f"\n\nExcellent! You've mastered **{skill_record['skill_name']}**."
                session['current_skill_index'] += 1
                
                plan = session.get('learning_plan', [])
                index = session.get('current_skill_index', 0)
                if index < len(plan):
                    next_skill_id = plan[index]
                    cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                    next_skill_record = cursor.fetchone()
                    ai_response += f"\n\nThe next topic is **{next_skill_record['skill_name']}**. Let's continue!"
                    session['phase'] = 'Summary' # Will transition to Crawl on next message
                else:
                    ai_response += "\n\nCongratulations, you've completed your entire learning plan!"
                    session = {"phase": "Awaiting_Goal"}
            else:
                ai_response += "\n\nLet's try that concept again."
                # Go back to the explanation phase for this skill
                session['phase'] = 'Presenting_Path' 

        # Final Step: Update session state and return response
        user_session_state[access_code] = session
        return ChatResponse(reply=ai_response, access_code=new_access_code or req.access_code)

    finally:
        if db and db.is_connected():
            cursor.close()
            db.close()
