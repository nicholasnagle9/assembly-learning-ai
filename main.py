# main.py (Updated with CORS)

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware #<-- NEW IMPORT
from pydantic import BaseModel
# import google.generativeai as genai 
# from dotenv import load_dotenv

# --- FastAPI App Initialization ---
app = FastAPI()

# --- CORS Middleware Configuration ---
# This is the new section that fixes the error.
# It tells the browser that it's okay for your frontend to make requests to this backend.
origins = [
    "http://ai-tutor.local", # The address of your frontend
    "http://165.227.109.220", # Your server's IP
    "*" # A wildcard, good for development
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods (GET, POST, etc.)
    allow_headers=["*"], # Allows all headers
)

# --- Pydantic Model for a user's message ---
class ChatRequest(BaseModel):
    message: str
    user_id: str

# --- The Main Chat Endpoint ---
@app.post("/chat")
async def chat_handler(chat_request: ChatRequest):
    """
    This is the main endpoint that will eventually contain our Assembly Logic.
    For the rough draft, it will just echo the message back.
    """
    print(f"Received message: '{chat_request.message}' from user: {chat_request.user_id}")
    
    ai_response = f"You said: '{chat_request.message}'. The AI logic is not yet connected."
    
    return {"reply": ai_response}


# --- A simple root endpoint to check if the server is running ---
@app.get("/")
def read_root():
    return {"message": "AI Tutor API is running!"}