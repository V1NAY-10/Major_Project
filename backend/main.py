from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import socket
from dotenv import load_dotenv
from google import genai

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("Warning: GEMINI_API_KEY not found")

client = genai.Client(api_key=api_key)

class PromptRequest(BaseModel):
    prompt: str


@app.get("/")
async def read_root():
    return {"message": "FastAPI backend is running"}


@app.post("/generate")
async def generate_freecad_code(request: PromptRequest):
    if not api_key:
        raise HTTPException(status_code=500, detail="Gemini API key not configured")

    try:
        system_instruction = (
            "You are a FreeCAD Python script generator. "
            "Generate ONLY the Python code to create the requested object in FreeCAD. "
            "Do not include markdown or explanations."
        )

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=f"{system_instruction}\n\nUser prompt: {request.prompt}",
        )

        return {"code": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/run-in-freecad")
async def run_in_freecad(request: PromptRequest):
    # The request body uses 'prompt' field to pass the code string
    code = request.prompt
    print(f"--- Attempting to send code to FreeCAD ({len(code)} bytes) ---")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2) # 2 second timeout
            s.connect(("127.0.0.1", 6666))
            s.sendall(code.encode('utf-8'))
            data = s.recv(1024)
            if data == b"OK":
                return {"status": "success", "message": "Code sent to FreeCAD successfully"}
            else:
                return {"status": "error", "message": "Unexpected response from FreeCAD"}
    except ConnectionRefusedError:
        raise HTTPException(status_code=503, detail="Could not connect to FreeCAD. Ensure the listener macro is running.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)