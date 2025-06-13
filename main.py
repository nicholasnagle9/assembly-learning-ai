import os
import json 
import mysql.connector
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from fastapi import FastAPI, HTTPException
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
    # The result from fetchall is a list of dictionaries, e.g., [{'skill_id': 1}, {'skill_id': 2}]
    return set(row['skill_id'] for row in cursor.fetchall())

def get_all_prerequisites_recursive(cursor, skill_id, visited=None):
    """Recursively get ALL prerequisites for a skill, including prerequisites of prerequisites"""
    if visited is None:
        visited = set()
    
    if skill_id in visited:
        return set()
    
    visited.add(skill_id)
    all_prerequisites = set()
    
    # Get direct prerequisites
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (skill_id,))
    
    for row in cursor.fetchall():
        prereq_id = row['prerequisite_id']
        all_prerequisites.add(prereq_id)
        # Recursively get prerequisites of this prerequisite
        all_prerequisites.update(get_all_prerequisites_recursive(cursor, prereq_id, visited.copy()))
    
    return all_prerequisites

def find_next_skill(cursor, mastered_skills, goal_skill_id):
    """Find the next skill to learn on the path to the goal"""
    if goal_skill_id in mastered_skills:
        return None
    
    # Get all prerequisites for the goal, including the goal itself in the path
    path_skills = get_all_prerequisites_recursive(cursor, goal_skill_id)
    path_skills.add(goal_skill_id)

    # Find unmastered prerequisites on the direct path
    unmastered_prereqs = path_skills - mastered_skills
    
    if not unmastered_prereqs:
        # This case is tricky, might mean goal is already mastered or something is off
        # If goal is not in mastered_skills, it should be the next one
        return goal_skill_id if goal_skill_id not in mastered_skills else None

    # Find the most foundational unmastered prerequisite
    # (one that has all its own prerequisites mastered)
    for prereq_id in sorted(list(unmastered_prereqs)): # sorted for deterministic order
        prereq_prereqs = get_all_prerequisites_recursive(cursor, prereq_id)
        if prereq_prereqs.issubset(mastered_skills):
            return prereq_id
            
    # Fallback: if no clear path, return the lowest ID unmastered prereq.
    # This might happen if the prerequisite graph has gaps or is imperfect.
    return min(unmastered_prereqs) if unmastered_prereqs else None


def get_skill_suggestions(cursor, mastered_skills, user_interests=None):
    """Suggest next skills based on mastered skills and user interests"""
    suggestions = []
    
    # Get all skills
    cursor.execute("SELECT skill_id, skill_name, subject, category FROM Skills")
    all_skills = cursor.fetchall()
    
    # Find skills where all prerequisites are mastered
    for skill in all_skills:
        if skill['skill_id'] not in mastered_skills:
            prereqs = get_all_prerequisites_recursive(cursor, skill['skill_id'])
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

def mark_skill_and_prerequisites_mastered(cursor, user_id, skill_id, already_processed=None):
    """Recursively mark a skill and all its prerequisites as mastered"""
    if already_processed is None:
        already_processed = set()
    
    if skill_id in already_processed:
        return
    
    # First, get all prerequisites of this skill BEFORE marking it as processed
    prereqs = get_all_prerequisites_recursive(cursor, skill_id)
    
    already_processed.add(skill_id)
    
    # Recursively mark all prerequisites
    for prereq_id in prereqs:
        if prereq_id not in already_processed:
            mark_skill_and_prerequisites_mastered(cursor, user_id, prereq_id, already_processed)
    
    # Then mark this skill as mastered
    cursor.execute(
        "INSERT INTO User_Skills (user_id, skill_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE skill_id=skill_id",
        (user_id, skill_id)
    )

# --- FastAPI App & Middleware ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- Pydantic Models ---
class ChatRequest(BaseModel):
    message: str
    user_id: str

class SetGoalRequest(BaseModel):
    user_id: str
    goal_skill_id: int

class BulkMasterSkillsRequest(BaseModel):
    skill_ids: List[int]

class SkillResponse(BaseModel):
    skill_id: int
    skill_name: str
    subject: str
    category: str
    description: Optional[str] = None

# --- New Endpoint for Getting Prerequisites for a Goal ---
# --- REVISED Endpoint for Getting Prerequisites for a Goal ---
@app.get("/skills/prerequisites_for_goal/{goal_skill_id}", response_model=List[SkillResponse])
async def get_prerequisites_for_goal(goal_skill_id: int, user_id: str):
    """Get all UNMASTERED prerequisite skills needed for a goal skill for a specific user."""
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Database connection error")
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # 1. (FIX) Get the specific user's numeric ID from the provided 'user_id' string.
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        numeric_user_id = user['user_id']
        
        # 2. (FIX) Get the skills this specific user has already mastered.
        mastered_skills = get_mastered_skills(cursor, numeric_user_id)

        # 3. Get all prerequisites for the goal skill.
        all_prereqs = get_all_prerequisites_recursive(cursor, goal_skill_id)
        
        # 4. (FIX) Calculate the prerequisites the user has NOT yet mastered.
        unmastered_prereqs = all_prereqs - mastered_skills

        if not unmastered_prereqs:
            return []
        
        # 5. Get and return the details for only the unmastered skills.
        placeholders = ','.join(['%s'] * len(unmastered_prereqs))
        query = f"""
            SELECT skill_id, skill_name, subject, category, description 
            FROM Skills 
            WHERE skill_id IN ({placeholders})
            ORDER BY subject, category, skill_id
        """
        cursor.execute(query, tuple(unmastered_prereqs))
        
        skills = cursor.fetchall()
        return [SkillResponse(**skill) for skill in skills]
        
    except mysql.connector.Error as e:
        print(f"Database error in get_prerequisites_for_goal: {e}")
        raise HTTPException(status_code=500, detail="A database error occurred.")
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()
# --- New Endpoint for Getting Single Skill Details ---
@app.get("/skills/{skill_id}", response_model=SkillResponse)
async def get_skill(skill_id: int):
    """Get details for a single skill"""
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Database connection error")
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT skill_id, skill_name, subject, category, description FROM Skills WHERE skill_id = %s",
            (skill_id,)
        )
        skill = cursor.fetchone()
        
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        
        return SkillResponse(**skill)
        
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

# --- New Endpoint for Bulk Skill Mastery ---
@app.post("/users/{user_id}/master_skills_bulk")
async def master_skills_bulk(user_id: str, request: BulkMasterSkillsRequest):
    """Mark multiple skills as mastered for a user, including all their prerequisites"""
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Database connection error")
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # Get numeric user ID
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        numeric_user_id = user['user_id']
        
        # Process each skill and its prerequisites
        processed_skills = set()
        for skill_id in request.skill_ids:
            mark_skill_and_prerequisites_mastered(cursor, numeric_user_id, skill_id, processed_skills)
        
        db_connection.commit()
        
        # Get updated count of mastered skills
        cursor.execute(
            "SELECT COUNT(*) as count FROM User_Skills WHERE user_id = %s",
            (numeric_user_id,)
        )
        mastered_count = cursor.fetchone()['count']
        
        return {
            "success": True,
            "message": f"Successfully updated {len(processed_skills)} skills",
            "skills_processed": len(processed_skills),
            "total_mastered": mastered_count
        }
        
    except Exception as e:
        db_connection.rollback()
        raise HTTPException(status_code=500, detail=f"Error updating skills: {str(e)}")
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

# --- Endpoint for Setting Learning Goals ---
@app.post("/set_goal")
async def set_goal(request: SetGoalRequest):
    username = request.user_id
    goal_skill_id = request.goal_skill_id
    
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Database connection error")
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # Verify skill exists
        cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (goal_skill_id,))
        skill = cursor.fetchone()
        
        if not skill:
            raise HTTPException(status_code=404, detail="Invalid skill ID")
        
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

# --- Endpoint for Getting Available Skills ---
@app.get("/available_skills/{user_id}")
async def get_available_skills(user_id: str):
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Database connection error")
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # Get user's numeric ID
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        numeric_user_id = user['user_id']
        mastered_skills = get_mastered_skills(cursor, numeric_user_id)
        
        # Get skill suggestions
        suggestions = get_skill_suggestions(cursor, mastered_skills)
        
        return {
            "skills": suggestions,
            "mastered_count": len(mastered_skills),
            "total_skills": 207  # Consider making this dynamic
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
    action_response = None
    goal_skill_id_for_checklist = None
    
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Could not connect to the database.")

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

        # --- State Initialization & Resumption Logic ---
        if username not in user_session_state or \
           user_message.lower() in ["progress updated", "ready to learn!", "resume", "continue", "progress updated! ready to continue learning."]:
            
            # If coming from a checklist, retrieve the goal. Otherwise, determine a new one.
            goal_skill_id = None
            if username in user_session_state and user_session_state[username].get('awaiting_checklist'):
                goal_skill_id = user_session_state[username].get('goal_skill_id')

            if username in user_session_state:
                del user_session_state[username] # Reset session to re-evaluate path

            mastered_skills = get_mastered_skills(cursor, numeric_user_id)

            if not goal_skill_id:
                # Determine goal based on user message or progress
                potential_skills = analyze_user_intent(user_message, cursor)
                if potential_skills:
                    for skill in potential_skills:
                        prereqs = get_all_prerequisites_recursive(cursor, skill['skill_id'])
                        if prereqs.issubset(mastered_skills):
                            goal_skill_id = skill['skill_id']
                            break
                    if not goal_skill_id:
                        goal_skill_id = potential_skills[0]['skill_id']
                else: # Default learning path
                    suggestions = get_skill_suggestions(cursor, mastered_skills)
                    if suggestions:
                        goal_skill_id = suggestions[0]['skill_id']
                    else:
                        return {"reply": "### Congratulations! 🎉\n\nYou have mastered the entire mathematics curriculum!"}

            # Now we have a goal_skill_id, check for prerequisites
            all_prereqs_for_goal = get_all_prerequisites_recursive(cursor, goal_skill_id)
            unmastered_prereqs = all_prereqs_for_goal - mastered_skills
            
            # If there are unmastered prerequisites, show the checklist
            if len(unmastered_prereqs) > 0 and user_message.lower() not in ["progress updated", "ready to learn!", "resume", "continue", "progress updated! ready to continue learning."]:
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (goal_skill_id,))
                goal_skill = cursor.fetchone()
                
                ai_response = f"### Great choice! Let's work towards {goal_skill['skill_name']}!\n\nThis topic builds on a few skills. To make sure we start in the right place, please review the list of prerequisites and check off any you've already mastered. This will help create the most efficient learning path for you!"
                
                action_response = "show_prerequisite_checklist"
                goal_skill_id_for_checklist = goal_skill_id
                
                user_session_state[username] = {
                    "numeric_user_id": numeric_user_id, "goal_skill_id": goal_skill_id,
                    "awaiting_checklist": True
                }
                
                response_data = {"reply": ai_response, "action": action_response, "goal_skill_id_for_checklist": goal_skill_id_for_checklist}
                return response_data

            # If no checklist needed or user is continuing from it, find the next immediate skill
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
            if next_skill_id:
                cursor.execute("SELECT skill_name, subject FROM Skills WHERE skill_id = %s", (next_skill_id,))
                skill_record = cursor.fetchone()
                user_session_state[username] = {
                    "numeric_user_id": numeric_user_id, "current_skill_id": next_skill_id,
                    "skill_name": skill_record['skill_name'], "subject": skill_record['subject'],
                    "phase": "Crawl", "last_question": None, "goal_skill_id": goal_skill_id
                }
            else:
                return {"reply": "### Excellent! It looks like you've mastered this entire topic. Let's find something new to learn."}

        # --- Crawl-Walk-Run-Fly State Machine ---
        current_session = user_session_state[username]
        skill_name = current_session['skill_name']
        subject = current_session.get('subject', 'Mathematics')
        phase = current_session['phase']
        last_question = current_session.get('last_question')

        system_prompt = ""
        if phase == "Crawl":
            system_prompt = f"You are a teacher. Your ONLY task is to EXPLAIN the concept of '{skill_name}' from the subject of {subject}. Use Markdown for formatting: use bolding for key terms, use lists, and wrap math equations in `code blocks`. Keep it simple and engaging. End by asking the user if they're ready to try a simple practice question."
            user_session_state[username]['phase'] = 'Walk_Ask'
        
        elif phase == "Walk_Ask":
            system_prompt = f"You are a friendly tutor. Your ONLY task is to ask a single, simple, leading question to help the user begin practicing '{skill_name}'. Do not solve it for them. Make it approachable and encouraging."
            user_session_state[username]['phase'] = 'Walk_Evaluate'

        elif phase == "Walk_Evaluate":
            system_prompt = f"A user was asked this question about '{skill_name}': '{last_question}'. They responded: '{user_message}'. Is their answer correct? Respond ONLY with a single, minified JSON object: {{\"is_correct\": boolean, \"feedback\": \"A short, encouraging piece of feedback, explaining why if they are wrong.\"}}"
            # Logic to handle correct/incorrect is in the response processing block
        
        elif phase == "Run_Ask":
            system_prompt = f"You are an examiner. Your ONLY task is to ask one clear, direct assessment question to test mastery of '{skill_name}'. Do not include the answer or any hints. The question should be a slightly more comprehensive problem than the practice one."
            user_session_state[username]['phase'] = 'Run_Evaluate'

        elif phase == "Run_Evaluate":
            system_prompt = f"You are an AI grader. A student was asked the question: '{last_question}' to test their knowledge of '{skill_name}'. The student responded: '{user_message}'. First, in a <thinking> block, reason step-by-step if the answer is correct and complete. Second, based on your reasoning, respond ONLY with a single, minified JSON object in the format: {{\"is_correct\": boolean, \"feedback\": \"Your short, encouraging feedback here. If incorrect, briefly explain the mistake.\"}}"

        elif phase == "Summary":
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            goal_skill_id = current_session.get('goal_skill_id')
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)
            if next_skill_id:
                cursor.execute("SELECT skill_name, subject FROM Skills WHERE skill_id = %s", (next_skill_id,))
                next_skill_record = cursor.fetchone()
                system_prompt = f"The user just mastered '{skill_name}'. Briefly congratulate them and introduce the next topic in their learning path: '{next_skill_record['skill_name']}'. Explain briefly why it's the next logical step. End by asking if they are ready to begin."
                del user_session_state[username] # Let the next message re-initialize the state machine
            else:
                system_prompt = f"The user just mastered '{skill_name}' and completed their current learning path! Congratulate them warmly and let them know they can ask 'what's next?' to explore other topics."
                del user_session_state[username]
        
        # --- AI Call and Response Processing ---
        response = generative_model.generate_content(system_prompt, request_options=request_options)
        response_text = response.text
        
        if phase.endswith("_Evaluate"):
            try:
                json_string = response_text[response_text.find('{'):response_text.rfind('}')+1]
                assessment = json.loads(json_string)
                ai_response = assessment.get("feedback", "Evaluation error.")
                
                if assessment.get("is_correct"):
                    if phase == "Walk_Evaluate":
                        user_session_state[username]['phase'] = 'Run_Ask' # Move to assessment
                    elif phase == "Run_Evaluate":
                        skill_id_mastered = current_session['current_skill_id']
                        cursor.execute("INSERT INTO User_Skills (user_id, skill_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE skill_id=skill_id", (numeric_user_id, skill_id_mastered))
                        db_connection.commit()
                        user_session_state[username]['phase'] = 'Summary'
                        ai_response += f"\n\n**🎉 Excellent! You've mastered: {skill_name}!**"
                else: # Incorrect
                    ai_response += "\nLet's try that again, but maybe from a different angle."
                    if phase == "Walk_Evaluate":
                        user_session_state[username]['phase'] = 'Crawl' # Re-explain
                    elif phase == "Run_Evaluate":
                        user_session_state[username]['phase'] = 'Walk_Ask' # Go back to guided practice
            except (json.JSONDecodeError, IndexError):
                ai_response = "My evaluation response was malformed. Let's try that question again."
                # Keep the phase the same to re-ask
        else:
            ai_response = response_text
            if phase.endswith("_Ask"):
                user_session_state[username]['last_question'] = response_text

    except Exception as e:
        print(f"An error occurred in chat_handler: {e}")
        if username in user_session_state: del user_session_state[username]
        raise HTTPException(status_code=500, detail=f"An API or logic error occurred: {e}")
        
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

    response_data = {"reply": ai_response}
    if action_response:
        response_data["action"] = action_response
    if goal_skill_id_for_checklist:
        response_data["goal_skill_id_for_checklist"] = goal_skill_id_for_checklist
    
    return response_data

@app.get("/user_progress/{user_id}")
async def get_user_progress(user_id: str):
    """Get user's learning progress"""
    db_connection = get_db_connection()
    if not db_connection:
        raise HTTPException(status_code=500, detail="Database connection error")
    
    try:
        cursor = db_connection.cursor(dictionary=True)
        
        cursor.execute("SELECT user_id FROM Users WHERE username = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        numeric_user_id = user['user_id']
        mastered_skills = get_mastered_skills(cursor, numeric_user_id)
        
        cursor.execute("SELECT COUNT(*) as total FROM Skills")
        total_skills = cursor.fetchone()['total']

        mastered_details = []
        if mastered_skills:
            placeholders = ','.join(['%s'] * len(mastered_skills))
            cursor.execute(
                f"SELECT skill_id, skill_name, subject, category FROM Skills WHERE skill_id IN ({placeholders}) ORDER BY skill_id DESC",
                tuple(mastered_skills)
            )
            mastered_details = cursor.fetchall()
        
        progress_by_subject = {}
        cursor.execute("SELECT DISTINCT subject FROM Skills ORDER BY subject")
        subjects = [row['subject'] for row in cursor.fetchall()]
        
        for subject in subjects:
            cursor.execute("SELECT COUNT(*) as total FROM Skills WHERE subject = %s", (subject,))
            total_in_subject = cursor.fetchone()['total']
            
            mastered_in_subject = len([s for s in mastered_details if s['subject'] == subject])
            
            progress_by_subject[subject] = {
                'mastered': mastered_in_subject,
                'total': total_in_subject,
                'percentage': round((mastered_in_subject / total_in_subject * 100) if total_in_subject > 0 else 0, 1)
            }
        
        return {
            'total_mastered': len(mastered_skills),
            'total_skills': total_skills,
            'overall_percentage': round((len(mastered_skills) / total_skills * 100) if total_skills > 0 else 0, 1),
            'progress_by_subject': progress_by_subject,
            'recent_skills': mastered_details[:5]
        }
    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}