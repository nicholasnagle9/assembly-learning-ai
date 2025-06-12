# main.py (Updated with full Assembly Logic)

import os
import mysql.connector
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# --- Database Connection Function ---
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

# --- NEW HELPER FUNCTION: Get User's Mastered Skills ---
def get_mastered_skills(cursor, user_id):
    """Queries the database to get a set of skill_ids the user has mastered."""
    query = "SELECT skill_id FROM User_Skills WHERE user_id = %s"
    cursor.execute(query, (user_id,))
    # The 'map' function efficiently extracts the first item (the skill_id) from each row
    mastered_ids = set(map(lambda row: row['skill_id'], cursor.fetchall()))
    print(f"User {user_id} has mastered skill IDs: {mastered_ids}")
    return mastered_ids

# --- NEW HELPER FUNCTION: The Assembly Logic ---
def find_next_skill(cursor, mastered_skills, goal_skill_id):
    """
    Recursively finds the first unmastered prerequisite for a given goal skill.
    This is the heart of the Assembly Logic.
    """
    # Base Case 1: If the user has already mastered the goal, there's nothing to learn.
    if goal_skill_id in mastered_skills:
        return None

    # Get the prerequisites for the current goal skill
    query = "SELECT prerequisite_id FROM Prerequisites WHERE skill_id = %s"
    cursor.execute(query, (goal_skill_id,))
    prerequisites = cursor.fetchall()

    # Base Case 2: If there are no prerequisites and the user hasn't mastered it, this is the skill to learn.
    if not prerequisites:
        return goal_skill_id

    # Recursive Step: Check each prerequisite
    for prereq in prerequisites:
        prereq_id = prereq['prerequisite_id']
        # Find the next skill needed for THIS prerequisite
        skill_to_learn = find_next_skill(cursor, mastered_skills, prereq_id)
        # If we found an unmastered skill down the chain, that's our answer.
        if skill_to_learn is not None:
            return skill_to_learn
    
    # If all prerequisites are mastered, then the goal skill itself is the next to learn.
    return goal_skill_id


# --- FastAPI App & Middleware ---
app = FastAPI()
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Model ---
class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- Main Chat Endpoint (Updated) ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    db_connection = get_db_connection()
    if not db_connection:
        return {"reply": "Error: Could not connect to the database."}

    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # 1. Get the user's current progress
        mastered_skills = get_mastered_skills(cursor, chat_request.user_id)
        
        # 2. For this MVP, we'll hardcode the ultimate goal to be "Solving Two-Step Equations" (skill_id = 7)
        goal_skill_id = 7
        
        # 3. Run the Assembly Logic to find the next skill ID
        next_skill_id = find_next_skill(cursor, mastered_skills, goal_skill_id)

        if next_skill_id:
            # 4. Get the name of the next skill to be taught
            cursor.execute("SELECT skill_name FROM Skills WHERE skill_id = %s", (next_skill_id,))
            skill_record = cursor.fetchone()
            ai_response = f"Your next topic is: {skill_record['skill_name']}"
        else:
            ai_response = "Congratulations! You have mastered all skills in this path!"

    except Exception as e:
        ai_response = f"An error occurred: {e}"
    finally:
        if db_connection.is_connected():
            cursor.close()
            db_connection.close()

    return {"reply": ai_response}

# --- Root Endpoint ---
@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}