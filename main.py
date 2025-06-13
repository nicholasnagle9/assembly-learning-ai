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
# Format: { "username": {"numeric_user_id": 1, "current_skill_id": 1, "skill_name": "...", "phase": "Crawl", "last_question": "...", "goal_topic_name": "...", "goal_skill_id": null} }
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

def find_next_skill(cursor, mastered_skills, goal_skill_id):
    # If the goal skill itself is mastered, there's nothing left to learn in this path.
    if goal_skill_id in mastered_skills:
        return None

    # Get prerequisites for the current goal_skill_id
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (goal_skill_id,))
    prerequisites = cursor.fetchall()

    # If there are no prerequisites, and the skill isn't mastered, it's the next to learn.
    if not prerequisites:
        return goal_skill_id

    # Recursively check prerequisites
    for prereq_row in prerequisites:
        prereq_id = prereq_row['prerequisite_id']
        if prereq_id not in mastered_skills:
            # If a prerequisite is not mastered, find the first unmastered skill in *its* chain
            skill_to_learn = find_next_skill(cursor, mastered_skills, prereq_id)
            if skill_to_learn is not None:
                return skill_to_learn
    
    # If all prerequisites are mastered but the goal itself is not, then the goal is the next to learn
    return goal_skill_id if goal_skill_id not in mastered_skills else None


def search_for_skill_id(cursor, query_text):
    """
    Searches the Skills table for a skill matching the query_text.
    Prioritizes exact matches, then starts-with, then contains.
    Returns skill_id if found, None otherwise.
    """
    # Try exact match first
    cursor.execute("SELECT skill_id, skill_name, subject, category FROM Skills WHERE skill_name = %s", (query_text,))
    result = cursor.fetchone()
    if result:
        return result

    # Then try case-insensitive exact match
    cursor.execute("SELECT skill_id, skill_name, subject, category FROM Skills WHERE LOWER(skill_name) = LOWER(%s)", (query_text,))
    result = cursor.fetchone()
    if result:
        return result

    # Then try partial match (starts with)
    cursor.execute("SELECT skill_id, skill_name, subject, category FROM Skills WHERE skill_name LIKE %s LIMIT 1", (f"{query_text}%",))
    result = cursor.fetchone()
    if result:
        return result

    # Finally, try partial match (contains)
    cursor.execute("SELECT skill_id, skill_name, subject, category FROM Skills WHERE skill_name LIKE %s LIMIT 1", (f"%{query_text}%",))
    result = cursor.fetchone()
    if result:
        return result
    
    return None

# --- FastAPI App & Middleware ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- Main Chat Endpoint (The Upgraded Brain) ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    username = chat_request.user_id
    user_message = chat_request.message.strip() # Strip whitespace
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

        # Initialize session if not present or if user asks to start a new topic
        if username not in user_session_state or user_message.lower() == "start new topic":
            user_session_state[username] = {
                "numeric_user_id": numeric_user_id,
                "current_skill_id": None,
                "skill_name": None,
                "phase": "Select_Topic", # New initial phase
                "last_question": None,
                "goal_topic_name": None,
                "goal_skill_id": None
            }

        current_session = user_session_state[username]
        skill_name = current_session['skill_name']
        phase = current_session['phase']
        last_question = current_session.get('last_question')
        goal_topic_name = current_session['goal_topic_name']
        goal_skill_id = current_session['goal_skill_id'] # Retrieve the goal_skill_id from session

        # --- State Machine Logic ---
        system_prompt = ""

        if phase == "Select_Topic":
            if user_message.lower() == "start new topic":
                ai_response = "What specific math topic or concept would you like to learn today? For example, 'Geometry', 'Linear Algebra', 'Functions', or 'Pythagorean Theorem'."
            else:
                # Try to find the skill based on user's message
                found_skill = search_for_skill_id(cursor, user_message)
                if found_skill:
                    # Found a potential starting point for the user's requested topic
                    current_session['goal_skill_id'] = found_skill['skill_id']
                    current_session['goal_topic_name'] = found_skill['skill_name'] # Store the *requested* high-level topic name
                    
                    mastered_skills = get_mastered_skills(cursor, numeric_user_id)
                    next_skill_id = find_next_skill(cursor, mastered_skills, current_session['goal_skill_id'])

                    if next_skill_id:
                        cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                        skill_record = cursor.fetchone()
                        current_session['current_skill_id'] = next_skill_id
                        current_session['skill_name'] = skill_record['skill_name']
                        current_session['phase'] = 'Crawl' # Move to Crawl for the first skill in the new path
                        skill_name = current_session['skill_name'] # Update skill_name for immediate use below
                        phase = current_session['phase'] # Update phase for immediate use below
                        system_prompt = f"You are a teacher. Your ONLY task is to EXPLAIN the concept of '{skill_name}'. Use Markdown: use bolding for key terms, lists, and wrap math in `code blocks`. Keep it simple. End by asking if the user understands."
                        ai_response = "" # Will be filled by AI call
                    else:
                        # User already mastered the entire requested topic path
                        ai_response = f"It looks like you've already mastered all the prerequisites for '{found_skill['skill_name']}'! What other topic would you like to explore?"
                        # Reset session to allow selection of new topic
                        user_session_state[username]['phase'] = 'Select_Topic'
                        user_session_state[username]['goal_skill_id'] = None
                        user_session_state[username]['goal_topic_name'] = None
                else:
                    # Could not find a matching skill
                    ai_response = "I couldn't find a direct match for that topic. Please try being more specific, or choose from general subjects like 'Algebra', 'Geometry', 'Calculus', 'Linear Algebra', 'Discrete Math', or 'Statistics'."
                    # Keep phase as Select_Topic

        elif phase == "Crawl":
            system_prompt = f"You are a teacher. Your ONLY task is to EXPLAIN the concept of '{skill_name}'. Use Markdown: use bolding for key terms, lists, and wrap math in `code blocks`. Keep it simple. End by asking if the user understands."
            user_session_state[username]['phase'] = 'Walk_Ask'
        elif phase == "Walk_Ask":
            system_prompt = f"You are a friendly tutor. Your ONLY task is to ask a single, simple, leading question to help the user begin practicing '{skill_name}'. Do not solve it for them."
            user_session_state[username]['phase'] = 'Walk_Evaluate'
        elif phase == "Walk_Evaluate":
            system_prompt = f"A user was asked: '{last_question}'. They responded: '{user_message}'. Is this a correct and logical step forward? Respond ONLY with a JSON object: {{\"is_correct\": boolean, \"feedback\": \"A short, one-sentence piece of feedback.\"}}"
            # If they get it right, we move to the real test. If not, we try another guided question.
            # The actual phase change logic should be here based on assessment result
            # For now, let's keep it simple and ensure we go to Run_Ask if correct, else back to Walk_Ask
            # This will be handled after the AI response is parsed.
        elif phase == "Run_Ask":
            system_prompt = f"You are an examiner. Your ONLY task is to ask one, direct assessment question to test mastery of '{skill_name}'. Do not include the answer."
            user_session_state[username]['phase'] = 'Run_Evaluate'
        elif phase == "Run_Evaluate":
            system_prompt = f"You are an AI grader. A student was asked the question: '{last_question}'. The student responded: '{user_message}'. First, in a <thinking> block, reason step-by-step if the answer is correct and complete. Second, based on your reasoning, respond ONLY with a single, minified JSON object in the format: {{\"is_correct\": boolean, \"feedback\": \"Your short, encouraging feedback here.\"}}"
        elif phase == "Summary":
            # In summary, we check if the overall goal skill is mastered or if there's a next prerequisite
            mastered_skills = get_mastered_skills(cursor, numeric_user_id)
            next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id) # Check against the overall goal!
            
            if next_skill_id:
                # If there's a next skill in the chosen path, move to it
                cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
                next_skill_record = cursor.fetchone()
                user_session_state[username]['current_skill_id'] = next_skill_id
                user_session_state[username]['skill_name'] = next_skill_record['skill_name']
                user_session_state[username]['phase'] = 'Crawl' # Start Crawl for the new skill
                system_prompt = f"The user just mastered '{skill_name}'. Briefly congratulate them and introduce the next topic: '{next_skill_record['skill_name']}'. Explain why it's the next logical step in learning '{goal_topic_name}'. End by asking if they are ready."
                ai_response = "" # Will be filled by AI call
            else:
                # The entire path for the chosen goal is complete
                system_prompt = f"The user has just mastered '{skill_name}'. Congratulate them on completing the entire path for '{goal_topic_name}'! Ask them what other topic they would like to learn next."
                del user_session_state[username] # Reset session to allow new topic selection
                ai_response = "" # Will be filled by AI call


        # --- AI Call and State Handling ---
        # Only call AI if a system_prompt was generated
        if system_prompt:
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
                            elif phase == "Walk_Evaluate": # Correct in Walk, move to Run
                                user_session_state[username]['phase'] = 'Run_Ask'
                        else: # If evaluation is incorrect
                            ai_response += "\nLet's try that another way."
                            if phase == "Run_Evaluate": # Failed the test, go back to guided practice
                                user_session_state[username]['phase'] = 'Walk_Ask'
                            elif phase == "Walk_Evaluate": # Incorrect in Walk, stay in Walk_Ask for another guided question
                                user_session_state[username]['phase'] = 'Walk_Ask'
                    else:
                        ai_response = "My evaluation response was malformed. Let's try again."
                        if username in user_session_state: del user_session_state[username] # Reset state on severe error
                else:
                    ai_response = response_text
                    if phase.endswith("_Ask"): # Store the question asked by the AI
                        user_session_state[username]['last_question'] = response_text
            except google_exceptions.GoogleAPIError as api_error:
                ai_response = f"Google AI API error: {api_error}. Please try again."
                print(f"Google API Error: {api_error}")
                if username in user_session_state: del user_session_state[username] # Reset state on error
            except Exception as e:
                ai_response = f"An internal error occurred: {e}. Please try again."
                print(f"General Error: {e}")
                if username in user_session_state: del user_session_state[username] # Reset state on error
        
        # If ai_response was empty (because a system_prompt led to an AI call, and the response needs to be set), set it here.
        # This prevents the initial "An unexpected error occurred." from showing.
        if not ai_response:
            ai_response = "..." # Placeholder, will be updated by AI response in the flow

    finally:
        if db_connection and db_connection.is_connected():
            cursor.close()
            db_connection.close()

    return {"reply": ai_response}

@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}