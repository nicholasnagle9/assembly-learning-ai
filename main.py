import os
from fastapi import FastAPI
from pydantic import BaseModel
# We will add more imports here later
# import google.generativeai as genai 
# from dotenv import load_dotenv

# --- Pydantic Model for a user's message ---
class ChatRequest(BaseModel):
    message: str
    user_id: str # In the future, we'll use this to track progress

# --- FastAPI App Initialization ---
app = FastAPI()

# --- The Main Chat Endpoint ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    """
    This is the main endpoint that will eventually contain our Assembly Logic.
    For the rough draft, it will just echo the message back.
    """
    print(f"Received message: '{chat_request.message}' from user: {chat_request.user_id}")
    
    # TODO:
    # 1. Load user's mastered_skills from database based on user_id.
    # 2. Run Assembly Logic to find the next topic.
    # 3. Enter the Crawl, Walk, or Run phase.
    # 4. Generate the correct system prompt.
    # 5. Send prompt to Google AI and get response.

    # For now, just send a simple reply:
    ai_response = f"You said: '{chat_request.message}'. The AI logic is not yet connected."
    
    return {"reply": ai_response}


# --- A simple root endpoint to check if the server is running ---
@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}