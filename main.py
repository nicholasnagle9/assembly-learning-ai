import os
import json 
import mysql.connector
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import Optional, Set, List, Dict, Tuple

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
user_session_state = {}

# --- Database & Helper Functions (Largely Unchanged) ---
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

def get_mastered_skills(cursor, numeric_user_id) -> Set[int]:
    query = "SELECT skill_id FROM User_Skills WHERE user_id = %s"
    cursor.execute(query, (numeric_user_id,))
    return {row['skill_id'] for row in cursor.fetchall()}

def get_all_prerequisites_recursive(cursor, skill_id, visited=None) -> Set[int]:
    if visited is None: visited = set()
    if skill_id in visited: return set()
    visited.add(skill_id)
    prerequisites = set()
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (skill_id,))
    for row in cursor.fetchall():
        prereq_id = row['prerequisite_id']
        prerequisites.add(prereq_id)
        prerequisites.update(get_all_prerequisites_recursive(cursor, prereq_id, visited))
    return prerequisites

def mark_skill_and_prerequisites_mastered(cursor, user_id: int, skill_id: int, already_processed: Set[int] = None):
    if already_processed is None: already_processed = set()
    if skill_id in already_processed: return
    
    prereqs = get_all_prerequisites_recursive(cursor, skill_id)
    already_processed.add(skill_id)
    
    for prereq_id in prereqs:
        if prereq_id not in already_processed:
            mark_skill_and_prerequisites_mastered(cursor, user_id, prereq_id, already_processed)
    
    cursor.execute(
        "INSERT INTO User_Skills (user_id, skill_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE skill_id=skill_id",
        (user_id, skill_id)
    )

def find_next_skill(cursor, mastered_skills: Set[int], goal_skill_id: int) -> Optional[int]:
    if goal_skill_id in mastered_skills: return None
    path_skills = get_all_prerequisites_recursive(cursor, goal_skill_id)
    path_skills.add(goal_skill_id)
    unmastered_prereqs = sorted(list(path_skills - mastered_skills))
    
    for prereq_id in unmastered_prereqs:
        prereq_prereqs = get_all_prerequisites_recursive(cursor, prereq_id)
        if prereq_prereqs.issubset(mastered_skills):
            return prereq_id
    return unmastered_prereqs[0] if unmastered_prereqs else None
def get_direct_prerequisites(cursor, skill_id: int) -> Set[int]:
    """Gets only the immediate prerequisites for a skill (one level down)."""
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (skill_id,))
    return {row['prerequisite_id'] for row in cursor.fetchall()}
# --- NEW: Helper function for the assessment logic ---
def get_next_assessment_question(cursor, skills_to_test: List[int]) -> Tuple[Optional[int], Optional[str]]:
    """Finds the best question to ask from a list of skills to test."""
    if not skills_to_test:
        return None, None

    skill_set = set(skills_to_test)
    prereqs_in_set = set()
    for skill_id in skill_set:
        query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
        cursor.execute(query, (skill_id,))
        for row in cursor.fetchall():
            if row['prerequisite_id'] in skill_set:
                prereqs_in_set.add(row['prerequisite_id'])

    # "Gateway" skills are those not serving as prerequisites for other skills in the test set.
    gateway_skills = sorted(list(skill_set - prereqs_in_set), reverse=True)
    
    # Prioritize asking questions for gateway skills
    testable_skills = gateway_skills + sorted(list(prereqs_in_set), reverse=True)

    for skill_id in testable_skills:
        cursor.execute("SELECT assessment_question FROM Skills WHERE skill_id = %s", (skill_id,))
        result = cursor.fetchone()
        if result and result['assessment_question']:
            return skill_id, result['assessment_question']
            
    return None, None # No more questions to ask

# (Pydantic Models and other endpoints like /skills/{skill_id} would go here, unchanged)
class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- REWRITTEN: The New Brain ---
# --- REWRITTEN: The New Brain (Version 2) ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    username = chat_request.user_id
    user_message = chat_request.message.lower()
    
    db_connection = get_db_connection()
    if not db_connection: raise HTTPException(status_code=500, detail="Database connection error.")
    cursor = db_connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (username,))
        user_record = cursor.fetchone()
        numeric_user_id = user_record['user_id'] if user_record else None
        if not numeric_user_id:
            cursor.execute("INSERT INTO Users (username, email, password_hash) VALUES (%s, %s, %s)",
                           (username, f"{username}@example.com", "placeholder"))
            db_connection.commit()
            numeric_user_id = cursor.lastrowid
        
        current_session = user_session_state.get(username, {})
        phase = current_session.get("phase")

        # --- ASSESSMENT LOGIC ---
        if phase and phase.startswith('Assessment'):
            if phase == 'Assessment_Evaluate':
                skill_being_tested = current_session['skill_being_tested']
                last_question = current_session['last_question']
                
                # --- FIX 1: Improved prompt that doesn't leak the answer ---
                eval_prompt = f"A user was asked: '{last_question}'. They responded: '{user_message}'. Is this answer correct? The concept is about an early topic in calculus. Respond ONLY with a single, minified JSON object: {{\"is_correct\": boolean, \"feedback\": \"A short, encouraging, one-sentence piece of feedback. Do NOT reveal the correct answer or explain the solution.\"}}"
                response = generative_model.generate_content(eval_prompt, request_options=request_options)
                assessment = json.loads(response.text[response.text.find('{'):response.text.rfind('}')+1])

                ai_response = assessment.get("feedback", "Got it.")
                
                if assessment.get("is_correct"):
                    # --- CORRECT ANSWER ---
                    ai_response += "\n\nCorrect! Let's try the next one."
                    mark_skill_and_prerequisites_mastered(cursor, numeric_user_id, skill_being_tested)
                    db_connection.commit()
                    
                    mastered_branch = get_all_prerequisites_recursive(cursor, skill_being_tested)
                    mastered_branch.add(skill_being_tested)
                    current_session['skills_to_test'] = [s for s in current_session['skills_to_test'] if s not in mastered_branch]
                else:
                    # --- INCORRECT ANSWER ---
                    ai_response += "\n\nNo problem, that's what this is for! Let's back up a step."
                    
                    # --- FIX 2: Use the new helper to get ONLY direct prerequisites ---
                    direct_prereqs = get_direct_prerequisites(cursor, skill_being_tested)
                    mastered_now = get_mastered_skills(cursor, numeric_user_id)
                    
                    # Add the more foundational skills to the front of the test list
                    new_skills_to_test = sorted(list(direct_prereqs - mastered_now), reverse=True)
                    current_session['skills_to_test'] = new_skills_to_test + current_session['skills_to_test']

                # --- Ask the next question or complete the assessment ---
                next_skill_id, next_question = get_next_assessment_question(cursor, current_session['skills_to_test'])
                if next_question:
                    ai_response += f"\n\nHere's the next question: {next_question}"
                    current_session['skill_being_tested'] = next_skill_id
                    current_session['last_question'] = next_question
                    user_session_state[username] = current_session
                else: # Assessment is complete
                    current_session['phase'] = 'Assessment_Complete'
                    # Fall through to the next block to generate the final response
            
            if current_session.get('phase') == 'Assessment_Complete':
                mastered_skills = get_mastered_skills(cursor, numeric_user_id)
                goal_skill_id = current_session['goal_skill_id']
                next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
                
                if next_skill_id:
                    cursor.execute("SELECT skill_name, subject FROM Skills WHERE skill_id = %s", (next_skill_id,))
                    skill_record = cursor.fetchone()
                    ai_response = f"Diagnostic complete! Your personalized learning path starts with **{skill_record['skill_name']}**. Ready to dive in?"
                    user_session_state[username] = {
                        "numeric_user_id": numeric_user_id, "current_skill_id": next_skill_id,
                        "skill_name": skill_record['skill_name'], "subject": skill_record['subject'],
                        "phase": "Crawl", "last_question": None, "goal_skill_id": goal_skill_id
                    }
                else: 
                    ai_response = "Wow, it looks like you've already mastered all the prerequisites for this topic! Great job. What would you like to learn next?"
                    if username in user_session_state: del user_session_state[username]
                
                return {"reply": ai_response}

        # --- NEW CONVERSATION / GOAL SETTING ---
        elif not current_session or user_message in ["start over", "new topic", "i want to learn calculus"]:
            goal_skill_id = 157 # Defaulting to "Calculus" for this example
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            
            if goal_skill_id in mastered_skills:
                ai_response = "It looks like you've already mastered that! What else would you like to learn?"
                if username in user_session_state: del user_session_state[username]
            else:
                unmastered_prereqs = get_all_prerequisites_recursive(cursor, goal_skill_id) - mastered_skills
                ai_response = "Excellent choice! To create the fastest learning path for you, let's start with a quick diagnostic to see what you already know. We'll skip everything you've mastered.\n\nReady to start?"
                user_session_state[username] = {
                    "phase": "Start_Assessment",
                    "goal_skill_id": goal_skill_id,
                    "skills_to_test": sorted(list(unmastered_prereqs), reverse=True),
                    "numeric_user_id": numeric_user_id
                }
        
        # --- START THE ASSESSMENT ---
        elif current_session.get("phase") == "Start_Assessment":
             next_skill_id, next_question = get_next_assessment_question(cursor, current_session['skills_to_test'])
             if next_question:
                 ai_response = f"Great! First question: {next_question}"
                 current_session['phase'] = 'Assessment_Evaluate'
                 current_session['skill_being_tested'] = next_skill_id
                 current_session['last_question'] = next_question
                 user_session_state[username] = current_session
             else: 
                 ai_response = "You know, it looks like this path is pretty straightforward. Let's just jump right in!"
                 # This would transition to Crawl-Walk-Run
                 if username in user_session_state: del user_session_state[username]

        # --- REGULAR CRAWL-WALK-RUN LOGIC ---
        else:
            ai_response = f"Continuing with your lesson on **{current_session.get('skill_name')}**... (Crawl-Walk-Run logic)"

        return {"reply": ai_response}

    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()