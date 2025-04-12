"""
main.py
-------
Entry point for the dental scheduler FastAPI application.
Sets up routes and starts the server.
"""

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn
from config import load_clean_config
from twilio_handler import handle_incoming_call, handle_media_stream, trigger_call
import logging

# Configure logging at the top
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

config = load_clean_config()
app = FastAPI(title="Dental Scheduler")

@app.get("/", response_class=HTMLResponse)
async def root():
    logger.info("Root endpoint accessed")
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    logger.info("Incoming call route triggered")
    return await handle_incoming_call(request)

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    logger.info("WebSocket connection attempt from %s", websocket.client)
    print("WebSocket connection attempt")  # Fallback log
    try:
        await handle_media_stream(websocket)
    except Exception as e:
        logger.error("WebSocket connection failed: %s", str(e))
        print(f"WebSocket error: {e}")
        raise

@app.get("/make-call")
async def make_call():
    logger.info("Make-call endpoint triggered")
    return await trigger_call()

@app.get("/verify-database")
async def verify_database():
    """Endpoint to verify database connection and transaction handling."""
    from robust_database import verify_database_access
    
    logger.info("Database verification endpoint triggered")
    results = verify_database_access()
    
    return {
        "success": all([
            results["connection_success"], 
            results["database_exists"], 
            results["tables_exist"], 
            results["test_transaction"]
        ]),
        "details": results
    }

if __name__ == "__main__":
    logger.info("Starting FastAPI server on port %d", config["PORT"])
    uvicorn.run(app, host="0.0.0.0", port=config["PORT"])
