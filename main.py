import os
import json 
import mysql.connector
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import Optional, Set, List, Dict

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
# Format: { "username": {"numeric_user_id": 1, "current_skill_id": 1, "skill_name": "...", "phase": "Crawl", "last_question": "...", "goal_skill_id": 7} }
user_session_state = {}

# --- Database & Helper Functions ---
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

def get_all_prerequisites(cursor, skill_id, visited=None):
    """Recursively get all prerequisites for a skill"""
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
        prerequisites.update(get_all_prerequisites(cursor, prereq_id, visited))
    
    return prerequisites

def find_next_skill(cursor, mastered_skills, goal_skill_id):
    """Find the next skill to learn on the path to the goal"""
    if goal_skill_id in mastered_skills:
        return None
    
    # Get all prerequisites for the goal
    all_prereqs = get_all_prerequisites(cursor, goal_skill_id)
    
    # Find unmastered prerequisites
    unmastered_prereqs = all_prereqs - mastered_skills
    
    if not unmastered_prereqs:
        return goal_skill_id
    
    # Find the most foundational unmastered prerequisite
    # (one that has all its prerequisites mastered)
    for prereq_id in unmastered_prereqs:
        prereq_prereqs = get_all_prerequisites(cursor, prereq_id)
        if prereq_prereqs.issubset(mastered_skills):
            return prereq_id
    
    # Fallback: return any unmastered prerequisite
    return min(unmastered_prereqs)

def get_skill_suggestions(cursor, mastered_skills, user_interests=None):
    """Suggest next skills based on mastered skills and user interests"""
    suggestions = []
    
    # Get all skills
    cursor.execute("SELECT skill_id, skill_name, subject, category FROM Skills")
    all_skills = cursor.fetchall()
    
    # Find skills where all prerequisites are mastered
    for skill in all_skills:
        if skill['skill_id'] not in mastered_skills:
            prereqs = get_all_prerequisites(cursor, skill['skill_id'])
            if prereqs.issubset(mastered_skills):
                suggestions.append({
                    'skill_id': skill['skill_id'],
                    'skill_name': skill['skill_name'],
                    'subject': skill['subject'],
                    'category': skill['category']
                })
    
    # Sort by subject and skill_id for consistent ordering
    suggestions.sort(key=lambda x: (x['subject'], x['skill_id']))
    
    # Limit to top 10 suggestions
    return suggestions[:10]

def analyze_user_intent(user_message, cursor):
    """Analyze user message to determine their learning intent"""
    message_lower = user_message.lower()
    
    # Check for specific subject mentions
    subjects = {
        'algebra': ['algebra', 'equation', 'variable', 'polynomial', 'quadratic'],
        'geometry': ['geometry', 'angle', 'triangle', 'circle', 'area', 'volume'],
        'calculus': ['calculus', 'derivative', 'integral', 'limit', 'differentiation'],
        'statistics': ['statistics', 'probability', 'distribution', 'hypothesis', 'regression'],
        'linear algebra': ['linear algebra', 'matrix', 'vector', 'eigenvalue'],
        'discrete': ['discrete', 'logic', 'set theory', 'graph theory', 'combinatorics']
    }
    
    detected_subjects = []
    for subject, keywords in subjects.items():
        if any(keyword in message_lower for keyword in keywords):
            detected_subjects.append(subject)
    
    # If specific subjects detected, find relevant skills
    if detected_subjects:
        skills = []
        for subject in detected_subjects:
            cursor.execute(
                "SELECT skill_id, skill_name FROM Skills WHERE LOWER(subject) LIKE %s",
                (f'%{subject}%',)
            )
            skills.extend(cursor.fetchall())
        return skills
    
    # Check for skill level mentions
    if any(word in message_lower for word in ['beginner', 'basic', 'start', 'beginning']):
        cursor.execute(
            "SELECT skill_id, skill_name FROM Skills WHERE skill_id <= 20 ORDER BY skill_id"
        )
        return cursor.fetchall()
    
    if any(word in message_lower for word in ['advanced', 'college', 'university']):
        cursor.execute(
            "SELECT skill_id, skill_name FROM Skills WHERE skill_id >= 127 ORDER BY skill_id"
        )
        return cursor.fetchall()
    
    return []

# --- FastAPI App & Middleware ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    message: str
    user_id: str

class SetGoalRequest(BaseModel):
    user_id: str
    goal_skill_id: int

# --- New Endpoint for Setting Learning Goals ---
@app.post("/set_goal")
async def set_goal(request: SetGoalRequest):
    username = request.user_id
    goal_skill_id = request.goal_skill_id
    
    db_connection = get_db_connection()
    if not db_connection:
        return {"success": False, "message": "Database connection error"}
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # Verify skill exists
        cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (goal_skill_id,))
        skill = cursor.fetchone()
        
        if not skill:
            return {"success": False, "message": "Invalid skill ID"}
        
        # Update user session state
        if username in user_session_state:
            user_session_state[username]['goal_skill_id'] = goal_skill_id
        
        return {
            "success": True,
            "message": f"Learning goal set to: {skill['skill_name']}",
            "skill_name": skill['skill_name']
        }
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

# --- New Endpoint for Getting Available Skills ---
@app.get("/available_skills/{user_id}")
async def get_available_skills(user_id: str):
    db_connection = get_db_connection()
    if not db_connection:
        return {"skills": [], "error": "Database connection error"}
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # Get user's numeric ID
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            return {"skills": [], "message": "User not found"}
        
        numeric_user_id = user['user_id']
        mastered_skills = get_mastered_skills(cursor, numeric_user_id)
        
        # Get skill suggestions
        suggestions = get_skill_suggestions(cursor, mastered_skills)
        
        return {
            "skills": suggestions,
            "mastered_count": len(mastered_skills),
            "total_skills": 207
        }
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

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
            
            # Check if user is asking about what to learn
            if any(phrase in user_message.lower() for phrase in 
                   ['what should i learn', 'what can i learn', 'help me choose', 
                    'what\'s next', 'what subjects', 'what topics']):
                suggestions = get_skill_suggestions(cursor, mastered_skills)
                
                if suggestions:
                    response = "### Here are some skills you're ready to learn:\n\n"
                    for i, skill in enumerate(suggestions[:5], 1):
                        response += f"{i}. **{skill['skill_name']}** ({skill['subject']})\n"
                    response += "\nTell me which subject interests you, or mention a specific topic!"
                else:
                    response = "You've mastered all available skills! Congratulations on completing the entire curriculum!"
                
                return {"reply": response}
            
            # Try to detect user intent from their message
            potential_skills = analyze_user_intent(user_message, cursor)
            
            if potential_skills:
                # Find the most appropriate skill based on prerequisites
                for skill in potential_skills:
                    prereqs = get_all_prerequisites(cursor, skill['skill_id'])
                    if prereqs.issubset(mastered_skills):
                        goal_skill_id = skill['skill_id']
                        break
                else:
                    # If no skill with satisfied prerequisites, pick the first one
                    goal_skill_id = potential_skills[0]['skill_id']
            else:
                # Default learning path based on mastered skills
                if len(mastered_skills) < 7:
                    goal_skill_id = 7  # Complete basic algebra first
                elif len(mastered_skills) < 53:
                    goal_skill_id = 53  # Complete Algebra I
                elif len(mastered_skills) < 81:
                    goal_skill_id = 81  # Complete Geometry
                elif len(mastered_skills) < 108:
                    goal_skill_id = 108  # Complete Algebra II
                elif len(mastered_skills) < 126:
                    goal_skill_id = 126  # Complete Pre-Calculus
                elif len(mastered_skills) < 157:
                    goal_skill_id = 157  # Complete Calculus
                else:
                    # Suggest college-level topics
                    suggestions = get_skill_suggestions(cursor, mastered_skills)
                    if suggestions:
                        goal_skill_id = suggestions[0]['skill_id']
                    else:
                        return {"reply": "### Congratulations!\n\nYou have mastered the entire mathematics curriculum!"}
            
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
            if next_skill_id:
                cursor.execute("SELECT skill_name, subject FROM Skills WHERE skill_id = %s", (next_skill_id,))
                skill_record = cursor.fetchone()
                user_session_state[username] = {
                    "numeric_user_id": numeric_user_id,
                    "current_skill_id": next_skill_id,
                    "skill_name": skill_record['skill_name'],
                    "subject": skill_record['subject'],
                    "phase": "Crawl",
                    "last_question": None,
                    "goal_skill_id": goal_skill_id
                }
            else:
                return {"reply": "### Congratulations!\n\nYou have mastered all available skills in your learning path!"}

        current_session = user_session_state[username]
        skill_name = current_session['skill_name']
        subject = current_session.get('subject', 'Mathematics')
        phase = current_session['phase']
        last_question = current_session.get('last_question')

        # --- Dynamic Prompt Generation & AI Call ---
        system_prompt = ""
        # The AI's only job is to EXPLAIN
        if phase == "Crawl":
            system_prompt = f"You are a teacher. Your ONLY task is to EXPLAIN the concept of '{skill_name}' from {subject}. Use Markdown: use bolding for key terms, lists, and wrap math in `code blocks`. Keep it simple and engaging. End by asking if the user understands."
            user_session_state[username]['phase'] = 'Walk_Ask'
        # The AI's only job is to ASK a guided question
        elif phase == "Walk_Ask":
            system_prompt = f"You are a friendly tutor. Your ONLY task is to ask a single, simple, leading question to help the user begin practicing '{skill_name}'. Do not solve it for them. Make it approachable and encouraging."
            user_session_state[username]['phase'] = 'Walk_Evaluate'
        # The AI's only job is to EVALUATE the user's answer to the guided question
        elif phase == "Walk_Evaluate":
            system_prompt = f"A user was asked: '{last_question}'. They responded: '{user_message}'. Is this a correct and logical step forward? Respond ONLY with a JSON object: {{\"is_correct\": boolean, \"feedback\": \"A short, encouraging piece of feedback.\"}}"
            # If they get it right, we move to the real test. If not, we try another guided question.
            user_session_state[username]['phase'] = 'Run_Ask' # Assume correct for now, can add logic later
        # The AI's only job is to ASK an assessment question
        elif phase == "Run_Ask":
            system_prompt = f"You are an examiner. Your ONLY task is to ask one clear, direct assessment question to test mastery of '{skill_name}'. Do not include the answer. Make sure the question is appropriate for the skill level."
            user_session_state[username]['phase'] = 'Run_Evaluate'
        # The AI's only job is to EVALUATE the assessment question
        elif phase == "Run_Evaluate":
            system_prompt = f"You are an AI grader. A student was asked the question: '{last_question}'. The student responded: '{user_message}'. First, in a <thinking> block, reason step-by-step if the answer is correct and complete. Second, based on your reasoning, respond ONLY with a single, minified JSON object in the format: {{\"is_correct\": boolean, \"feedback\": \"Your short, encouraging feedback here.\"}}"
        # The AI's only job is to SUMMARIZE and introduce the next topic
        elif phase == "Summary":
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            goal_skill_id = current_session.get('goal_skill_id', 207)
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
            if next_skill_id:
                cursor.execute("SELECT skill_name, subject FROM Skills WHERE skill_id = %s", (next_skill_id,))
                next_skill_record = cursor.fetchone()
                system_prompt = f"The user just mastered '{skill_name}'. Briefly congratulate them and introduce the next topic: '{next_skill_record['skill_name']}' from {next_skill_record['subject']}. Explain why it's the next logical step. End by asking if they are ready."
            else:
                # Check if there are other skills to learn
                suggestions = get_skill_suggestions(cursor, mastered_skills)
                if suggestions:
                    system_prompt = f"The user just mastered '{skill_name}' and completed their current learning path! Congratulate them and mention they can explore other topics like {suggestions[0]['skill_name']} from {suggestions[0]['subject']}."
                else:
                    system_prompt = "The user has just mastered the final skill. Congratulate them on completing the entire mathematics curriculum - from pre-algebra through college-level topics!"
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
                            ai_response += f"\n\n**🎉 Excellent! You've mastered: {skill_name}!**"
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

@app.get("/user_progress/{user_id}")
async def get_user_progress(user_id: str):
    """Get user's learning progress"""
    db_connection = get_db_connection()
    if not db_connection:
        return {"error": "Database connection error"}
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # Get user's numeric ID
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            return {"error": "User not found"}
        
        numeric_user_id = user['user_id']
        mastered_skills = get_mastered_skills(cursor, numeric_user_id)
        
        # Get skill details for mastered skills
        mastered_details = []
        if mastered_skills:
            placeholders = ','.join(['%s'] * len(mastered_skills))
            cursor.execute(
                f"SELECT skill_id, skill_name, subject, category FROM Skills WHERE skill_id IN ({placeholders})",
                tuple(mastered_skills)
            )
            mastered_details = cursor.fetchall()
        
        # Group by subject
        progress_by_subject = {}
        cursor.execute("SELECT DISTINCT subject FROM Skills")
        subjects = [row['subject'] for row in cursor.fetchall()]
        
        for subject in subjects:
            cursor.execute("SELECT COUNT(*) as total FROM Skills WHERE subject = %s", (subject,))
            total = cursor.fetchone()['total']
            
            mastered_in_subject = len([s for s in mastered_details if s['subject'] == subject])
            
            progress_by_subject[subject] = {
                'mastered': mastered_in_subject,
                'total': total,
                'percentage': round((mastered_in_subject / total * 100) if total > 0 else 0, 1)
            }
        
        return {
            'total_mastered': len(mastered_skills),
            'total_skills': 207,
            'overall_percentage': round((len(mastered_skills) / 207 * 100), 1),
            'progress_by_subject': progress_by_subject,
            'recent_skills': mastered_details[-5:] if mastered_details else []
        }
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()