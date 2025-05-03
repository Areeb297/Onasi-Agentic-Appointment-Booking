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
# Import from specific handlers
from twilio_inbound_handler import handle_incoming_call as handle_inbound_call_request, handle_media_stream_inbound
from twilio_outbound_handler import trigger_call as trigger_outbound_call_request, handle_incoming_call as handle_outbound_twiml_request, handle_media_stream as handle_media_stream_outbound
import logging

# Configure logging at the top
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

config = load_clean_config()
app = FastAPI(title="Dental Scheduler")

@app.get("/", response_class=HTMLResponse)
async def root():
    logger.info("Root endpoint accessed")
    return "<html><body><h1>Twilio Media Stream Server is running! (Inbound/Outbound Split)</h1></body></html>"

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    """Handles calls made TO your Twilio number."""
    logger.info("Inbound call route triggered")
    return await handle_inbound_call_request(request)

@app.websocket("/media-stream-inbound")
async def media_stream_inbound_ws(websocket: WebSocket):
    """Handles WebSocket for INBOUND calls."""
    logger.info("Inbound WebSocket connection attempt from %s", websocket.client)
    print("Inbound WebSocket connection attempt")
    try:
        await handle_media_stream_inbound(websocket)
    except Exception as e:
        logger.error("Inbound WebSocket connection failed: %s", str(e), exc_info=True)
        print(f"Inbound WebSocket error: {e}")
        # Ensure WebSocket is closed on error
        try:
            await websocket.close(code=1011)
        except:
            pass
        raise

@app.get("/make-call")
async def make_call():
    """Triggers your app to make an OUTBOUND call."""
    logger.info("Make-call (outbound trigger) endpoint triggered")
    return await trigger_outbound_call_request()

@app.api_route("/outbound-call-twiml", methods=["GET", "POST"])
async def outbound_call_twiml(request: Request):
    """Provides TwiML instructions FOR the outbound call initiated by /make-call."""
    logger.info("Outbound call TwiML route triggered")
    # This reuses the TwiML generation logic but points it to the outbound stream
    # We need to adjust the function in the outbound handler slightly
    return await handle_outbound_twiml_request(request, stream_endpoint="/media-stream-outbound")

@app.websocket("/media-stream-outbound")
async def media_stream_outbound_ws(websocket: WebSocket):
    """Handles WebSocket for OUTBOUND calls."""
    logger.info("Outbound WebSocket connection attempt from %s", websocket.client)
    print("Outbound WebSocket connection attempt")
    try:
        # Call the original media stream handler, now designated for outbound
        await handle_media_stream_outbound(websocket)
    except Exception as e:
        logger.error("Outbound WebSocket connection failed: %s", str(e), exc_info=True)
        print(f"Outbound WebSocket error: {e}")
        # Ensure WebSocket is closed on error
        try:
            await websocket.close(code=1011)
        except:
            pass
        raise

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
