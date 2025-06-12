# main.py (Updated to connect to MySQL)

import os
import mysql.connector # <--- NEW: Import the MySQL library
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv # <--- NEW: To read the .env file

# Load the environment variables from .env file
load_dotenv()

# --- NEW: Function to get a database connection ---
def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME")
        )
        print("Database connection successful!")
        return conn
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        return None

# --- FastAPI App Initialization ---
app = FastAPI()

# --- CORS Middleware Configuration ---
origins = ["*"] # Allowing all for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Model for a user's message ---
class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- The Main Chat Endpoint ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    print(f"Received message: '{chat_request.message}' from user: {chat_request.user_id}")
    
    # --- NEW: Database Logic ---
    # This is our first test to see if we can read from the DB.
    db_connection = get_db_connection()
    if not db_connection:
        return {"reply": "Error: Could not connect to the database."}

    try:
        cursor = db_connection.cursor(dictionary=True)
        
        # A simple query to get the very first skill from our Knowledge Graph
        cursor.execute("SELECT * FROM Skills WHERE skill_id = 1;")
        first_skill = cursor.fetchone()
        
        if first_skill:
            # If successful, send the skill name back to the user
            ai_response = f"DB Connection OK! First skill in graph: {first_skill['skill_name']}"
        else:
            ai_response = "DB Connection OK, but could not find skill #1."

    except Exception as e:
        ai_response = f"An error occurred while querying the database: {e}"
    finally:
        if db_connection.is_connected():
            cursor.close()
            db_connection.close()
            print("Database connection closed.")

    return {"reply": ai_response}


# --- A simple root endpoint to check if the server is running ---
@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}